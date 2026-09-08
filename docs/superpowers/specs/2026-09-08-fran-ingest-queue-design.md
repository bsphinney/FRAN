# FRAN ingest queue — design

- **Date:** 2026-09-08
- **Status:** approved design, not yet implemented
- **Table:** `delimp_ingest_queue` (PG Farm, `uc-davis-genome-center-proteomics-core/delimp`)

## Problem

Searches produced on Hive are found by `find_uningested.py`, which walks three roots and
identifies engine output by marker files. Discovery is therefore a filesystem heuristic, and
it fails in two independent ways that were both measured on 2026-09-08.

### 1. One stray file can blind an entire root

`detect_engine()` ends with a catch-all:

```python
if any(_SN_REPORT.search(e) for e in files):   # r"_Report.*\.(tsv|parquet)$"
    return "spectronaut"
```

and `scan()` prunes descent as soon as a directory is classified:

```python
engine = detect_engine(dirpath, dirnames, filenames)
...
dirnames[:] = []          # a search dir's children are its own outputs
```

A single file, `/quobyte/proteomics-grp/brett/20250910_120054_KG-human-2_Report.tsv`, makes the
scanner classify the whole `brett` root as one Spectronaut search and skip all 246 entries under
it. Measured:

```
$ find_uningested.py --roots /quobyte/proteomics-grp/brett
walked 1 directories under 1 root(s)
  spectronaut  /quobyte/proteomics-grp/brett
```

The cost is silent. `/quobyte/proteomics-grp/brett/PROT_0793` holds two finished DIA-NN searches
(`search_hela/report.parquet`, `search_mouse/report.parquet`, created 2026-09-03/04) and has zero
rows in `delimp_searches`. Scanned directly they are found immediately:

```
$ find_uningested.py --roots /quobyte/proteomics-grp/brett/PROT_0793
walked 14 directories under 1 root(s)
  diann  /quobyte/proteomics-grp/brett/PROT_0793/search_hela
  diann  /quobyte/proteomics-grp/brett/PROT_0793/search_mouse
```

So the searches are perfectly ingestable; the cron simply cannot see them.

### 2. The consumer starves late-sorting candidates

`auto_ingest.select()` groups candidates by search name and iterates
`sorted(by_search.items())`; `main()` then takes `todo = chosen[:a.limit]` with `limit=5`.
Ordering is alphabetical, and the FRAN_reports backlog is dominated by names beginning with an
export timestamp (`20201210_...`, `20250717_...`), which sort ahead of any alphabetic name.

Consequence: the two DIA-NN searches correctly registered in the `incoming/` drop box on
2026-08-26 (`GallPlasCer__5b11a0d9`, `GallPlasStrap__74bf2f97`) were still uningested on
2026-09-08 — 13 days and roughly 78 cron ticks later. The drop box worked; the consumer never
reached them.

### Prior art to not repeat

`delimp_spectrum_regen_queue` is a cautionary precedent: 1,871 rows sharing a single
`requested_at`, no heartbeat or lease column, and completion marks written by a human running a
script. It became a stale one-shot worklist rather than a channel. Any queue added here must have
a lease and a terminal state, or it will decay the same way.

## Goals

- Producers on Hive (Claude sessions, the core pipeline) declare a finished search explicitly,
  instead of relying on a scanner to infer it.
- A registered search is ingested unattended within one cron interval (~4 h).
- Registration is idempotent, and a failing row can never wedge the queue or consume the per-run
  budget forever.

## Non-goals

- Replacing filesystem discovery for the Windows/Spectronaut `FRAN_reports` tree. Nobody registers
  those and there are ~1,700 of them; the scan stays authoritative there.
- Enabling the Lance spectrum/XIC lanes unattended. They are GB-scale per search and remain a
  human decision, exactly as `auto_ingest.py` documents today.
- Fixing the root-shadowing bug itself. That is a separate change (see Related work).

## Schema

```sql
CREATE TABLE delimp_ingest_queue (
  id            bigserial PRIMARY KEY,
  output_dir    text NOT NULL UNIQUE,   -- identity: search_id = uuid5(ns, output_dir)
  searchdir     text NOT NULL,          -- corpus_ingest positional; the report FILE for spectronaut
  engine        text NOT NULL CHECK (engine IN ('diann','spectronaut','fragpipe','radiant')),
  search_name   text,
  organism_name text,
  taxon         int,
  host          text NOT NULL,          -- where searchdir is readable, e.g. 'hive'
  registered_by text NOT NULL,          -- node name: win-1, win-forge, mac-clip-fran, ...
  registered_at timestamptz NOT NULL DEFAULT now(),
  priority      int  NOT NULL DEFAULT 0,
  status        text NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued','claimed','done','parked')),
  claimed_by    text,
  claimed_at    timestamptz,
  attempts      int  NOT NULL DEFAULT 0,
  last_error    text,
  last_attempt_at timestamptz,
  search_id     uuid
);

CREATE INDEX delimp_ingest_queue_ready_idx
  ON delimp_ingest_queue (status, priority DESC, registered_at);
```

A row is a serialized `corpus_ingest.py` invocation plus provenance. The columns map directly onto
its CLI: `searchdir` is positional, and `engine` / `search_name` / `organism_name` / `taxon` /
`output_dir` map to `--engine` / `--name` / `--organism-name` / `--taxon` / `--output-dir`.

