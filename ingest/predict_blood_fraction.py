"""predict_blood_fraction.py — detect plasma / serum runs, which the tissue panel cannot.

WHY A SEPARATE DETECTOR. The Yue et al. 2026 atlas has 64 tissue columns and NONE of them is plasma,
serum, or blood -- verified in both Supplementary Table 5 sheets. That is not an oversight: "tissue
enriched" means >=4x higher in one tissue than all others, and plasma proteins are LIVER-SECRETED,
so albumin and friends are enriched in liver, not plasma. The paper profiled whole blood and
platelet-rich plasma; they simply yield no tissue-enriched proteins by that definition.

So plasma can never win a tissue panel, and every plasma run in FRAN correctly abstains there.

A DIFFERENT SIGNAL ENTIRELY. Tissue identity is about WHICH proteins are present. Plasma identity is
about DOMINANCE: albumin alone is roughly half of plasma protein, and the classical ~26 abundant
plasma proteins are the overwhelming majority of the signal. A tissue lysate with a little blood
contamination CONTAINS albumin; plasma is MADE of it. Presence/absence cannot separate those --
intensity share can.

    blood_fraction(run) = sum(intensity of plasma-core proteins) / sum(intensity of all proteins)

PLASMA vs SERUM comes free from the same numbers. Serum is plasma after clotting, and clotting
consumes fibrinogen -- so FGA/FGB/FGG are abundant in plasma and strongly depleted in serum. The
fibrinogen share of the blood signal separates them without any extra measurement.

Nothing here is asserted onto delimp_sample_metadata; this writes its own predicted_* table.

    python ingest/predict_blood_fraction.py            # dry run + validation against CoreOmics text
    python ingest/predict_blood_fraction.py --apply
"""
import argparse
import functools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
print = functools.partial(print, flush=True)   # noqa: A001

HUMAN = 9606

# The classical abundant plasma proteome. Deliberately NOT including immunoglobulin variable genes
# (IGHV*, IGKV*) -- they are highly abundant in plasma but their gene symbols vary between search
# engines and FASTA versions, which would make the score depend on the pipeline rather than the sample.
PLASMA_CORE = [
    "ALB", "TF", "APOA1", "APOB", "C3", "HP", "A2M", "SERPINA1", "HPX", "CP", "APOA2", "AHSG",
    "ORM1", "ORM2", "C4A", "C4B", "ITIH4", "ITIH2", "APOC3", "TTR", "AMBP", "SERPINC1", "PLG",
    "F2", "VTN", "GC", "LRG1", "CFB", "APOH", "SERPINF2", "KNG1", "HRG", "C9", "CLU",
]
FIBRINOGEN = ["FGA", "FGB", "FGG"]

# Thresholds. A tissue lysate carries some blood; these separate "contains blood" from "is blood".
MIN_BLOOD_FRACTION = 0.30    # below this the run is not blood-derived
FIB_SERUM_MAX = 0.005        # fibrinogen share of blood signal: serum is depleted by clotting
FIB_PLASMA_MIN = 0.02        # plasma retains it

DDL = """
CREATE TABLE IF NOT EXISTS delimp_blood_prediction (
    raw_basename      text PRIMARY KEY,
    predicted_matrix  text,          -- 'plasma' | 'serum' | 'blood_derived_unresolved' | NULL
    status            text NOT NULL,
    blood_fraction    double precision,
    fibrinogen_share  double precision,
    n_blood_proteins  integer,
    total_intensity   double precision,
    scored_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bloodpred_matrix ON delimp_blood_prediction (predicted_matrix)
    WHERE predicted_matrix IS NOT NULL;
"""

