"""The heatmap's filters must be applied IN SQL, before the top-N cut.

The load-bearing assertion is "fills the page": only 62 of 6,284 protein groups on the fixture
carry a phospho site, so filtering the 50 rows the ranking already selected leaves a handful —
while every other check here, including "changes which genes are returned", still passes. A
client-side filter looks like it works. That check is the only one that catches it.
"""
import os, sys
os.environ.setdefault("DELIMP_PG_TOKEN_FILE", "/Users/brettphinney/.pgfarm_token")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import queries                                # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

PHOS = "2c4911a3-79fd-5367-bdd0-ee85a16cd25b"

base = queries.search_protein_matrix(PHOS, mode="abundance", limit=50)
phos = queries.search_protein_matrix(PHOS, mode="abundance", limit=50, filters="phospho")

check("unfiltered returns rows", len(base["proteins"]) > 0, str(len(base["proteins"])))
check("phospho filter returns rows", len(phos["proteins"]) > 0, str(len(phos["proteins"])))

# THE FILTER MUST ACTUALLY CHANGE THE SET. A filter applied after the top-N cut, or ignored
# entirely, yields the same genes — and every other assertion here would still pass.
check("the phospho filter changes which genes are returned",
      {p["gene"] for p in base["proteins"]} != {p["gene"] for p in phos["proteins"]},
      "identical gene sets — filter had no effect")

# APPLIED BEFORE THE LIMIT, not after. Only 62 of 6,284 protein groups carry phospho, so filtering
# the already-selected 50 would leave a handful. Anything close to 50 proves pre-LIMIT filtering.
check("phospho filter fills the page (pre-LIMIT), not a remnant of it",
      len(phos["proteins"]) >= 20, f'{len(phos["proteins"])} rows')

# n_rankable must describe the FILTERED population, or the header lies.
check("n_rankable shrinks under a filter",
      phos.get("n_rankable", 0) < base.get("n_rankable", 0),
      f'{phos.get("n_rankable")} vs {base.get("n_rankable")}')

nc = queries.search_protein_matrix(PHOS, mode="abundance", limit=50, filters="noncontam")
check("noncontam returns no contaminants",
      all(not p.get("is_contaminant") for p in nc["proteins"]),
      str([p["gene"] for p in nc["proteins"] if p.get("is_contaminant")][:3]))

comp = queries.search_protein_matrix(PHOS, mode="abundance", limit=50, filters="complete")
n_tot = comp["n_samples_total"]
check("complete returns only proteins seen in every sample",
      all(p["n_samples"] == n_tot for p in comp["proteins"]),
      str([(p["gene"], p["n_samples"]) for p in comp["proteins"] if p["n_samples"] != n_tot][:3]))

patchy = queries.search_protein_matrix(PHOS, mode="abundance", limit=50, filters="patchy")
check("patchy and complete are disjoint",
      not ({p["gene"] for p in patchy["proteins"]} & {p["gene"] for p in comp["proteins"]}))

check("an unknown filter token is ignored, not fatal",
      len(queries.search_protein_matrix(PHOS, mode="abundance", limit=5,
                                        filters="not_a_filter")["proteins"]) > 0)

# THE ROUTE, not just the query function — this is the interface the UI task consumes, and a
# FastAPI handler that forgot the parameter would silently serve the unfiltered matrix forever.
from fastapi.testclient import TestClient                    # noqa: E402
from app.main import app                                     # noqa: E402
client = TestClient(app)

r = client.get(f"/api/search/{PHOS}/matrix", params={"mode": "abundance", "limit": 50,
                                                    "filters": "phospho"})
check("the route accepts ?filters= and returns 200", r.status_code == 200, str(r.status_code))
body = r.json().get("data", r.json())
check("the route's response echoes the NORMALIZED filters, never the raw string",
      body.get("filters") == ["phospho"], repr(body.get("filters")))
check("the route actually filtered (route-level pre-LIMIT check)",
      len(body["proteins"]) >= 20 and body["n_rankable"] < base["n_rankable"],
      f'{len(body["proteins"])} rows, n_rankable={body.get("n_rankable")}')

# Free text must not reach SQL: a quoting attempt normalizes away to no filter at all.
inj = client.get(f"/api/search/{PHOS}/matrix",
                 params={"mode": "abundance", "limit": 5, "filters": "phospho') OR true--"})
ibody = inj.json().get("data", inj.json())
check("an injection-shaped filter token is dropped, not executed",
      inj.status_code == 200 and ibody.get("filters") == [], repr(ibody.get("filters")))

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
