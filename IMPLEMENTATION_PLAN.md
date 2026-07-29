# Implementation plan: STORAGE_DESIGN.md + LANCE_PRIORS_AND_XIC_SPEC.md

**Date:** 2026-07-29
**Inputs:** `STORAGE_DESIGN.md` (2026-07-29), `LANCE_PRIORS_AND_XIC_SPEC.md` (2026-07-28, partly superseded).
**Status:** plan, nothing built yet.

`STORAGE_DESIGN.md` says up front: *"What I did not inspect: the Postgres schema."* I inspected it —
live, against PG Farm — plus the Lance writers in `ingest/`. The design's **reasoning holds
completely**; several of its **factual premises about what exists do not**. Building the docs as
written would rebuild things that already exist and would still not deliver scoped queries.

This plan keeps the design and rebases it on measured reality.

---

## 1. What the docs get wrong (measured 2026-07-29)

### 1.1 Run similarity metadata mostly EXISTS

`STORAGE_DESIGN.md` §3 says selecting comparable runs required *"grepping dataset filenames for the
string `60spd`… that is the entire similarity signal available today"*, and calls the missing table
*"the highest-value single addition."*

There is a `raw_files` table, 19,874 rows, 41 columns, keyed on `raw_path`:

| §3 wish-list field | reality | population |
|---|---|---|
| `gradient_length_min` | `raw_files.gradient_minutes` | **19,625 / 19,874 = 98.7%** |
| `instrument_model` | `raw_files.instrument_model` | 12,538 = 63.1% (Bruker only) |
| `serial` | `raw_files.instrument_serial` | 12,538 = 63.1% |
| — | `raw_files.acquisition_method` | 19,874 = **100%** |
| — | `raw_files.platform` | 19,874 = **100%** |
| `acquisition_date` | `raw_files.acquisition_date` | 7,241 = 36.4% |
| `spd` | `raw_files.samples_per_day` | 1,767 = 8.9% |
| `species` | `delimp_sample_metadata.organism_name` | populated |
| `search_engine`, `engine_version` | `delimp_searches.*` | 1,142 / 1,963 versioned |
| `lc_method` | column exists | **0 rows** |
| `column_id`, `column_age_injections` | — | **absent** |
| `library_type` | — | **absent** |
| `irt_calibration_source` | — | **absent** |

**The dominant term in RT comparability — gradient — is 98.7% populated today.** The doc's own
example query (`instrument='timsTOF HT' AND gradient_min BETWEEN 18 AND 22`) is answerable right now
and returns ~4,900 raw files on timsTOF HT alone, not 16.

So §3's premise is wrong, but §3's *conclusion* survives for a different reason — see 1.3.

### 1.2 An additive aggregate already exists — with the wrong key

`STORAGE_DESIGN.md` §4 specifies per-`(stripped_seq, charge, run_id)` sums, *"never means."*

`delimp_peptide_consensus` already exists: **3,956,950 rows**, columns
`stripped_seq, charge, n_obs, n_searches, irt_sum, irt_sumsq, im_sum, mz_sum, min_q, irt_mean,
irt_sd, im_mean, updated_at`. That is §4's additive shape, already built. `delimp_peptide_consensus_done`
(1,760 searches) is a per-search checkpoint table — so §8.1's checkpointing exists here too.

Three real defects, and they are the ones that matter:

1. **Keyed by `(stripped_seq, charge)` only** — pooled over the whole corpus. This is precisely the
   shape the supersede-notice calls *"the wrong shape."* It cannot be scoped to a run subset and
   cannot be subtracted, so it violates §5's exclusion constraint outright.
2. **No fragment aggregates at all.** No `frg_*`. The two things the docs identify as FRAN's unique
   value — `frg_loss` and `frg_charge` — are in Lance and in no aggregate anywhere.
3. **Stale and unversioned.** `updated_at` is 2026-07-10, with no corpus-revision stamp (§8.4).

### 1.3 The actual blocker is that `run_id` does not exist anywhere

This is the finding that reorders the plan.

