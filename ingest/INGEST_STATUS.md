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
| datasets linked to a `search_id` | **1,424** | 115 still unlinked — see below |
| on-disk store | `glendon/spectra_lance/` — **137 GB** | one `<search>.lance` dataset per search |
| ingest window | **2026-07-17 → 2026-07-20** | |

_Update 2026-07-20: two link passes of `link_spectrum_lane.py`. Pass 1 (name-match) linked 171;
pass 2 added a **precursor-count tiebreak** (`n_precursors_total`) that linked 75 more. Linked went
1,178 → **1,424**; unlinked 361 → **115**._

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
2. **544 Spectronaut searches (~82.9M precursors) still have NO linked lane dataset** — the real long
   tail, and bigger than first thought. All 544 are `status='completed'` with precursors in the DB, so
   this is genuine observed-spectrum data still to recover (roughly +23% on top of the 353.9M already
   stored). **Copy-paste steps are in [`BACKFILL_RUNBOOK.md`](BACKFILL_RUNBOOK.md)** — it all runs
   from Hive (plan with `plan_spectrum_backfill.py`, pull any Windows-only reports with
   `pull_reports_to_hive.py`, then `sbatch backfill_spectra.sbatch`; resume skips the already-done).
   (Note: some of the 115 unlinked datasets above cover a few of these 544 once linked, so the true
   still-to-backfill count is slightly under 544.)

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
