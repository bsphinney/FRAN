"""Aggregation, and the length-normalised confidence that replaces peptide_score.

Run:  python tests/test_denovo_aggregate.py     (no pytest needed)
"""
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingest"))
from denovo_aggregate import aggregate_peptides, run_stats  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


psms = [
    {"scan": 1, "stripped_seq": "PEPTIDEK", "seq_il": "PEPTLDEK", "peptide_score": 0.5, "aa_scores": []},
    {"scan": 2, "stripped_seq": "PEPTIDEK", "seq_il": "PEPTLDEK", "peptide_score": 0.7, "aa_scores": []},
    {"scan": 3, "stripped_seq": "AAAAAA", "seq_il": "AAAAAA", "peptide_score": 0.5, "aa_scores": []},
]
rows = {r["stripped_seq"]: r for r in aggregate_peptides(psms)}
check("two distinct peptides", len(rows) == 2)
check("n_psms counted", rows["PEPTIDEK"]["n_psms"] == 2)
check("best score kept", abs(rows["PEPTIDEK"]["peptide_score"] - 0.7) < 1e-9)
check("length stored", rows["PEPTIDEK"]["length"] == 8)
check("geomean of the 8-mer", abs(rows["PEPTIDEK"]["conf_geomean"] - 0.7 ** (1 / 8)) < 1e-9)
check("geomean of the 6-mer", abs(rows["AAAAAA"]["conf_geomean"] - 0.5 ** (1 / 6)) < 1e-9)
# The 6-mer and 8-mer sit in the same raw-score band, but the longer one is better per residue.
check("geomean reorders vs raw score",
      rows["PEPTIDEK"]["conf_geomean"] > rows["AAAAAA"]["conf_geomean"])

st = run_stats(list(rows.values()))
check("run n_psms", st["n_psms"] == 3)
check("run n_peptides", st["n_peptides"] == 2)
check("len_median", st["len_median"] == statistics.median([8, 6]))
check("negative score clamped, not crashed",
      aggregate_peptides([{"scan": 9, "stripped_seq": "AA", "seq_il": "AA",
                           "peptide_score": -0.99, "aa_scores": []}])[0]["conf_geomean"] >= 0)

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
