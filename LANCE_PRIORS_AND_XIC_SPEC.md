# Spec: fast priors + XIC storage in the FRAN Lance corpus

**Audience:** whoever is working on FRAN.
**Requested by:** the retriever-dia engine work, 2026-07-28.
**Status:** proposal with measured evidence. Nothing here has been built beyond a pilot.

> ⚠️ **PARTLY SUPERSEDED by `STORAGE_DESIGN.md` (2026-07-29). Read that one first for Problem 1.**
>
> This document's central design — *one materialised aggregate per `(stripped_seq, charge)`, pooled
> over the whole corpus* — was re-measured the next day and is **the wrong shape**.
>
> | predictor, 4,115 identical precursors, 5-fold held out | robust sd |
> |---|---|
> | DIA-NN 2.6 predicted iRT (what the engine ships) | 27.42 s |
> | FRAN pooled over all 1,552 runs — *this doc's design* | 15.52 s |
> | **FRAN scoped to 16 LC-comparable runs** | **10.57 s** |
>
> **Using more of the corpus made the answer worse by 5 s**, and the scoped query ran in 37 seconds
> against 5+ hours. There is no trade-off: the better answer is also ~500× cheaper. The requirement is
> not *"aggregate the corpus"* but ***"select comparable runs, then aggregate"***, which needs (a) run
> similarity metadata in Postgres and (b) **additive** partial aggregates keyed by `run_id` — sums and
> counts, never means, so any run subset is a summation and exclusion is subtraction. Both are
> specified in `STORAGE_DESIGN.md` §3–§5.
>
> The 17.8 s figure in the table below came from a partial scan and is superseded by 15.52 s. **Problem
> 2 (XIC storage), the exclusion constraint, the coverage numbers, and the checkpointing point all
> stand unchanged** — and the checkpointing one has since been paid for again: the pooled builder was
> resubmitted, ran 6:00:14 against a 6-hour wall, timed out, and wrote nothing.

---

## Why this is being asked for

retriever-dia is an open DIA search engine. Its weakest axis is retention-time prediction, and FRAN
turns out to be the best lever available — measured today on one benchmark file
(`11May2026_DIA_60spd_VER_185_S2-B5_1_21766`), same anchors, same fit, 5-fold held out:

| RT predictor | robust sd |
|---|---|
| DIA-NN 2.6 predicted iRT (what the engine ships with) | **27.9 s** |
| ~~FRAN empirical iRT, cross-run, leave-cohort-out~~ *(partial scan; → 15.52 s complete, 10.57 s scoped)* | ~~17.8 s~~ |
| DIA-NN 2.6.1 within-run (reference) | 16.7 s |
| Spectronaut 21 within-run (reference) | 7.4 s |

FRAN beats the shipped predictor by 36% and clears a ceiling that no change of fit function could
(every fit form — isotonic, LOWESS, binned median, trimmed variants — lands at 27–28 s). *Scoped to
LC-comparable runs it beats it by 61%, and clears DIA-NN 2.6.1's own within-run 16.7 s.*

FRAN is also the only source of several things nobody predicts: **`frg_loss`** (NH₃/H₂O neutral
losses, which DIA-NN never predicts and Spectronaut routinely uses for quantification) and
**`frg_charge`** (2+ fragments, 26.5% of all fragments).

**The blocker is not the data. It is that the data cannot be reached quickly.**

---

## Problem 1 — a prior costs hours to compute

`spectra_lance/` is **1,552 per-run Lance datasets, 137 GB**, at PSM-row granularity. Anything that
wants a per-peptide prior has to open all 1,552 and reduce them in Python.

Measured today (`glendon/fran_lance_fragments.py`): **~5 hours** for a single pass that produces one
pickle of consensus iRT/IM/fragment intensities. Observed rate is highly variable — 8 s/dataset early,
1.2–11 s later depending on run size.

Two consequences worth naming:

- Nobody runs it. An earlier attempt **crashed 67 seconds in on a `None` in `irt_empirical`, was
  patched, and then sat un-rerun for a week.** The 137 GB corpus had a working script pointed at it
  and produced nothing.
- It cannot be used at search time. A prior that takes 5 hours to materialise is a batch artefact,
  not something a pipeline can consult.

### What is needed: a materialised prior dataset

One row per `(stripped_seq, charge)`:

| field | why |
|---|---|
| `stripped_seq`, `charge` | the join key the engine has |
| `irt_consensus`, `irt_sd`, `n_obs` | **`irt_sd` and `n_obs` are as important as the mean** — they say whether to trust the prior for this peptide. Without them the engine must apply FRAN uniformly, which is wrong: coverage is uneven. |
| `im_consensus`, `im_sd` | same, for ion mobility |
| `frg_type[]`, `frg_num[]`, `frg_charge[]`, `frg_loss[]`, `frg_relint_mean[]`, `frg_n[]` | the empirical spectrum. **Keep `frg_charge` and `frg_loss`** — they are the two things downstream libraries drop. |
| `int_corr_score_median`, `interference_ms1_median`, `interference_ms2_median` | Spectronaut's own scores, useful as priors in their own right |

Sorted/indexed on `(stripped_seq, charge)` so a lookup for millions of library precursors is a
vectorised join rather than a scan.

### ⚠️ The constraint that must not be designed away

