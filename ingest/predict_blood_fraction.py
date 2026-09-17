"""predict_blood_fraction.py — detect blood-derived runs and separate whole blood / plasma / serum.

WHY A SEPARATE DETECTOR. The Yue et al. 2026 atlas has 64 tissue columns and NONE of them is plasma,
serum, or blood -- verified in both Supplementary Table 5 sheets. That is not an oversight: "tissue
enriched" means >=4x higher in one tissue than all others, and plasma proteins are LIVER-SECRETED,
so albumin and friends are enriched in liver, not plasma. The paper profiled whole blood and
platelet-rich plasma; they simply yield no tissue-enriched proteins by that definition.

So plasma can never win a tissue panel, and every plasma run in FRAN correctly abstains there.

A DIFFERENT SIGNAL ENTIRELY. Tissue identity is about WHICH proteins are present. Blood identity is
about DOMINANCE: albumin alone is roughly half of plasma protein, and the classical ~26 abundant
plasma proteins are the overwhelming majority of the signal. A tissue lysate with a little blood
contamination CONTAINS albumin; plasma is MADE of it. Presence/absence cannot separate those --
intensity share can.

    blood_fraction(run) = sum(intensity of plasma-core proteins) / sum(intensity of all proteins)

--- WHY FIBRINOGEN CANNOT SPLIT PLASMA FROM SERUM (measured 2026-09-03, do not retry) -------------
The textbook rule -- serum is plasma after clotting, so FGA/FGB/FGG are consumed -- predicts that
fibrinogen share separates them. IT DOES NOT, and the reason is now measured rather than guessed:

  FIBRINOGEN SHARE TRACKS ALBUMIN, NOT CLOTTING. Across the known-plasma runs it is a near-perfect
  inverse of albumin share -- alb 88% -> fib/blood 0.07%; alb 21% -> fib/blood 29.6%. Undepleted
  plasma is 78-88% albumin, which dilutes fibrinogen to ~0.1%; EV-enriched plasma drops albumin to
  3.7% and fibrinogen rises to 61%. Same matrix, 400x apart, ordered entirely by sample prep.

  The clincher: the eleven runs whose FILENAME says serum span fib/blood 1.8% .. 27.7%. Serum should
  have almost none. Fibrinogen share measures depletion and EV enrichment. It is stored as evidence
  and never used as a label.

--- WHAT DOES SPLIT THEM: PLATELET RELEASE --------------------------------------------------------
Clotting ACTIVATES PLATELETS, which empty their alpha-granules into serum. Fibrinogen asks "what is
missing?", a weak question when prep also removes things; platelet release asks "what was added?",
which depletion cannot fake. Calibrated against the eleven serum-named runs:

    serum   (n=11, filename says serum) : platelet share 1.18% .. 4.39%
    plasma  (n=36, filename says plasma): platelet share 0.00% .. 0.247%     -> 4.8x gap, no overlap

  THE PANEL MUST BE PLATELET-RESTRICTED. A first version included SPARC/THBS1/MYH9/TLN1/ACTN1/
  TMSB4X/PECAM1, which are ECM and cytoskeletal proteins abundant in ANY cell prep. That made a
  GFP-transfected cell secretome (Ex120523_Fiero, GFP at 1.27% of signal) rank as the most
  serum-like run in the corpus. Only alpha-granule / megakaryocyte-lineage markers are used here.

  THE OVERLAP IS REAL AND IS WHY THERE IS AN ABSTAIN BAND. Text-confirmed plasma reaches 1.93%
  (a Gallegos run). That is not a method failure -- incompletely centrifuged plasma retains
  platelets. So 0.5%..2.0% is called `blood_derived` and NOT resolved. At >=2.0% there are zero
  known-plasma false positives; the cost is recall (5 of 11 known serum), which is the right
  trade for a label that would otherwise be asserted onto hundreds of runs.

--- TWO TRAPS THAT COST REAL TIME, RECORDED SO THEY ARE NOT REDISCOVERED -------------------------
  * FBS IS NOT BLOOD. Cell-culture media scores >30% "blood" because fetal bovine serum supplies
    ALB/AHSG/TF, which map onto human gene symbols (Ex010321_UoTorMedia, 23May2024_Coll_Blank_Dev,
    29May2024_MCF10AEV, Ex120523_Fiero). Two markers that DO NOT work, each tried and measured:
      - AFP is not an FBS marker. Alpha-fetoprotein is a genuine human plasma protein, elevated in
        liver disease and pregnancy; FL180724_Wang plasma carries 0.36-0.61% AFP. Using it excluded
        18 text-confirmed plasma runs at 80-90% blood fraction.
      - Intracellular share does not separate. Culture spans 0.4-4.1% while EV-enriched plasma
        reaches 1.1% and a Gallegos plasma run hits 3.6%. It is stored as evidence, never used to
        classify.
    WHAT WORKS is a conjunction, because FBS is FETAL: fetuin-rich relative to albumin, and almost
    devoid of immunoglobulin (a fetal calf has mounted no immune response).

        culture  <=>  AHSG/ALB >= 0.10  AND  IgG/ALB < 0.05

    Each half covers the other's failure. High fetuin alone would condemn serum (enhSerum sits at
    0.216 because its albumin is only ~2%); its IgG/ALB of 0.51 rescues it. Low IgG alone would
    condemn the Wang and DWang plasma cohorts, which are immunoglobulin-poor; their fetuin ratio of
    0.0001 rescues them. Measured 11/11 correct across four culture and seven blood cohorts.
  * `\\bserum\\b` MISSES `enhSerum`. There is no word boundary inside camelCase, so a word-boundary
    regex reports zero serum in a corpus that has eleven. Matrix words are matched as SUBSTRINGS
    against the basename, which is where this corpus actually records the matrix -- CoreOmics text
    names a blood matrix for only 23 runs and says "serum" for none of them.

Nothing here is asserted onto delimp_sample_metadata; this writes its own predicted_* table.

    python ingest/predict_blood_fraction.py            # dry run + validation against ground truth
    python ingest/predict_blood_fraction.py --apply
"""
import argparse
import functools
import os
import re
import sys
from collections import Counter, defaultdict

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

