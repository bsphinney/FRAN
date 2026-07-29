"""build_xic_fragment_index.py — flatten delimp_precursor_xic.fragments into an indexable table.

Why. The peptide page's "Shared transitions & interference" panel asks, per quant transition, "which
OTHER peptides have a fragment at this m/z?". That was answered with:

    FROM delimp_precursor_xic x, jsonb_array_elements(x.fragments) f
    WHERE (f->>'mz')::float BETWEEN %s AND %s

`delimp_precursor_xic` has exactly one index — the primary key on `precursor_id`. There is no way to
index a numeric value living inside a jsonb array with a btree, so every one of those queries
explodes ~264k jsonb arrays and filters the result. Measured on DLTDYLMK: **all 12 of 12 transition
scans hit the 5 s timeout**, so the panel computed nothing — and then rendered "No co-eluting
interferers — transitions look specific.", which is the opposite of what had been established.

The fix is to store one row per (precursor, fragment) and put a btree on `mz`. ~264k precursors at
roughly 26 fragments each is ~7M rows — small, and the query becomes an index range scan.

    python ingest/build_xic_fragment_index.py            # report size, do nothing
    python ingest/build_xic_fragment_index.py --apply    # build + index
    python ingest/build_xic_fragment_index.py --apply --refresh   # rebuild from scratch

Re-run after any XIC ingest. `--refresh` drops and rebuilds; without it the build is skipped when the
table already has rows, so it is safe to call from a pipeline.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DDL = """
CREATE TABLE IF NOT EXISTS delimp_xic_fragment (
    precursor_id  TEXT NOT NULL,
    stripped_seq  TEXT NOT NULL,
    charge        SMALLINT,
    rt_apex       REAL,
    label         TEXT,
    mz            DOUBLE PRECISION NOT NULL,
    quant         BOOLEAN DEFAULT TRUE   -- NOT exclude_from_quant, i.e. used for quantification
);
"""

# Built after the insert: index maintenance during a 7M-row insert costs more than one pass at the end.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_xic_fragment_mz  ON delimp_xic_fragment (mz);
CREATE INDEX IF NOT EXISTS idx_xic_fragment_seq ON delimp_xic_fragment (stripped_seq);
"""

FILL = """
INSERT INTO delimp_xic_fragment (precursor_id, stripped_seq, charge, rt_apex, label, mz, quant)
SELECT x.precursor_id, x.stripped_seq, x.charge, x.rt_apex,
       f->>'label',
       (f->>'mz')::float8,
       COALESCE((f->>'exclude_from_quant')::int, 0) = 0
  FROM delimp_precursor_xic x,
       jsonb_array_elements(x.fragments) f
 WHERE f ? 'mz' AND (f->>'mz') ~ '^[0-9.eE+-]+$'
"""


def _conn():
    import psycopg2
    from refresh_leaderboards import _token
    return psycopg2.connect(
        host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
        dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
        user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
        password=_token(), sslmode="require", connect_timeout=30,
        options="-c statement_timeout=1800000")  # 30 min: one bulk insert over 264k jsonb arrays


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="drop existing rows and rebuild")
    a = ap.parse_args()

    conn = _conn(); conn.autocommit = False
    cur = conn.cursor()

    cur.execute("SELECT count(*) FROM delimp_precursor_xic")
    n_prec = cur.fetchone()[0]
    cur.execute("""SELECT count(*) FROM information_schema.tables
                   WHERE table_schema='public' AND table_name='delimp_xic_fragment'""")
    exists = cur.fetchone()[0] > 0
    n_have = 0
    if exists:
        cur.execute("SELECT count(*) FROM delimp_xic_fragment")
        n_have = cur.fetchone()[0]
    print(f"delimp_precursor_xic rows : {n_prec:,}")
    print(f"delimp_xic_fragment rows  : {n_have:,}" + ("" if exists else "  (table absent)"))

    if not a.apply:
        print("\nDRY RUN — re-run with --apply to build.")
        conn.close(); return

    cur.execute(DDL)
    conn.commit()

    if n_have and not a.refresh:
        print("\nAlready populated; pass --refresh to rebuild. Nothing to do.")
        conn.close(); return
    if n_have and a.refresh:
        print("\n--refresh: truncating")
        cur.execute("TRUNCATE delimp_xic_fragment")
        conn.commit()

    t = time.time()
    print("filling (one pass over the jsonb arrays)...", flush=True)
    cur.execute(FILL)
    n = cur.rowcount
    conn.commit()
    print(f"  inserted {n:,} fragment rows in {time.time()-t:.0f}s", flush=True)

    t = time.time()
    print("building indexes...", flush=True)
    for stmt in filter(str.strip, INDEXES.split(";")):
        cur.execute(stmt)
    conn.commit()
    cur.execute("ANALYZE delimp_xic_fragment")
    conn.commit()
    print(f"  indexed + analyzed in {time.time()-t:.0f}s", flush=True)

    # Prove the thing this exists for: the transition lookup must now be fast.
    cur.execute("SET statement_timeout = 5000")
    t = time.time()
    cur.execute("""SELECT stripped_seq, charge, rt_apex FROM delimp_xic_fragment
                   WHERE mz BETWEEN %s AND %s AND stripped_seq <> %s LIMIT 3000""",
                (770.3753 - 0.01, 770.3753 + 0.01, "DLTDYLMK"))
    rows = cur.fetchall()
    print(f"\nverification: DLTDYLMK y6^1 lookup returned {len(rows)} rows in {time.time()-t:.2f}s "
          f"(previously: timed out at 5s)")

    import versions as V
    V.record_run(cur, "xic_fragment_index", "1.0.0", notes=f"{n} rows from {n_prec} precursors")
    conn.commit()
    conn.close()
    print("DONE")


if __name__ == "__main__":
    main()