- Lance datasets are **one per SEARCH, not per run** (`ingest/backfill_fragments.py:245`). Run identity
  survives only as plain `run` / `raw_path` *string columns inside* the 48-column schema.
- `delimp_spectrum_lane` (1,553 rows) has **no `raw_path` and no `run` column** — only `search_id`.
- Searches are overwhelmingly multi-raw: only **288 of 1,963** are single-raw; many carry 6, 12, 15+.

So "select comparable runs, then scan only those partitions" cannot be expressed today at any level:
you cannot tell which runs a `.lance` contains without opening it. Adding metadata to Postgres does
not fix this on its own — §3 alone buys nothing without the join.

The good news: `search_raw_files` (19,810 rows) joins to `raw_files` on `raw_path` at **100%**, and
the Lance schema already carries `raw_path` per row. Run-level scoping is *derivable* — it just has
never been materialised. Today **1,438 of 1,553** lane datasets can already reach `gradient_minutes`,
and 937 can reach `instrument_model` **and** `gradient_minutes`.

### 1.4 The XIC trace lane registry already exists

`delimp_xic_trace_lane` is live (`id, lance_path, run, n_precursors, content_md5, lance_version,
ingested_at`) holding the 1-row pilot (6,000 precursors). Notably it is the **only place in the schema
where `run` is a first-class key** — the right precedent to follow in Phase 1.

### 1.5 A silent ingest bug is the root cause of the metadata gaps

`ingest/corpus_ingest.py:414` does `from raw_metadata import read_raw_metadata` inside a bare
`except Exception:` that sets `read_raw_metadata = None`. **`raw_metadata.py` does not exist in this
repo, nor in the live Hive copy** at `.../glendon/fran_ingest/`.

Every ingest therefore silently writes NULL for `instrument_model`, `instrument_serial`,
`acquisition_date`, `mobility_*`, `n_ms*_frames`, `file_size_bytes`, `instrument_metadata_json`. The
`COALESCE(EXCLUDED.x, raw_files.x)` upsert hides it by preserving whatever an earlier
`record_raw_metadata.py` pass filled. This explains `instrument_model` stalled at 63.1% and
`acquisition_date` at 36.4%: they are not "not collected yet," they are being dropped on every run.

### 1.6 Neither builder the docs plan around is in version control

`fran_lance_fragments.py` and `build_xic_trace_lance.py` exist only on Hive. The 6-hour job that
timed out and *"wrote nothing"* was an untracked script. Checkpointing (§8.1) cannot be added to code
that is not in the repo.

---

## 2. Revised priority

`STORAGE_DESIGN.md` §9 orders: metadata → aggregates → checkpointing → XIC. Rebased:

| # | Phase | Why here |
|---|---|---|
| 0 | Stop the bleeding; get builders into git | Cheap, and 1.5 is actively destroying the metadata Phase 1 depends on |
| 1 | **Materialise the run dimension + run-level lane index** | The real §3. Metadata already exists; the *join* does not |
| 2 | Additive aggregates re-keyed by run, plus fragments | §4/§5 — rework `delimp_peptide_consensus`, don't start over |
| 3 | XIC lane to corpus scale | §6 — unchanged, but blocked on a defect fix |

---

## 3. Phase 0 — stop the bleeding (small, do first)

1. **Fix `corpus_ingest.py:414`.** Either restore `raw_metadata.py` (its interface is implied by
   `_runmeta`, and `record_raw_metadata.py` already has working Bruker and Thermo readers to fold in)
   or delete the dead import path. Either way, **make the failure loud** — a bare `except Exception`
   around a missing module is what let this run silently for months. Log once per ingest.
2. **Commit the two Hive builders** into `ingest/` and `scp` from the repo thereafter. Untracked
   scripts cannot be reviewed, checkpointed, or resumed.
3. **`raw_files` metadata backfill.** `record_raw_metadata.py` extracts `acquisition_date` for
   neither vendor, and extracts `gradient_minutes` for Thermo but never writes it (its `UPDATE` at
   `:84-88` omits the column). Add both; run the Thermo pass, which is *"ready, not run."* Expect
   `instrument_model` 63.1% → ~100%.

