"""Collaborators are ordered by when work was last ACQUIRED, not when it was ingested.

Ingest date is useless for this: 1,899 of 2,086 searches were ingested in a single June 2026
backfill, so ordering by it reproduces backfill sequence. raw_files.acquisition_date is the real
signal — 86% coverage at the raw_files level, and (measured below, against the live DB) every
keep-flagged collaborator GROUP has at least one dated raw file once merged, so today's live data
has no undated collaborator to witness a NULLs-first regression end to end. A second block below
exercises the real sort key with a synthetic dataset that DOES include an undated collaborator,
so the "sort last" behaviour is still proven even though production data can't show it today.

Also asserts the raw_files join added for last_run does not fan out n_searches (a search with
several raw files must not be counted once per raw file) — see the fan-out witness block below.

Run:  python tests/test_collaborator_recency.py
"""
import os, sys
os.environ["DELIMP_INTERNAL_MODE"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import queries                            # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)

d = queries.internal_collaborators()
rows = d.get("collaborators") or []
check("collaborators returned", len(rows) > 100, str(len(rows)))
check("every row carries last_run", all("last_run" in r for r in rows))
check("searches count is still present", all("n_searches" in r for r in rows))

# --- fan-out witness: the raw_files join must not inflate n_searches ----------------------
# internal_collaborators() LEFT JOINs delimp_search_provenance to search_raw_files to raw_files
# to compute last_run. That join changes the grain from one row per search to one row per
# (search, raw_file) pair — a search with several raw files fans out into several rows. Any
# aggregate over that joined query that isn't COUNT(DISTINCT ...) or MAX(...)/MIN(...) will count
# PAIRS, not searches. The directory's own drill-down (internal_collaborator_searches) is
# unaffected by this join, so a fan-out makes the list page and the drill-down disagree by
# exactly the average raw-files-per-search ratio — the visible symptom is "NIST — 1,046 searches"
# on the list page and a few dozen rows one click into it.
# The strongest, cheapest witness: summed over EVERY merged group (keep + excluded — the fan-out
# would inflate both), the directory's n_searches must equal the count of provenance rows that
# have a service_customer at all, since internal_collaborators() partitions exactly those rows
# into groups and n_searches is meant to be a count of them (each search_id appears in exactly
# one provenance row, so COUNT(DISTINCT search_id) per group summed over all groups == that row
# count). Derived from the live DB at test time, not hardcoded, so this doesn't rot as the corpus
# grows but still fails hard the moment a join reintroduces fan-out.
directory_total = sum(r["n_searches"] for r in rows) + sum(r["n_searches"] for r in (d.get("excluded") or []))
prov_total = queries.query(
    "SELECT COUNT(*) FROM delimp_search_provenance WHERE service_customer IS NOT NULL",
    tables=["delimp_search_provenance"], fetch="val") or 0
check("directory n_searches sums to the provenance row count (no raw-file fan-out)",
      directory_total == prov_total, f"directory={directory_total} vs provenance={prov_total}")

dates = [str(r["last_run"]) for r in rows if r.get("last_run")]
# Measured against the live DB on 2026-09-08: 183 of 183 keep-flagged collaborator groups carry a
# last_run — confirming the brief's docstring claim. Asserted exactly, so a join that silently
# drops groups (or a future collaborator with no dated raw file) fails loudly instead of hiding
# under a threshold.
check("every collaborator group has a last_run",
      len(dates) == len(rows), f"{len(dates)}/{len(rows)}")

# "most recent first" must hold over the WHOLE list, on last_run, not just the non-null
# subsequence. A comprehension that filters out rows with no last_run (like `dates` above) is
# blind to the one bug that matters here: it can't tell "sorted" from "sorted, but with undated
# collaborators interleaved through the top" — an undated collaborator sitting above one acquired
# last week is exactly the visible bug this page must not have. So build a key for EVERY row,
# mapping a missing last_run to "" — the smallest possible string, sorting LAST in descending
# order — and assert the whole sequence is non-increasing. This mirrors
# tests/test_submission_lookup.py's "newest first" check (NULLS LAST <-> date.min sentinel).
keys = [str(r["last_run"]) if r.get("last_run") else "" for r in rows]
check("sorted by last_run descending, undated collaborators sorting last",
      keys == sorted(keys, reverse=True), f"first five {keys[:5]}")

# --- synthetic witness for "undated sorts last" -------------------------------------------
# Live data (above) has zero undated keep-flagged groups, so it cannot show the one regression
# that matters: an undated collaborator ranking above a dated one. Monkeypatch queries.query
# (imported by name into app.queries, so reassigning it here reaches the real
# internal_collaborators()) to return four distinct, unmapped customer names — resolve() falls
# back to a "keep"-flagged group per raw name when there's no curation match — with one deliberately
# undated (last_run=None) and interleaved by n_searches so it would rank near the top under both
# the OLD n_searches order and a naive NULLs-first key.
_SYN_ROWS = [
    {"raw": "Zeta Synthetic Lab", "n_searches": 50, "n_pis": 1, "n_projects": 1, "n_lims_linked": 0,
     "campus": "Davis", "source": "s", "last_run": None},               # undated, HIGH n_searches
    {"raw": "Alpha Synthetic Lab", "n_searches": 5, "n_pis": 1, "n_projects": 1, "n_lims_linked": 0,
     "campus": "Davis", "source": "s", "last_run": "2026-08-01"},       # most recent
    {"raw": "Beta Synthetic Lab", "n_searches": 5, "n_pis": 1, "n_projects": 1, "n_lims_linked": 0,
     "campus": "Davis", "source": "s", "last_run": "2025-01-01"},
    {"raw": "Gamma Synthetic Lab", "n_searches": 5, "n_pis": 1, "n_projects": 1, "n_lims_linked": 0,
     "campus": "Davis", "source": "s", "last_run": "2020-06-15"},       # oldest dated
]
_real_query = queries.query
def _fake_query(sql, *args, **kwargs):
    if "GROUP BY" in sql:            # the collaborator-grouping query
        return list(_SYN_ROWS)
    return 0                          # n_unattributed COUNT(*)
queries.query = _fake_query
try:
    syn = queries.internal_collaborators()
    syn_rows = syn.get("collaborators") or []
    syn_names = [r["client"] for r in syn_rows]
    check("synthetic: all four synthetic groups present", len(syn_rows) == 4, syn_names)
    check("synthetic: the undated collaborator (Zeta, n_searches=50) sorts LAST, not first",
          syn_names and syn_names[-1] == "Zeta Synthetic Lab", syn_names)
    check("synthetic: the dated collaborators are ordered by last_run descending ahead of it",
          syn_names[:3] == ["Alpha Synthetic Lab", "Beta Synthetic Lab", "Gamma Synthetic Lab"],
          syn_names)
finally:
    queries.query = _real_query      # restore before any other test module imports queries

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