# Alpha-granule / megakaryocyte-lineage ONLY -- see the docstring on why SPARC/THBS1/MYH9/TLN1/
# ACTN1/TMSB4X/PECAM1 are excluded. Every one of these is platelet-restricted.
PLATELET = ["PF4", "PF4V1", "PPBP", "ITGA2B", "GP1BA", "GP1BB", "GP9", "GP6", "SELP", "CLEC1B",
            "MMRN1", "TREML1", "NRGN", "TUBB1", "PLEK"]

# Erythrocyte cytosol + membrane. Whole blood is RBC-dominated; hemoglobin alone is ~97% of
# erythrocyte protein, so this needs no calibration -- a run at 20% is unambiguously whole blood.
ERYTHROCYTE = ["HBB", "HBA1", "HBA2", "HBD", "CA1", "CA2", "PRDX2", "CAT", "SLC4A1", "SPTA1",
               "SPTB", "ANK1", "BLVRB", "BPGM", "ALAS2", "AHSP", "EPB42", "GYPA"]

# Intracellular / cytoskeletal. STORED AS EVIDENCE ONLY -- it does not separate culture from
# blood (see the docstring), so nothing classifies on it.
INTRACELLULAR = ["VIM", "ACTB", "GAPDH", "ENO1", "PKM", "TUBB", "HSPA8", "LMNA", "PLEC", "FLNA",
                 "AHNAK", "ANXA2", "HSPA5"]