**Done when:** a re-ingest of one known search leaves `raw_files.instrument_model` non-NULL, and
`git log` shows both builders.

## 4. Phase 1 — the run dimension (the real §3)

1. **`delimp_runs`** — a view (not a table) over `raw_files` + `delimp_sample_metadata`, keyed
   `raw_path`, projecting the §3 similarity fields. A view because these are already maintained
   elsewhere; a second copy would drift.
2. **`delimp_spectrum_lane_runs (lance_path, run, raw_path, n_precursors, search_id)`** — the missing
   join. Built by scanning each of the 1,553 datasets for its distinct `(run, raw_path)`: a
   projection of two string columns, cheap in Lance, and a one-off. **This is the single
   highest-value item in the plan** — it is what makes `WHERE run IN (...)` expressible.
   `ingest/build_lane_run_index.py` + `.sbatch`; checkpointed per dataset and flushed, per §8.1/§8.2.

   **The join key is `run` → `raw_files.raw_basename`, NOT `raw_path`.** Measured on a smoke test,
   because the obvious choice fails in two independent ways:

   - The Lance `raw_path` column is in the 48-column schema but is **NULL** — the fragment backfill
     never populated it.
   - `raw_files.raw_path` is a *synthetic Windows* path (`R:\Data\…\<x>.sne\<run>.d`), so it could
     never match a Spectronaut run name even if Lance had one.
   - `raw_files.raw_basename` **is** the run name and does match: on the smoke test, **100 of 100
     runs reached `gradient_minutes`** through it.

   ⚠️ That join is **1:many** — 100 runs matched 183 `raw_files` rows (~1.8×), because a basename
   recurs across resubmits. Cohort queries must `SELECT DISTINCT` on the run or pick a canonical
   raw_file, or run-scoped aggregates will double-count. This is the same duplicate-name problem that
   left 64 lane datasets ambiguously linked; it does not block the gate, but §4's additive aggregates
   must be keyed on `run`, not on a `raw_files` row.
3. **Add the four genuinely-absent fields**: `lc_method` (column exists, 0 rows),
   `column_id`, `column_age_injections`, `library_type`, `irt_calibration_source`.
   Prioritise `irt_calibration_source` — §3 is right that it is the subtle one: if two runs normalised
   iRT differently their consensus is meaningless and *nothing in the data says so*. For Spectronaut
   it should be recoverable from `RunSummaries/`, the same place `engine_version.py` already reads.
   `column_id`/`column_age` have no upstream source and need a lab-side convention; treat as a
   separate, slower thread — don't let them block Phase 2.
4. **Verify against the measurement.** Reproduce the 16-run cohort *by query* rather than by
   filename grep, re-run the 5-fold held-out RT fit, and confirm it lands near 10.57 s. If it does
   not, the metadata is not capturing what the `60spd` grep captured, and that is worth knowing
   before Phase 2 is built on top of it.

**Done when:** step 4 reproduces ~10.57 s from a SQL-selected cohort.

### 4.1 Phase 1 step 2 — BUILT and measured, 2026-07-29

`delimp_spectrum_lane_runs` is live: **15,249 rows, 11,392 distinct runs, 1,552 of 1,553 datasets**
(one is not on disk). Built on a compute node in ~14 minutes, checkpointed per dataset.

Reachability into the run dimension, via `raw_files.raw_basename = run`:

| | runs |
|---|---|
| matching a `raw_files` row | 10,801 |
| reaching `gradient_minutes` | **10,801** |
| reaching `instrument_model` | 6,525 |

**The cohort question is now SQL, and the answer is 31× bigger than the grep:**

| cohort | datasets | runs |
|---|---|---|
| filename grep `60spd` (the original) | 16 | 111 |
| `gradient_minutes BETWEEN 18 AND 22` | 559 | **3,480** |
| … `AND instrument_model ILIKE '%timsTOF HT%'` | — | 2,999 |

That is the concrete reason to expect the gate to beat 10.57 s rather than merely match it: the grep
cohort was 111 runs selected on whether someone typed "60spd" in a filename; the band is 3,480 runs
selected on the actual LC parameter.

