"""The Submissions tab must use the SAME definition of "analyzed" as the rest of the app.

internal_lab() (app/queries.py) carries an explicit comment declaring the single definition of
"analyzed" to be `n_searches > 0 OR in_fran` — precisely so the same submission can't read
"analyzed" on the institution page and "on the share" on this one (bug-logic #6). The submission
detail page (app/static/app.js, renderSubmission) already keys off `sd.in_fran`. The Submissions
tab's `state()` (renderSubmissions, app/static/app.js) originally keyed on `n_searches>0` ALONE,
so a submission whose disk-match says in_fran=true but has no FK-linked provenance search (the
disk-match can know a submission is in FRAN before/without a linked search_id) rendered as
"on the share" WITH a "Re-search this data" export button — offering to re-search data that is
already ingested.

PROT_0601 is the concrete live witness: n_searches=0, in_fran=True, run_count=337. Under the old
condition it renders state 2 ("on the share"); under the fix it must render state 1 ("analyzed").

This test can't execute renderSubmissions() directly (it's a local closure inside an async
function that talks to the DOM and the live API), so it extracts the ACTUAL shipped `state()`
condition out of app/static/app.js by regex and evaluates it with node against both a PROT_0601
shaped object and a genuinely-unlocated one — testing the real source, not a Python reimplementation.

Run:  python tests/test_submission_analyzed_definition.py
"""
import json, os, re, subprocess, sys
os.environ["DELIMP_INTERNAL_MODE"] = "1"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
from app import queries                             # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)

# --- live-data witness: PROT_0601 really is the n_searches=0 / in_fran=true case --------------
d = queries.internal_submissions(q="PROT_0601", limit=5)
prot0601 = next((s for s in d.get("submissions") or [] if s.get("internal_id") == "PROT_0601"), None)
check("PROT_0601 is found", prot0601 is not None)
if prot0601:
    check("PROT_0601 has n_searches == 0 (the case the old condition mishandled)",
          prot0601.get("n_searches") == 0, str(prot0601.get("n_searches")))
    check("PROT_0601 has in_fran == True (the disk-match knows it's ingested)",
          prot0601.get("in_fran") is True, str(prot0601.get("in_fran")))

# --- source-level teeth: extract and evaluate the ACTUAL shipped state() condition ------------
APPJS = os.path.join(ROOT, "app", "static", "app.js")
with open(APPJS) as f:
    src = f.read()
m = re.search(r"const state = s => (\([^\n]*\))", src)
check("app.js still defines `const state = s => (...)`  in the expected shape", m is not None)
expr = m.group(1) if m else "(false)"

def eval_condition(sample: dict) -> bool:
    """Run the exact extracted JS boolean expression through node against a sample `s` object."""
    js = f"const s = {json.dumps(sample)}; console.log(!!({expr}));"
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True)
    return out.stdout.strip() == "true"

prot0601_shaped = {"n_searches": 0, "in_fran": True, "run_count": 337}
check("state() analyzed-condition treats PROT_0601-shaped data (n_searches=0, in_fran=true) as analyzed, not 'on the share'",
      eval_condition(prot0601_shaped), expr)

on_share_shaped = {"n_searches": 0, "in_fran": False, "run_count": 5}
check("state() analyzed-condition still leaves a genuinely un-ingested submission (in_fran=false) OUT of 'analyzed'",
      not eval_condition(on_share_shaped), expr)

has_searches_shaped = {"n_searches": 3, "in_fran": False}
check("state() analyzed-condition still treats a submission with FK-linked searches as analyzed",
      eval_condition(has_searches_shaped), expr)

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
