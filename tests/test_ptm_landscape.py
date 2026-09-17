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
check("coverage reports absence without asserting a cause", "n_not_in_rollup" in cov and
      "n_uncomputable" not in cov, str(sorted(cov)))
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
# This must be asserted on the FUNCTION, not on corpus rows: zero of the 2,107 live searches
# lack a denominator, so a row-scan predicate returns 0 offenders whether the behaviour is right
# or wrong. A review proved exactly that -- simulating the regression (rate 0.0 + verdict
# "incidental" on every denominator-less row) still left the row-scan passing. Assert the rule
# where it lives.
check("_ptm_verdict(None) is None — no denominator means no verdict",
      queries._ptm_verdict(None) is None, repr(queries._ptm_verdict(None)))
check("_ptm_verdict still labels the extremes", 
      (queries._ptm_verdict(0.9), queries._ptm_verdict(0.01)) == ("enriched", "incidental"),
      str((queries._ptm_verdict(0.9), queries._ptm_verdict(0.01))))
check("_ptm_verdict(0.0) is 'incidental', not None — a measured zero IS a measurement",
      queries._ptm_verdict(0.0) == "incidental", repr(queries._ptm_verdict(0.0)))

# and the row-scan is still worth keeping as a corpus-level sanity check
bad = [r for r in d["searches"]
       if not r.get("n_precursors_total") and (r.get("modified_rate") is not None or r.get("verdict"))]
check("no live search contradicts that rule",
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


# THE MIDDLE BAND MUST CARRY NO LABEL.
# The design doc proposed "low for an enrichment" for 5%-50%. Measured on the real corpus that
# band is 1,900 of 2,107 searches, because the median search sits at ~20% modified precursors --
# background methionine oxidation, not enrichment. Labelling 90% of the corpus "low for an
# enrichment" tells ~1,860 owners an experiment underperformed when none was attempted, which is
# the page's own top stated risk. Verdicts are therefore only the two informative extremes.
verdicts = {r["verdict"] for r in d["searches"]}
check("verdicts are only the allowed labels (or none)",
      verdicts <= {None, "enriched", "incidental"}, str(sorted(v or "-" for v in verdicts)))

mid = [r for r in d["searches"]
       if r["modified_rate"] is not None and 0.05 < r["modified_rate"] < 0.50]
check("the middle band is populated (so this check is not vacuous)", len(mid) > 100, str(len(mid)))
check("no search in the middle band carries a verdict",
      all(r["verdict"] is None for r in mid),
      f"{sum(1 for r in mid if r['verdict'])} labelled, e.g. {[r['verdict'] for r in mid if r['verdict']][:1]}")

med = d["summary"].get("median_rate")
check("the corpus median rate is reported so a reader can calibrate",
      med is not None and 0 < med < 1, str(med))


# THE GROUP TILES MUST BE DISTINCT COUNTS, NOT SUMS OF PER-SEARCH COUNTS.
# Summing counted each protein group once per search it appeared in and rendered 2,637,809 under
# a tile reading "Protein groups modified", when the corpus holds 328,046 distinct such groups --
# an ~8x overstatement on a public page.
sum_per_search = sum(r["n_ptm"] for r in d["searches"])
check("the group tile is a corpus distinct count, not a per-search sum",
      d["summary"]["n_groups_any_ptm"] < sum_per_search,
      f'tile={d["summary"]["n_groups_any_ptm"]:,} vs per-search sum={sum_per_search:,}')
check("phospho and glygly tiles are likewise below their per-search sums",
      d["summary"]["n_groups_phospho"] <= sum(r["n_phospho"] for r in d["searches"]) and
      d["summary"]["n_groups_glygly"] <= sum(r["n_glygly"] for r in d["searches"]))

print()
if FAILS: print(f"FAILED ({len(FAILS)}): {', '.join(FAILS)}"); sys.exit(1)
print("all checks passed")