**And it confirms why the index was necessary at all** — dataset-level scoping is not run-level
scoping. Only **218 of 1,552** datasets hold a single run; the rest hold 2, 3, 4, 6, 8 or more. Any
"scope by dataset" approach silently drags in every other run that search happened to contain.

The gate itself is `ingest/fran_scoped_sql.py` — `fran_scoped.py` with exactly one thing changed
(cohort selection), and a `--legacy-grep` mode that reproduces the original cohort as a control so
the two are compared under an identical fit.

### 4.2 Phase 1 step 4 — THE GATE PASSED, 2026-07-29

The control first: `--legacy-grep` reproduced **10.57 s at n=4,115**, with coverage 37.4%, median 14
observations, max 229 — matching `STORAGE_DESIGN.md` §4 exactly. The harness is faithful, so the
comparison below is real.

*(Note for anyone quoting these numbers: **10.57 s is the `right-peak only` row**, not the headline.
Unrestricted, the same grep cohort scores 26.07 s at n=5,716. Compare like with like.)*

| | filename grep | SQL cohort (gradient 18–22 min) |
|---|---|---|
| datasets scanned | 16 | **559** |
| consensus precursors | 103,459 | **2,753,503** (26.6×) |
| coverage of SN-confident precursors | 5,716 (37.4%) | **14,193 (92.9%)** |
| robust sd, unrestricted | 26.07 s (n=5,716) | **21.43 s** (n=14,193) |
| **robust sd, right-peak only** | **10.57 s** (n=4,115) | **7.52 s** (n=10,039) |

**7.52 s against a 10.57 s target — a 29% improvement, on 2.4× more evaluated precursors.** The
prediction that 10.57 s was not the floor was correct, and the margin is not marginal.

Two things deserve to be said plainly:

1. **This is at Spectronaut-within-run parity.** Spectronaut 21 achieves 7.4 s *within a single run*.
   A SQL-selected FRAN cohort now predicts retention time on a run it has never seen to 7.52 s —
   essentially the same precision, cross-run. Against the shipped DIA-NN 2.6 predicted iRT (27.42 s)
   that is a **73% reduction**.
2. **Coverage is the quieter result and may matter more.** 37.4% → **92.9%** of SN-confident
   precursors now have a prior at all. A predictor that is excellent on a third of precursors is a
   research result; one that covers 93% is infrastructure.

The mechanism is exactly what §1.3 argued: the grep cohort was 111 runs selected on whether someone
typed "60spd" in a filename. The band is 3,480 runs selected on the LC parameter that actually drives
comparability — reachable only because `delimp_spectrum_lane_runs` now exists.

Remaining to close Phase 1 fully: sweep the gradient band and add `instrument_model` /
`acquisition_date` terms to see whether tighter or differently-shaped cohorts do better still. 7.52 s
is now the number to beat, and there is no reason to assume *it* is the floor either.

## 5. Phase 2 — additive aggregates keyed by run (§4/§5)

Evolve, don't restart — `delimp_peptide_consensus` is 3.96M rows of correct arithmetic with the wrong
key.

1. **New `(stripped_seq, charge, raw_path)` aggregate** carrying `n, irt_sum, irt_sumsq, im_sum,
   im_sumsq, q_best`. Note the existing table has `im_sum` but **no `im_sumsq`** — so it can give an
   IM mean but not an IM spread. Add it; §4 is explicit that variance must ship alongside the mean.
2. **Store it in Lance, not Postgres.** 3.96M keys at a median 10 observations implies roughly 40M
   `(seq, charge, run)` rows — heavy for PG next to a 182 GB `delimp_precursors`, ordinary for Lance.
   Sort on `(stripped_seq, charge)` per §4. Keep a small pooled rollup in PG for the website, derived
   from the Lance lane so the two cannot disagree.
