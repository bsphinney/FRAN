"""The ingestor assembles rows correctly and is DRY-RUN by default.

Run:  python tests/test_denovo_ingest.py     (no pytest needed)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingest"))
from denovo_ingest import build_rows  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


MZTAB = ("MTD\tsoftware[1]\t[MS, MS:1003281, Casanovo, 5.2.1]\n"
         "PSH\topt_global_cv_MS:1003169_proforma_peptidoform_sequence\tPSM_ID\t"
         "search_engine_score[1]\tcharge\texp_mass_to_charge\tspectra_ref\topt_global_aa_scores\n"
         "PSM\tPEPTIDEK\t1\t0.9\t2\t500.5\tms_run[1]:controllerType=0 controllerNumber=1 scan=42\t0.9\n"
         "PSM\tWWWWWWWW\t2\t0.8\t2\t600.5\tms_run[1]:controllerType=0 controllerNumber=1 scan=43\t0.8\n")
fh = tempfile.NamedTemporaryFile("w", suffix=".mztab", delete=False,
                                 prefix="QEPlus2_03162018_36_ZG171_")
fh.write(MZTAB)
fh.close()

corpus = {"PEPTLDEK": ["PEPTIDEK", "PEPTLDEK"]}
out = build_rows(fh.name, cohort="test", corpus=corpus)

check("run row present", out["run"]["run_name"].startswith("QEPlus2_03162018_36_ZG171"))
check("donor parsed", out["run"]["donor_id"] == "ZG171")
check("role is donor", out["run"]["sample_role"] == "donor")
check("engine version recorded", out["run"]["engine_version"] == "5.2.1")
check("engine recorded", out["run"]["denovo_engine"] == "casanovo")
check("two peptides", len(out["peptides"]) == 2)
check("two psms", len(out["psms"]) == 2)

# PEPTIDEK matches two corpus spellings -> two match rows; WWWWWWWW matches none.
check("match rows = 2", len(out["matches"]) == 2, f"got {len(out['matches'])}")
check("n_candidates = 2", all(m["n_candidates"] == 2 for m in out["matches"]))

cls = {p["stripped_seq"]: p["corpus_class"] for p in out["peptides"]}
check("matched peptide is conserved", cls["PEPTIDEK"] == "conserved")
check("unmatched peptide is novel", cls["WWWWWWWW"] == "novel")

check("run_id deterministic",
      build_rows(fh.name, cohort="test", corpus=corpus)["run"]["run_id"] == out["run"]["run_id"])

# match_fdr must be PRESENT as a field -- a candidate count without its null overstates ~2x.
check("match_fdr field present", "match_fdr" in out["run"])
# ...but None on a tiny match count, rather than a precise-looking number built from noise.
check("match_fdr is None below the floor", out["run"]["match_fdr"] is None,
      str(out["run"]["match_fdr"]))

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
