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

# ---------------------------------------------------------------------------------------------
# ABSENT DATA vs CLEAN DATA. The rollup covers 2 of the corpus's 2,086 searches. On the other
# 2,084 a PTM condition matches nothing, so an empty grid would read as "this search has no
# phosphoproteins" when the truth is that nobody has computed it. That is the specific failure
# this project keeps producing, and these are the checks that stop it here.
#
# DERIVED, NOT PINNED. The backfill will eventually populate every search, so a hard-coded
# "un-backfilled" id rots into a false pass the day someone runs it. Ask the database which search
# is un-backfilled at the moment the test runs. n_raw_files/n_protein_groups_total keep it small
# and non-degenerate; ORDER BY id keeps it reproducible between runs.
from app.db import query as _q                                # noqa: E402
_cand = _q("""SELECT s.id FROM delimp_searches s
               WHERE s.n_raw_files BETWEEN 3 AND 10
                 AND s.n_protein_groups_total > 500
                 AND NOT EXISTS (SELECT 1 FROM delimp_search_protein_ptm m WHERE m.search_id = s.id)
                 AND EXISTS (SELECT 1 FROM delimp_proteins p WHERE p.search_id = s.id)
               ORDER BY s.id LIMIT 1""",
           tables=["delimp_searches", "delimp_search_protein_ptm", "delimp_proteins"])
check("a search with no rollup rows exists to test against", bool(_cand),
      "every search is backfilled — this suite can no longer prove the not-ready path; "
      "re-point it at a freshly ingested search")
NO_ROLLUP = str(_cand[0]["id"]) if _cand else None
print(f"  [fixture] un-backfilled search derived at runtime: {NO_ROLLUP}")

if NO_ROLLUP:
    nr = queries.search_protein_matrix(NO_ROLLUP, mode="abundance", limit=50, filters="phospho")
    check("a search with no rollup rows still returns proteins, not an empty grid",
          len(nr["proteins"]) > 0, f'{len(nr["proteins"])} rows — an empty grid here reads as '
                                   f'"no phosphoproteins", which nobody has established')
    check("...and says ptm_rollup_ready is False rather than answering 'none'",
          nr.get("ptm_rollup_ready") is False, repr(nr.get("ptm_rollup_ready")))
    check("...and reports phospho as NOT applied, so `filters` never claims a filter that did not run",
          "phospho" not in nr["filters"], repr(nr["filters"]))
    # "Phospho (0)" beside a checkbox is the same lie as an empty grid, only harder to spot.
    check("...and drops the PTM counts rather than reporting them as 0",
          not ({"ptm", "phospho", "glygly"} & set(nr.get("filter_counts", {}))),
          repr(nr.get("filter_counts")))
    check("...while the counts it CAN compute are still reported",
          nr.get("filter_counts", {}).get("noncontam", 0) > 0, repr(nr.get("filter_counts")))

    # NOT CACHED: the backfill will populate this search, and a cached "not ready" would keep
    # asserting it for the full 30-minute TTL after the data landed.
    from app.db import SLOW_CACHE                              # noqa: E402
    _k = f"matrix_{NO_ROLLUP}_abundance_50_phospho"
    check("a not-ready result is NOT cached", SLOW_CACHE.cached(_k) is None,
          "cached — a backfill would then take up to 30 min to become visible")

    # An uncomputable PTM token must not throw away the tokens that ARE computable.
    nrc = queries.search_protein_matrix(NO_ROLLUP, mode="abundance", limit=50,
                                        filters="phospho,noncontam")
    check("non-PTM filters still apply when the PTM rollup is missing",
          nrc["filters"] == ["noncontam"] and all(not p.get("is_contaminant") for p in nrc["proteins"]),
          repr(nrc["filters"]))

# ready IS reported as True — not merely absent — so `if (!ready)` cannot misread a healthy
# response. The key stays absent only when no PTM filter was asked for.
check("ptm_rollup_ready is True on a search whose rollup IS computed",
      phos.get("ptm_rollup_ready") is True, repr(phos.get("ptm_rollup_ready")))
check("the key is absent when no PTM filter was asked for",
      "ptm_rollup_ready" not in base, repr(base.get("ptm_rollup_ready")))

# "computed, and the answer is none" must be distinguishable from "never computed". glygly is
# genuinely 0 on the phospho fixture, so this is the real case, not a contrived one.
gg = queries.search_protein_matrix(PHOS, mode="abundance", limit=50, filters="glygly")
check("a genuinely-empty PTM answer reports ready=True, not the not-computed flag",
      len(gg["proteins"]) == 0 and gg.get("ptm_rollup_ready") is True,
      f'{len(gg["proteins"])} rows, ready={gg.get("ptm_rollup_ready")}')

# ---------------------------------------------------------------------------------------------
# PER-FILTER COUNTS, so Task 3 can put a number beside each checkbox and nobody ticks a box that
# silently blanks the grid. Each count must equal the n_rankable that same filter produces ALONE —
# that is what keeps the tally in `tallies` and the predicate in WHERE from drifting apart.
fc = base["filter_counts"]
check("every filter has a count", set(fc) == set(queries._MATRIX_FILTERS), repr(sorted(fc)))
for _t in sorted(fc):
    _alone = queries.search_protein_matrix(PHOS, mode="abundance", limit=1, filters=_t)
    check(f"filter_counts[{_t}] equals the population that filter alone leaves",
          fc[_t] == _alone["n_rankable"], f'count={fc[_t]} vs n_rankable={_alone["n_rankable"]}')

# ---------------------------------------------------------------------------------------------
# max_peptides_any_run: a MAX over per-run counts. Summing that column overstated a peptide count
# 56-fold in this project, so the key name has to carry the semantics to whoever renders it.
check("each protein row carries max_peptides_any_run",
      all("max_peptides_any_run" in p for p in base["proteins"]))
check("max_peptides_any_run is a positive integer, not a sum-shaped total",
      all(isinstance(p["max_peptides_any_run"], int) and p["max_peptides_any_run"] >= 1
          for p in base["proteins"]),
      str([p["max_peptides_any_run"] for p in base["proteins"]][:5]))
check("...and multipeptide keeps only rows where it is >= 2",
      all(p["max_peptides_any_run"] >= 2 for p in
          queries.search_protein_matrix(PHOS, mode="abundance", limit=50,
                                        filters="multipeptide")["proteins"]))

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

if NO_ROLLUP:
    rr = client.get(f"/api/search/{NO_ROLLUP}/matrix",
                    params={"mode": "abundance", "limit": 50, "filters": "phospho"})
    rbody = rr.json().get("data", rr.json())
    check("the route surfaces ptm_rollup_ready=False and still returns rows",
          rbody.get("ptm_rollup_ready") is False and len(rbody["proteins"]) > 0,
          f'flag={rbody.get("ptm_rollup_ready")}, {len(rbody.get("proteins", []))} rows')
    check("the route surfaces filter_counts for the UI's checkbox labels",
          isinstance(rbody.get("filter_counts"), dict) and "noncontam" in rbody["filter_counts"],
          repr(rbody.get("filter_counts")))

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
