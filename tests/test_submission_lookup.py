"""A submission number must reach the submission page.

PROT_0793 is ProtiFi LLC. Before this change the only way to reach it was its hex id
(1ed8b74497e4), which nobody has in hand.

Run:  python tests/test_submission_lookup.py
"""
import os, sys
os.environ["DELIMP_INTERNAL_MODE"] = "1"          # internal tables; see app/db.py
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import queries                            # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)

n = queries.normalize_submission_ref
check("PROT_0793 normalises to itself", n("PROT_0793") == "PROT_0793", repr(n("PROT_0793")))
check("lowercase prot_0793 normalises", n("prot_0793") == "PROT_0793", repr(n("prot_0793")))
check("bare 0793 normalises", n("0793") == "PROT_0793", repr(n("0793")))
check("bare 793 zero-pads", n("793") == "PROT_0793", repr(n("793")))
check("whitespace is tolerated", n("  PROT_0793 ") == "PROT_0793", repr(n("  PROT_0793 ")))
check("a hex id is NOT a number", n("1ed8b74497e4") is None, repr(n("1ed8b74497e4")))
check("a name is not a number", n("ProtiFi") is None, repr(n("ProtiFi")))
check("empty is None", n("") is None)
# 5 digits is not this scheme; refusing beats silently truncating to 4
check("five digits is refused", n("12345") is None, repr(n("12345")))
# the collision this whole scheme is built to avoid: some real submission_ids are all-numeric
# (14 of 4,488, measured 2026-09-08) — the {1,4} cap, not luck, is what keeps this None
check("a 12-digit all-numeric hex id is refused", n("123456789012") is None, repr(n("123456789012")))

d = queries.internal_submission("PROT_0793")
sub = d.get("submission") or {}
check("PROT_0793 resolves to the ProtiFi submission",
      (sub.get("institute") or "") == "ProtiFi LLC", repr(sub.get("institute")))
check("resolving by number returns the same submission as the hex id",
      d.get("submission_id") == queries.internal_submission("1ed8b74497e4").get("submission_id"),
      f'{d.get("submission_id")} vs hex lookup')
check("an unknown number returns no submission, without raising",
      (queries.internal_submission("PROT_9999") or {}).get("submission") is None)

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
