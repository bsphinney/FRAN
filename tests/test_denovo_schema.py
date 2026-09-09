"""The de novo schema, asserted rather than eyeballed. Parses the DDL text; needs no DB.

Run:  python tests/test_denovo_schema.py     (no pytest needed)
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SQL = open(os.path.join(HERE, "..", "schema", "denovo.sql")).read()

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


for t in ("delimp_denovo_run", "delimp_denovo_peptide", "delimp_denovo_psm",
          "delimp_denovo_corpus_match"):
    check(f"{t} created idempotently", f"CREATE TABLE IF NOT EXISTS {t}" in SQL)

pep = re.search(r"CREATE TABLE IF NOT EXISTS delimp_denovo_peptide \((.*?)\n\);", SQL, re.S)
check("peptide table parses", pep is not None)
if pep:
    body = pep.group(1)
    check("peptide_score stored WITH length", "peptide_score" in body and "length" in body)
    check("conf_geomean stored", "conf_geomean" in body)
    check("seq_il stored", "seq_il" in body)
    check("corpus_class stored", "corpus_class" in body)

check("I/L expression index on the corpus side",
      "idx_consensus_seq_il" in SQL and "replace(stripped_seq" in SQL)
check("sample_role defaults to unknown", "sample_role" in SQL and "'unknown'" in SQL)
check("replicate_kind distinguishes the three kinds", "replicate_kind" in SQL)
check("no DROP statements", "DROP TABLE" not in SQL.upper())

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
