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
