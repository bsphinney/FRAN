"""The de novo read layer: allowlisting, SQL hygiene, and the I/L reverse lookup.

Run:  python tests/test_denovo_queries.py     (no pytest needed)
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import db  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


for t in ("delimp_denovo_run", "delimp_denovo_peptide", "delimp_denovo_psm",
          "delimp_denovo_corpus_match"):
    check(f"{t} allowlisted", t in db.PUBLIC_TABLES)

src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "denovo.py")).read()
check("every query names its tables", src.count("tables=[") >= 3)

# SQL INJECTION. An f-string in a query is fine ONLY when what it interpolates is a fixed SQL
# fragment; it is a vulnerability the moment a caller value goes in. So rather than banning
# f-strings (the codebase uses them for optional WHERE clauses), check the two things that
# actually make them safe:
#   1. every {name} interpolated into a query is in a small allowlist of fragment variables, and
#   2. each of those variables is only ever assigned a STRING LITERAL.
FRAGMENT_VARS = {"where", "cls"}
interpolated = set(re.findall(r"\{(\w+)\}", src))
bad = interpolated - FRAGMENT_VARS
check("only fragment vars are interpolated", not bad, f"unexpected: {sorted(bad)}")

for var in FRAGMENT_VARS:
    assigns = re.findall(rf"^\s*{var}\s*=\s*(.+)$", src, re.M)
    # A tuple-unpacking assignment like `where, params[...] = "...", value` also counts as a
    # literal assignment to the fragment, so check the fragment's own side only.
    literal = all(a.lstrip().startswith(('"', "'")) for a in assigns) if assigns else True
    check(f"'{var}' is only ever a string literal", literal, f"assignments: {assigns}")

check("no caller value formatted into SQL",
      not re.search(r"\{\s*(?:seq|run_id|cohort|corpus_class|limit|offset)\s*\}", src))
check("uses named placeholders", "%(" in src)
# The reverse lookup must go through the I/L key or it finds only same-spelling peptides.
check("peptide_denovo joins on seq_il", "seq_il" in src)
check("length selected wherever peptide_score is",
      "peptide_score" not in src or "length" in src)
# match_fdr must reach the UI: a candidate count without its null overstates the result ~2x.
check("match_fdr exposed", "match_fdr" in src)

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
