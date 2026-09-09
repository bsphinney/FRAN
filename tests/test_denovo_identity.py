"""Donor identity from a run name. Every case here is a real filename that broke an attempt.

Run:  python tests/test_denovo_identity.py     (no pytest needed)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingest"))
from denovo_identity import parse_identity  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def donor(r):
    return parse_identity(r)["donor_id"]


def role(r):
    return parse_identity(r)["sample_role"]


check("grammar 1 plain ZG", donor("QEPlus2_03162018_36_ZG171") == "ZG171")
check("grammar 2 ZG_ underscore", donor("QEPlus2_03262019_54_ZG_313") == "ZG313")
check("grammar 3 no position field", donor("QEPlus2_08162019_ZG377") == "ZG377")
check("grammar 4 the 2G typo", donor("QEPlus2_02162017_10_2G26") == "ZG26")
check("2G after underscore (\\b is unsafe here)", donor("QEplus2_02162017_10_2G11") == "ZG11")
check("case variants", donor("QEPlus2_01012018_10_Zg99") == "ZG99")
check("RDIL suffix", donor("QEPlus2_03162018_99_ZG171_RDIL") == "ZG171")
check("timestamp suffix", donor("QEplus2_08152017_86_ZG125_170821145253") == "ZG125")

check("BSA is a standard", role("QEPlus2_02162017_52_BSA") == "standard")
check("Pooled is a standard", role("QEPlus2_02162018_78_Pooled100Femto") == "standard")
check("ZG is a donor", role("QEPlus2_03162018_36_ZG171") == "donor")
check("RR is unknown", role("QEplus2_09172020_28_RR1") == "unknown")
check("JE is unknown", role("QEPlus2_02162017_46_JE1") == "unknown")
check("numeric is unknown", role("QEPlus2_01042017_27_155") == "unknown")
check("numeric has no donor", donor("QEPlus2_01042017_27_155") is None)

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
