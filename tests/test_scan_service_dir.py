"""The service-share walk: what counts as a run, and what is skipped.

Run:  python tests/test_scan_service_dir.py
"""
import os, sys, tempfile, pathlib
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingest"))
import scan_service_dir as sd                              # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)

check("win_path spells the R: drive",
      sd.win_path("on_campus/A/B") == r"R:\Data\lab\service\on_campus\A\B",
      sd.win_path("on_campus/A/B"))

with tempfile.TemporaryDirectory() as root:
    p = pathlib.Path(root)
    proj = p / "on_campus" / "SomeLab" / "proj1"
    proj.mkdir(parents=True)
    (proj / "run1.d").mkdir()
    (proj / "run1.d" / "analysis.tdf").write_text("x")     # must NOT be counted
    (proj / "run2.d").mkdir()
    (proj / "run3.raw").write_text("x")
    (proj / "notes.txt").write_text("x")                   # must NOT be counted
    check("count_runs counts .d dirs and .raw files only", sd.count_runs(str(proj)) == 3,
          str(sd.count_runs(str(proj))))

    # campus-level junk is skipped
    (p / "Thumbs.db").write_text("x")
    (p / "htrms_quarantine_20250916_134754").mkdir()
    empty = p / "off_campus" / "OtherLab" / "proj2"
    empty.mkdir(parents=True)

    rows = sd.walk_projects(str(root))
    folders = {r["service_folder"] for r in rows}
    check("finds the on_campus project", "on_campus/SomeLab/proj1" in folders, str(folders))
    check("finds the off_campus project", "off_campus/OtherLab/proj2" in folders, str(folders))
    check("skips Thumbs.db and quarantine dirs",
          not any("htrms_quarantine" in f or "Thumbs" in f for f in folders), str(folders))
    r1 = next(r for r in rows if r["service_folder"] == "on_campus/SomeLab/proj1")
    check("row carries campus", r1["campus"] == "on_campus", r1["campus"])
    check("row carries run_count", r1["run_count"] == 3, str(r1["run_count"]))
    check("a project with no runs is still inventoried",
          next(r for r in rows if r["service_folder"].endswith("proj2"))["run_count"] == 0)

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
