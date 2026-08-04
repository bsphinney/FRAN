"""backfill_lane_run_raw_path.py — give the Lance<->run bridge a real join key.

delimp_spectrum_lane_runs is how you scope the Lance spectrum lane by RUN (Lance datasets are one per
SEARCH, and only 218 of 1,552 hold a single run). But raw_path was 100% NULL on all 15,249 rows, so
the bridge could only be joined to raw_files by `run` -- a NAME, not a key -- and 4,534 raw_basenames
appear at more than one path, so that join fans out ~1.8x and silently multiplies any COUNT(*).

Why it was left NULL: the Lance data carries `run` (= raw_basename) and no path, and run ALONE does
not identify a raw_path.

Why it is recoverable anyway: the pair (search_id, run) does. Measured before writing anything --
13,639 pairs, 13,639 resolving to exactly one raw_path, ZERO ambiguous. A search sees a given
basename at exactly one path, which is the property that makes this safe.

The remaining 1,596 rows have a NULL search_id and stay NULL; there is no second route, and a guessed
path in a join key is worse than an honest NULL.

    python ingest/backfill_lane_run_raw_path.py            # dry run
    python ingest/backfill_lane_run_raw_path.py --apply
"""
import argparse
import functools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
print = functools.partial(print, flush=True)   # noqa: A001

# Guard: this must stay 1:1. If a future ingest ever makes (search_id, run) ambiguous, writing any
# single path would silently pick one -- so measure first and refuse rather than guess.
CHECK = """
SELECT count(*) FILTER (WHERE n > 1) AS ambiguous, count(*) AS pairs
FROM (SELECT lr.search_id, lr.run, count(DISTINCT rf.raw_path) n
      FROM delimp_spectrum_lane_runs lr
      JOIN search_raw_files srf ON srf.search_id = lr.search_id
      JOIN raw_files rf ON rf.raw_path = srf.raw_path AND rf.raw_basename = lr.run
      WHERE lr.raw_path IS NULL
      GROUP BY 1, 2) x
"""

FILL = """
UPDATE delimp_spectrum_lane_runs lr
SET raw_path = rf.raw_path
FROM search_raw_files srf
JOIN raw_files rf ON rf.raw_path = srf.raw_path
WHERE srf.search_id = lr.search_id
  AND rf.raw_basename = lr.run
  AND lr.raw_path IS NULL
"""


def _conn():
    import psycopg2
    from refresh_leaderboards import _token
    return psycopg2.connect(
        host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
        dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
        user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
        password=_token(), sslmode="require", connect_timeout=30,
        options="-c statement_timeout=300000")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    conn = _conn(); conn.autocommit = False
    cur = conn.cursor()
    cur.execute("SET LOCAL lock_timeout = '10s'")

    cur.execute("""SELECT count(*), count(raw_path), count(search_id)
                   FROM delimp_spectrum_lane_runs""")
    total, have, sid = cur.fetchone()
    print(f"before: {total:,} rows, raw_path {have:,}, search_id {sid:,}")

    cur.execute(CHECK)
    ambiguous, pairs = cur.fetchone()
    print(f"(search_id, run) pairs resolvable: {pairs:,}, ambiguous: {ambiguous:,}")
    if ambiguous:
        print("REFUSING: some pairs map to more than one raw_path; a guessed join key is worse "
              "than NULL. Investigate before forcing this.")
        conn.rollback(); conn.close(); sys.exit(1)

    if not a.apply:
        print("\nDRY RUN — re-run with --apply. Writes only where raw_path IS NULL.")
        conn.rollback(); conn.close(); return

    cur.execute(FILL)
    n = cur.rowcount
    cur.execute("""SELECT count(*), count(raw_path) FROM delimp_spectrum_lane_runs""")
    total, have = cur.fetchone()
    print(f"updated {n:,} rows")
    print(f"after:  raw_path {have:,}/{total:,} ({100*have/total:.1f}%)")

    # every written path must exist in raw_files, and must still agree with the run it came from
    cur.execute("""SELECT count(*) FROM delimp_spectrum_lane_runs lr
                   WHERE lr.raw_path IS NOT NULL
                     AND NOT EXISTS (SELECT 1 FROM raw_files rf
                                     WHERE rf.raw_path = lr.raw_path
                                       AND rf.raw_basename = lr.run)""")
    bad = cur.fetchone()[0]
    print(f"check: rows whose raw_path does not resolve back to the same run: {bad:,}")
    if bad:
        print("ROLLING BACK"); conn.rollback(); conn.close(); sys.exit(1)

    cur.execute("SELECT count(*) FROM delimp_spectrum_lane_runs WHERE raw_path IS NULL")
    print(f"still NULL (no search_id, no second route): {cur.fetchone()[0]:,}")

    import versions as V
    V.record_run(cur, "lane_run_raw_path", "1.0.0", notes=f"{n} rows")
    conn.commit(); conn.close()
    print("DONE")


if __name__ == "__main__":
    main()