3. **Fragment aggregates** per `(frg_type, frg_num, frg_charge, frg_loss)` → `relint_sum,
   relint_sumsq, n`. This is new work with no precedent in the schema, and it is the part that
   delivers `frg_loss` and `frg_charge` — the capability no other engine has.

   **Plus the per-fragment usability signal — cheap now, expensive to backfill.** Carry `n_used` and
   `n_total` per fragment key, sums and counts for `frg_mass_acc_ppm`, and precursor-level sums and
   counts for `int_corr_score` / `interference_ms1` / `interference_ms2`. Same additive rule, keyed
   so it scopes by run.

   Why it earns the slot: Spectronaut excludes ~41% of fragments as interfered and our engine
   excludes none. A per-fragment "did SN trust this fragment" is supervision for the co-elution
   judgment DIA-Umpire derives unsupervised — and the measured bottleneck, 29.9% wrong peak picks,
   is a co-elution failure. Nobody else can train on this, because nobody else has 1,552 runs of
   another engine's per-fragment verdicts.

### 5.1 Does that signal exist? — measured 2026-07-29

It was right to check before designing around it. Results, and one correction:

**The proposed proxy is falsified.** "`frg_peak_area`/`frg_norm_area` is 0 or NaN on excluded
fragments" is not true. On `Dog_yeast_entrapment_SN21.lance`, of 23,786 fragments: **0% NULL, 0%
zero or non-finite** — every fragment carries a real positive peak area, excluded or not.
Spectronaut excludes a fragment for interference, not for absence of signal. Building the aggregate
on that proxy would have measured ~0% excluded and concluded Spectronaut excludes nothing.

**The real verdict exists in the source reports, under a different naming convention.** Report
columns use underscores, not dots (`F_ExcludedFromQuantification`, not `F.ExcludedFromQuantification`)
— which is also why the `F.*` scan finds nothing. Measured:

| | |
|---|---|
| `F_ExcludedFromQuantification` | **31.1% True** on the entrapment benchmark, 48.4% on a mouse open-PTM search |
| also present, also un-ingested | `F_HasChannelInterference`, `EG_UsedForPeptideQuantity`, `EG_UsedForProteinGroupQuantity`, `EG_UsedInNormalizationSet`, `PEP_UsedForProteinGroupQuantity` |
| already ingested | `FG_HasPossibleInterference_(MS1)`/`(MS2)` → the Lance `interference_ms1`/`_ms2` |

**Good news on cost: only the FLAG was dropped, not the rows.** A matched comparison of the same
search, report vs Lance, gives an identical per-precursor fragment distribution — **median 6, min 3,
max 6 on both sides** — while the report marks 31.1% of those rows excluded. So the excluded
fragments are already sitting in the Lance corpus; ingest simply never carried the column that says
which they are.

So this is **not** the expensive finding it could have been:

- It is a **one-column addition** to `spectrum_lance.SCHEMA` plus a mapping entry in
  `backfill_fragments.py` (`"frg_excluded": ["F_ExcludedFromQuantification"]`), then a re-parse.
- The re-parse reads the **archived reports already on Hive** (`FRAN_reports` / `FRAN_SNE_export`) —
  it is not a Windows re-export. That is the same path the original backfill ran.
- Worth adding the other un-ingested verdicts in the same pass, since the expensive part is the scan,
  not the columns.

**Separate defect found while measuring: `frg_norm_area` is 100% NULL** across all 23,786 fragments
sampled, while `frg_peak_area` is 100% populated. The mapping is
`"frg_norm_area": ["F.NormalizedPeakArea"]`. Given the underscore convention above, confirm whether
`F_NormalizedPeakArea` exists in the reports at all before concluding it is a mapping bug — but a
column that is 100% NULL corpus-wide is either a bug or should be dropped, per the `lc_method`
argument in §8.1.
4. **Checkpoint per search**, reusing the `delimp_peptide_consensus_done` pattern, which already
   works. Flush stdout (§8.2).
5. **Version-stamp** each build against the corpus revision (§8.4).
6. **Exclusion stays a query parameter** (§5). With `run_id` in the key this is free; the invariant to
   enforce in review is that no builder ever takes an exclusion list as an argument.

