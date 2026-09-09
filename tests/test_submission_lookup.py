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

# --- search must find a submission by number, and submissions with no searches -------------
r = queries.internal_people_search("PROT_0793", 50)
check("searching PROT_0793 returns something", (r.get("total") or 0) > 0, str(r.get("total")))
check("...and it names the right institute",
      any("protifi" in str(x.get("institute") or x.get("co_institute") or "").lower()
          for x in r.get("rows") or []), "no ProtiFi row")

r2 = queries.internal_people_search("0793", 50)
check("bare 0793 also finds it", (r2.get("total") or 0) > 0, str(r2.get("total")))

# A submission with NO linked FRAN search must still be findable. PROT_0804 (UC Davis, 2026-09-08)
# has none; the old query was rooted in delimp_search_provenance so it could not return one.
r3 = queries.internal_people_search("PROT_0804", 50)
check("a submission with no linked search is still findable",
      (r3.get("total") or 0) > 0, str(r3.get("total")))

# PROT_0793 is a poor witness for the provenance-side co.internal_id match: its two "linked"
# searches actually have p.coreomics_submission_id = NULL (linkage_status='unlinked') and only
# surface because p.real_search_name literally contains the substring "PROT_0793" — a
# pre-existing clause unrelated to this change. So PROT_0793 alone cannot prove the
# `co.internal_id` provenance clause does anything. PROT_0652 (hex 652c08d115d8) is a genuine
# FK-linked witness: 6 searches with p.coreomics_submission_id actually set to its hex id.
# Reaching those 6 real search rows (not a bare submission stub) by number, through the
# provenance branch, is exactly what the co.internal_id provenance clause is for.
for term in ("PROT_0652", "0652"):
    r4 = queries.internal_people_search(term, 50)
    check(f"{term} reaches PROT_0652's real searches, not just a submission stub",
          any(x.get("kind") == "search" and x.get("search_engine") for x in r4.get("rows") or []),
          f'rows={[(x.get("kind"), x.get("search_engine")) for x in r4.get("rows") or []]}')

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
