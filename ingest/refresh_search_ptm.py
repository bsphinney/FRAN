#!/usr/bin/env python3
"""refresh_search_ptm.py — per-(search, protein_group) modification flags for the heatmap filter.

WHY THIS TABLE EXISTS: computing "does this protein carry a modification" live costs 16.4 s on a
10-sample search and 90.9 s on a 21-sample one. The matrix endpoint it would serve is PUBLIC,
anonymous, auto-fires on every search-page view and already costs 2.8-8.9 s. So the flags are
precomputed here.

WHY IT BATCHES. Measured: 12 searches in ONE pass = 13.7 s (1.1 s each). The SAME searches queried
individually = 16-91 s each. One batched pass is a single sequential scan of delimp_precursors;
a per-search loop is thousands of index lookups and sorts. A naive loop over 2,086 searches would
run 9-53 HOURS against ~40 minutes batched. Do not "simplify" this into a loop.

WRITES DO NOT GO THROUGH app.db.query(): that layer is read-only by design (SELECT/WITH only —
see its GovernanceError). Like refresh_leaderboards.py and build_protein_peptide_counts.py, this
script opens its own psycopg2 connection for the INSERT ... ON CONFLICT, reusing
refresh_leaderboards._token() for the PG Farm credential. Reads (the pending-search list) still go
through app.db.query() against the public allowlist.

Run from ingest/fran_mv_refresh.sbatch on the existing weekly schedule. Do NOT add a new cron.
"""
from __future__ import annotations
import argparse, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))          # ingest/ (for refresh_leaderboards)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app.db import query                                          # noqa: E402
from refresh_leaderboards import _token                           # noqa: E402

# GlyGly matches BOTH spellings on purpose. Historical Spectronaut rows store the literal
# `[GlyGly (K)]` because ingest/spectronaut_to_corpus.py's _MOD_UNIMOD lacked GlyGly until
# 2026-09-10; only re-ingested rows carry [UNIMOD:121]. Matching one form silently misses
# ~750,000 ubiquitin remnants in the Bennett_Penn searches.
SQL = """
INSERT INTO delimp_search_protein_ptm
      (search_id, protein_group, has_ptm, has_phospho, has_glygly, n_mod_precursors, computed_at)
SELECT p.search_id,
       p.protein_group,
       bool_or(p.n_mods > 0),
       bool_or(p.modified_seq_proforma LIKE '%%UNIMOD:21%%'),
       bool_or(p.modified_seq_proforma ILIKE '%%glygly%%'
            OR p.modified_seq_proforma LIKE '%%UNIMOD:121%%'),
       count(*) FILTER (WHERE p.n_mods > 0),
       now()
  FROM delimp_precursors p
 WHERE p.search_id = ANY(%(ids)s::uuid[])
   AND p.protein_group IS NOT NULL
 GROUP BY p.search_id, p.protein_group
ON CONFLICT (search_id, protein_group) DO UPDATE SET
       has_ptm          = EXCLUDED.has_ptm,
       has_phospho      = EXCLUDED.has_phospho,
       has_glygly       = EXCLUDED.has_glygly,
       n_mod_precursors = EXCLUDED.n_mod_precursors,
       computed_at      = EXCLUDED.computed_at
"""

PENDING = """
SELECT s.id FROM delimp_searches s
 WHERE NOT EXISTS (SELECT 1 FROM delimp_search_protein_ptm t WHERE t.search_id = s.id)
 LIMIT %(lim)s
"""


def _conn(timeout_ms: int = 900_000):
    import psycopg2
    return psycopg2.connect(
        host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
        dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
        user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
        password=_token(), sslmode="require", connect_timeout=30,
        options=f"-c statement_timeout={timeout_ms}")


def _run_batch(conn, ids: list[str]) -> None:
    with conn.cursor() as cur:
        cur.execute(SQL, {"ids": ids})
    conn.commit()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=50, help="searches per batched query")
    ap.add_argument("--limit", type=int, default=100000, help="max searches this run")
    ap.add_argument("--rebuild", action="store_true", help="reprocess every search, not just new ones")
    a = ap.parse_args()

    if a.rebuild:
        ids = [str(r["id"]) for r in query("SELECT id FROM delimp_searches", tables=["delimp_searches"])]
    else:
        ids = [str(r["id"]) for r in query(PENDING, {"lim": a.limit}, tables=["delimp_searches",
                                      "delimp_search_protein_ptm"])]
    if not ids:
        print("nothing to do — every search already has rows"); return 0

    print(f"{len(ids)} search(es) to process, {a.batch} per batch")
    conn = _conn()
    conn.autocommit = False
    done = 0
    try:
        for i in range(0, len(ids), a.batch):
            chunk = ids[i:i + a.batch]
            t = time.time()
            _run_batch(conn, chunk)
            done += len(chunk)
            print(f"  {done}/{len(ids)}  ({time.time() - t:.1f}s for {len(chunk)})", flush=True)
    finally:
        conn.close()
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