**Done when:** the benchmark cohort's consensus can be computed with the 16 test runs excluded
*at query time*, and excluding them changes the answer (proof the run key is live, not decorative).

## 6. Phase 3 — XIC lane to corpus scale (§6)

**Blocked on a defect fix — do not scale first.**

1. **Fix the MS2 timestamp shift before any corpus build.** `xic_extractor.py:274` stamps every event
   in a cycle with the *first* frame's time, which is the MS1 frame; MS2 apexes land −0.295 s early
   (−0.263 s systematic, ~¼ cycle). Shape is unaffected, absolute fragment RT is not. **Any lane built
   before the fix carries the shift**, and rebuilding 50 GB to fix a known-in-advance bug is the
   avoidable version of this mistake.
2. **Reconsider the 1.8% keep rate first.** Events are indexed by cycle but not by isolation window,
   so each precursor reads all ~12 diaPASEF windows to use one. At 1,552 runs that inefficiency is
   the whole cost of the phase. The cache-format change is likely cheaper than the build it saves.
3. Then: version the writer (Phase 0), add sbatch + checkpointing, scale, register into the existing
   `delimp_xic_trace_lane`.
4. Known-and-accepted: the fixed IM window vs Spectronaut's dynamic one (measured widths vary 1.7×).
   Worth recording in the lane's provenance so a later fix is detectable rather than silent.

---

## 7. Two things to preserve exactly as written

The docs are right about both, and both are the kind of thing that gets designed away under time
pressure:

- **§5, exclusion as a query-time parameter.** `Dog_yeast_entrapment_SN21.lance` (18,287 rows) is in
  the corpus and is the benchmark file. A baked-in exclusion set produces a circular result that
  *looks excellent*, which is the worst failure mode available here.
- **§4, ship `n` and the variance.** Coverage is uneven — the consensus table's `n_obs` runs from 1 to
  32,892 with a median of 10. A consumer that cannot distinguish "seen 30,000 times" from "seen twice,
  and they disagree" has to apply the prior uniformly, which is wrong.

## 8. Decisions taken (were §8 open questions)

1. **Drop `column_id` / `column_age_injections`.** No upstream source, and a permanently-NULL column
   is worse than an absent one — it implies the signal was considered and found empty, which is how
   `lc_method` reads today. Use **`acquisition_date` as the column-aging proxy**: runs close in time
   share a column, which is the thing that actually matters. That makes the Phase 0 `acquisition_date`
   backfill carry the aging signal. **Keep `irt_calibration_source`** — genuinely load-bearing and
   recoverable from `RunSummaries/`.
2. **Lance, with the PG rollup.** The per-run aggregate is scanned *through*, not filtered *on* — the
   design's own division-of-labour rule. ~40M rows is the wrong tenant for PG next to a 182 GB
   `delimp_precursors`.
3. **Hold Phase 3.** Building 50 GB with a known −0.263 s shift is the avoidable mistake. One thing
   to establish before calling the cache re-index a detour: **if the search engine's own extraction
   pays the same ~12× window-read amplification, it is shared infrastructure rather than a lane-build
   cost**, which changes the arithmetic entirely.

## 9. The Phase 1 gate, pinned

Phase 1 step 4 is the gate, and it must be a **diff against the existing script, not a re-derivation**.
That script is now committed verbatim (byte-identical to the Hive original, md5
`c9297a12f5a7f73e2e9af33f2da51165`) at **`ingest/fran_scoped.py`**.

Its method, which must not change:

| | |
|---|---|
| cohort | 16 datasets whose basename contains `60spd` |
| filter | `q_value <= 0.01`, `is_decoy` false |
| leakage guard | drop any row whose `run` contains one of the 16 TEST ids |
| consensus | mean `irt_empirical` per `(stripped_seq, charge)` |
| truth | `sn21cmp/every_precursor.parquet`, `our_rt = sn_apex_rt_s + our_delta_rt_to_sn` |
| fit | `IsotonicRegression(out_of_bounds="clip")`, `KFold(5, shuffle=True, random_state=0)` |
| metric | robust sd = `1.4826 × MAD` of held-out residuals |
| evaluated on | the **4,115 covered precursors** |