`output_dir` carries the UNIQUE constraint because it is already the corpus identity —
`search_id = uuid5(namespace, output_dir)`. Registering the same search twice is therefore one
row, and the constraint enforces at the queue what `corpus_ingest`'s duplicate guard enforces at
the write.

The table is additive and is deliberately **not** added to `app/db.py`'s `PUBLIC_TABLES`, so the
read-only web layer cannot query it. Same posture as the `claude_bus` tables: operational state,
not corpus.

## Lifecycle

```
queued ──claim──> claimed ──success──> done
   ^                  │
   │                  ├──failure, attempts < 3 ──> queued
   │                  └──failure, attempts >= 3 ──> parked
   └── stale lease (claimed_at older than 6h) ──────┘
```

Two escapes carry the design, and both are what `delimp_spectrum_regen_queue` lacked:

- **Stale-lease reclaim.** A `claimed` row whose `claimed_at` is older than 6 h returns to
  `queued`. A node that dies mid-ingest releases its work automatically.
- **Parking.** At `attempts >= 3` the row becomes `parked` and retains `last_error`. A poison row
  stops consuming the per-run budget and waits for a human.

Rows are never deleted. The table doubles as an audit log of what was registered, by whom, when it
was ingested, and which `search_id` resulted.

## Consumer changes — `auto_ingest.py`

1. Before scanning, atomically claim up to `--limit` rows:

   ```sql
   UPDATE delimp_ingest_queue SET status='claimed', claimed_by=%s, claimed_at=now()
    WHERE id IN (
      SELECT id FROM delimp_ingest_queue
       WHERE status='queued'
          OR (status='claimed' AND claimed_at < now() - interval '6 hours')
       ORDER BY priority DESC, registered_at
       LIMIT %s
       FOR UPDATE SKIP LOCKED
    )
   RETURNING *;
   ```

   `FOR UPDATE SKIP LOCKED` makes concurrent ingesters safe without a separate lock.

2. Ingest claimed rows **first**, then spend any remaining budget on scan candidates exactly as
   today. `--limit` becomes a total across both sources rather than a scan-only cap.

3. On success: `status='done'`, `search_id` recorded. On failure: `attempts+1`, `last_error`,
   `last_attempt_at`, and back to `queued` or `parked` per the lifecycle.

Queue-before-scan ordering is the fix for the starvation described above.

## Producer interface — `fran_queue.py`

Modeled on `claude_bus.py`, which the other nodes already use, so the ergonomics are familiar:

```
fran_queue.py add <searchdir> --engine diann [--output-dir D] [--name N]
                              [--organism-name O] [--taxon T] [--priority P]
                              --registered-by <node>
fran_queue.py list [--status queued|claimed|done|parked]
fran_queue.py retry <id>      # parked -> queued, attempts reset
fran_queue.py park  <id>      # manual removal from circulation
```

`add` validates at registration rather than four hours later:

- `searchdir` exists and is readable from `host`
- `detect_engine()` agrees with the declared `--engine` (reusing `find_uningested`'s own function,
  so producer and consumer cannot disagree about what an engine's output looks like)
- `output_dir` is not already present in `delimp_searches`
- `--output-dir` defaults to `searchdir` when omitted

A bad registration fails in front of the person making it.

## Error handling

| Failure | Behavior |
|---|---|
| Duplicate registration | UNIQUE violation on `output_dir`, reported as "already queued"; not an error |
| Already in the corpus | Rejected by `add`, with the existing `search_id` reported |
| Path unreadable at ingest time | `attempts+1`, `last_error` recorded, retried next tick |
| Engine mismatch | Rejected at `add` |
| Ingester dies mid-row | Lease goes stale after 6 h, row returns to `queued` |
| Persistently failing row | `parked` after 3 attempts, with the error retained |

## Testing

- Registering the same `output_dir` twice yields one row.
- A search already in `delimp_searches` is rejected by `add`.
- Engine mismatch between declaration and `detect_engine()` is rejected.
- A `claimed` row older than 6 h is reclaimed by the next run.
- A row failing 3 times becomes `parked` and is skipped afterwards.
- With both queued rows and scan candidates present, queued rows are ingested first.
- Two concurrent claim transactions do not claim the same row (`SKIP LOCKED`).

## Rollout

1. Create the table. The service account owns all 48 public tables in `delimp` and has CREATE on
   the schema, so this needs **no CAS login** — unlike the `stan` database, where all tables are
   owned by `brettsp`.
2. Ship `fran_queue.py` and patch `auto_ingest.py`; deploy both to
   `/quobyte/proteomics-grp/brett/glendon/fran_ingest/` (a loose scp copy, not a checkout).
3. Backfill the known stranded work as the first real rows: `PROT_0793/search_hela`,
   `PROT_0793/search_mouse`, and the two `incoming/` Gallegos searches.
4. Tell the other Claude sessions the `add` contract.

## Related work, deliberately out of scope

- **Root shadowing.** `/quobyte/proteomics-grp/brett/20250910_120054_KG-human-2_Report.tsv` still
  blinds that root for the scanner. The queue routes around it for registered searches but does
  not fix it. Options: move the stray report into a subdirectory, or stop `detect_engine` from
  classifying a directory that is a configured scan root. Needs its own decision.
- **`auto_ingest` alphabetical ordering** remains for scan candidates. Queue rows bypass it; the
  FRAN_reports backlog still drains alphabetically.
