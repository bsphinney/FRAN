"""Refresh the FRAN leaderboard materialized views (the Highlights snapshots).

A full GROUP BY over the millions of delimp_precursors rows times out in a live web
request on PG Farm, so the leaderboards are served from precomputed materialized views
(delimp_mv_top_peptides / _proteins / _genes). Run this AFTER ingesting new searches (or
on a schedule, e.g. daily) to refresh those snapshots. Slow-but-offline: uses a long
statement_timeout. Token from $DELIMP_PG_PASSWORD or ~/.pgfarm_token (a JWT, or the
service-account secret which is auto-exchanged — see docs/PGFARM_SERVICE_ACCOUNT_AUTH.md).

Usage:  python refresh_leaderboards.py [--create]   (--create builds them if missing)
"""
from __future__ import annotations

import os
import sys
import time

import psycopg2

# Order matters: refresh the cheap, high-value overview views FIRST so the site's headline numbers
# update even if a later view times out, and put the most expensive view (top_peptides — multiple
# COUNT(DISTINCT) over the ~400M-row delimp_precursors) LAST. protein_agg DEPENDS ON
# species_proteins so it must follow it.
_MVS = ("delimp_mv_corpus_stats", "delimp_mv_top_proteins", "delimp_mv_top_genes",
        "delimp_mv_species_proteins", "delimp_mv_protein_agg", "delimp_mv_im_scatter",
        "delimp_mv_top_peptides")

_CREATE = {
    "delimp_mv_top_peptides": """CREATE MATERIALIZED VIEW IF NOT EXISTS delimp_mv_top_peptides AS
      SELECT stripped_seq, COUNT(*) AS n_obs, COUNT(DISTINCT raw_path) AS n_runs,
             COUNT(DISTINCT search_id) AS n_searches, COUNT(DISTINCT charge) AS n_charges,
             bool_or(im IS NOT NULL) AS has_im
      FROM delimp_precursors GROUP BY stripped_seq ORDER BY n_runs DESC, n_obs DESC LIMIT 100000""",
    "delimp_mv_top_proteins": """CREATE MATERIALIZED VIEW IF NOT EXISTS delimp_mv_top_proteins AS
      SELECT protein_group, MAX(gene) AS gene, COUNT(DISTINCT search_id) AS n_searches,
             COUNT(DISTINCT raw_path) AS n_runs, SUM(n_precursors) AS sum_precursors,
             bool_or(is_contaminant) AS any_contaminant
      FROM delimp_proteins GROUP BY protein_group ORDER BY n_runs DESC, sum_precursors DESC NULLS LAST LIMIT 5000""",
    "delimp_mv_top_genes": """CREATE MATERIALIZED VIEW IF NOT EXISTS delimp_mv_top_genes AS
      SELECT gene, COUNT(DISTINCT protein_group) AS n_groups, COUNT(DISTINCT search_id) AS n_searches,
             COUNT(DISTINCT raw_path) AS n_runs
      FROM delimp_proteins WHERE gene IS NOT NULL AND gene<>'' GROUP BY gene ORDER BY n_runs DESC, n_searches DESC LIMIT 5000""",
    # im > 0.3: exclude precursors with no real ion mobility (DIA-NN writes 0 when 1/K0 was
    # never determined; Orbitrap has none) — those would smear a false band along 1/K0=0.
    "delimp_mv_im_scatter": """CREATE MATERIALIZED VIEW IF NOT EXISTS delimp_mv_im_scatter AS
      SELECT rt, irt, im, charge, precursor_mz, intensity_log2 FROM (
        SELECT DISTINCT ON (stripped_seq, charge) stripped_seq, charge, rt, irt, im, precursor_mz, intensity_log2
        FROM delimp_precursors WHERE im > 0.3 AND rt IS NOT NULL
          -- drop mis-predicted iRT (corpus has stray -2900 and 3e12 values) so the iRT axis
          -- isn't blown out; real Biognosys-scale iRT sits well within [-100,300].
          AND (irt IS NULL OR (irt > -100 AND irt < 300))
        ORDER BY stripped_seq, charge) q LIMIT 20000""",
    # EXACT distinct counts for the dashboard header. The planner's pg_stats.n_distinct
    # estimate is wildly low for high-cardinality columns (it read 43k for stripped_seq vs a
    # true 356k — ANALYZE samples ~30k of 16M rows and under-counts when values are near-unique).
    # A COUNT(DISTINCT) is a few seconds offline, so we precompute it here and the dashboard
    # reads this 1-row view instantly instead of estimating.
    "delimp_mv_corpus_stats": """CREATE MATERIALIZED VIEW IF NOT EXISTS delimp_mv_corpus_stats AS
      SELECT
        (SELECT COUNT(DISTINCT stripped_seq) FROM delimp_precursors
           WHERE stripped_seq IS NOT NULL AND stripped_seq<>'') AS distinct_peptides,
        (SELECT COUNT(DISTINCT protein_group) FROM delimp_proteins
           WHERE protein_group IS NOT NULL AND protein_group<>'') AS distinct_protein_groups,
        (SELECT COUNT(*) FROM delimp_precursors) AS total_precursors,
        (SELECT COUNT(*) FROM delimp_proteins) AS total_proteins,
        (SELECT COUNT(*) FROM delimp_precursors WHERE im IS NOT NULL) AS im_bearing_precursors,
        -- distinct physical acquisitions (unique raw basenames) vs total raw-file rows: the gap
        -- is RE-ANALYSES (same .d searched >1 way) — kept on purpose (cross-engine/param compare),
        -- surfaced as its own honest metric so 'searches' isn't mistaken for unique data.
        (SELECT COUNT(DISTINCT raw_basename) FROM raw_files
           WHERE raw_basename IS NOT NULL AND raw_basename<>'') AS distinct_acquisitions,
        (SELECT COUNT(*) FROM raw_files) AS raw_file_rows,
        CURRENT_TIMESTAMP AS computed_at""",
    # Per-species protein aggregation for the species detail page. A live GROUP BY over
    # delimp_proteins per species can exceed the 30s web timeout, so the page reads this
    # precomputed view (indexed on organism_name -> sub-second).
    "delimp_mv_species_proteins": """CREATE MATERIALIZED VIEW IF NOT EXISTS delimp_mv_species_proteins AS
      SELECT m.organism_name, p.protein_group, MAX(p.gene) AS gene,
             COUNT(DISTINCT p.raw_path) AS n_runs, COUNT(DISTINCT p.search_id) AS n_searches,
             SUM(p.n_precursors) AS sum_prec, MAX(p.n_unique_peptides) AS max_pep,
             AVG(NULLIF(p.intensity,0)) AS mean_int, bool_or(p.is_contaminant) AS contam
      FROM delimp_proteins p JOIN delimp_sample_metadata m ON m.raw_path = p.raw_path
      WHERE m.organism_name IS NOT NULL AND m.organism_name <> ''
        AND p.protein_group IS NOT NULL AND p.protein_group <> ''
      GROUP BY m.organism_name, p.protein_group;
      CREATE INDEX IF NOT EXISTS idx_mv_species_proteins_org ON delimp_mv_species_proteins (organism_name)""",
    # Per-protein-group rollup OF the species matview — powers the proteins showcase via top-N slices
    # instead of a live 499k-row GROUP BY (which timed out on the web node). DEPENDS ON
    # delimp_mv_species_proteins, so it's last in _MVS (refreshed after it).
    "delimp_mv_protein_agg": """CREATE MATERIALIZED VIEW IF NOT EXISTS delimp_mv_protein_agg AS
      SELECT protein_group, MAX(gene) AS gene, COUNT(DISTINCT organism_name) AS n_species,
             SUM(n_runs) AS sum_runs, SUM(n_searches) AS sum_searches, MAX(max_pep) AS max_pep,
             MAX(mean_int) AS peak_mean_int, bool_or(contam) AS contam,
             (bool_or(contam) OR protein_group ~* '^(crap|cont[_-])') AS is_cont,
             (array_agg(organism_name ORDER BY n_runs DESC))[1] AS top_organism
      FROM delimp_mv_species_proteins GROUP BY protein_group;
      CREATE INDEX IF NOT EXISTS idx_pagg_cont_species ON delimp_mv_protein_agg (is_cont, n_species DESC, sum_runs DESC);
      CREATE INDEX IF NOT EXISTS idx_pagg_cont_int ON delimp_mv_protein_agg (is_cont, peak_mean_int DESC NULLS LAST);
      CREATE INDEX IF NOT EXISTS idx_pagg_cont_pep ON delimp_mv_protein_agg (is_cont, max_pep DESC NULLS LAST)""",
}


