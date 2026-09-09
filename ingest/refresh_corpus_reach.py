"""Refresh delimp_protein_corpus_reach — the corpus-wide facts behind the search heatmap.

Two things per gene, both far too slow for a web request (measured 2026-09-09 on PG Farm):
  n_searches / n_samples   121 s over 298,391 genes
  mean_pct_rank             97 s
Run on the same weekly schedule as refresh_leaderboards.py, after new searches land.

KEYED ON upper(gene). See the migration comment for why protein_group and raw gene are both wrong.
USES intensity, NOT normalized_intensity: the latter exists for 75 of 2,086 searches (4%).

Usage:  python refresh_corpus_reach.py
"""
from __future__ import annotations

import os
import sys
import time

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from coreomics_import import _conn                           # noqa: E402

# mean_pct_rank is meaningless off one search — a gene seen once scores a spurious 1.000 (measured:
# OR6C75 1.000 from one search, GM6133 0.033 from one). Readers must apply their own floor too; this
# column records how many searches contributed so they can.
SQL = """
WITH per AS (
  SELECT search_id, upper(gene) AS g,
         avg(intensity)          AS ai,
         count(DISTINCT raw_path) AS ns
    FROM delimp_proteins
   WHERE intensity > 0 AND NULLIF(gene,'') IS NOT NULL
   GROUP BY 1, 2),
rk AS (
  SELECT search_id, g, ns,
         percent_rank() OVER (PARTITION BY search_id ORDER BY ai) AS pr
    FROM per)
INSERT INTO delimp_protein_corpus_reach
      (gene, n_searches, n_samples, mean_pct_rank, n_pct_searches, computed_at)
SELECT g, count(*), sum(ns), avg(pr)::real, count(*), now()
  FROM rk GROUP BY g
ON CONFLICT (gene) DO UPDATE SET
  n_searches     = EXCLUDED.n_searches,
  n_samples      = EXCLUDED.n_samples,
  mean_pct_rank  = EXCLUDED.mean_pct_rank,
  n_pct_searches = EXCLUDED.n_pct_searches,
  computed_at    = EXCLUDED.computed_at;
"""


def main() -> int:
    con = _conn()
    con.autocommit = False
    with con.cursor() as cur:
        cur.execute("SET statement_timeout = '3600s'")
        t = time.time()
        cur.execute(SQL)
        n = cur.rowcount
        con.commit()
        print(f"delimp_protein_corpus_reach: {n:,} genes in {time.time() - t:.0f}s")
        cur.execute("SELECT count(*), max(computed_at) FROM delimp_protein_corpus_reach")
        total, when = cur.fetchone()
        print(f"table now holds {total:,} genes, computed_at {when}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
