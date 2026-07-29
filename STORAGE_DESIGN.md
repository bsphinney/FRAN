# FRAN storage design: Postgres and Lance

**For:** whoever is working on FRAN.
**From:** the retriever-dia engine work, 2026-07-29.
**Status:** design proposal. Every number below was measured yesterday; the reasoning is shown so you
can disagree with it on evidence rather than taste.

⚠️ **What I did not inspect:** the Postgres schema. I worked only from the Lance corpus and from
engine-side use. Treat the Postgres section as a proposal about *roles*, not a critique of what
exists.

---

## 1. The result that should drive the design

FRAN's job, from the engine's side, is to answer: *"what do we already know about this precursor?"*
We measured how well it does that, 5-fold held out, predicting retention time on a run FRAN had
never seen.

**Read the filter column before quoting any number from this table.** The residual is dominated by
peak *localisation*, not prediction — a *perfect* predictor (Spectronaut's own apex RT for this run)
scores 16.0 s unrestricted but 0.65 s on right-peak precursors only. So restricting to right-peak
flatters every predictor, and a right-peak number compared against an unrestricted one is not a
result. Both columns below are measured on **identical rows with an identical fit**
(`rt_likeforlike.py`, job 19532307, 2026-07-29).

| predictor | right-peak only | unrestricted |
|---|---|---|
| DIA-NN 2.6 predicted iRT (what the engine ships) | 27.42 s *(n=4,115)* | 35.87 s *(n=5,716)* |
| FRAN, 16 filename-grep runs | **10.57 s** −61% | 26.07 s −27% |
| FRAN, 3,480 SQL-selected runs (Phase 1) | **7.52 s** *(n=10,039)* | 21.43 s *(n=14,193)* |
| *Spectronaut 21 within-run, for reference* | *7.4 s* | — |

Note the fair set is FRAN's **coverage**, not the whole cohort: DIA-NN covers 100% of precursors,
FRAN 37.4% at grep scope. Comparing across different coverage is the other way to get a fake margin.

Two things fall out, and the second one contradicts the obvious design:

**Comparability decides the answer, not volume.** Both directions are now measured. Pooling *all*
1,552 runs indiscriminately was worse than 16 hand-selected ones — a 2021 Orbitrap run and a 2024
manatee serum run both vote on a dog peptide's retention time, and their votes are noise. But
**3,480 runs selected on gradient beat 111 runs selected on a filename substring**, by 29%. So the
lesson is not "use less corpus"; it is that a *selected* corpus improves monotonically while an
*unselected* one degrades. Volume is only an asset downstream of selection.

**Selection is also the cheap direction.** The scoped query took 37 seconds; the pooled build takes
over 5 hours and, run against a 6-hour wall, has already timed out once producing nothing. The better
answer is also ~500× cheaper — there is no trade-off to manage here.

**Coverage may be the more important half.** Phase 1 moved SN-confident precursors with any prior at
all from **37.4% → 92.9%**. A predictor that is excellent on a third of precursors is a research
result; one covering 93% is infrastructure.

⇒ **The core requirement is not "aggregate the corpus". It is "select comparable runs, then
aggregate."** Everything below follows from that.

> **Open cell.** The 7.52 s row is compared against DIA-NN's 27.42 s, but 27.42 s was measured on the
> grep cohort's 4,115 right-peak rows, not on Phase 1's 10,039. DIA-NN has 100% coverage so the
> direction is safe, but the exact margin on the Phase 1 cohort is unmeasured. Anyone quoting "73%"
> should measure that cell first.

---

## 2. Division of labour

The rule: **Postgres holds what you filter *on*. Lance holds what you scan *through*.**

| | Postgres | Lance |
|---|---|---|
| run / search registry, provenance | ✅ | ✗ |
| **run similarity metadata** (see §3) | ✅ | ✗ |
| peptide→protein, taxonomy, annotation | ✅ | ✗ |
| PSM rows (402M+) | ✗ | ✅ |
| fragment arrays | ✗ | ✅ |
| XIC traces | ✗ | ✅ |
| per-(peptide, run) partial aggregates | ✗ | ✅ (§4) |

The query pattern this enables, which is the whole point:

```
1. Postgres:  SELECT run_id FROM runs
              WHERE instrument='timsTOF HT' AND gradient_min BETWEEN 18 AND 22
                AND run_id NOT IN (:excluded)          -- see §5
2. Lance:     scan only those run partitions
3. combine    (see §4 -- additive, no rescan)
```

Step 1 is what makes step 2 small. Today step 1 is impossible, so everything scans everything.

---

## 3. Run similarity metadata — the highest-value single addition

> ⚠️ **CORRECTED 2026-07-29 — see `IMPLEMENTATION_PLAN.md` §1.1 and §1.3.** This section was written
> without inspecting Postgres (§0 says so). The schema was then inspected live, and the premise below
> is wrong: **the metadata largely exists.** `raw_files` holds 19,874 rows with `gradient_minutes`
> **98.7% populated** — the dominant term in RT comparability — plus `instrument_model`/`serial` at
> 63.1%, `acquisition_method` and `platform` at 100%. The query this document proposes as
> aspirational is answerable today.
>
> **What does not exist is the JOIN.** Lance datasets are one per *search*, not per run; only 288 of
> 1,963 searches are single-raw; and `delimp_spectrum_lane` carries no run or raw_path column. So you
> cannot tell which runs a `.lance` holds without opening it, and adding more metadata buys nothing
> on its own. The highest-value single item is therefore `delimp_spectrum_lane_runs`
> (`IMPLEMENTATION_PLAN.md` §4.2), not a metadata-collection push.
>
> Genuinely absent and worth adding: `irt_calibration_source` (the subtle one this section is right
> about — recoverable from `RunSummaries/`). Genuinely absent and **dropped by decision**:
> `column_id` and `column_age_injections` — no upstream source, and a permanently-NULL column is
> worse than an absent one. `acquisition_date` is the column-aging proxy instead.

**This is worth 5 seconds of RT accuracy and a 500× speedup, and it does not exist.**

Yesterday I had to select comparable runs by **grepping dataset filenames for the string `60spd`**.
That is the entire similarity signal available today. It found 16 runs — horse, manatee, mouse, HeLa
and one canine — and those 16 beat all 1,552.

A proper `runs` table should carry at least:

| field | why it matters |
|---|---|
| `instrument_model`, `serial` | timsTOF vs Orbitrap changes IM and peak shape entirely |
| `gradient_length_min`, `spd` | the dominant term in RT comparability |
| `lc_method`, `column_id`, `column_age_injections` | column aging shifts RT systematically |
| `acquisition_date` | drift; also lets you weight recent runs |
| `species`, `sample_type` | matrix effects |
| `search_engine`, `engine_version`, `library_type` | the corpus is >90% Spectronaut; mixing engines mixes iRT conventions |
| `irt_calibration_source` | if two runs normalised iRT differently, their consensus is meaningless |

That last one is the subtle one. FRAN's `irt_empirical` is only comparable across runs to the extent
the runs share an iRT normalisation. If that varies silently, the consensus degrades and nothing in
the data says so. **Record it.**

Similarity should be *computed from these fields at query time*, not frozen into a similarity score —
what counts as comparable depends on what you are searching.

---

## 4. Partial aggregates: how to make any run-subset cheap

The obvious move — precompute one consensus per peptide — is wrong, because the correct consensus
depends on which runs you include, and you cannot precompute every subset.

The fix is to precompute aggregates that **combine additively**. Store, per `(stripped_seq, charge,
run_id)`:

```
n                     observation count
irt_sum, irt_sumsq    -> mean and variance for ANY run subset by summation
im_sum,  im_sumsq
frg[]                 per (frg_type, frg_num, frg_charge, frg_loss):
                        relint_sum, relint_sumsq, n
q_best                best q_value for this peptide in this run
```

Then a scoped consensus is `GROUP BY (seq, charge)` over the selected `run_id`s — a vectorised sum,
not a rescan of 402M PSM rows. Excluding a run is subtraction. Adding a new run is an append.

**Store sums and counts, never means.** Means cannot be recombined or subtracted; sums can. This one
choice is what makes §5 possible.

Sort/index on `(stripped_seq, charge)` so the engine's lookup — millions of library precursors — is a
join rather than a scan.

**Keep `n` and the variance.** Coverage is 37.4% of the precursors we care about, at a median of 14
observations (max 229). A consumer must be able to ask *"how well does FRAN know this peptide"* and
back off when the answer is "twice, and they disagree." Without `n` and spread, the prior has to be
applied uniformly, which is wrong.

---

## 5. The exclusion constraint — do not design this away

**Any aggregate that bakes in a fixed exclusion set is a latent leak.**

Benchmarking requires excluding the runs being benchmarked on. The engine currently excludes 16 test
runs, including its benchmark file — whose Spectronaut search *is in the corpus*
(`Dog_yeast_entrapment_SN21.lance`, 18,287 rows). Use a prior that silently included it and the
result is circular and looks excellent.

Tomorrow the excluded set is different. So exclusion must be a **query-time parameter**, which §4's
additive aggregates give for free.

Corollary: `run_id` must survive into every aggregate. An aggregate that has lost track of which runs
contributed cannot be corrected, only rebuilt.

---

## 6. The XIC lane

`build_xic_trace_lance.py` already exists as a pilot — one run, 6.7 MB, `[9 channels × 32 cycles]`
per precursor, about **1.1 KB per precursor**. A run identifying ~30k precursors is ~33 MB; the whole
1,552-run corpus is roughly **50 GB**. Against 137 GB of spectra that is cheap.

**What it buys:** retires the raw `.d` for training, re-scoring, feature development, priors, QC and
visualisation — where essentially all iteration happens. It also enables a feature no other engine
can have: comparing a newly extracted chromatogram against the shape the same peptide produced across
prior runs.

**What it does not buy — state this plainly so nobody plans around it:** it cannot replace raw data
for *searching*. A search extracts XICs at arbitrary m/z for millions of *candidate* precursors, most
of which are wrong. Stored traces only cover what was already extracted. Storing traces for every
library precursor instead is ~9.3 GB per run against a 15 GB cache — no saving, and library-specific.

Schema: one row per `(run_id, stripped_seq, charge)` — `trace` (fixed `[n_channels, n_cycles]`
float32), `channel_kind[]`, `frg_type[]`/`frg_num[]`/`frg_charge[]` aligned to fragment channels,
`cycle_rt[]`, `apex_cycle`, and a content hash. The pilot already keeps a durability registry with
content-md5; keep it.

---

## 7. What the corpus already has that nothing can reach quickly

The 48 Lance columns include measured values for things the engine currently treats as modelling
problems:

- **`frg_loss`** — NH₃/H₂O neutral losses. DIA-NN predicts none; Spectronaut used them as 2 of 6
  quantifying fragments on one peptide we examined, 1 of 6 on another.
- **`frg_charge`** — 2+ fragments, 26.5% of all fragments, which the engine's library build silently
  discards.
- **`ms1_iso_rel_measured` / `_predicted`** — the MS2 isotope envelope panel.
- **`int_corr_score`, `interference_ms1/ms2`** — Spectronaut's own scores for the same peptides.

None of this needs new collection. It needs to be reachable in seconds instead of hours.

---

## 8. Operational rules

1. **Checkpoint every long build.** The current fragment-prior builder writes one pickle at the very
   end; a 5-hour job that dies at hour 4 yields nothing. An earlier version crashed 67 s in on a
   `None`, was patched, and sat un-rerun for a week — the corpus had a working script pointed at it
   and produced nothing.
2. **Flush progress output.** Block-buffered stdout makes a running job indistinguishable from a hung
   one.
3. **Make scoped queries the default path**, pooled the exception. The default should be the one that
   is both faster and more accurate.
4. **Version the aggregates** alongside the corpus revision they were built from, so a stale prior is
   detectable rather than silently wrong.

---

## 9. Priority

1. **Run similarity metadata in Postgres** — unblocks everything, worth 5 s and 500× on its own
2. **Additive partial aggregates in Lance** (§4), keyed by `run_id`
3. **Checkpointing and flushed progress** in the builders
4. **XIC lane** scaled from pilot to corpus

Item 1 alone converts FRAN from an interesting corpus into usable infrastructure. Items 1 and 2
together turn a 5-hour batch job into a query the engine can make inline.
