"""Substring peptide search over delimp_peptide_index (pg_trgm), no live DB needed.

Mocks queries.query so the SQL path/params/return shape are checked offline. The index itself was
measured separately (see ingest/migrations/2026-11-16_peptide_trgm_index.sql).
Run: python3 tests/test_peptide_substring_trgm.py
"""
import os, sys, time
os.environ.setdefault("DELIMP_INTERNAL_MODE", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import queries  # noqa: E402

calls = []

def idx():
    return [c for c in calls if "delimp_peptide_index" in c[0]][0]

def fake_query(sql, params=None, *, tables, fetch="all", timeout_ms=None, work_mem=None):
    calls.append((sql, params, tuple(tables)))
    if "COUNT(DISTINCT stripped_seq)" in sql:   # exact-spelling probe: nothing spelled NEEDS
        return 0
    if "stripped_seq = ANY" in sql and "LIMIT" in sql:
        return []
    if "FROM delimp_peptide_index" in sql and "COUNT(*)" in sql:
        return 2
    if "FROM delimp_peptide_index" in sql:
        return [{"stripped_seq": "AANEEDSK"}, {"stripped_seq": "LNEEDSR"}]
    return [{"stripped_seq": "AANEEDSK", "n_precursors": 9}, {"stripped_seq": "LNEEDSR", "n_precursors": 3}]

queries.query = fake_query
queries._peptide_index_state.update(ok=True, checked=0.0)

# 1. NEEDS goes through the index relation, never the 763M-row table's ILIKE.
r = queries.search_peptides("needs")
assert r["total"] == 2 and len(r["rows"]) == 2 and "degraded" not in r, r
assert all("ILIKE" not in c[0] for c in calls), "must not use the unindexed ILIKE"
assert idx()[1] == ("%NEEDS%",)
# 2. I/L: 'NEEDI' is searched as 'NEEDL'
calls.clear(); queries.search_peptides("NEEDI")
assert idx()[1] == ("%NEEDL%",), calls
# 3. LIKE wildcards in user input are escaped
calls.clear(); queries.search_peptides("A_C%")
assert idx()[1] == ("%A\\_C\\%%",), calls
# 4. 1-2 letter motifs are refused up front with a hint (no query at all)
calls.clear(); r = queries.search_peptides("NE")
assert r["degraded"] and "at least 3" in r["hint"] and not calls, r
# 5. relation absent -> legacy path is chosen (probe is cached negative)
queries._peptide_index_state.update(ok=False, checked=time.time())
calls.clear(); queries.search_peptides("NEEDS")
assert any("ILIKE" in c[0] for c in calls)
print("ok")
