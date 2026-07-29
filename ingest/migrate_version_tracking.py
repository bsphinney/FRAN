"""One-off migration: create delimp_component_version and add the per-artefact version columns.

Additive and idempotent — CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS, no rewrites, no
backfill of existing data beyond marking what is already known. All three lane registries are tiny
(1,553 / 1 / 1 rows) and ADD COLUMN without a default is a catalog-only change in modern Postgres,
so this does not take the kind of long AccessExclusiveLock that stalled PG Farm on 2026-06-15 when
`delimp_precursors` was altered.

  python ingest/migrate_version_tracking.py            # dry run: report what would change
  python ingest/migrate_version_tracking.py --apply
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import versions as V  # noqa: E402
import spectrum_lance as SL  # noqa: E402
import xic_lance as XL  # noqa: E402
import xic_trace_lance as XTL  # noqa: E402


def _conn():
    import psycopg2
    from refresh_leaderboards import _token
    return psycopg2.connect(
        host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
        dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
        user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
        password=_token(), sslmode="require", connect_timeout=30,
        options="-c statement_timeout=120000")


def _cols(cur, table):
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_schema='public' AND table_name=%s""", (table,))
    return {r[0] for r in cur.fetchall()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    conn = _conn(); conn.autocommit = False
    cur = conn.cursor()

    print("current component versions in this checkout:")
    for k, v in V.stamp().items():
        print(f"  {k:24s} {v}")

    before = {t: _cols(cur, t) for t in
              ("delimp_spectrum_lane", "delimp_xic_lane", "delimp_xic_trace_lane",
               "delimp_component_version")}
    print("\nplanned additions:")
    want = {
        "delimp_component_version": {"component", "version", "git_sha", "host", "notes", "recorded_at"},
        "delimp_spectrum_lane": {"writer_version"},
        "delimp_xic_lane": {"writer_version"},
        "delimp_xic_trace_lane": {"writer_version", "extractor_version", "n_channels",
                                  "n_cycles", "extract_params", "updated_at"},
    }
    for t, cols in want.items():
        missing = sorted(cols - before[t]) if before[t] else sorted(cols)
        state = "CREATE TABLE" if not before[t] else (", ".join(missing) or "(nothing missing)")
        print(f"  {t:30s} {state}")

    if not a.apply:
        print("\nDRY RUN — re-run with --apply to make these changes.")
        conn.close(); return

    V.ensure_table(cur)
    SL.ensure_registry(conn)
    XL.ensure_registry(conn)
    XTL.ensure_registry(conn)
    cur = conn.cursor()

    # Record every component version in this checkout, so the log has a baseline from day one.
    for comp, ver in (("corpus_ingest", V.CORPUS_INGEST_VERSION),
                      ("spectrum_lane_writer", V.SPECTRUM_LANE_WRITER_VERSION),
                      ("xic_lane_writer", V.XIC_LANE_WRITER_VERSION),
                      ("xic_trace_lane_writer", V.XIC_TRACE_LANE_WRITER_VERSION),
                      ("raw_metadata", V.RAW_METADATA_VERSION),
                      ("xic_extractor", V.xic_extractor_version()),
                      ("fran_app", V.fran_app_version())):
        if ver:
            V.record(cur, comp, ver, notes="baseline recorded by migrate_version_tracking")

    # The one existing trace-lane dataset predates version tracking AND predates the MS2 timestamp
    # fix. Say so explicitly rather than leaving NULL to be read as "unknown, probably fine".
    cur.execute("""UPDATE delimp_xic_trace_lane
                      SET extractor_version = COALESCE(extractor_version, 'pre-1.0.0-pilot'),
                          writer_version    = COALESCE(writer_version, 'pilot'),
                          n_channels        = COALESCE(n_channels, 9),
                          n_cycles          = COALESCE(n_cycles, 32)
                    WHERE extractor_version IS NULL OR writer_version IS NULL""")
    print(f"\nmarked {cur.rowcount} pre-versioning trace-lane dataset(s) as pilot/pre-fix")

    conn.commit()

    cur.execute("""SELECT DISTINCT ON (component) component, version, git_sha, recorded_at
                     FROM delimp_component_version ORDER BY component, recorded_at DESC""")
    print("\ndelimp_component_version now holds:")
    for c, v, g, t in cur.fetchall():
        print(f"  {c:24s} {v:12s} git={g or '-':10s} {t}")
    conn.close()
    print("\nDONE")


if __name__ == "__main__":
    main()
