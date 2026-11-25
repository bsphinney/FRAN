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
