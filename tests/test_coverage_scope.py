"""Coverage gains an optional search scope — WITHOUT changing its unscoped behaviour.

Fixtures measured 2026-09-09 against the live endpoint:
  Fabp1  P12710  127 aa  38 corpus peptides, 20 in PROT_0793_search_mouse, 18 corpus-only
  Mup2   P11589  180 aa  26 corpus peptides, 22 here, 4 corpus-only

These counts DRIFT as new searches land (a sibling plan's literal count went red overnight on
healthy data), so this test asserts the INVARIANTS the feature actually rests on, not the
literals above:
  (a) scoped and unscoped calls return the SAME peptide set (the scope adds a flag, not a filter),
      and it's non-empty so the comparison isn't vacuous.
  (b) for Fabp1, "here" is a proper non-empty subset — that's what makes a two-colour peptide map
      meaningful at all.
  (c) the relationship that carries the finding: Mup2's found-fraction exceeds Fabp1's. Mup2 is
      mouse-specific and rare in the corpus, so this one search knows almost as much about it as
      the whole corpus does; Fabp1 is common, so the corpus knows much more than this search saw.
      That ordering, unlike a literal count, doesn't drift into a false red as the corpus grows.

Run:  DELIMP_PG_TOKEN_FILE=/Users/brettphinney/.pgfarm_token python3 tests/test_coverage_scope.py
"""
import os, sys
os.environ.setdefault("DELIMP_INTERNAL_MODE", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import queries                                      # noqa: E402

SID = "8221f5fc-492e-5c9d-a08d-542cfdb48791"
FABP1 = "P12710"
MUP2 = "P11589"
FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)

# BACKWARD COMPATIBILITY. Existing callers pass no search_id and must see exactly what they saw.
un = queries.protein_coverage_peptides(FABP1)
check("unscoped call still returns peptides", len(un.get("peptides") or []) > 0,
      str(len(un.get("peptides") or [])))
check("unscoped peptides carry NO 'here' key",
      all("here" not in p for p in un["peptides"]),
      "adding 'here' unconditionally would change the shape for every existing caller")

# (a) scope adds a flag, does not change membership.
sc = queries.protein_coverage_peptides(FABP1, search_id=SID)
check("scoped call returns the SAME corpus peptide set as unscoped",
      len(sc["peptides"]) == len(un["peptides"]) and len(sc["peptides"]) > 0,
      f"scoped={len(sc['peptides'])} unscoped={len(un['peptides'])}")
check("scoped peptides all carry 'here'", all("here" in p for p in sc["peptides"]))

# (b) Fabp1: "here" is a proper non-empty subset.
here = sum(1 for p in sc["peptides"] if p["here"])
check("Fabp1: 'here' is a non-empty proper subset of the corpus peptides",
      0 < here < len(sc["peptides"]), f"{here}/{len(sc['peptides'])}")

# (c) Mup2's found-fraction exceeds Fabp1's — the substance of the feature.
sc2 = queries.protein_coverage_peptides(MUP2, search_id=SID)
here2 = sum(1 for p in sc2["peptides"] if p["here"])
frac1 = here / len(sc["peptides"]) if sc["peptides"] else 0
frac2 = here2 / len(sc2["peptides"]) if sc2["peptides"] else 0
check("Mup2 (rare, mouse-specific) has a higher found-fraction than Fabp1 (common)",
      len(sc2["peptides"]) > 0 and frac2 > frac1,
      f"Mup2 {here2}/{len(sc2['peptides'])}={frac2:.2f} vs Fabp1 {here}/{len(sc['peptides'])}={frac1:.2f}")

# A search that never saw this protein must mark everything corpus-only, not crash.
none = queries.protein_coverage_peptides(FABP1, search_id="00000000-0000-0000-0000-000000000000")
check("an unrelated search marks every peptide corpus-only",
      bool(none["peptides"]) and not any(p["here"] for p in none["peptides"]))

# --- Fix round 1, IMPORTANT #1: a malformed search_id is a client error, not a fake outage -------
# psycopg2 raises InvalidTextRepresentation for a non-UUID against the uuid column, which the
# global handler turns into a 503 "Database unavailable" -- exactly the wrong message for a typo
# in a URL. The endpoint must validate BEFORE it ever reaches the DB.
from fastapi.testclient import TestClient                    # noqa: E402
from app.main import app                                     # noqa: E402

client = TestClient(app)
bad = client.get(f"/api/protein/{FABP1}/coverage", params={"search_id": "not-a-uuid"})
check("a malformed search_id returns 400, not a 503 'database unavailable'",
      bad.status_code == 400, str(bad.status_code))

# --- Fix round 1, IMPORTANT #2: the "here" lookup degrades like its two sibling queries ----------
# Force ONLY the "here" lookup to fail (monkeypatch queries.query so the tell-tale "stripped_seq =
# ANY" statement raises) while the corpus scan and gene lookup -- the siblings that already
# degrade gracefully -- run for real. A fresh (protein, search_id) pair that nothing above has
# touched, so this can't be served from an existing cache entry either way.
FLAKY_SID = "11111111-1111-1111-1111-111111111111"
_real_query = queries.query
def _flaky_query(sql, *a, **kw):
    if "stripped_seq = ANY" in sql:
        raise RuntimeError("simulated transient failure in the here-lookup")
    return _real_query(sql, *a, **kw)

queries.query = _flaky_query
try:
    degraded = queries.protein_coverage_peptides(FABP1, search_id=FLAKY_SID)
finally:
    queries.query = _real_query

check("a failed here-lookup still returns the corpus peptides",
      len(degraded.get("peptides") or []) > 0, str(len(degraded.get("peptides") or [])))
check("a failed here-lookup leaves 'here' ABSENT on every peptide (never a lying False)",
      all("here" not in p for p in degraded["peptides"]))
check("a failed here-lookup sets scope_unavailable=True",
      degraded.get("scope_unavailable") is True)
check("a failed here-lookup is NOT cached (so a retry can still succeed)",
      queries.CACHE.cached(f"covpep_{FABP1}_{FLAKY_SID}") is None)

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
