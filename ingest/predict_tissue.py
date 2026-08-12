"""predict_tissue.py — infer sample tissue for human runs from tissue-enriched marker proteins.

Scores every human acquisition against the Yue et al. 2026 panel (ingest/build_tissue_panel.py) and
writes an EMIT-OR-ABSTAIN prediction to delimp_tissue_prediction. Writes only `predicted_*` fields;
the curated ontology columns on delimp_sample_metadata are never touched.

THE CONFOUND THIS IS BUILT AROUND. Marker detection tracks proteome DEPTH. Measured on this corpus:
runs positive for >=2 liver markers average 3,804 identified genes against 2,350 for the rest. A raw
marker count would therefore rank "deep run" above "liver", and the deepest runs would win every
tissue at once.

Two things establish that the signal is nonetheless real, and both shape the scoring:

  * Liver-positive and pancreas-positive runs are NEAR-DISJOINT -- 10 of 132, an 8% overlap. Depth
    alone cannot produce that; a deep run would light up both panels.
  * Panels are rare rather than ubiquitous: 0.3% (stomach) to 7.0% (brain) of human acquisitions.

SO THE SCORE IS AN ENRICHMENT, NOT A COUNT. For run r and tissue t:

      hits      = |observable_markers(t) INTERSECT genes(r)|
      expected  = |observable_markers(t)| * detection_rate(r)
      enrichment = hits / expected

where detection_rate(r) = |genes(r)| / |genes in the whole human corpus|. A run that detects 60% of
everything is expected to detect 60% of any panel by chance; only exceeding that is evidence. This
is depth-normalised by construction.

AND A MARGIN IS REQUIRED, NOT A THRESHOLD. Tissues share biology -- liver and pancreas are both
secretory, brain and spinal cord overlap heavily. A run is only labelled when the top tissue beats
the runner-up by MARGIN, so "high for everything" abstains rather than picking the alphabetically
lucky panel. Every abstention records WHICH reason, because a NULL that cannot be distinguished from
"not scored" is the defect this whole exercise exists to avoid.

Matching handles two corpus quirks: `delimp_proteins.gene` is ';'-separated for shared protein groups
(1,239,649 rows), and capitalisation is inconsistent ('Hsd17b13' vs 'HSD17B13').

    python ingest/predict_tissue.py                 # dry run, prints the distribution
    python ingest/predict_tissue.py --apply
"""
import argparse
import functools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
print = functools.partial(print, flush=True)   # noqa: A001

SOURCE = "Yue2026_Nature_656_227_SuppTable5A"
HUMAN = 9606

# A PLAIN ENRICHMENT RATIO IS BIASED TOWARD SMALL PANELS, and badly. Measured on the first run of
# this scorer: Cowper's gland (6 markers) was the top call for 233 human acquisitions and Tendon
# (21) scored 23.9x, while Brain (349) was emitted for only 9 -- an obviously wrong answer that a
# blind test on a known-brain dataset caught. Detecting 6 of 6 small-panel markers is unremarkable
# in a run that sees a third of the proteome; detecting 319 of 349 is not, and a ratio cannot tell
# them apart because both are ~3x.
#
# So tissues are ranked by HYPERGEOMETRIC significance: given a draw of n_groups from the universe,
# how surprising is n_hit of n_panel? That penalises small panels by construction. The enrichment
# ratio is still stored, because it is the interpretable number, but it does not decide the call.
MIN_OBSERVABLE = 15     # below this a panel cannot reach significance anyway
MIN_HITS = 5            # at least this many markers seen before any claim
MIN_ENRICHMENT = 1.5    # still require real excess, not just significance from a big panel
MAX_PVALUE = 1e-4       # the primary bar
MIN_LOGP_MARGIN = 2.0   # top must beat runner-up by 2 orders of magnitude in p

