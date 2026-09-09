"""mzTab reading across BOTH casanovo column generations.

Casanovo 5.2.0 renamed the mzTab columns to the spec. A reader that knows only the 5.1 names
returns ZERO peptides from a 5.2 file WITHOUT erroring -- indistinguishable from a sample that
genuinely had none. That silent zero is the failure this test exists to prevent.

Run:  python tests/test_denovo_mztab.py     (no pytest needed)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingest"))
from denovo_mztab import read_mztab  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


V51 = ("MTD\tsoftware[1]\t[MS, MS:1003281, Casanovo, 5.1.2]\n"
       "PSH\tsequence\tPSM_ID\tsearch_engine_score[1]\tcharge\texp_mass_to_charge\t"
       "spectra_ref\topt_ms_run[1]_aa_scores\n"
       "PSM\tPEPTIDEK\t1\t0.9\t2\t500.5\tms_run[1]:controllerType=0 controllerNumber=1 scan=42\t"
       "0.9,0.9,0.9,0.9,0.9,0.9,0.9,0.9\n")
V52 = ("MTD\tsoftware[1]\t[MS, MS:1003281, Casanovo, 5.2.1]\n"
       "PSH\topt_global_cv_MS:1003169_proforma_peptidoform_sequence\tPSM_ID\t"
       "search_engine_score[1]\tcharge\texp_mass_to_charge\tspectra_ref\topt_global_aa_scores\n"
       "PSM\tPEPTIDEK\t1\t0.9\t2\t500.5\tms_run[1]:controllerType=0 controllerNumber=1 scan=42\t"
       "0.9,0.9,0.9,0.9,0.9,0.9,0.9,0.9\n")


def write(text):
    fh = tempfile.NamedTemporaryFile("w", suffix=".mztab", delete=False)
    fh.write(text)
    fh.close()
    return fh.name


for label, text, ver in (("5.1.x", V51, "5.1.2"), ("5.2.x", V52, "5.2.1")):
    d = read_mztab(write(text))
    check(f"{label}: one PSM", len(d["psms"]) == 1, f"got {len(d['psms'])}")
    if d["psms"]:
        p = d["psms"][0]
        check(f"{label}: sequence", p["stripped_seq"] == "PEPTIDEK")
        check(f"{label}: seq_il", p["seq_il"] == "PEPTLDEK")
        check(f"{label}: scan from spectra_ref", p["scan"] == 42)
        check(f"{label}: aa_scores parsed", len(p["aa_scores"]) == 8)
    check(f"{label}: engine version", d["engine_version"] == ver)

MOD = V52.replace("PEPTIDEK", "PEPTN+0.984DEK")
d = read_mztab(write(MOD))
check("mods stripped from the key", d["psms"][0]["stripped_seq"] == "PEPTNDEK")
check("raw sequence preserved", "+0.984" in d["psms"][0]["sequence"])

BAD = V52.replace("opt_global_cv_MS:1003169_proforma_peptidoform_sequence", "mystery")
try:
    read_mztab(write(BAD))
    check("unknown column raises", False, "returned instead of raising")
except ValueError:
    check("unknown column raises", True)

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