**The aggregate cannot bake in a fixed exclusion set.**

Any benchmark must exclude the runs it is being benchmarked on, or the result is circular. Today the
engine excludes 16 test runs — including the benchmark file, whose Spectronaut search *is in the
corpus* (`Dog_yeast_entrapment_SN21.lance`, 18,287 rows, added 2026-07-27). Tomorrow it will be a
different cohort. A pre-aggregated table with one baked-in exclusion silently leaks the moment the
benchmark changes.

Two workable shapes:
1. **Long form**: keep per-`(seq, charge, run)` contributions in one consolidated dataset and
   aggregate at query time with a run filter. Larger, fully flexible.
2. **Aggregate + subtractable detail**: store sums and counts (not means) plus per-run contributions,
   so excluding a run is arithmetic rather than a rescan.

Either is fine. Silently baking in one exclusion set is not.

### Also
Add **checkpointing**. The current builder writes its pickle only at the very end, so a 5-hour job
that dies at hour 4 yields nothing.

---

## Problem 2 — XIC traces exist only as a pilot

`glendon/build_xic_trace_lance.py` already does this, and its docstring states the goal: *"so the .d
is no longer needed for training."* But `xic_trace_lance/` holds **one run, 6.7 MB**.

The pilot stored 6,000 precursors as `[9, 32]` arrays (6 fragment + 3 MS1 channels × 32 cycles) —
about **1.1 KB per precursor**. Scaling to a run identifying ~30k precursors is ~33 MB; the full
1,552-run corpus is roughly **50 GB**. Against 137 GB of spectra and 239 GB of `.d` cache, that is
cheap.

### What it buys

Retires the raw `.d` for **training, re-scoring, feature development, priors, QC and visualisation** —
which is where essentially all iteration happens.

It also enables a feature no other engine can have: comparing a newly extracted chromatogram against
the shape the same peptide produced across prior runs. DIA-NN and Spectronaut have no cross-experiment
memory.

### ⚠️ What it does NOT buy — state this plainly so nobody plans around it

**It cannot replace raw data for searching.** A search extracts XICs at arbitrary m/z for millions of
*candidate* precursors, most of which are wrong. Stored traces only cover what was already extracted,
i.e. what was already looked for. A new library — different peptides, modifications, or charge states
— needs the raw signal again.

Storing traces for *every* library precursor instead is ~9.3 GB per run against a 15 GB cache: no
saving, and library-specific. Don't.

So: raw `.d` remains required for search; XIC storage retires it downstream.

### Suggested schema
One row per `(run, stripped_seq, charge)`: `trace` (fixed `[n_channels, n_cycles]`, float32),
`channel_kind[]` (fragment vs MS1 vs isotope), `frg_type[]`/`frg_num[]`/`frg_charge[]` aligned to the
fragment channels, `cycle_rt[]`, `apex_cycle`, and a content hash so corruption is detectable and the
trace is re-derivable from the `.d`. The pilot already carries a durability registry with content-md5
— keep that.

---

## Coverage: set expectations honestly

Measured mid-scan today: **74,357** covered precursors after 50 datasets → **109,941** after 100 →
**113,385** after 150. It plateaus hard; later runs mostly re-observe peptides already seen.

Against the full library (~2.5M sequence+charge keys) that is low single-digit percent. Against what a
single run actually detects (~4,600 precursors) it is 24× more. **FRAN is a rich prior for the
findable population and a sparse one for the library as a whole** — which is exactly why `n_obs` and
`irt_sd` must ship alongside the means.

---

## Priority

⚠️ **Superseded — use `STORAGE_DESIGN.md` §9.** Item 1 below is the pooled design that measured 5 s
*worse* than scoping; the corrected order puts **run similarity metadata in Postgres first**, because
without it there is no way to select comparable runs and everything falls back to pooling.

1. ~~Materialised prior dataset with run-level exclusion preserved~~ → **run similarity metadata**
   (instrument, gradient/spd, LC method, column, date, species, engine version, iRT calibration
   source), then **additive** partial aggregates keyed by `run_id`
2. Checkpointing in whatever builds it *(unchanged — and since re-confirmed by a 6-hour timeout that
   produced nothing)*
3. XIC lane scaled from pilot to corpus *(unchanged)*
4. Consolidate 1,552 per-run datasets into fewer, indexed datasets *(unchanged)*

---

## Reference: what the corpus already has

`spectra_lance/*.lance`, 48 columns, >90% Spectronaut-derived:
```
search_id search_name raw_path run stripped_seq modified_seq charge precursor_mz
prec_mz_calibrated rt rt_predicted irt_empirical irt_predicted im q_value global_q_value
pg_q_value signal_to_noise int_corr_score ms1_iso_measured ms1_iso_rel_measured
ms1_iso_rel_predicted ms1_quantity ms2_quantity prec_window prec_window_number xicdbid
fragment_count interference_ms1 interference_ms2 is_decoy missed_cleavages is_proteotypic
ptm_localization protein_group genes organism frg_mz frg_type frg_num frg_ion frg_charge
frg_loss frg_peak_area frg_norm_area frg_measured_relint frg_predicted_relint frg_mass_acc_ppm
```
Nothing new needs collecting for Problem 1 — it is all already there, just not reachable quickly.