SQL = """
WITH human_runs AS (
  SELECT DISTINCT rf.raw_basename AS bn, rf.raw_path
  FROM raw_files rf JOIN delimp_sample_metadata m ON m.raw_path = rf.raw_path
  WHERE m.organism_taxon_id = %(human)s
),
-- one intensity per (run, protein group): the same group appears once per search, and summing
-- across searches would weight a protein by how many times its run was re-searched.
pg AS (
  SELECT hr.bn, p.protein_group, max(upper(p.gene)) AS gene,
         max(coalesce(p.normalized_intensity, p.intensity)) AS inten
  FROM delimp_proteins p JOIN human_runs hr ON hr.raw_path = p.raw_path
  WHERE coalesce(p.normalized_intensity, p.intensity) IS NOT NULL
  GROUP BY 1, 2
),
tagged AS (
  SELECT bn, inten,
         EXISTS (SELECT 1 FROM unnest(string_to_array(gene, ';')) g
                 WHERE btrim(g) = ANY(%(core)s)) AS is_blood,
         EXISTS (SELECT 1 FROM unnest(string_to_array(gene, ';')) g
                 WHERE btrim(g) = ANY(%(fib)s))  AS is_fib
  FROM pg
)
SELECT bn,
       sum(inten)                                    AS total_int,
       sum(inten) FILTER (WHERE is_blood)            AS blood_int,
       sum(inten) FILTER (WHERE is_fib)              AS fib_int,
       count(*)   FILTER (WHERE is_blood)            AS n_blood
FROM tagged GROUP BY 1
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


def classify(frac, fib_share, n_blood):
    if frac is None or n_blood < 5:
        return None, "abstained_too_few_blood_proteins"
    if frac < MIN_BLOOD_FRACTION:
        return None, "not_blood_derived"
    if fib_share is None:
        return "blood_derived_unresolved", "emitted"
    if fib_share <= FIB_SERUM_MAX:
        return "serum", "emitted"
    if fib_share >= FIB_PLASMA_MIN:
        return "plasma", "emitted"
    # between the two: blood-derived for certain, plasma-vs-serum genuinely undecidable
    return "blood_derived_unresolved", "emitted"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    conn = _conn(); conn.autocommit = False
    cur = conn.cursor()
    cur.execute("SET LOCAL lock_timeout = '10s'")
    cur.execute("SET work_mem = '256MB'")

    print("computing blood fraction per human acquisition...")
    cur.execute(SQL, {"human": HUMAN, "core": PLASMA_CORE, "fib": FIBRINOGEN})
    rows = cur.fetchall()
    print(f"{len(rows):,} runs with usable intensity")

    out = []
    for bn, total, blood, fib, n_blood in rows:
        total = float(total or 0); blood = float(blood or 0); fib = float(fib or 0)
        frac = (blood / total) if total > 0 else None
        fib_share = (fib / blood) if blood > 0 else None
        matrix, st = classify(frac, fib_share, n_blood or 0)
        out.append((bn, matrix, st, frac, fib_share, n_blood, total))

    from collections import Counter
    print("\nstatus:")
    for k, v in Counter(r[2] for r in out).most_common():
        print(f"   {k:34s} {v:>6,}  ({100*v/len(out):.1f}%)")
    called = [r for r in out if r[1]]
    print(f"\n{len(called):,} blood-derived:")
    for k, v in Counter(r[1] for r in called).most_common():
        fr = sorted(r[3] for r in called if r[1] == k)
        print(f"   {k:28s} {v:>5}  (median blood fraction {fr[len(fr)//2]*100:.0f}%)")

    # ---- VALIDATE against what the submitter wrote, before writing anything -------------------
    print("\n=== validation vs CoreOmics submission text ===")
    cur.execute("""
      SELECT DISTINCT rf.raw_basename,
             (coalesce(sub.description,'') || ' ' || coalesce(sub.other_info,'')) AS txt
      FROM raw_files rf
      JOIN delimp_sample_metadata m ON m.raw_path = rf.raw_path
      JOIN search_raw_files srf ON srf.raw_path = rf.raw_path
      JOIN delimp_search_provenance sp ON sp.search_id = srf.search_id
      JOIN coreomics_submissions_cache sub ON sub.submission_id = sp.coreomics_submission_id
      WHERE m.organism_taxon_id = %s
        AND (sub.description ~* 'plasma|serum' OR sub.other_info ~* 'plasma|serum')""", (HUMAN,))
    import re
    truth = {}
    for bn, txt in cur.fetchall():
        has_p = bool(re.search(r"plasma", txt, re.I))
        has_s = bool(re.search(r"serum", txt, re.I))
        truth[bn] = ("plasma" if has_p and not has_s else
                     "serum" if has_s and not has_p else "both_mentioned")
    pred = {r[0]: (r[1], r[3], r[4]) for r in out}
    agree = wrong = missed = 0
    for bn, want in truth.items():
        got = pred.get(bn, (None, None, None))
        blood_ok = got[0] in ("plasma", "serum", "blood_derived_unresolved")
        if not blood_ok:
            missed += 1
            print(f"   MISSED  text={want:16s} pred={got[0]}  frac="
                  f"{'-' if got[1] is None else f'{got[1]*100:.0f}%'}  {bn[:44]}")
        elif want == "both_mentioned" or got[0] == "blood_derived_unresolved" or got[0] == want:
            agree += 1
        else:
            wrong += 1
            print(f"   MISMATCH text={want:16s} pred={got[0]:10s} fib_share="
                  f"{'-' if got[2] is None else f'{got[2]*100:.2f}%'}  {bn[:44]}")
    print(f"\n   ground-truth runs: {len(truth)}   detected as blood: {agree+wrong}   "
          f"missed: {missed}   plasma/serum mismatched: {wrong}")

    if not a.apply:
        print("\nDRY RUN — re-run with --apply.")
        conn.rollback(); conn.close(); return

    cur.execute(DDL); conn.commit()
    import psycopg2.extras
    psycopg2.extras.execute_values(cur, """
        INSERT INTO delimp_blood_prediction
          (raw_basename, predicted_matrix, status, blood_fraction, fibrinogen_share,
           n_blood_proteins, total_intensity)
        VALUES %s ON CONFLICT (raw_basename) DO UPDATE SET
          predicted_matrix=EXCLUDED.predicted_matrix, status=EXCLUDED.status,
          blood_fraction=EXCLUDED.blood_fraction, fibrinogen_share=EXCLUDED.fibrinogen_share,
          n_blood_proteins=EXCLUDED.n_blood_proteins, total_intensity=EXCLUDED.total_intensity,
          scored_at=now()""", out, page_size=500)
    conn.commit()
    print(f"\nstored {len(out):,} rows")

    import versions as V
    V.record_run(cur, "blood_prediction", "1.0.0", notes=f"{len(called)} blood-derived")
    conn.commit(); conn.close()
    print("DONE")


if __name__ == "__main__":
    main()