# The FBS conjunction. Fetuin is the fetal-bovine signal; immunoglobulin is the human-blood signal
# that vetoes it. Both are measured against albumin so the test is invariant to loading and depth.
FETUIN = ["AHSG"]
IMMUNOGLOBULIN = ["IGHG1", "IGHG2", "IGHG3", "IGHG4", "IGHA1", "IGHA2", "IGHM",
                  "IGKC", "IGLC1", "IGLC2", "IGLC3"]
ALBUMIN = ["ALB"]

PANELS = {"core": PLASMA_CORE, "fib": FIBRINOGEN, "plt": PLATELET, "rbc": ERYTHROCYTE,
          "cell": INTRACELLULAR, "ahsg": FETUIN, "ig": IMMUNOGLOBULIN, "alb": ALBUMIN}
ALL_GENES = sorted({g for v in PANELS.values() for g in v})

# Thresholds. A tissue lysate carries some blood; these separate "contains blood" from "is blood",
# and then which blood. Every one is calibrated in the docstring above.
MIN_BLOOD_FRACTION = 0.30    # below this the run is not blood-derived
WHOLE_BLOOD_RBC    = 0.20    # erythrocyte share; 11 runs corpus-wide, self-evident at this level
SERUM_PLATELET     = 0.020   # zero known-plasma false positives at this cut
PLASMA_PLATELET    = 0.005   # below this, platelet release is absent -> plasma
CULTURE_FETUIN_ALB = 0.10    # AHSG/ALB at or above this is fetal-bovine-like ...
CULTURE_IG_ALB     = 0.05    # ... unless immunoglobulin says human blood. Both must hold.
MIN_BLOOD_PROTEINS = 5

DDL = """
CREATE TABLE IF NOT EXISTS delimp_blood_prediction (
    raw_basename      text PRIMARY KEY,
    predicted_matrix  text,          -- 'whole_blood' | 'serum' | 'plasma' | 'blood_derived' | NULL
    status            text NOT NULL,
    blood_fraction    double precision,
    fibrinogen_share  double precision,
    n_blood_proteins  integer,
    total_intensity   double precision,
    scored_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bloodpred_matrix ON delimp_blood_prediction (predicted_matrix)
    WHERE predicted_matrix IS NOT NULL;
-- Added 2026-09-03 with the whole-blood/plasma/serum split. IF NOT EXISTS so re-running against an
-- existing table is safe; the four axes are stored because the CALL is a threshold on them and a
-- reader needs the evidence to disagree with it.
ALTER TABLE delimp_blood_prediction ADD COLUMN IF NOT EXISTS platelet_share      double precision;
ALTER TABLE delimp_blood_prediction ADD COLUMN IF NOT EXISTS erythrocyte_share   double precision;
ALTER TABLE delimp_blood_prediction ADD COLUMN IF NOT EXISTS intracellular_share double precision;
ALTER TABLE delimp_blood_prediction ADD COLUMN IF NOT EXISTS albumin_share       double precision;
ALTER TABLE delimp_blood_prediction ADD COLUMN IF NOT EXISTS fetuin_albumin      double precision;
ALTER TABLE delimp_blood_prediction ADD COLUMN IF NOT EXISTS ig_albumin          double precision;
"""

# WHY TWO STATEMENTS. The first version tested every protein row with
# `EXISTS (SELECT 1 FROM unnest(string_to_array(gene,';')) ...)`, i.e. it unnested and scanned the
# gene string of every row of delimp_proteins for every marker. That ran past 30 minutes without
# returning. Marker membership is a property of the PROTEIN GROUP, not of the row, so resolve the
# groups once (a few thousand, with a regex prefilter that the planner can restrict on) and then
# join on plain equality. Same answer, seconds instead of tens of minutes.
RESOLVE_SQL = """
SELECT DISTINCT protein_group, upper(gene) AS gu
FROM delimp_proteins
WHERE gene IS NOT NULL
  AND upper(gene) ~ ('(^|;)(' || array_to_string(%(all)s::text[], '|') || ')(;|$)')
"""

