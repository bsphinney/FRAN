# FRAN ingest — LIVE STATUS (observed-spectrum Lance lane)

> Snapshot **2026-07-20**. This file records the *current state* of the ingest, because the design
> docs ([`README.md`](README.md), [`SPECTRONAUT_FRAN_INGEST.md`](SPECTRONAUT_FRAN_INGEST.md)) describe
> how the pipeline works but were written before the bulk backfill ran and still say "reports are
> trapped on Windows." **They are not — the backfill is essentially done.** Re-run the query at the
> bottom to refresh these numbers.

## TL;DR

The observed-spectrum Lance lane is **built at scale**. Almost every Spectronaut report was pulled
off the Windows export box, parsed, and ingested into per-search Lance datasets + the DB registry.

| metric | value | note |
|---|---|---|
| Lance datasets registered (`delimp_spectrum_lane` rows) | **1,551** | of 1,890 Spectronaut searches |
| precursors stored | **354,049,515** | of ~384M Spectronaut corpus = **~92% by volume** |
| fragments stored | **2,098,777,434** (~2.1 B) | observed MS2 fragment rows |
| datasets linked to a `search_id` | **1,436** | 115 still unlinked — see below |
| on-disk store | `glendon/spectra_lance/` — **137 GB** | on-disk datasets == registry rows (verified, no gap) |
| ingest window | **2026-07-17 → 2026-07-20** | |

_Update 2026-07-20: two link passes of `link_spectrum_lane.py` (name-match +171, then a
**precursor-count tiebreak** on `n_precursors_total` +75) took linked 1,178 → 1,424. A long-tail
backfill run (jobs 18938889 + 18938890, compute nodes) then found **FRAN_reports had 0 new** (those
datasets already existed on disk, just unlinked) and **FRAN_SNE_export added 12 new datasets**
(165,147 precursors / 979,783 fragments). Registry now **1,551 datasets / 1,436 linked / 354.0M
precursors / 2.099B fragments**; 115 still unlinked._

## Where the data lives

- **Lance datasets:** `/quobyte/proteomics-grp/brett/glendon/spectra_lance/<search>.lance` (on Hive).
  One row per precursor; observed MS2 spectrum + MS1 isotope envelope as Arrow list columns. Schema
  = `spectrum_lance.py` `SCHEMA` (48 cols).
- **Registry (source of truth):** PG-Farm table **`delimp_spectrum_lane`**
  (cols: `id, lance_path, search_id, search_name, n_precursors, n_fragments, content_md5,
  lance_version, ingested_at, updated_at`). Upsert key = `lance_path`.
- **Read for training:** `lance.dataset(path).to_table()` — or iterate `lance_path` from the registry.

## Two open follow-ups (small)

1. **115 datasets still have `search_id = NULL`** (down from 361 → 190 → 115 after two link passes on
   2026-07-20). The spectra are stored safely; only the FK link to `delimp_searches` is missing. The
   remaining 115 split into two hard cases that the linker deliberately will **not** guess:
   - **64 ambiguous duplicates.** The name maps to >1 Spectronaut search AND those searches have the
     *same* `n_precursors_total` (e.g. `[52772, 52772]`) — they are resubmits/re-runs of the same
     data, so the count tiebreak can't split them. Linking to either is nearly equivalent; a real fix
     picks the canonical search via `resubmit_of_search_id` / `parent_chain_depth`. Low priority.
   - **51 no name match.** The dataset's `search_name` matches no Spectronaut search name at all
     (renamed search, or report recorded under a different name) — needs manual/provenance lookup.
   Re-run **`python link_spectrum_lane.py`** after any new ingest; it links unambiguous names plus the
   exact-precursor-count tiebreak, and never mislinks. `diag_unlinked.py` breaks down what's left.
