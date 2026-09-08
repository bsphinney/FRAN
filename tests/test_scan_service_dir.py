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

    # Symlink handling
    # Symlinked .d inside a project is counted
    (proj / "run4.d").symlink_to(proj / "run1.d")
    check("symlinked .d inside a project IS counted",
          sd.count_runs(str(proj)) == 4, str(sd.count_runs(str(proj))))

    # Symlinked project directory is inventoried
    link_proj = p / "on_campus" / "SomeLab" / "proj_link"
    link_proj.symlink_to(proj)
    rows = sd.walk_projects(str(root))
    folders = {r["service_folder"] for r in rows}
    check("symlinked project directory IS inventoried",
          "on_campus/SomeLab/proj_link" in folders, str(folders))

    # Symlink loop: on_campus/loop -> root should not cause junk entries
    loop = p / "on_campus" / "loop"
    loop.symlink_to(p)
    rows = sd.walk_projects(str(root))
    all_folders = {r["service_folder"] for r in rows}
    expected = {"on_campus/SomeLab/proj1", "on_campus/SomeLab/proj_link", "off_campus/OtherLab/proj2"}
    check("ancestor symlink produces no junk rows", all_folders == expected,
          f"unexpected {sorted(all_folders - expected)}, missing {sorted(expected - all_folders)}")

    # Uppercase extensions are counted
    (proj / "run5.D").mkdir()
    (proj / "run6.RAW").write_text("x")
    check(".D and .RAW uppercase are counted",
          sd.count_runs(str(proj)) == 6, str(sd.count_runs(str(proj))))

    # Unreadable directory handling (only on platforms where chmod affects the current user)
    unreadable_proj = p / "on_campus" / "RestrictedLab" / "restricted"
    unreadable_proj.mkdir(parents=True)
    (unreadable_proj / "run_hidden.d").mkdir()
    unreadable_proj_str = str(unreadable_proj)
    try:
        os.chmod(unreadable_proj_str, 0o000)
        rows = sd.walk_projects(str(root))
        unreadable = sd.get_unreadable_paths()
        # On some systems (e.g., running as root), chmod may not restrict access
        if os.access(unreadable_proj_str, os.R_OK):
            check("unreadable directory check skipped (running as root or similar)",
                  True, "")
        else:
            check("unreadable directory is reported as unreadable",
                  any("restricted" in u for u in unreadable), str(unreadable))
            check("unreadable directory has None run_count, not 0",
                  any(r["service_folder"] == "on_campus/RestrictedLab/restricted" and r["run_count"] is None
                      for r in rows),
                  str([r for r in rows if "restricted" in r["service_folder"]]))
    finally:
        os.chmod(unreadable_proj_str, 0o755)

# --- in_fran resolution -------------------------------------------------------
rows = [
    {"service_folder": "on_campus/SomeLab/proj1", "campus": "on_campus"},
    {"service_folder": "off_campus/OtherLab/proj2", "campus": "off_campus"},
]
sd.mark_in_fran(rows, {"on_campus/SomeLab/proj1"})
check("in_fran true when the folder has a search", rows[0]["in_fran"] is True)
check("in_fran false otherwise", rows[1]["in_fran"] is False)

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
