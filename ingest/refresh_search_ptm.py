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

NOTHING HERE IMPORTS app.*, AND THAT IS A DEPLOYMENT CONSTRAINT, not a style choice. On Hive this
file runs from /quobyte/proteomics-grp/brett/glendon/fran_ingest/, a flat scp'd directory with no
`app/` beside or above it — verified 2026-09-16. An earlier revision did `from app.db import query`
behind a `sys.path.insert(0, "..")`, which resolves in the repo (app/ is a sibling of ingest/) and
raises ModuleNotFoundError on the cluster. It could not have used that layer anyway: app.db is
read-only by construction and this script writes.

The one sibling it may import is coreomics_import — the SAME import refresh_corpus_reach.py makes,
and coreomics_import.py is confirmed present in fran_ingest/. Note refresh_leaderboards is NOT:
it lives in fran_refresh/, a different directory, so `from refresh_leaderboards import _token`
would fail here exactly as app.db did. Every statement below is parameterised, so the reads lose
nothing by going straight through psycopg2.
tests/test_refresh_search_ptm_imports.py imports this module under a reconstructed Hive sys.path
and fails if an app.* import ever returns.

Run from ingest/fran_ptm_refresh.sbatch, its OWN weekly SLURM job. It does NOT belong in
fran_mv_refresh.sbatch: that job already TIMEOUTs at its 4-hour wall in 4 of its last 6 runs with
one payload (sacct, verified 2026-09-16), so a third payload would usually never start.
"""
from __future__ import annotations
import argparse, os, sys, time
# This directory only. No ".." — see the module docstring: there is no app/ above it on Hive.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from coreomics_import import _conn as _base_conn                  # noqa: E402

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

# PENDING MEANS "CAN PRODUCE ROWS AND HAS NOT YET", not merely "has no rows".
#
# Five real searches (223106d8, 58918226, 90d20943, e7bf2b7b, f0501ee6) have delimp_proteins rows
# and render heatmaps, but hold NO delimp_precursors row with a non-NULL protein_group — so SQL
# above groups nothing for them and inserts nothing. Under the old predicate they came back
# PENDING on every run, forever: "nothing to do" could never print, and each weekly run re-scanned
# them. The job never converged.
#
# The second EXISTS is the fix, and it is preferred over the two alternatives because it invents
# nothing. A sentinel row would converge but would pollute delimp_search_protein_ptm, and the
# matrix's readiness probe (EXISTS any row for this search) would then read the sentinel as
# "computed" and let the UI claim these searches have no modified proteins — a claim nobody can
# support. A processed-watermark table needs DDL and would record these five as done permanently,
# so a later re-ingest that finally gives them protein_groups would be ignored. This predicate
# re-includes them the moment they become computable, and excludes them the rest of the time.
#
# Cost: one anti-join over delimp_precursors, measured 21.6 s, once per weekly run of a job that
# takes ~40 minutes. The five stay absent from the rollup, so the matrix keeps reporting
# ptm_rollup_ready=False for them — which is true: their PTM state has not been computed, and
# from this data it cannot be. That is the honest answer, and it is never "no modified proteins".
PENDING = """
SELECT s.id FROM delimp_searches s
 WHERE NOT EXISTS (SELECT 1 FROM delimp_search_protein_ptm t WHERE t.search_id = s.id)
   AND EXISTS (SELECT 1 FROM delimp_precursors p
                WHERE p.search_id = s.id AND p.protein_group IS NOT NULL)
 LIMIT %(lim)s
"""


def _conn(timeout_ms: int = 900_000):
    """The shared connection helper, plus a statement timeout this job actually needs.

    coreomics_import._conn() is the same one refresh_corpus_reach.py uses and carries the token
    convention (DELIMP_PG_TOKEN_FILE -> JWT exchange); it sets no statement_timeout, and the
    default would kill both the batched INSERT and the PENDING anti-join. SET rather than a
    connect option so there is one connection helper on this cluster, not two.
    """
    con = _base_conn()
    with con.cursor() as cur:
        cur.execute(f"SET statement_timeout = {int(timeout_ms)}")
    con.commit()
    return con


def _rows(con, sql: str, params: dict) -> list[str]:
    """Read a one-column id list. Parameterised, like every statement in this file."""
    with con.cursor() as cur:
        cur.execute(sql, params)
        return [str(r[0]) for r in cur.fetchall()]


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

    conn = _conn()
    conn.autocommit = False
    if a.rebuild:
        ids = _rows(conn, "SELECT id FROM delimp_searches", {})
    else:
        ids = _rows(conn, PENDING, {"lim": a.limit})
    if not ids:
        conn.close()
        print("nothing to do — every search that can produce rows has them"); return 0

    print(f"{len(ids)} search(es) to process, {a.batch} per batch")
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
