"""sne_one.py archives its report on Flinders; find_missed_sne.py lists experiments FRAN lacks."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingest"))
import find_missed_sne as fm  # noqa: E402
import sne_one  # noqa: E402


# ------------------------------------------------------------------ sne_one: the report archive
def _export(tmp_path, body=b"x" * 1000):
    d = tmp_path / "c_out" / "20261125_101010_MyExp"
    d.mkdir(parents=True)
    rep = d / "20261125_101010_MyExp_Report_FRAN (Normal).tsv"
    rep.write_bytes(body)
    (d / "RunOverview.tsv").write_bytes(b"side")
    return str(rep)


def test_stem_is_the_sne_file_name():
    assert sne_one.sne_stem(r"B:\proj\x\20250101_010101_My Exp.sne") == "20250101_010101_My Exp"
    assert sne_one.sne_stem(r"B:\proj\My Exp\.sne") == "My Exp"
    assert sne_one.sne_stem("/a/b/Thing.SNE") == "Thing"


def test_copy_lands_where_hive_looks_and_leaves_no_part(tmp_path):
    rep = _export(tmp_path)
    root = tmp_path / "FRAN_reports"; root.mkdir()
    sne = r"B:\proj\My Exp.sne"
    assert not sne_one.flinders_has_report(sne, str(root))
    dst = sne_one.copy_report_to_flinders(rep, sne, str(root))
    assert dst == str(root / "My Exp" / "20261125_101010_MyExp" / os.path.basename(rep))
    assert os.path.getsize(dst) == 1000
    assert sorted(os.listdir(os.path.dirname(dst))) == sorted(
        ["RunOverview.tsv", os.path.basename(rep)])
    assert sne_one.flinders_has_report(sne, str(root))


def test_part_file_and_empty_report_do_not_count(tmp_path):
    root = tmp_path / "FRAN_reports"
    d = root / "My Exp" / "ts"; d.mkdir(parents=True)
    (d / "x_Report_FRAN (Normal).tsv.part").write_bytes(b"half")
    (d / "y_Report_FRAN (Normal).tsv").write_bytes(b"")
    assert not sne_one.flinders_has_report(r"B:\p\My Exp.sne", str(root))
    assert not sne_one.is_report("x_Report_FRAN (Normal).tsv.part")
    assert sne_one.is_report("x_Report_FRAN (Normal).tsv")


def test_copy_is_resumable_and_report_goes_last(tmp_path, monkeypatch):
    rep = _export(tmp_path)
    root = tmp_path / "FRAN_reports"; root.mkdir()
    order = []
    real = sne_one.shutil.copyfile
    monkeypatch.setattr(sne_one.shutil, "copyfile", lambda s, d: (order.append(os.path.basename(s)), real(s, d))[1])
    sne_one.copy_report_to_flinders(rep, "X.sne", str(root))
    assert order[-1] == os.path.basename(rep)
    order.clear()
    sne_one.copy_report_to_flinders(rep, "X.sne", str(root))      # everything already there
    assert order == []


def test_short_copy_raises_and_report_is_not_published(tmp_path, monkeypatch):
    rep = _export(tmp_path)
    root = tmp_path / "FRAN_reports"; root.mkdir()

    def short(src, dst):
        with open(dst, "wb") as fh:
            fh.write(b"xx")
    monkeypatch.setattr(sne_one.shutil, "copyfile", short)
    with pytest.raises(OSError):
        sne_one.copy_report_to_flinders(rep, "X.sne", str(root))
    assert not sne_one.flinders_has_report("X.sne", str(root))


def test_same_rule_as_the_hive_lane_job():
    """fill_spectrum_lanes.is_report is what decides whether Hive sees the report."""
    for fn in ("a_Report_FRAN (Normal).tsv", "a_Report_FRAN (Normal).parquet", "RunOverview.tsv",
               "a_Report_FRAN (Normal).tsv.part", "REPORT_fran.TSV"):
        f = fn.lower()
        assert sne_one.is_report(fn) == (f.endswith((".parquet", ".tsv")) and "report_fran" in f)


# ------------------------------------------------------------------ find_missed_sne
def _walk(tree):
    """A fake os.walk over {dirpath: (dirnames, filenames)} that honours in-place pruning."""
    def walk(root):
        stack = [root]
        while stack:
            d = stack.pop()
            dirs, files = tree.get(d, ([], []))
            dirs = list(dirs)
            yield d, dirs, list(files)
            stack += [os.path.join(d, x) for x in dirs]
    return walk


def test_finds_files_dirs_and_dot_sne_and_skips_raw_and_system():
    j = os.path.join
    tree = {
        "B:": (["proj", "Windows", "run1.d", "$Recycle.Bin"], ["Top.sne"]),
        j("B:", "proj"): (["Exp2.sne", "Exp3"], ["Exp1.sne", "notes.txt"]),
        j("B:", "proj", "Exp2.sne"): ([], ["inner.sne"]),            # never entered
        j("B:", "proj", "Exp3"): ([".sne"], []),
        j("B:", "Windows"): ([], ["sys.sne"]),
        j("B:", "run1.d"): ([], ["x.sne"]),
        j("B:", "$Recycle.Bin"): ([], ["dead.sne"]),
    }
    found, _ = fm.find_sne(["B:"], walk=_walk(tree))
    assert sorted(s for _, s in found) == ["Exp1", "Exp2", "Exp3", "Top"]
    assert dict((s, p) for p, s in found)["Exp3"] == j("B:", "proj", "Exp3")


def _item(path, stem, size=10, mtime=1.0):
    return {"path": path, "stem": stem, "bytes": size, "mtime": mtime}


def _known(rows):
    """known_keys() over (output_dir, search_name) rows, without a database."""
    class Cur:
        def execute(self, *_): pass
        def fetchall(self): return rows
    class Conn:
        def cursor(self): return Cur()
    return fm.known_keys(Conn())


def test_classify_path_name_missed_copy_and_too_new():
    paths, names, bases = _known([
        (r"B:\proj\20250101_101010_Done.sne", "20250101_101010_Done"),
        (r"R:\Data\lab\Elsewhere\OnShare.sne", "OnShare"),
        ("/nfs/lssc0/flinders/proteomics/Data/FRAN_reports/ReExp/20260101_010101_ReExp", "ReExp"),
    ])
    now = 10_000_000.0
    old = now - 86400
    items = [
        _item(r"B:\proj\20250101_101010_Done.sne", "20250101_101010_Done", mtime=old),
        _item(r"S:\sne_storage\OnShare.sne", "OnShare", mtime=old),            # other path, same name
        _item(r"D:\x\20240303_030303_ReExp.sne", "20240303_030303_ReExp", mtime=old),  # name w/o timestamp
        _item(r"S:\sne_storage\New1.sne", "New1", size=500, mtime=old),
        _item(r"B:\proj\New1.sne", "New1", size=500, mtime=old),               # the canonical copy
        _item(r"D:\x\New1.sne", "New1", size=777, mtime=old),                  # same name, other size
        _item(r"B:\proj\Fresh.sne", "Fresh", mtime=now - 60),
    ]
    out = {it["path"]: it for it in fm.classify(items, paths, names, bases, now=now)}
    assert out[r"B:\proj\20250101_101010_Done.sne"]["status"] == "INGESTED"
    assert out[r"S:\sne_storage\OnShare.sne"]["status"] == "SAME_NAME"
    assert out[r"D:\x\20240303_030303_ReExp.sne"]["status"] == "SAME_NAME"
    assert out[r"B:\proj\New1.sne"]["status"] == "MISSED"
    assert out[r"B:\proj\New1.sne"]["n_copies"] == 2
    assert out[r"S:\sne_storage\New1.sne"]["status"] == "COPY"
    assert out[r"S:\sne_storage\New1.sne"]["matched_by"] == r"B:\proj\New1.sne"
    assert out[r"D:\x\New1.sne"]["status"] == "MISSED"          # different size: not a copy
    assert out[r"B:\proj\Fresh.sne"]["status"] == "TOO_NEW"
    assert [it["status"] for it in fm.classify(items, paths, names, bases, now=now)][0] == "MISSED"


def test_outputs_list_everything_but_command_only_missed(tmp_path):
    items = [dict(_item(r"B:\p\New One.sne", "New One", size=2_000_000_000), status="MISSED", matched_by="", n_copies=2, xic=True),
             dict(_item(r"S:\s\New One.sne", "New One", size=2_000_000_000), status="COPY", matched_by=r"B:\p\New One.sne", n_copies=2),
             dict(_item(r"S:\s\Old.sne", "Old"), status="SAME_NAME", matched_by="name", n_copies=1)]
    tsv, cmd = fm.write_outputs(items, str(tmp_path / "o"), "HOST", "20261125", r"K:\Data\FRAN_SNE_export\sne_one.py")
    rows = open(tsv, encoding="utf-8").read().splitlines()
    assert len(rows) == 4 and rows[1].split("\t")[:4] == ["MISSED", r"B:\p\New One.sne", "New One", "2.00"]
    lines = [l for l in open(cmd, encoding="utf-8").read().splitlines() if l.startswith("py ")]
    assert lines == [r'py "K:\Data\FRAN_SNE_export\sne_one.py" --sne "B:\p\New One.sne" --name="New One"']


def test_cmd_line_survives_percent_and_leading_dash():
    line = fm.command_for({"path": r"B:\p\100%_x (a&b).sne", "stem": "-100%_x (a&b)"}, r"K:\s\sne_one.py")
    assert line == r'py "K:\s\sne_one.py" --sne "B:\p\100%%_x (a&b).sne" --name="-100%%_x (a&b)"'


def test_xic_dbs_beside_a_windows_path():
    seen = {}
    def listdir(d):
        seen["d"] = d
        return ["Exp_XIC-DBs", "other"]
    assert fm.has_xic_dbs(r"B:\proj\Exp.sne", listdir=listdir, isdir=lambda p: True)
    assert seen["d"] == r"B:\proj"


def test_dot_sne_directory_is_known_by_its_folder():
    paths, names, bases = _known([(r"B:\proj\My Exp\.sne", "Custom Name")])
    assert "my exp" in names
    out = fm.classify([_item(r"S:\copy\My Exp.sne", "My Exp")], paths, names, bases, now=1e9)
    assert out[0]["status"] == "SAME_NAME"


# ------------------------------------------------------------------ sne_one: review fixes
def test_stem_never_raises_index_error():
    with pytest.raises(ValueError):
        sne_one.sne_stem(".sne")
    with pytest.raises(ValueError):
        sne_one.sne_stem(r"x:\.sne")


def test_export_dir_is_unique_per_sne_even_with_a_shared_long_name():
    long = "A" * 90
    a = sne_one.export_dir(long + "_one", r"B:\p\one.sne", "C:/o")
    b = sne_one.export_dir(long + "_two", r"B:\p\two.sne", "C:/o")
    assert a != b
    assert sne_one.export_dir("n", r"B:\P\One.sne", "C:/o") == sne_one.export_dir("n", r"b:\p\one.sne", "C:/o")


def test_pick_report_ignores_an_older_export(tmp_path):
    old = tmp_path / "20260101_000000_x"; old.mkdir()
    new = tmp_path / "20261125_000000_x"; new.mkdir()
    (old / "x_Report_FRAN (Normal).tsv").write_bytes(b"o" * 5000)       # bigger, but stale
    (new / "x_Report_FRAN (Normal).tsv").write_bytes(b"n" * 10)
    t0 = 2_000_000_000.0
    os.utime(old / "x_Report_FRAN (Normal).tsv", (t0 - 9999, t0 - 9999))
    os.utime(new / "x_Report_FRAN (Normal).tsv", (t0 + 60, t0 + 60))
    assert sne_one.pick_report(str(tmp_path), t0) == str(new / "x_Report_FRAN (Normal).tsv")
    assert sne_one.pick_report(str(tmp_path), t0 + 9999) is None


def test_known_elsewhere_by_name_not_by_this_path():
    rows = [("s1", r"S:\sne_storage\20250101_010101_My Exp.sne", "whatever"),
            ("s2", r"B:\proj\Other.sne", "Other"),
            ("s3", r"D:\x\Folder Exp\.sne", "Renamed")]
    assert sne_one.find_known_elsewhere(r"B:\proj\My Exp.sne", rows)[0] == "s1"
    assert sne_one.find_known_elsewhere(r"B:\copy\Folder Exp.sne", rows)[0] == "s3"
    assert sne_one.find_known_elsewhere(r"B:\proj\Other.sne", rows) is None      # its own row
    assert sne_one.find_known_elsewhere(r"B:\proj\Brand New.sne", rows) is None


def test_local_export_removed_only_inside_its_own_folder(tmp_path):
    base = tmp_path / "fran_sne_one"
    out = base / "exp_abcd1234"; (out / "20261125_x").mkdir(parents=True)
    (out / "20261125_x" / "x_Report_FRAN (Normal).tsv").write_bytes(b"x" * 2000)
    (out / "manageSNE.log").write_text("log")
    assert sne_one.remove_local_export(str(out), str(base)) == pytest.approx(2e-6)
    assert sorted(os.listdir(out)) == ["manageSNE.log"]
    other = tmp_path / "elsewhere"; (other / "d").mkdir(parents=True); (other / "d" / "f").write_text("keep")
    assert sne_one.remove_local_export(str(other), str(base)) == 0.0          # not under the base
    assert sne_one.remove_local_export(str(base), str(base)) == 0.0           # the base itself
    assert (other / "d" / "f").exists() and out.exists()


def test_free_gb_of_a_folder_that_does_not_exist_yet(tmp_path):
    assert sne_one.free_gb(str(tmp_path / "not" / "yet")) > 0


def test_node_report_appends_per_host_and_never_raises(tmp_path):
    import node_report
    p = node_report.report("preflight", "a\nb", str(tmp_path))
    node_report.report("preflight", "c", str(tmp_path))
    lines = open(p, encoding="utf-8").read().splitlines()
    assert [l[20:] for l in lines] == ["a", "b", "c"]
    assert os.path.dirname(p) == os.path.join(str(tmp_path), "node_reports", node_report.host())
    assert node_report.report("x", "y", os.path.join(str(tmp_path), "file-not-dir", "\0")) is None


# ------------------------------------------------------------------ find_missed_sne: QC / lists
def test_qc_and_test_names_are_listed_not_ingested_unless_included(tmp_path):
    paths, names, bases = _known([])
    items = [_item(r"B:\p\Lumos QC 2025.sne", "Lumos QC 2025"),
             _item(r"B:\p\method test\RealName.sne", "RealName"),
             _item(r"B:\p\Protest_march.sne", "Protest_march"),          # 'test' inside a word
             _item(r"B:\p\Kept Test.sne", "Kept Test"),
             _item(r"B:\p\Dropped.sne", "Dropped")]
    out = {it["stem"]: it["status"] for it in fm.classify(
        items, paths, names, bases, now=1e9, include={"kept test"}, exclude={r"b:\p\dropped.sne"})}
    assert out == {"Lumos QC 2025": "QC_OR_TEST", "RealName": "QC_OR_TEST", "Protest_march": "MISSED",
                   "Kept Test": "MISSED", "Dropped": "EXCLUDED"}


def test_read_list_skips_comments_and_missing_file(tmp_path):
    p = tmp_path / "exclude.txt"
    p.write_text('# no\n\n"B:\\p\\X.sne"\nSome Name\n', encoding="utf-8")
    assert fm.read_list(str(p)) == {"b:\\p\\x.sne", "some name"}
    assert fm.read_list(str(tmp_path / "none.txt")) == set()


# ------------------------------------------------------------------ win_ingest_all
import win_ingest_all as wa  # noqa: E402


def _todo(*sizes):
    return [dict(_item(rf"B:\p\E{i}.sne", f"E{i}", size=s), status="MISSED") for i, s in enumerate(sizes)]


def test_plan_is_missed_only_smallest_first_with_limit():
    items = _todo(30, 10, 20) + [dict(_item(r"B:\p\Q.sne", "Q", size=1), status="QC_OR_TEST"),
                                 dict(_item(r"B:\p\S.sne", "S", size=1), status="SAME_NAME")]
    assert [it["stem"] for it in wa.plan(items)] == ["E1", "E2", "E0"]
    assert [it["stem"] for it in wa.plan(items, 2)] == ["E1", "E2"]


def _run(codes):
    seq = iter(codes)
    return wa.run_all(_todo(*range(1, len(codes) + 1)), lambda it: next(seq), say=lambda m: None, status=lambda **k: None)


def test_first_failure_stops_everything():
    res, stop = _run([1, 0, 0])
    assert len(res) == 1 and "first experiment failed" in stop


def test_one_failure_in_the_middle_is_skipped_over():
    res, stop = _run([0, 2, 0, 0])
    assert [r["rc"] for r in res] == [0, 2, 0, 0] and stop is None


def test_three_failures_in_a_row_stop_the_run():
    res, stop = _run([0, 1, 1, 1, 0])
    assert len(res) == 4 and "in a row" in stop


def test_full_drive_or_lost_share_stops_at_once():
    res, stop = _run([0, 6, 0])
    assert len(res) == 2 and "full" in stop
    res, stop = _run([0, 0, 5, 0])
    assert len(res) == 3 and "share" in stop


def test_lock_is_taken_once_and_a_dead_owner_is_replaced(tmp_path):
    p = str(tmp_path / "d" / "x.lock")
    assert wa.take_lock(p) and wa.take_lock(p)              # our own pid: still ours
    open(p, "w").write("999999999")                         # a process that does not exist
    assert wa.take_lock(p)
    import subprocess as sp, sys as _s
    child = sp.Popen([_s.executable, "-c", "import time; time.sleep(30)"])
    try:
        open(p, "w").write(str(child.pid))
        assert not wa.take_lock(p)                          # a live owner keeps it
    finally:
        child.kill(); child.wait()


# ------------------------------------------------------------------ sne_one: XIC dbs to Flinders
def test_xic_dbs_archived_beside_the_report_complete_marker_last(tmp_path):
    rep = _export(tmp_path)
    root = tmp_path / "FRAN_reports"; root.mkdir()
    sne = r"B:\proj\My Exp.sne"
    dst = sne_one.copy_report_to_flinders(rep, sne, str(root))
    assert sne_one.flinders_report_path(sne, str(root)) == dst
    xd = tmp_path / "My_XIC-DBs"; xd.mkdir()
    (xd / "run1.xic.db").write_bytes(b"a" * 300); (xd / "run2.xic.db").write_bytes(b"b" * 500)
    (xd / "._run1.xic.db").write_bytes(b"stub"); (xd / "notes.txt").write_text("no")
    assert not sne_one.flinders_has_xic(sne, str(root))
    n, gb = sne_one.copy_xic_dbs_to_flinders(str(xd), dst)
    d = os.path.join(os.path.dirname(dst), "xic_dbs")
    assert n == 2 and sorted(os.listdir(d)) == ["COMPLETE.txt", "run1.xic.db", "run2.xic.db"]
    assert open(os.path.join(d, "COMPLETE.txt")).read().startswith("2 files\n800 bytes")
    assert sne_one.flinders_has_xic(sne, str(root))


def test_xic_copy_failure_leaves_no_complete_marker(tmp_path, monkeypatch):
    rep = _export(tmp_path)
    root = tmp_path / "FRAN_reports"; root.mkdir()
    dst = sne_one.copy_report_to_flinders(rep, "X.sne", str(root))
    xd = tmp_path / "x"; xd.mkdir(); (xd / "r.xic.db").write_bytes(b"a" * 300)
    monkeypatch.setattr(sne_one.shutil, "copyfile", lambda s, d: open(d, "wb").write(b"zz"))
    with pytest.raises(OSError):
        sne_one.copy_xic_dbs_to_flinders(str(xd), dst)
    assert not sne_one.flinders_has_xic("X.sne", str(root))
    empty = tmp_path / "e"; empty.mkdir()
    with pytest.raises(OSError):
        sne_one.copy_xic_dbs_to_flinders(str(empty), dst)


def test_share_options_file_only_passes_known_flags(tmp_path):
    p = tmp_path / "win_ingest_options.txt"
    p.write_text("# trial\n--no-export-xics\n--force-ingest\nrm -rf\n", encoding="utf-8")
    assert wa.extra_options(str(p)) == ["--no-export-xics"]
    assert wa.extra_options(str(tmp_path / "none.txt")) == []


def test_export_limit_scales_with_the_experiment():
    assert sne_one.export_timeout_min(0) == 40
    assert sne_one.export_timeout_min(2) == 100
    assert sne_one.export_timeout_min(16) == 520
    assert sne_one.export_timeout_min(500) == 720
    assert sne_one.export_timeout_min(500, override=30) == 30


def test_sne_gb_of_a_file_a_folder_and_nothing(tmp_path):
    f = tmp_path / "a.sne"; f.write_bytes(b"x" * 3000)
    d = tmp_path / "b.sne"; (d / "sub").mkdir(parents=True); (d / "sub" / "p").write_bytes(b"x" * 5000)
    assert sne_one.sne_gb(str(f)) == pytest.approx(3e-6)
    assert sne_one.sne_gb(str(d)) == pytest.approx(5e-6)
    assert sne_one.sne_gb(str(tmp_path / "none.sne")) == 0.0


def test_timeouts_at_the_end_do_not_stop_the_run():
    res, stop = _run([0, 0, 7, 7, 7, 0])
    assert len(res) == 6 and stop is None
    res, stop = _run([7, 0])                       # nothing has worked yet: still stop
    assert len(res) == 1 and stop


def test_watch_export_kills_on_timeout_and_on_a_full_disk():
    class P:
        def __init__(self, ends_after): self.n, self.ends, self.killed, self.returncode = 0, ends_after, False, None
        def poll(self):
            self.n += 1
            if self.n > self.ends: self.returncode = 0; return 0
            return None
        def kill(self): self.killed = True
        def wait(self, timeout=None): return 0
    tick = iter(range(0, 100000, 30))
    clock = lambda: next(tick)
    p = P(3); assert sne_one.watch_export(p, 600, "x", 15, free=lambda o: 100, clock=clock, sleep=lambda s: None) is None and not p.killed
    p = P(10**6); assert sne_one.watch_export(p, 600, "x", 15, free=lambda o: 100, clock=clock, sleep=lambda s: None) == "timeout" and p.killed
    p = P(10**6); assert sne_one.watch_export(p, 10**9, "x", 15, free=lambda o: 5, clock=clock, sleep=lambda s: None) == "disk" and p.killed


def test_backfill_plan_takes_known_experiments_once_each_smallest_first():
    items = [dict(status="INGESTED", stem="A", bytes=500, path="B:/A.sne"),
             dict(status="SAME_NAME", stem="a", bytes=10, path="S:/A.sne"),
             dict(status="SAME_NAME", stem="B", bytes=20, path="S:/B.sne"),
             dict(status="MISSED", stem="C", bytes=1, path="S:/C.sne"),
             dict(status="QC_OR_TEST", stem="D", bytes=1, path="S:/D.sne")]
    got = wa.backfill_plan(items)
    assert [(i["stem"], i["path"]) for i in got] == [("B", "S:/B.sne"), ("A", "B:/A.sne")]


def test_same_path_is_found_whatever_the_case_or_slashes():
    rows = [("s1", r"b:\Proj\My Exp.sne", "x"), ("s2", None, "y")]
    assert sne_one.find_same_path(r"B:/proj/my exp.sne", rows) == "s1"
    assert sne_one.find_same_path(r"B:\proj\other.sne", rows) is None


def test_has_xic_sees_a_report_archived_flat(tmp_path):
    root = tmp_path / "FRAN_reports"; d = root / "My Exp" / "xic_dbs"; d.mkdir(parents=True)
    assert not sne_one.flinders_has_xic("My Exp.sne", str(root))
    (d / "COMPLETE.txt").write_text("1 files")
    assert sne_one.flinders_has_xic("My Exp.sne", str(root))


def test_four_timeouts_in_a_row_stop_the_run_and_backfill_misses_do_not():
    res, stop = _run([0, 7, 7, 7, 7, 0])
    assert len(res) == 5 and "time limit" in stop
    res, stop = _run([8, 4, 0, 0])                 # first results say nothing about the setup
    assert len(res) == 4 and stop is None
