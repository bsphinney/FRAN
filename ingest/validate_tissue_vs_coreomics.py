"""validate_tissue_vs_coreomics.py — check predicted tissue against what the submitter actually wrote.

The tissue predictions in delimp_tissue_prediction are inferred from marker-protein enrichment and
have never been checked against ground truth. CoreOmics holds the closest thing FRAN has: free text a
human wrote when submitting the samples -- `coreomics_submissions_cache.description` / `other_info`
and per-sample `sample_name` / `condition_name`. Examples: "150mg of frozen liver samples",
"20 human heart samples", "16 pancreata samples from mice".

WHAT THIS IS AND IS NOT. It is a CONCORDANCE check on the subset where both exist, not an accuracy
measurement. Three limits, all of which inflate apparent disagreement rather than agreement, so a
high agreement rate is trustworthy and a low one needs reading:

  * COVERAGE. Only 499 of 5,507 human acquisitions (9.1%) reach a CoreOmics submission at all.
  * GRAIN. description/other_info are per SUBMISSION, not per sample. A submission of 20 samples
    across several tissues yields one text for every run in it, so a run can be "wrong" against text
    that describes a different sample in the same batch. sample_name/condition_name are per sample
    and are matched separately for that reason.
  * SPECIES DRIFT. Some submissions linked to runs labelled human describe mouse or bat material
    ("2 male mouse liver samples"). Either the organism label or the linkage is wrong for those.
    They are reported separately rather than silently counted as agreement.

Text is EVIDENCE, not truth: absence of a tissue word means the submitter did not write one, never
that the prediction is wrong. Those rows are `no_tissue_in_text`, never `disagree`.

    python ingest/validate_tissue_vs_coreomics.py
    python ingest/validate_tissue_vs_coreomics.py --show-disagreements 25
"""
import argparse
import functools
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
print = functools.partial(print, flush=True)   # noqa: A001

# panel tissue name -> regex matching how a submitter would WRITE it. Deliberately generous on
# synonyms and narrow on ambiguity: 'Bone' must not match 'bone marrow', 'Eye-iris' needs 'iris'.
TISSUE_WORDS = {
    "Liver": r"\bliver|hepat(?:ic|ocyte)",
    "Brain": r"\bbrain|cortex|cortical|hippocamp|cerebell|neuron|striatum|\bPFC\b|prefrontal",
    "Kidney": r"\bkidney|renal|nephr",
    "Heart": r"\bheart|cardiac|myocard|ventric",
    "Lung": r"\blung|pulmonar|alveolar|bronch",
    "Pancreas": r"pancrea",
    "Skeletal muscle": r"\bmuscle|myotube|myoblast|gastrocnem|quadricep|skeletal muscle",
    "Testis": r"\btestis|testes|testicular|sperm|semen",
    "Prostate": r"prostat",
    "Ovary": r"\bovar",
    "Spleen": r"\bspleen|splenic",
    "Stomach": r"\bstomach|gastric(?! cancer cell line)",
    "Small intestine": r"small intestine|duoden|jejun|ileum",
    "Colon": r"\bcolon|colorect",
    "Thyroid": r"thyroid",
    "Adrenal gland": r"adrenal",
    "Bone marrow": r"bone marrow|\bBM\b",
    "Bone": r"\bbone\b(?!\s*marrow)|osteo",
    "Skin": r"\bskin|dermal|epiderm|keratinocyte",
    "Plasma": r"\bplasma\b",
    "Serum": r"\bserum\b",
    "Whole blood": r"whole blood|\bblood\b",
    "Saliva": r"saliva",
    "Tear": r"\btear[s]?\b",
    "Urine": r"\burine|urinary",
    "Placenta": r"placent",
    "Thymus": r"thymus|thymic",
    "Lymph node": r"lymph node",
    "Tonsil": r"tonsil",
    "Spinal cord": r"spinal cord",
    "Eye-crystalline lens": r"\blens\b|crystallin",
    "Eye-corneal": r"cornea",
    "Eye-iris": r"\biris\b",
    "Eye-sclera": r"sclera",
    "Cartilage": r"cartilage|chondro",
    "Nerve": r"\bnerve|sciatic",
    "Breast": r"\bbreast|mammary",
    "Uterus": r"uter|endometri",
    "Bladder": r"bladder",
    "Esophagus": r"esophag|oesophag",
    "Fat": r"\bfat\b|adipos|adipocyte",
}
NONHUMAN = r"\bmouse|mice|murine|\brat\b|\brats\b|bovine|\bcow\b|porcine|\bpig\b|canine|\bdog\b|zebrafish|yeast|\bbat[s]?\b|monkey|macaque|chicken|drosophila|C\. ?elegans"