2. **457 Spectronaut searches (~74.9M precursors) have NO linked lane dataset** — but this is mostly a
   *linking* gap, **not** missing data. The 2026-07-20 backfill run proved it: scanning FRAN_reports
   wrote **0 new** datasets because the target `<search>.lance` already existed for every report, and
   the planner only flagged **36 searches whose report is genuinely absent from disk**. So of the 457:
   - **~421 already have their observed spectra on disk**, under a dataset whose name collides with a
     sibling/duplicate/renamed search — the data is stored, it's just not linked 1:1 to every search
     record. Chasing these is a **name-reconciliation** problem (see `diag_unlinked.py` /
     `diag_resubmits.py`), not a re-ingest. Low value for training (the spectra are already usable).
   - **~36 have no report on disk** — the only ones that need a true Spectronaut **re-export from
     Windows** before backfill. Use `pull_reports_to_hive.py` (cheap, if the report is on `C:\`) or
     `manageSNE -rs FRAN.rs` (full re-export) — see [`BACKFILL_RUNBOOK.md`](BACKFILL_RUNBOOK.md).

   > **Gotcha for whoever re-runs the planner:** `plan_spectrum_backfill.py` keys "already done" on
   > `search_id` present in the registry, so datasets with `search_id = NULL` (the 115 unlinked) look
   > *un-done* and inflate its "to backfill" count. `backfill_fragments.py` resume keys on the dataset
   > **file** existing, so it correctly skips them — which is why the run wrote 0 new from FRAN_reports.
   > Trust the file-level resume, not the planner's search_id count, for "is the data already there."

## Do NOT trust `delimp_spectrum_regen_queue`

That table still reads **1,871 rows** (`pending` / `copied_to_hive`) — it is a **stale, abandoned
tracker** from the planning phase and was never updated as the real backfill ran. Use
**`delimp_spectrum_lane`** for actual state, not the regen queue.

## Refresh these numbers

```python
# needs $DELIMP_PG_TOKEN_FILE (or ~/.pgfarm_token); run from Hive (alphadia2 env)
import psycopg2
tok = open("/quobyte/proteomics-grp/brett/.pgfarm_token").read().strip()
c = psycopg2.connect(host="pgfarm.library.ucdavis.edu",
                     dbname="uc-davis-genome-center-proteomics-core/delimp",
                     user="genome-proteomics-service-account", password=tok, sslmode="require")
cur = c.cursor()
cur.execute("""select count(*), count(search_id),
                      coalesce(sum(n_precursors),0), coalesce(sum(n_fragments),0),
                      min(ingested_at)::date, max(ingested_at)::date
               from delimp_spectrum_lane""")
print(cur.fetchone())  # (datasets, linked, precursors, fragments, first, last)
```

---

# XIC lane — provenance & the `shortcourse_diann19` negative result

> Snapshot **2026-09-23**. The sections above cover the observed-spectrum Lance lane. This one
> covers its sibling, the **XIC / chromatogram lane** (`delimp_precursor_xic`), which the design
> docs do not track. Added after a peptide-page mirror plot was traced to an ingest gap.

## `rel_intensity` has TWO incompatible meanings — check `trace_rt_basis` first

`delimp_precursor_xic.fragments[].rel_intensity` is **not one quantity**:

| `trace_rt_basis` | rows | what `rel_intensity` is | safe to compare against the trace? |
|---|---|---|---|
| `relative_to_apex` / NULL (consensus) | 442,035 | library value — DIA-NN `Relative.Intensity` (`xic_ingest.py:187`) or Spectronaut `frg_rel` (`sne_xic_ingest.py:127`) | **yes** — independent of the measurement |
| `absolute` (per-run) | 18,634 | `apex / max(apex)` from the trace itself (`ingest_perrun_xic.py:106`) | **NO — self-referential** |

Plotting the `absolute` form against those same traces compares a thing with a rescaled copy of
itself: it renders as near-perfect agreement and proves nothing. **Two guards keep it out, and
both are load-bearing:** the `trace_rt_basis IS DISTINCT FROM 'absolute'` filter in
`app/queries.py` `peptide_xic()`, and the basis guard on the upsert in `xic_engine_display_set.py`.
Note `engine_run_xic()` (`/api/engines/run/{raw}/xic`) has **no** such filter and does publish the
self-referential value — it is not rendered by any client, but do not start plotting it.

## Every DIA-NN group in the XIC lane is an orphan

31 distinct `(search_id, engine, version)` groups; **13 carry name-slugs that do not join to
`delimp_searches`** — so no `output_dir`, no raw-file list, no date. All 71,336 DIA-NN rows are in
that orphaned set (`shortcourse_diann19`, `savannah_nov2025`, ten `bat_*`). Every Spectronaut group
but one joins and has an `output_dir`.

## `shortcourse_diann19` — unsourced, and the UI change IS the fix

13,852 rows / 12,898 peptides / 13,852 precursors, **`rel_intensity` NULL on every fragment of
every row** — ingested via `xic_ingest.py` `_records_xiconly()`, the fallback taken when no
`report-lib.parquet` sits beside the DIA-NN output. That path also computes fragment *m/z*
theoretically from the sequence and sets `precursor_mz` NULL. 10,036 peptide pages open on one of
these rows.

**Do not re-run the search for the source directory. It was done on 2026-09-23 and came back
negative.** `find` over `/quobyte/proteomics-grp` and `/nfs/lssc0/flinders/proteomics/Data`
(pruning `*.d`, `*.raw`, `*.lance`, `.snapshot`):

| scanned / found | count |
|---|---|
| `report.log.txt` scanned | 5,321 |
| DIA-NN 1.8.1 / 1.8.2 / **1.9** / 1.9.1 | 8 / 42 / **5** / 2 |
| `report-lib.parquet` beside a **1.9** log | **0** (the only 2 are 1.9.1 — Lauren's Ceres_SDS mouse runs, unrelated) |
| `report_xic.parquet` on the whole estate | **2** — `brett/glendon/diann251_fragexport16` (DIA-NN 2.5.1) and `brett/siegel_glp1_2026-08-12/par/timstof/xic`; **neither is 1.9** |

The source directory is not on the scanned estate. Without its `report_xic.parquet` the ingest
cannot be re-run, so there is **no precursor correspondence against which to validate any
substitute library** — a library that covers some of the same sequences is not evidence it is the
right one. (`lib/UCD_Sample_prep_mouse.empirical.parquet` looks like a candidate and is not: 15%
precursor coverage, zero `(UniMod:1)` acetyl entries against 53% acetylated precursors in the lane,
and its searches are DIA-NN **2.6.0**.) The lane is permanently unsourced; the UI change that stops
drawing an empty predicted half **is the fix, not a stopgap while the data is looked for**.

*Scan caveats:* pruned `*.d`/`*.raw`/`*.lance`; covered those two roots only — not the `/Volumes`
SMB share, not the Windows `B:`/`S:` drives.

## The process lesson

The `bat_*` cohort came through the same hand-run slug workflow and **kept its library** —
its rows have full `rel_intensity`. `shortcourse_diann19` did not, and because the ingest recorded
no `output_dir`, there is now no way to repair it. **An ad-hoc ingest that records no `output_dir`
produces data that cannot be repaired later.** This is the second time that has bitten. Register
the search (or at minimum persist its output path) before ingesting a lane from it.
