"""build_fragments_duckdb.py — a thin DuckDB *view* over the fragment Parquet shards.

Design choice (2026-07-17): the bulk observed fragments live as per-search **Parquet shards**
(written by corpus_ingest.py's fragment lane + backfill_fragments.py). This builds a small
`fragments.duckdb` that just *views* those shards — the data stays in Parquet.

Why a view, not a monolithic DuckDB table:
  - Parquet shards are PARALLEL-SAFE to write (one file per search); a single DuckDB file is
    single-writer and would serialize/deadlock concurrent ingest jobs.
  - No corruption blast-radius: an interrupted write damages one shard, not the whole corpus.
  - DuckDB reads Parquet natively, so you STILL get one queryable handle + full SQL + fast
    predicate/column pushdown over the glob.
  - Sidesteps the DuckDB float16 misread we hit on the XIC tensors (fragments have no float16).

Usage:
    python build_fragments_duckdb.py --shards /path/to/fragments --db fragments.duckdb
    # then:
    duckdb fragments.duckdb "SELECT run, stripped_seq, charge, count(*) frags
                             FROM fragments GROUP BY 1,2,3 LIMIT 5"

If you ever DO want a materialized copy (e.g. to ship a single file off-cluster), pass
--materialize to build a real table instead of a view (slower, single-writer, larger).
"""
from __future__ import annotations

import argparse
import glob
import os
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shards", required=True, help="dir of *_fragments.parquet shards")
    ap.add_argument("--db", default="fragments.duckdb", help="output DuckDB file")
    ap.add_argument("--materialize", action="store_true", help="build a real table (copy) instead of a view")
    a = ap.parse_args()

    pattern = os.path.join(a.shards, "*_fragments.parquet")
    n = len(glob.glob(pattern))
    if n == 0:
        sys.exit(f"no *_fragments.parquet shards under {a.shards}")
    print(f"{n} fragment shard(s) under {a.shards}")

    import duckdb
    con = duckdb.connect(a.db)
    con.execute("DROP VIEW IF EXISTS fragments")
    con.execute("DROP TABLE IF EXISTS fragments")
    src = f"read_parquet('{pattern}', union_by_name=true, filename=true)"
    if a.materialize:
        con.execute(f"CREATE TABLE fragments AS SELECT * FROM {src}")
        kind = "table (materialized)"
    else:
        con.execute(f"CREATE VIEW fragments AS SELECT * FROM {src}")
        kind = "view (over Parquet)"
    nrows = con.execute("SELECT count(*) FROM fragments").fetchone()[0]
    nprec = con.execute("""SELECT count(*) FROM (
                             SELECT DISTINCT run, coalesce(modified_seq_proforma, stripped_seq), charge
                             FROM fragments)""").fetchone()[0]
    print(f"built {a.db}: fragments {kind} — {nrows:,} fragment rows, {nprec:,} distinct precursors")
    con.close()


if __name__ == "__main__":
    main()
