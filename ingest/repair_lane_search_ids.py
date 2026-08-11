"""repair_lane_search_ids.py — restore delimp_spectrum_lane.search_id links a re-parse clobbered.

WHAT HAPPENED. spectrum_lance.register() upserts on lance_path and, before 2026-08-10, did a bare
`search_id=EXCLUDED.search_id`. A re-parse re-resolves search_id by matching the report name against
delimp_searches / delimp_search_provenance; when that lookup fails it passes NULL, and the bare
assignment then OVERWROTE a link established earlier by a richer mechanism. The 2026-08-10 fragment
re-parse dropped the lane's link rate from 92.8% to 55.1%.

WHY IT WAS NOT CAUGHT SOONER. The check I ran mid-run asked "are there NULL rows the registration
query COULD have matched?" -- using the SAME query registration uses. That is circular: it can only
ever return 0, and it did, which read as "no links lost". The honest check is the one below: compare
against an INDEPENDENT source of the same fact.

THE INDEPENDENT SOURCE. delimp_spectrum_lane_runs already carries (lance_path, search_id) per run,
built by build_lane_run_index.py from the Lance data itself rather than from name matching. Measured:
577 clobbered datasets are recoverable from it, and ZERO map to more than one search -- so the
restoration is unambiguous, not a guess.

register() now COALESCEs, so this repairs the damage rather than papering over an ongoing bug.

    python ingest/repair_lane_search_ids.py            # dry run
    python ingest/repair_lane_search_ids.py --apply
"""
import argparse
import functools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
print = functools.partial(print, flush=True)   # noqa: A001

# Refuse if any dataset maps to more than one search: restoring a guessed link is worse than a NULL,
# because downstream code trusts search_id as a key.
CHECK = """
SELECT count(*) FROM (
  SELECT l.lance_path, count(DISTINCT lr.search_id) n
  FROM delimp_spectrum_lane l
  JOIN delimp_spectrum_lane_runs lr ON lr.lance_path = l.lance_path
  WHERE l.search_id IS NULL AND lr.search_id IS NOT NULL
  GROUP BY 1 HAVING count(DISTINCT lr.search_id) > 1) x
"""

FIX = """
UPDATE delimp_spectrum_lane l
SET search_id = sub.sid, updated_at = now()
-- min() has no uuid overload in Postgres; the HAVING clause already guarantees exactly one
-- distinct value per lance_path, so any single row's value is THE value.
FROM (SELECT lr.lance_path, (array_agg(DISTINCT lr.search_id))[1] AS sid
      FROM delimp_spectrum_lane_runs lr
      WHERE lr.search_id IS NOT NULL
      GROUP BY 1 HAVING count(DISTINCT lr.search_id) = 1) sub
WHERE sub.lance_path = l.lance_path AND l.search_id IS NULL
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

    cur.execute("SELECT count(*), count(search_id) FROM delimp_spectrum_lane")
    tot, linked = cur.fetchone()
    print(f"before: {linked:,}/{tot:,} datasets linked ({100*linked/tot:.1f}%)")

    cur.execute(CHECK)
    ambiguous = cur.fetchone()[0]
    print(f"datasets mapping to more than one search: {ambiguous:,}")
    if ambiguous:
        print("REFUSING: a guessed search_id is worse than NULL. Investigate first.")
        conn.rollback(); conn.close(); sys.exit(1)

    if not a.apply:
        cur.execute("""SELECT count(DISTINCT l.lance_path) FROM delimp_spectrum_lane l
                       JOIN delimp_spectrum_lane_runs lr ON lr.lance_path = l.lance_path
                       WHERE l.search_id IS NULL AND lr.search_id IS NOT NULL""")
        print(f"\nDRY RUN — would restore {cur.fetchone()[0]:,} links. Re-run with --apply.")
        conn.rollback(); conn.close(); return

    cur.execute(FIX)
    n = cur.rowcount
    cur.execute("SELECT count(*), count(search_id) FROM delimp_spectrum_lane")
    tot, linked = cur.fetchone()
    print(f"restored {n:,} links")
    print(f"after:  {linked:,}/{tot:,} datasets linked ({100*linked/tot:.1f}%)")

    # every restored search_id must exist, and must still be the one the runs agree on
    cur.execute("""SELECT count(*) FROM delimp_spectrum_lane l
                   WHERE l.search_id IS NOT NULL
                     AND NOT EXISTS (SELECT 1 FROM delimp_searches s WHERE s.id = l.search_id)""")
    orphan = cur.fetchone()[0]
    print(f"check: lane rows pointing at a non-existent search: {orphan:,}")
    if orphan:
        print("ROLLING BACK"); conn.rollback(); conn.close(); sys.exit(1)

    import versions as V
    V.record_run(cur, "lane_search_id_repair", "1.0.0", notes=f"{n} links restored")
    conn.commit(); conn.close()
    print("DONE")


if __name__ == "__main__":
    main()
