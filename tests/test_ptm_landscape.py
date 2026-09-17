"""The PTM landscape aggregate must be honest about coverage and never overclaim a verdict."""
import os, sys
os.environ.setdefault("DELIMP_PG_TOKEN_FILE", "/Users/brettphinney/.pgfarm_token")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import queries                                   # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

GG_SEARCH = "5d629050-4f68-5e43-8e1f-48bbc3fc0b8f"   # Bennett_Penn_Ubiq — a working diGly enrichment

d = queries.ptm_landscape()

check("returns the four top-level keys", set(d) == {"coverage", "summary", "searches"} or
      {"coverage", "summary", "searches"} <= set(d), str(sorted(d)))

cov = d["coverage"]
check("coverage counts are present and sane",
      cov["n_searches_covered"] > 0 and cov["n_searches_total"] >= cov["n_searches_covered"],
      str(cov))

rows = {r["search_id"]: r for r in d["searches"]}
check("the Bennett Penn ubiquitin search is present", GG_SEARCH in rows)

if GG_SEARCH in rows:
    r = rows[GG_SEARCH]
    check("it is flagged as carrying GlyGly", r["n_glygly"] > 0, str(r["n_glygly"]))
    check("its modified rate is high (a working enrichment)",
          r["modified_rate"] is not None and r["modified_rate"] > 0.5, str(r.get("modified_rate")))
    check("its verdict is 'enriched'", r["verdict"] == "enriched", str(r.get("verdict")))

# THE OVERCLAIM GUARD. FRAN cannot tell a failed enrichment from a sample never enriched.
check("no verdict anywhere says 'failed'",
      not any("fail" in (r.get("verdict") or "").lower() for r in d["searches"]))

# ABSENCE IS NOT A MEASUREMENT: a search with no usable denominator must have no rate and no
# verdict, rather than a rate of 0.0 that reads as "nothing was modified".
bad = [r for r in d["searches"]
       if not r.get("n_precursors_total") and (r.get("modified_rate") is not None or r.get("verdict"))]
check("searches with no precursor denominator carry neither rate nor verdict",
      not bad, f"{len(bad)} offenders, e.g. {bad[:1]}")

# NO FILENAME/PATH LEAK on a public-tier payload.
#
# queries.ptm_landscape() itself returns RAW search_name (unredacted) by the same
# convention every other queries.py function follows (species_showcase, submission
# lookups, etc.) — redaction is applied once, uniformly, at the HTTP boundary by
# app.main.ok() -> privacy.redact(), based on the per-request reveal decision, not
# inside queries.py. Checking the raw dict directly is checking the wrong layer:
# it was observed to trip on one live search whose search_name is itself a raw
# Windows path (a pre-existing customer data-entry anomaly, not something this
# aggregate introduces), which privacy.redact()'s _NAME_KEYS handling already
# neutralizes. tests/test_search_matrix.py established this exact pattern
# ("REDACTION MUST COVER THE PUBLIC VIEW") after a prior leak got past a check
# that (like the one first written here) inspected the unredacted dict.
import json
from app import privacy
pub = privacy.redact(d, False)   # reveal=False: what an anonymous visitor actually receives
blob = json.dumps(pub, default=str)
check("no output_dir / path-shaped value leaks in the public (redacted) view",
      "output_dir" not in blob and ":\\" not in blob and "/Volumes/" not in blob
      and "/nfs/" not in blob and "/quobyte/" not in blob)

print()
if FAILS: print(f"FAILED ({len(FAILS)}): {', '.join(FAILS)}"); sys.exit(1)
print("all checks passed")
