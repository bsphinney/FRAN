"""The folder inventory needs its own table, keyed on the folder.

delimp_submission_service_dir is one row per SUBMISSION and cannot be keyed on service_folder:
285 folders there map to more than one submission (one maps to eleven). This table is one row per
FOLDER, including folders no submission can be matched to.

Run:  python tests/test_service_dir_schema.py
"""
import os, sys
# The service-dir tables are in app/db.py's _INTERNAL_TABLES; the governed query layer refuses
# them unless the request is internal. Set this BEFORE importing app.db.
os.environ.setdefault("DELIMP_INTERNAL_MODE", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app.db import query                                  # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)

cols = {r["column_name"]: r for r in query(
    "SELECT column_name, is_nullable, data_type FROM information_schema.columns "
    "WHERE table_name='delimp_service_dir_inventory'",
    tables=["delimp_service_dir_inventory"])}
for c in ("service_folder", "service_folder_win", "campus", "run_count", "in_fran", "scanned_at"):
    check(f"{c} column exists", c in cols, sorted(cols))
check("run_count is nullable (NULL = could not be read)",
      cols.get("run_count", {}).get("is_nullable") == "YES",
      str(cols.get("run_count", {}).get("is_nullable")))

idx = query("SELECT indexdef FROM pg_indexes WHERE tablename='delimp_service_dir_inventory'",
            tables=["delimp_service_dir_inventory"])
defs = " ".join(r["indexdef"] for r in idx)
check("service_folder is the primary key", "UNIQUE" in defs and "service_folder" in defs, defs[:200])

# The existing table must be untouched — its consumers depend on it.
old = query("SELECT count(*) AS n FROM delimp_submission_service_dir",
            tables=["delimp_submission_service_dir"])
check("delimp_submission_service_dir still has its 1862 rows", old[0]["n"] == 1862, str(old[0]["n"]))
oldcols = {r["column_name"] for r in query(
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_name='delimp_submission_service_dir'",
    tables=["delimp_submission_service_dir"])}
check("delimp_submission_service_dir gained no columns",
      oldcols == {"submission_id", "service_folder", "service_folder_win", "campus", "in_fran",
                  "run_count", "match_confidence", "clue", "matched_by", "matched_at"},
      str(sorted(oldcols)))

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
