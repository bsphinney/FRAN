"""build_protein_group_index.py — index delimp_precursors.protein_group.

WHY. `/api/protein/{acc}/coverage` returns n_mapped=0 for low-abundance proteins. Reproduced on
P61278 (SST, 116 aa): the endpoint takes 8 arbitrary runs of the 164 that report it, collects the
123,432 peptides co-observed there, keeps the top 4,000 by frequency, and only then maps onto the
sequence. SST's own peptides rank 8,065 / 21,541 / 72,197 — truncated before mapping. They exist
(361 / 268 / 24 precursors corpus-wide); the endpoint just never sees them. So it is biased by
abundance and returns 0 in a way indistinguishable from "not observed".

`app/queries.py` uses that heuristic because its comment says "There is NO precursor->protein link in
the schema (delimp_precursors has no protein_group)". That is out of date: the column EXISTS and is
100% populated (200,000/200,000 sampled). What is missing is an INDEX — the eight existing indexes
cover id, search_id, raw_path, (stripped_seq,charge), q_value, mods, superseded_by, but not
protein_group — so the exact query seq-scans 416M rows and times out. Verified: it does.

This adds the index so the heuristic can be retired for an exact filter.

SAFETY, all deliberate:
  * CONCURRENTLY — takes ShareUpdateExclusiveLock, NOT AccessExclusiveLock. Readers and writers keep
    working. A plain CREATE INDEX would block every reader of a 416M-row table, including the live
    site; an ungranted AccessExclusiveLock nearly stalled PG Farm on 2026-07-29.
  * autocommit — CREATE INDEX CONCURRENTLY cannot run inside a transaction block.
  * statement_timeout = 0 — a timeout would CANCEL the build partway and leave an INVALID index
    behind, which then has to be dropped by hand. This is the one place a timeout is wrong.
  * pre-checked: max(length(protein_group)) = 276 chars, far under btree's ~2704-byte entry limit,
    so the build cannot abort hours in on an oversized entry. Mean is 7.5 chars => ~8-10 GB.
  * IF EXISTS drop of any prior INVALID attempt before starting.

Run via sbatch, not srun-over-ssh: this takes hours and an ssh timeout would hide the outcome.
"""
import argparse
import functools
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
print = functools.partial(print, flush=True)  # noqa: A001

IDX = "idx_prec_protein_group"


def _conn():
    import psycopg2
    from refresh_leaderboards import _token
    c = psycopg2.connect(
        host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
        dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
        user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
        password=_token(), sslmode="require", connect_timeout=30,
        keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=20,
        options="-c statement_timeout=0")     # see module docstring: a timeout here is harmful
    c.autocommit = True                        # CONCURRENTLY cannot run in a transaction
    return c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    conn = _conn(); cur = conn.cursor()

    cur.execute("""SELECT indexrelid::regclass::text, indisvalid
                   FROM pg_index WHERE indexrelid::regclass::text = %s""", (IDX,))
    row = cur.fetchone()
    if row:
        print(f"index {IDX} already exists, valid={row[1]}")
        if row[1]:
            print("nothing to do")
            conn.close(); return
        print("  previous attempt left an INVALID index — dropping it before rebuilding")
        if a.apply:
            cur.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {IDX}")

    cur.execute("""SELECT pg_size_pretty(pg_relation_size('delimp_precursors')),
                          pg_size_pretty(pg_indexes_size('delimp_precursors')),
                          pg_size_pretty(pg_database_size(current_database()))""")
    heap, idxs, db = cur.fetchone()
    print(f"before: heap {heap}, indexes {idxs}, database {db}")

    if not a.apply:
        print("\nDRY RUN — would run:")
        print(f"  CREATE INDEX CONCURRENTLY {IDX} ON delimp_precursors (protein_group)")
        conn.close(); return

    print(f"\nbuilding {IDX} CONCURRENTLY — expect hours; readers and writers stay live")
    t0 = time.time()
    try:
        cur.execute(f"CREATE INDEX CONCURRENTLY {IDX} ON delimp_precursors (protein_group)")
    except Exception as e:  # noqa: BLE001
        print(f"BUILD FAILED after {(time.time()-t0)/60:.1f} min: {str(e)[:200]}")
        print(f"An INVALID {IDX} may remain. Re-running this script drops and retries it.")
        conn.close(); sys.exit(1)
    print(f"built in {(time.time()-t0)/60:.1f} min")

    cur.execute("SELECT indisvalid FROM pg_index WHERE indexrelid::regclass::text = %s", (IDX,))
    print(f"valid: {cur.fetchone()[0]}")
    cur.execute(f"SELECT pg_size_pretty(pg_relation_size('{IDX}'))")
    print(f"index size: {cur.fetchone()[0]}")
    cur.execute("ANALYZE delimp_precursors (protein_group)")
    print("analyzed")

    # Prove the thing this exists for.
    print("\nverification — the query that used to time out:")
    cur.execute("SET statement_timeout = 30000")
    t = time.time()
    cur.execute("""SELECT stripped_seq, count(*) AS n
                   FROM delimp_precursors WHERE protein_group = 'P61278'
                   GROUP BY 1 ORDER BY 2 DESC LIMIT 5""")
    rows = cur.fetchall()
    print(f"  P61278 -> {len(rows)} peptides in {time.time()-t:.2f}s (was: timeout)")
    for s, n in rows:
        print(f"     {s:34s} {n:>6,}")

    import versions as V
    V.record_run(cur, "protein_group_index", "1.0.0", notes="idx_prec_protein_group")
    conn.close()
    print("DONE")


if __name__ == "__main__":
    main()