**Change exactly one thing: how the cohort is selected.** Replace the filename-substring glob with a
SQL-selected cohort over the run dimension. Everything else stays identical, or the comparison means
nothing.

**Expect to beat 10.57 s.** The 10.57 s baseline came from a crude cohort — horse, manatee, mouse,
HeLa and one canine, selected on a filename substring. With `gradient_minutes` populated at 98.7% the
selection should be strictly better, so **10.57 s is probably not the floor**. If a SQL-selected
cohort does *worse*, the metadata is not capturing what the grep captured, and that is the finding —
do not paper over it before Phase 2 is built on top.

Note the gate genuinely depends on Phase 1 step 2: `fran_scoped.py` scopes by *dataset* (per-search
`.lance` files), while a run-level cohort needs the `lance_path → raw_path` index. Only 288 of 1,963
searches are single-raw, so dataset-scoping and run-scoping are not the same cohort.

## 10. Verification status

Split explicitly, because the two halves were checked by different people:

- **Postgres — verified live** (2026-07-29, this plan). Every row count, population percentage and
  reachability number in §1 was measured against PG Farm, not inferred. The engine-side design doc
  explicitly had *not* inspected the schema.
- **Lance — verified by reading the writers** in `ingest/`, and independently being re-checked
  against the corpus on the engine side. The PG row counts here have **not** been independently
  confirmed by that second pass.

## 11. Version tracking (added 2026-07-29)

Nothing recorded which code produced which artefact. Three version constants existed in three files
and none reached the database: `ingested_schema_version` is `"1.0.0"` for **all** 1,963 searches and
**all** 19,874 raw_files (it tracks the schema, which never changed), `xic_extractor.VERSION` was
never persisted at all, and the app's `APP_VERSION` was recorded against nothing.

That gap has a concrete cost. The XIC extractor's MS2 timestamp defect means "any lane built before
this fix carries the shift" — unanswerable without a recorded extractor version except by rebuilding
everything. Same shape as the `content_md5` chunking fix: datasets written before and after are
indistinguishable on disk.

Now in place, and applied live:

- **`ingest/versions.py`** — one home for every component version, plus `git_sha()` qualified by repo
  name (`FRAN@0d55345` vs `glendon@7961e27`) because Hive's copy sits inside a different git repo and
  a bare sha there is a real revision from the wrong project. The app version is *parsed from*
  `app/main.py` rather than copied, since the Dockerfile ships only `app/` and a second constant is
  the drift this is meant to remove.
- **`delimp_component_version`** — append-only log of (component, version, git_sha, host). Answers
  "what has touched this corpus?"
- **`writer_version`** on all three lane registries; plus `extractor_version`, `n_channels`,
  `n_cycles` and `extract_params` on `delimp_xic_trace_lane`, because only that lane's *contents*
  depend on our extraction code. Answers "which code wrote **this** dataset?"
- The existing pilot trace dataset is now explicitly marked `pilot` / `pre-1.0.0-pilot`, 9 channels —
  rather than left NULL to be misread as "unknown, probably fine".
- `/version` reports the app version plus the recorded pipeline versions, so one URL answers the
  question.

Rule going forward: **bump the constant in the same commit that changes the component's behaviour.**
A version that lags the code is worse than no version, because it is trusted.

## 12. Everything FRAN needs is now in git

An audit of Hive's `fran_ingest` against this repo found 10 files that existed **only on Hive** —
including `upload_spectra_to_blob.sbatch` (the only copy of a real SLURM job) and `diag.py` (the
script that found the `content_md5` chunking bug). All are now committed, along with the two builders
the design docs plan around, which had never been in version control.

Two files differed from git; both turned out benign (`corpus_ingest.py` was an uncommitted local
edit, `xic_extractor.py` was docstring-only with byte-identical code) — but nothing was *enforcing*
that, which is the actual problem. See **`ingest/HIVE_SYNC.md`** for the audit command and for the
target state: replace Hive's loose `scp`-populated copy with a real git clone, so "what is running"
and "what is committed" stop being separate questions.