SQL = """
WITH human_runs AS (
  SELECT DISTINCT rf.raw_basename AS bn, rf.raw_path
  FROM raw_files rf JOIN delimp_sample_metadata m ON m.raw_path = rf.raw_path
  WHERE m.organism_taxon_id = %(human)s
),
pg AS (
  SELECT hr.bn, p.protein_group,
         max(coalesce(p.normalized_intensity, p.intensity)) AS inten
  FROM delimp_proteins p JOIN human_runs hr ON hr.raw_path = p.raw_path
  WHERE coalesce(p.normalized_intensity, p.intensity) IS NOT NULL
  GROUP BY 1, 2
)
SELECT bn,
       sum(inten)                                              AS total_int,
       sum(inten) FILTER (WHERE protein_group = ANY(%(core)s)) AS blood_int,
       sum(inten) FILTER (WHERE protein_group = ANY(%(fib)s))  AS fib_int,
       sum(inten) FILTER (WHERE protein_group = ANY(%(plt)s))  AS plt_int,
       sum(inten) FILTER (WHERE protein_group = ANY(%(rbc)s))  AS rbc_int,
       sum(inten) FILTER (WHERE protein_group = ANY(%(cell)s)) AS cell_int,
       sum(inten) FILTER (WHERE protein_group = ANY(%(ahsg)s)) AS ahsg_int,
       sum(inten) FILTER (WHERE protein_group = ANY(%(ig)s))   AS ig_int,
       sum(inten) FILTER (WHERE protein_group = ANY(%(alb)s))  AS alb_int,
       count(*)   FILTER (WHERE protein_group = ANY(%(core)s)) AS n_blood
FROM pg GROUP BY 1 HAVING sum(inten) > 0
"""

# Matrix words as they are actually written in this corpus. SUBSTRINGS, not \b-delimited words --
# `enhSerum` has no word boundary before "Serum" and a \b regex reports zero serum runs.
MATRIX_WORDS = {
    "serum": r"serum",
    "plasma": r"plasma",
    "whole_blood": r"whole.?blood|\bDBS\b|dried.?blood",
}


def _conn():
    import psycopg2
    from refresh_leaderboards import _token
    return psycopg2.connect(
        host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
        dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
        user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
        password=_token(), sslmode="require", connect_timeout=30,
        options="-c statement_timeout=1800000")


def classify(frac, n_blood, plt, rbc, fetuin_alb, ig_alb):
    """Which blood matrix, or None with a reason.

    Order matters. The blood-fraction bar comes first: a tissue lysate has so little albumin that
    its fetuin/albumin ratio is meaningless, and testing culture first filed ordinary lysates as
    "culture". FBS-fed media clears the blood bar on bovine albumin, so it still reaches the
    culture test. Whole blood is tested before the plasma/serum split because an RBC-dominated run
    is neither.

    The 0.5%..2.0% platelet band deliberately returns 'blood_derived' WITHOUT resolving it: real
    plasma that was not fully centrifuged lands there alongside real serum, and asserting either
    label would be the same mistake as the fibrinogen split this replaced.
    """
    if frac is None:
        return None, "abstained_no_intensity"
    if frac < MIN_BLOOD_FRACTION:
        return None, "not_blood_derived"
    if fetuin_alb >= CULTURE_FETUIN_ALB and ig_alb < CULTURE_IG_ALB:
        return None, "culture_medium_not_blood"
    if n_blood < MIN_BLOOD_PROTEINS:
        return None, "abstained_too_few_blood_proteins"
    if rbc >= WHOLE_BLOOD_RBC:
        return "whole_blood", "emitted"
    if plt >= SERUM_PLATELET:
        return "serum", "emitted"
    if plt < PLASMA_PLATELET:
        return "plasma", "emitted"
    return "blood_derived", "unresolved_platelet_rich"


