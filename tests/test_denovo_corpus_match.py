"""I/L normalisation, and the one-to-many the join table exists for.

Run:  python tests/test_denovo_corpus_match.py     (no pytest needed)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingest"))
from denovo_corpus_match import il, match_peptides  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


check("I->L", il("AIIEVLGR") == "ALLEVLGR")
check("already L unchanged", il("ALLEVLGR") == "ALLEVLGR")

corpus = {"ALLEVLGR": ["AIIEVLGR", "ALIEVLGR", "ALLEVLGR"], "PEPTLDEK": ["PEPTIDEK"]}
rows = match_peptides(["ALLEVLGR"], corpus)
check("one-to-many yields 3 rows", len(rows) == 3, f"got {len(rows)}")
check("n_candidates recorded", all(r["n_candidates"] == 3 for r in rows))
check("own spelling marked exact",
      any(r["corpus_stripped_seq"] == "ALLEVLGR" and r["match_kind"] == "exact" for r in rows))
check("others marked il",
      any(r["corpus_stripped_seq"] == "AIIEVLGR" and r["match_kind"] == "il" for r in rows))

r1 = [r["corpus_stripped_seq"] for r in match_peptides(["ALLEVLGR"], corpus)]
r2 = [r["corpus_stripped_seq"] for r in match_peptides(["ALLEVLGR"], corpus)]
check("deterministic and sorted", r1 == r2 == sorted(r1))

check("no match yields no rows", match_peptides(["WWWWWWWW"], corpus) == [])
check("I/L reach", len(match_peptides(["PEPTIDEK"], corpus)) == 1)

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
