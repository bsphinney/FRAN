"""The federation trust boundary, asserted rather than reviewed (FEDERATION_DESIGN.md §4.1).

These tests exist because the egress rule is one function deep and one careless `dict(row)` from
being wrong. Reviewing that by eye works until the day someone adds a column.

Run:  python tests/test_federation_boundary.py     (no pytest needed)
"""
import os, sys

os.environ.setdefault("FRAN_NODE_ID", "testnode")
os.environ.setdefault("FRAN_SHARE_POLICY", "public-tier")
os.environ.setdefault("FRAN_FED_SALT", "x" * 64)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app import federation as fed          # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)

# A row shaped like the real join: every leaky column a precursor query can carry.
# Values are SYNTHETIC ON PURPOSE. This repo is public, and an earlier draft of this fixture used a
# real PI folder, a real search name, a real instrument serial and a real LIMS submission id — so the
# test proving customer data cannot leak would itself have published customer data. Keep them fake.
DIRTY_ROW = {
    "stripped_seq": "LVNELTEFAK", "modified_seq_proforma": "LVNELTEFAK", "charge": 2,
    "precursor_mz": 575.3, "rt": 31.2, "im": 0.94, "q_value": 0.0004, "intensity": 1.2e7,
    "protein_group": "P02769", "gene": "ALB", "organism_name": "Bos taurus",
    "platform": "timstof", "instrument_model": "timsTOF Pro 2", "engine": "spectronaut",
    # ---- everything below must never leave the node ----
    "raw_path": r"R:\Data\lab\service\on_campus\Example_Lab\confidential_study\x.d",
    "raw_basename": "FL30525_DWang40-Dia_90m_39",
    "raw_name_anonymized": "run-3921ae",
    "search_name": "20260101_120000_01Jan2026_ExampleLab_studyname",
    "output_dir": r"B:\Automatic_SNE_storage\thing.sne",
    "id": "3e79da92-6821-51fa-b76f-6e998eb8446a",
    "search_id": "3e79da92-6821-51fa-b76f-6e998eb8446a",
    "pi": "Example_Lab", "client": "Example University", "project": "confidential_study",
    "instrument_serial": "0000000.00000", "coreomics_submission_id": "0123456789ab",
    "hive_path": "/nfs/lssc0/flinders/...", "notes": "unpublished",
}

print("federation egress boundary")
rec = fed.public_record(DIRTY_ROW, run_local=DIRTY_ROW["raw_path"], search_local=DIRTY_ROW["search_id"])

leaked = sorted(set(rec) & fed.FORBIDDEN_FIELDS)
check("no forbidden field survives public_record()", not leaked, f"leaked {leaked}")
check("no non-allowlisted field survives", not (set(rec) - fed.ALLOWED_FIELDS),
      f"extra {sorted(set(rec) - fed.ALLOWED_FIELDS)}")

# The values, not just the keys: a pseudonym that embeds the original is not a pseudonym.
blob = repr(rec)
for secret, label in ((DIRTY_ROW["raw_path"], "raw path"),
                      (DIRTY_ROW["raw_basename"], "raw basename"),
                      (DIRTY_ROW["search_name"], "search name"),
                      ("Example_Lab", "PI name"), ("confidential_study", "project name"),
                      (DIRTY_ROW["instrument_serial"], "instrument serial")):
    check(f"{label} does not appear anywhere in the payload", secret not in blob)

check("science survives (peptide kept)", rec.get("stripped_seq") == "LVNELTEFAK")
check("science survives (ion mobility kept)", rec.get("im") == 0.94)
check("attribution present", rec.get("node_id") == "testnode")
check("run id is namespaced", str(rec.get("run", "")).startswith("testnode:run-"))

print("\npseudonym properties")
p1 = fed.pseudonym("run", "/x/a.d")
check("stable across calls", p1 == fed.pseudonym("run", "/x/a.d"))
check("differs per value", p1 != fed.pseudonym("run", "/x/b.d"))
check("domain-separated (run vs search)", fed.pseudonym("run", "/x/a.d") != fed.pseudonym("search", "/x/a.d"))
# The reason we salt at all: an attacker who guesses the filename must not be able to confirm it.
import hashlib
check("NOT a bare unsalted digest of the input",
      hashlib.sha1(b"/x/a.d").hexdigest()[:6] not in p1 and hashlib.sha256(b"/x/a.d").hexdigest()[:16] not in p1)
salt_before = fed._SALT
fed._SALT = "y" * 64
check("changes when the node salt changes", fed.pseudonym("run", "/x/a.d") != p1)
fed._SALT = salt_before

print("\nfail-closed behaviour")
pol = fed.SHARE_POLICY
fed.SHARE_POLICY = "closed"
try:
    fed.public_record(DIRTY_ROW); ok = False
except PermissionError:
    ok = True
fed.SHARE_POLICY = pol
check("a closed node refuses to build a record", ok)

salt_before = fed._SALT
fed._SALT = ""
try:
    fed.public_record(DIRTY_ROW); ok = False
except PermissionError:
    ok = True
fed._SALT = salt_before
check("a node with no salt refuses (never falls back to unsalted)", ok)

try:
    fed.assert_clean([{**rec, "raw_path": "/leak"}]); ok = False
except PermissionError:
    ok = True
check("assert_clean catches a hand-built leaky record", ok)

print("\nallowlist/denylist coherence")
check("allow and forbid sets are disjoint", not (fed.ALLOWED_FIELDS & fed.FORBIDDEN_FIELDS),
      f"overlap {sorted(fed.ALLOWED_FIELDS & fed.FORBIDDEN_FIELDS)}")

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
