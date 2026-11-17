"""fill_diann_xic_lanes.plan(): when a search's output_dir is gone (staff renamed or moved the session
folder after ingest), the chromatograms are read from the drop-box entry whose manifest names that
output_dir. 2026-11-17: Cacho PROT_0835 (x2) and Loots MuCer18 sat in `no_output_dir` for exactly
this reason."""
import datetime
import json
import os
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ingest"))


class _Cur:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, *_a, **_k):
        pass

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    def set_session(self, **_k):
        pass

    def cursor(self):
        return _Cur(self.rows)

    def close(self):
        pass


def _touch(p, body="x"):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write(body)


class DropboxFallback(unittest.TestCase):
    def _plan(self, d, rows):
        sys.modules["coreomics_import"] = types.SimpleNamespace(_conn=lambda: _Conn(rows))
        os.environ["FRAN_XIC_FILL_DIR"] = os.path.join(d, "fill")
        import importlib
        import fill_diann_xic_lanes as f
        importlib.reload(f)
        f.DROP = os.path.join(d, "incoming")
        out, _n = f.plan(types.SimpleNamespace(max=0))
        with open(out) as fh:
            return json.load(fh)

    def tearDown(self):
        sys.modules.pop("coreomics_import", None)
        os.environ.pop("FRAN_XIC_FILL_DIR", None)

    def _entry(self, d, output_dir, n_runs, dead_links=0):
        ent = os.path.join(d, "incoming", "search__abcd1234")
        _touch(os.path.join(ent, "fran_manifest.json"), json.dumps({"output_dir": output_dir}))
        _touch(os.path.join(ent, "report.parquet"))
        os.makedirs(os.path.join(ent, "report_xic"), exist_ok=True)
        for i in range(n_runs):
            _touch(os.path.join(ent, "report_xic", f"run{i}.xic.parquet"))
        for i in range(dead_links):
            os.symlink(os.path.join(d, "gone", f"dead{i}.xic.parquet"),
                       os.path.join(ent, "report_xic", f"dead{i}.xic.parquet"))
        return ent

    def test_a_moved_search_is_read_from_its_drop_box_copy(self):
        with tempfile.TemporaryDirectory() as d:
            gone = os.path.join(d, "old", "sessions", "s1", "output", "search")
            ent = self._entry(d, gone, 3)
            p = self._plan(d, [("sid-1", "Moved", gone, 3, datetime.datetime(2026, 11, 13))])
            self.assertEqual(len(p["jobs"]), 1, p["skipped"])
            j = p["jobs"][0]
            self.assertEqual((j["dir"], j["via"], j["n_xic"]), (ent, "dropbox", 3))
            self.assertEqual(j["report"], os.path.join(ent, "report.parquet"))

    def test_no_drop_entry_is_still_no_output_dir(self):
        with tempfile.TemporaryDirectory() as d:
            gone = os.path.join(d, "old", "search")
            p = self._plan(d, [("sid-1", "Moved", gone, 3, None)])
            self.assertEqual(p["summary"]["skipped"], {"no_output_dir": 1})

    def test_dead_links_do_not_count_toward_coverage(self):
        """A drop entry whose report_xic/ is links into the moved folder holds nothing readable:
        counting the dead links would pass the 0.9..1.1 guard and fail inside the writer."""
        with tempfile.TemporaryDirectory() as d:
            gone = os.path.join(d, "old", "search")
            self._entry(d, gone, 0, dead_links=3)
            p = self._plan(d, [("sid-1", "Moved", gone, 3, None)])
            self.assertEqual(p["summary"]["skipped"], {"coverage_mismatch": 1})

    def test_an_existing_output_dir_still_wins(self):
        with tempfile.TemporaryDirectory() as d:
            od = os.path.join(d, "live", "search")
            _touch(os.path.join(od, "report.parquet"))
            for i in range(2):
                _touch(os.path.join(od, "xic", "t0_xic", f"run{i}.xic.parquet"))
            self._entry(d, od, 2)
            p = self._plan(d, [("sid-1", "Live", od, 2, None)])
            self.assertEqual((p["jobs"][0]["dir"], p["jobs"][0]["via"]), (od, "output_dir"))


if __name__ == "__main__":
    unittest.main()