def _token():
    pw = os.environ.get("DELIMP_PG_PASSWORD")
    if not pw:
        tf = os.environ.get("DELIMP_PG_TOKEN_FILE") or os.path.expanduser("~/.pgfarm_token")
        if os.path.exists(tf):
            pw = open(tf).read().strip()
    if not pw:
        sys.exit("No PG Farm token (DELIMP_PG_PASSWORD or ~/.pgfarm_token)")
    # if it's the long-lived secret (not a JWT), exchange it for a JWT
    if not (pw.startswith("eyJ") and pw.count(".") == 2):
        import json, urllib.request
        body = json.dumps({"username": os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
                           "secret": pw}).encode()
        req = urllib.request.Request("https://pgfarm.library.ucdavis.edu/auth/service-account/login",
                                     data=body, headers={"Content-Type": "application/json"})
        pw = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())["access_token"]
    return pw


def main():
    create = "--create" in sys.argv
    con = psycopg2.connect(host="pgfarm.library.ucdavis.edu", port=5432,
        dbname="uc-davis-genome-center-proteomics-core/delimp",
        user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
        password=_token(), sslmode="require", connect_timeout=30,
        keepalives=1, keepalives_idle=20, keepalives_interval=10, keepalives_count=6,
        options="-c statement_timeout=7200000")  # 2h — top_peptides/corpus_stats do COUNT(DISTINCT) over ~400M rows
    con.autocommit = True
    cur = con.cursor()
    for mv in _MVS:
        t = time.time()
        try:
            if create:
                cur.execute(_CREATE[mv])
            cur.execute(f"REFRESH MATERIALIZED VIEW {mv}")
            cur.execute(f"SELECT COUNT(*) FROM {mv}")
            print(f"{mv}: refreshed in {time.time()-t:.0f}s ({cur.fetchone()[0]} rows)")
        except Exception as e:  # noqa: BLE001
            print(f"{mv}: {type(e).__name__}: {str(e)[:120]}")
    con.close()


if __name__ == "__main__":
    main()