SQL = """
SELECT tp.raw_basename, tp.predicted_tissue, tp.enrichment, tp.margin, tp.n_markers_hit,
       max(coalesce(sub.description,'')) AS descr,
       max(coalesce(sub.other_info,''))  AS other,
       string_agg(DISTINCT coalesce(s.sample_name,''), ' | ')    AS sample_names,
       string_agg(DISTINCT coalesce(s.condition_name,''), ' | ') AS cond_names
FROM delimp_tissue_prediction tp
JOIN raw_files rf                 ON rf.raw_basename = tp.raw_basename
JOIN search_raw_files srf         ON srf.raw_path = rf.raw_path
JOIN delimp_search_provenance sp  ON sp.search_id = srf.search_id
JOIN coreomics_submissions_cache sub ON sub.submission_id = sp.coreomics_submission_id
LEFT JOIN coreomics_samples_cache s  ON s.submission_id = sub.submission_id
WHERE tp.status = 'emitted'
GROUP BY 1,2,3,4,5
"""


def _conn():
    import psycopg2
    from refresh_leaderboards import _token
    return psycopg2.connect(
        host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
        dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
        user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
        password=_token(), sslmode="require", connect_timeout=30,
        options="-c statement_timeout=600000")


def tissues_in(text):
    t = (text or "")
    return {name for name, pat in TISSUE_WORDS.items() if re.search(pat, t, re.I)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show-disagreements", type=int, default=12)
    a = ap.parse_args()
    conn = _conn(); cur = conn.cursor()
    cur.execute(SQL)
    rows = cur.fetchall()
    print(f"{len(rows):,} emitted predictions have a CoreOmics submission to check against\n")
    if not rows:
        print("nothing to validate — has predict_tissue.py --apply been run?")
        conn.close(); return

    agree = disagree = no_text = 0
    nonhuman_text = 0
    dis_examples, agr_examples = [], []
    by_tissue = {}
    for bn, pred, enr, margin, nhit, descr, other, snames, cnames in rows:
        blob = " ".join(x for x in (descr, other, snames, cnames) if x)
        found = tissues_in(blob)
        nh = bool(re.search(NONHUMAN, blob, re.I))
        st = by_tissue.setdefault(pred, {"agree": 0, "disagree": 0, "no_text": 0})
        if not found:
            no_text += 1; st["no_text"] += 1
        elif pred in found:
            agree += 1; st["agree"] += 1
            if len(agr_examples) < 10:
                agr_examples.append((bn, pred, enr, blob[:88]))
        else:
            disagree += 1; st["disagree"] += 1
            if nh:
                nonhuman_text += 1
            dis_examples.append((bn, pred, enr, margin, sorted(found), nh, blob[:88]))

    checked = agree + disagree
    print(f"  agree              {agree:>5}")
    print(f"  disagree           {disagree:>5}   ({nonhuman_text} of these mention a NON-HUMAN species)")
    print(f"  no tissue in text  {no_text:>5}   (submitter wrote none — NOT evidence against)")
    if checked:
        print(f"\n  CONCORDANCE on the {checked} rows where text names a tissue: "
              f"{100*agree/checked:.1f}%")

    print("\n=== per predicted tissue ===")
    for t, d in sorted(by_tissue.items(), key=lambda x: -(x[1]['agree'] + x[1]['disagree'])):
        ch = d["agree"] + d["disagree"]
        if not ch:
            continue
        print(f"   {t:24s} agree {d['agree']:>4} / checked {ch:<4} "
              f"({100*d['agree']/ch:>5.1f}%)   no-text {d['no_text']}")

    if agr_examples:
        print("\n=== agreements (spot-check the text really says it) ===")
        for bn, p, e, txt in agr_examples[:6]:
            print(f"   {p:18s} enr={e:>5.1f}  {txt}")
    if dis_examples:
        print(f"\n=== disagreements (first {a.show_disagreements}) ===")
        for bn, p, e, m, found, nh, txt in dis_examples[:a.show_disagreements]:
            flag = " [NON-HUMAN TEXT]" if nh else ""
            print(f"   predicted {p:20s} text says {found}{flag}")
            print(f"      enr={e:.1f} margin={m if m is None else round(m,2)}  {txt}")
    conn.close()


if __name__ == "__main__":
    main()
