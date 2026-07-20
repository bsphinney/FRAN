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
| Lance datasets registered (`delimp_spectrum_lane` rows) | **1,539** | of 1,890 Spectronaut searches = **81% of searches** |
| precursors stored | **353,884,368** | of ~384M Spectronaut corpus = **~92% by volume** |
| fragments stored | **2,097,797,651** (~2.1 B) | observed MS2 fragment rows |
| datasets linked to a `search_id` | **1,349** | 190 still unlinked — see below |
| on-disk store | `glendon/spectra_lance/` — **137 GB** | one `<search>.lance` dataset per search |
| ingest window | **2026-07-17 → 2026-07-20** | |

_Update 2026-07-20 (later): ran `link_spectrum_lane.py`, which linked **171** of the 361 NULL rows.
Linked went 1,178 → **1,349**; unlinked 361 → **190**._

## Where the data lives

- **Lance datasets:** `/quobyte/proteomics-grp/brett/glendon/spectra_lance/<search>.lance` (on Hive).
  One row per precursor; observed MS2 spectrum + MS1 isotope envelope as Arrow list columns. Schema
  = `spectrum_lance.py` `SCHEMA` (48 cols).
- **Registry (source of truth):** PG-Farm table **`delimp_spectrum_lane`**
  (cols: `id, lance_path, search_id, search_name, n_precursors, n_fragments, content_md5,
  lance_version, ingested_at, updated_at`). Upsert key = `lance_path`.
- **Read for training:** `lance.dataset(path).to_table()` — or iterate `lance_path` from the registry.

## Two open follow-ups (small)

1. **190 datasets still have `search_id = NULL`** (down from 361 — `link_spectrum_lane.py` cleared 171
   on 2026-07-20). The spectra are stored safely; only the FK link to `delimp_searches` is missing
   (report name didn't exactly match a search record). Of the remaining 190: **139 are ambiguous**
   (the name maps to >1 Spectronaut search, so the linker refuses to guess — these need a manual/
   provenance-based tiebreak) and **51 have no name match** at all. Re-run
   **`python link_spectrum_lane.py`** (idempotent; `--dry-run` to preview) after any new ingest — it
   only links unambiguous matches, so it never mislinks.
2. **~351 searches (1,890 − 1,539) not yet in the lane.** These are the long tail — reports still
   missing/corrupt, or searches whose report never got copied. Check with
   `python plan_spectrum_backfill.py` and, for anything still only on Windows, `pull_reports_to_hive.py`
   then `sbatch backfill_spectra.sbatch`.

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
