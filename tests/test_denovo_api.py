"""The de novo routes exist and delegate to app.denovo.

Run:  python tests/test_denovo_api.py     (no pytest needed)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


from app.main import app  # noqa: E402

paths = {r.path for r in app.routes}
check("/api/denovo/runs registered", "/api/denovo/runs" in paths)
check("run detail registered", "/api/denovo/run/{run_id}" in paths)
check("run peptides registered", "/api/denovo/run/{run_id}/peptides" in paths)
check("peptide denovo registered", "/api/peptide/{stripped_seq}/denovo" in paths)

src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "main.py")).read()
check("delegates to app.denovo",
      "denovo.list_runs" in src and "denovo.run_peptides" in src and "denovo.peptide_denovo" in src)
check("denovo module imported", "denovo" in src.split("\n")[20] or "import denovo" in src
      or ", denovo" in src)

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