def ground_truth(cur):
    """What a human actually recorded, from the BASENAME and from CoreOmics text.

    The basename is the primary source here: it carries eleven serum runs and thirty-six plasma
    runs, while CoreOmics text names a blood matrix for only 23 runs and never says serum.
    """
    truth = {}
    cur.execute("""SELECT DISTINCT rf.raw_basename FROM raw_files rf
                   JOIN delimp_sample_metadata m ON m.raw_path = rf.raw_path
                   WHERE m.organism_taxon_id = %s""", (HUMAN,))
    for (bn,) in cur.fetchall():
        for matrix, pat in MATRIX_WORDS.items():
            if re.search(pat, bn, re.I):
                truth[bn] = matrix
                break
    cur.execute("""
      SELECT DISTINCT rf.raw_basename,
             (coalesce(sub.description,'') || ' ' || coalesce(sub.other_info,'') || ' '
              || coalesce(s.sample_name,'') || ' ' || coalesce(s.condition_name,'')) AS txt
      FROM raw_files rf
      JOIN delimp_sample_metadata m ON m.raw_path = rf.raw_path
      JOIN search_raw_files srf ON srf.raw_path = rf.raw_path
      JOIN delimp_search_provenance sp ON sp.search_id = srf.search_id
      JOIN coreomics_submissions_cache sub ON sub.submission_id = sp.coreomics_submission_id
      LEFT JOIN coreomics_samples_cache s ON s.submission_id = sub.submission_id
      WHERE m.organism_taxon_id = %s""", (HUMAN,))
    for bn, txt in cur.fetchall():
        if bn in truth:
            continue                      # the filename is the more specific source; keep it
        for matrix, pat in MATRIX_WORDS.items():
            if re.search(pat, txt, re.I):
                truth[bn] = matrix
                break
    return truth


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    conn = _conn(); conn.autocommit = False
    cur = conn.cursor()
    cur.execute("SET LOCAL lock_timeout = '10s'")
    cur.execute("SET work_mem = '256MB'")

    print("resolving marker protein groups (once, not per row)...")
    cur.execute(RESOLVE_SQL, {"all": ALL_GENES})
    gene_groups = defaultdict(set)
    wanted = set(ALL_GENES)
    for grp, gu in cur.fetchall():
        for g in gu.split(";"):
            g = g.strip()
            if g in wanted:
                gene_groups[g].add(grp)
    resolved = {k: sorted({x for g in v for x in gene_groups.get(g, ())}) for k, v in PANELS.items()}
    for k, v in resolved.items():
        print(f"  {k:5s} {len(v):>5} groups")

    print("scoring every human acquisition on all axes (one pass)...")
    params = {"human": HUMAN}
    params.update(resolved)
    cur.execute(SQL, params)
    rows = cur.fetchall()
    print(f"{len(rows):,} runs with usable intensity")

    out = []
    for bn, total, blood, fib, plt, rbc, cell, ahsg, ig, alb, n_blood in rows:
        total = float(total or 0)
        if total <= 0:
            continue
        blood = float(blood or 0)
        frac = blood / total
        shares = {k: float(v or 0) / total for k, v in
                  (("plt", plt), ("rbc", rbc), ("cell", cell), ("alb", alb))}
        alb_i = float(alb or 0)
        # Ratios against albumin, not against total: both sides of the FBS test scale with albumin,
        # which is what makes the test independent of loading and of depletion depth. No albumin
        # means no test -- 0.0 keeps such a run out of the culture branch rather than into it.
        fetuin_alb = (float(ahsg or 0) / alb_i) if alb_i > 0 else 0.0
        ig_alb = (float(ig or 0) / alb_i) if alb_i > 0 else 1.0
        fib_share = (float(fib or 0) / blood) if blood > 0 else None
        matrix, st = classify(frac, n_blood or 0, shares["plt"], shares["rbc"], fetuin_alb, ig_alb)
        out.append((bn, matrix, st, frac, fib_share, n_blood, total,
                    shares["plt"], shares["rbc"], shares["cell"], shares["alb"],
                    fetuin_alb, ig_alb))

    print("\nstatus:")
    for k, v in Counter(r[2] for r in out).most_common():
        print(f"   {k:34s} {v:>6,}  ({100*v/len(out):.1f}%)")
    called = [r for r in out if r[1]]
    print(f"\n{len(called):,} blood-derived:")
    for k, v in Counter(r[1] for r in called).most_common():
        fr = sorted(r[7] for r in called if r[1] == k)
        print(f"   {k:28s} {v:>5}  (median platelet share {fr[len(fr)//2]*100:.3f}%)")

    # ---- VALIDATE against what a human recorded, before writing anything ----------------------
    print("\n=== validation vs recorded matrix (basename + CoreOmics text) ===")
    truth = ground_truth(cur)
    pred = {r[0]: r for r in out}
    BLOOD_LABELS = ("whole_blood", "serum", "plasma", "blood_derived")
    agree = wrong = missed = 0
    per = Counter()
    for bn, want in sorted(truth.items()):
        got = pred.get(bn)
        matrix = got[1] if got else None
        if matrix not in BLOOD_LABELS:
            missed += 1
            per[f"{want}:not_detected_as_blood"] += 1
            print(f"   MISSED   text={want:12s} pred={matrix}  "
                  f"frac={'-' if not got else f'{got[3]*100:.0f}%'}  {bn[:44]}")
        elif matrix == want:
            agree += 1
            per[f"{want}:exact"] += 1
        elif matrix == "blood_derived":
            per[f"{want}:blood_but_unresolved"] += 1
        else:
            wrong += 1
            per[f"{want}:MISLABELLED_{matrix}"] += 1
            print(f"   MISMATCH text={want:12s} pred={matrix:13s} "
                  f"plt={got[7]*100:.3f}%  {bn[:44]}")
    print(f"\n   ground-truth runs: {len(truth)}   exact: {agree}   "
          f"mislabelled: {wrong}   not detected as blood: {missed}")
    for k, v in sorted(per.items()):
        print(f"     {k:44s} {v:>4}")

    if not a.apply:
        print("\nDRY RUN — re-run with --apply.")
        conn.rollback(); conn.close(); return

    cur.execute(DDL); conn.commit()
    import psycopg2.extras
    psycopg2.extras.execute_values(cur, """
        INSERT INTO delimp_blood_prediction
          (raw_basename, predicted_matrix, status, blood_fraction, fibrinogen_share,
           n_blood_proteins, total_intensity, platelet_share, erythrocyte_share,
           intracellular_share, albumin_share, fetuin_albumin, ig_albumin)
        VALUES %s ON CONFLICT (raw_basename) DO UPDATE SET
          predicted_matrix=EXCLUDED.predicted_matrix, status=EXCLUDED.status,
          blood_fraction=EXCLUDED.blood_fraction, fibrinogen_share=EXCLUDED.fibrinogen_share,
          n_blood_proteins=EXCLUDED.n_blood_proteins, total_intensity=EXCLUDED.total_intensity,
          platelet_share=EXCLUDED.platelet_share, erythrocyte_share=EXCLUDED.erythrocyte_share,
          intracellular_share=EXCLUDED.intracellular_share, albumin_share=EXCLUDED.albumin_share,
          fetuin_albumin=EXCLUDED.fetuin_albumin, ig_albumin=EXCLUDED.ig_albumin,
          scored_at=now()""", out, page_size=500)
    conn.commit()
    print(f"\nstored {len(out):,} rows")

    import versions as V
    V.record_run(cur, "blood_prediction", "2.0.0",
                 notes=f"{len(called)} blood-derived; whole_blood/serum/plasma split")
    conn.commit(); conn.close()
    print("DONE")


if __name__ == "__main__":
    main()
