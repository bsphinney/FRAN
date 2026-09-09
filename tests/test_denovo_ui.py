"""The SPA wires the de novo views and reuses the existing peptide chip.

Run:  python tests/test_denovo_ui.py     (no pytest needed)
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
JS = os.path.join(HERE, "..", "app", "static", "app.js")
js = open(JS).read()

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


check("denovo route", "case 'denovo':" in js)
check("denovo run route", "case 'denovorun':" in js)
check("renderDenovo defined", "function renderDenovo(" in js)
check("renderDenovoRun defined", "function renderDenovoRun(" in js)
# Reuse the existing chip so a matched peptide behaves like any other peptide in FRAN.
check("reuses pepChip", "pepChip(" in js)
check("hits the runs endpoint", "/api/denovo/runs" in js)
check("hits the peptides endpoint", "/api/denovo/run/" in js)
check("peptide page queries de novo", "/denovo`" in js)
# Length must appear wherever confidence does -- the raw score is length-confounded.
check("length shown alongside confidence", "conf_geomean" in js and "length" in js)
# The null must reach the screen: a candidate count without it overstates the result ~2x.
check("match_fdr surfaced in the UI", "match_fdr" in js)
# Ambiguous I/L matches must be visible, not silently collapsed to one.
check("I/L ambiguity shown", "n_candidates" in js)

# The file must still parse -- a syntax error here blanks the whole site.
node = subprocess.run(["node", "--check", JS], capture_output=True, text=True)
check("app.js parses", node.returncode == 0, node.stderr.strip()[:200])

html = open(os.path.join(HERE, "..", "app", "templates", "index.html")).read()
check("navbar has a De novo button", 'data-view="denovo"' in html)

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