DDL = """
CREATE TABLE IF NOT EXISTS delimp_tissue_prediction (
    raw_basename        text PRIMARY KEY,
    predicted_tissue    text,
    status              text NOT NULL,
    enrichment          double precision,
    margin              double precision,
    n_markers_hit       integer,
    n_markers_panel     integer,
    n_genes_in_run      integer,
    top_candidate       text,          -- always set: what it WOULD have said, even when abstaining
    top_neglog10p       double precision,
    runner_up_tissue    text,
    runner_up_enrichment double precision,
    source              text NOT NULL,
    scored_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tispred_tissue ON delimp_tissue_prediction (predicted_tissue)
    WHERE predicted_tissue IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tispred_status ON delimp_tissue_prediction (status);
"""

# COST NOTE. The first attempt unnested every gene of every human protein row to get both the panel
# hits and the per-run depth. That is a very large scan and did not return. The depth term only needs
# to be MONOTONE in how much a run detected -- it divides out identically across tissues, so it
# cannot change which tissue wins or the margin between them, only the absolute enrichment scale.
# So depth uses count(DISTINCT protein_group), which needs no unnest, and the unnest is confined to
# rows that actually match a panel gene.
# QUANT-WEIGHTED. The panel is not a list of yes/no markers: Supplementary Table 5A is a
# 1,717 x 64 z-score matrix (16,298 positive cells, 9.5 tissues per protein). A protein at z=7.9 in
# Brain is far stronger evidence than one at z=0.4, and the earlier argmax-only version treated them
# identically -- it discarded almost all the quantitative information the paper provides.
#
# So each tissue now gets TWO numbers per run:
#   * a hypergeometric p on marker COUNTS, which stays the significance gate because it is what
#     killed the small-panel bias (Cowper's gland, 6 markers, was winning 233 runs on a bare ratio);
#   * a z-WEIGHTED enrichment, sum of z over detected markers against the sum expected at this run's
#     own detection rate, which decides the ranking among tissues that pass the gate.
# Negative z cells are excluded upstream: "depleted here" is not evidence FOR a tissue.
SCORE_SQL = """
WITH human_runs AS (
  SELECT DISTINCT rf.raw_basename AS bn, rf.raw_path
  FROM raw_files rf
  JOIN delimp_sample_metadata m ON m.raw_path = rf.raw_path
  WHERE m.organism_taxon_id = %(human)s
),
depth AS (
  SELECT hr.bn, count(DISTINCT p.protein_group) AS n_groups
  FROM delimp_proteins p JOIN human_runs hr ON hr.raw_path = p.raw_path
  GROUP BY 1
),
zpanel AS (
  SELECT tissue, gene_upper, z_score
  FROM delimp_tissue_marker_z WHERE source = %(source)s
),
-- resolve marker protein GROUPS once, then join on equality. Testing every protein row with an
-- EXISTS+unnest is what made an earlier version of this run for tens of minutes.
mg AS (
  SELECT DISTINCT p.protein_group, upper(btrim(g)) AS gene_upper
  FROM delimp_proteins p
  CROSS JOIN LATERAL unnest(string_to_array(p.gene, ';')) AS g
  WHERE p.gene IS NOT NULL
    AND upper(btrim(g)) IN (SELECT DISTINCT gene_upper FROM zpanel)
),
hit_genes AS (
  SELECT hr.bn, mg.gene_upper
  FROM delimp_proteins p
  JOIN human_runs hr ON hr.raw_path = p.raw_path
  JOIN mg ON mg.protein_group = p.protein_group
  GROUP BY 1, 2
),
hits AS (
  SELECT h.bn, z.tissue,
         count(*)      AS n_hit,
         sum(z.z_score) AS z_hit
  FROM hit_genes h JOIN zpanel z ON z.gene_upper = h.gene_upper
  GROUP BY 1, 2
),
-- panel totals over markers the corpus can actually detect anywhere
observable AS (
  SELECT z.tissue, count(*) AS n_panel, sum(z.z_score) AS z_panel
  FROM zpanel z
  WHERE EXISTS (SELECT 1 FROM hit_genes h WHERE h.gene_upper = z.gene_upper)
  GROUP BY 1 HAVING count(*) >= %(min_obs)s
)
SELECT h.bn, h.tissue, h.n_hit, o.n_panel, d.n_groups, h.z_hit, o.z_panel
FROM hits h
JOIN observable o ON o.tissue = h.tissue
JOIN depth d ON d.bn = h.bn
"""


def _conn():
    import psycopg2
    from refresh_leaderboards import _token
    return psycopg2.connect(
        host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
        dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
        user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
        password=_token(), sslmode="require", connect_timeout=30,
        options="-c statement_timeout=1800000")


def classify(n_hit, enr, logp, logp_margin):
    """emit-or-abstain, with the REASON recorded. A bare NULL is indistinguishable from
    'never scored', which is exactly the gap this table exists to close."""
    if n_hit < MIN_HITS:
        return "abstained_too_few_markers"
    if logp is None or logp < -__import__("math").log10(MAX_PVALUE):
        return "abstained_not_significant"
    if enr is None or enr < MIN_ENRICHMENT:
        return "abstained_low_enrichment"
    if logp_margin is not None and logp_margin < MIN_LOGP_MARGIN:
        return "abstained_ambiguous"
    return "emitted"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    conn = _conn(); conn.autocommit = False
    cur = conn.cursor()
    cur.execute("SET LOCAL lock_timeout = '10s'")
    cur.execute("SET work_mem = '256MB'")

    print("scoring (one pass over human runs; this is the expensive step)...")
    cur.execute(SCORE_SQL, {"human": HUMAN, "source": SOURCE, "min_obs": MIN_OBSERVABLE})
    raw = cur.fetchall()
    print(f"{len(raw):,} (run, tissue) candidate pairs")

    # UNIVERSE: distinct protein groups seen anywhere in the human corpus. The hypergeometric needs
    # a population size; using the corpus max keeps it on the same scale as n_groups.
    cur.execute("""SELECT max(n) FROM (
        SELECT count(DISTINCT p.protein_group) n FROM delimp_proteins p
        JOIN raw_files rf ON rf.raw_path = p.raw_path
        JOIN delimp_sample_metadata m ON m.raw_path = p.raw_path
        WHERE m.organism_taxon_id = %s GROUP BY rf.raw_basename) x""", (HUMAN,))
    universe = int(cur.fetchone()[0] or 0)
    print(f"universe (deepest human run): {universe:,} protein groups")

    from scipy.stats import hypergeom
    import math
    from collections import defaultdict
    per_run = defaultdict(list)
    for bn, tissue, n_hit, n_panel, n_groups, z_hit, z_panel in raw:
        per_run[bn].append((tissue, n_hit, n_panel, n_groups, z_hit, z_panel))

    out = []
    for bn, cands in per_run.items():
        scored = []
        for tissue, n_hit, n_panel, n_groups, z_hit, z_panel in cands:
            n = min(int(n_groups), universe)
            K = min(int(n_panel), universe)
            k = int(n_hit)
            sf = float(hypergeom.sf(k - 1, universe, K, n))
            logp = -math.log10(max(sf, 1e-300))
            # z-weighted enrichment: observed z mass vs the z mass expected if this run's markers
            # were drawn at its own detection rate. Falls back to the count ratio if z is missing.
            z_exp = (float(z_panel or 0) * n / universe) if z_panel else 0
            z_enr = (float(z_hit or 0) / z_exp) if z_exp > 0 else None
            exp = K * n / universe
            enr = (k / exp) if exp > 0 else None
            scored.append((tissue, k, K, n_groups, z_enr if z_enr is not None else enr, logp))
        # gate on significance, then rank by the QUANT-weighted score among those that pass
        sig = [x for x in scored if x[5] >= -__import__("math").log10(MAX_PVALUE)]
        (sig if sig else scored).sort(key=lambda x: -x[4])
        scored = (sig + [x for x in scored if x not in sig]) if sig else scored
        top = scored[0]
        nxt = scored[1] if len(scored) > 1 else None
        logp_margin = (top[5] - nxt[5]) if nxt else None
        st = classify(top[1], top[4], top[5], logp_margin)
        # top_candidate is recorded even when abstaining. Without it an abstention is opaque --
        # you cannot tell a near-miss from a run with no signal at all, and cannot judge whether a
        # threshold is set sensibly. predicted_tissue stays NULL unless the call was actually made.
        out.append((bn, top[0] if st == "emitted" else None, st, top[4], logp_margin,
                    top[1], top[2], top[3], top[0], top[5],
                    nxt[0] if nxt else None, nxt[4] if nxt else None, SOURCE))

    from collections import Counter
    st_counts = Counter(r[2] for r in out)
    print(f"\n{len(out):,} human acquisitions scored\n\nstatus:")
    for k, v in st_counts.most_common():
        print(f"   {k:28s} {v:>6,}  ({100*v/len(out):.1f}%)")
    emitted = [r for r in out if r[2] == "emitted"]
    tis = Counter(r[1] for r in emitted)
    print(f"\n{len(emitted):,} labelled across {len(tis)} tissues:")
    for t, n in tis.most_common(20):
        es = sorted(r[3] for r in emitted if r[1] == t)
        print(f"   {t:26s} {n:>5}  (median enrichment {es[len(es)//2]:.1f}x, "
              f"panel {[r[6] for r in emitted if r[1]==t][0]})")

    if not a.apply:
        print("\nDRY RUN — re-run with --apply.")
        conn.rollback(); conn.close(); return

    cur.execute(DDL); conn.commit()
    import psycopg2.extras
    psycopg2.extras.execute_values(cur, """
        INSERT INTO delimp_tissue_prediction
          (raw_basename, predicted_tissue, status, enrichment, margin, n_markers_hit,
           n_markers_panel, n_genes_in_run, top_candidate, top_neglog10p,
           runner_up_tissue, runner_up_enrichment, source)
        VALUES %s
        ON CONFLICT (raw_basename) DO UPDATE SET
          predicted_tissue=EXCLUDED.predicted_tissue, status=EXCLUDED.status,
          enrichment=EXCLUDED.enrichment, margin=EXCLUDED.margin,
          n_markers_hit=EXCLUDED.n_markers_hit, n_markers_panel=EXCLUDED.n_markers_panel,
          n_genes_in_run=EXCLUDED.n_genes_in_run, top_candidate=EXCLUDED.top_candidate,
          top_neglog10p=EXCLUDED.top_neglog10p, runner_up_tissue=EXCLUDED.runner_up_tissue,
          runner_up_enrichment=EXCLUDED.runner_up_enrichment, source=EXCLUDED.source,
          scored_at=now()""", out, page_size=500)
    conn.commit()
    print(f"\nstored {len(out):,} predictions")

    cur.execute("""COMMENT ON TABLE delimp_tissue_prediction IS
      'INFERRED tissue per human acquisition, from tissue-enriched marker enrichment against the '
      'Yue et al. 2026 panel. NEVER curated -- keep separate from delimp_sample_metadata.tissue_name '
      '(which is 0%% populated). predicted_tissue is NULL unless status = ''emitted''; the other '
      'statuses record WHY it abstained rather than leaving an unexplained NULL. The score is a '
      'depth-normalised ENRICHMENT (observed hits / hits expected at this run''s own detection rate), '
      'because raw marker counts track proteome depth: liver-positive runs average 3,804 genes vs '
      '2,350. A label also requires beating the runner-up tissue by a margin.'""")
    conn.commit()

    import versions as V
    V.record_run(cur, "tissue_prediction", "1.0.0",
                 notes=f"{len(emitted)} emitted of {len(out)} scored, {len(tis)} tissues")
    conn.commit(); conn.close()
    print("DONE")


if __name__ == "__main__":
    main()
