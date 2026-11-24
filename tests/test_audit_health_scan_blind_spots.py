"""The four health-scan checks added after the 2026-11 data audit, against a fake cursor (no DB).

Each must turn RED on the failure the audit found, stay quiet on healthy data, and never read a
huge table without a search_id scope.
"""
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_INGEST = os.path.join(_HERE, "..", "ingest")
sys.path.insert(0, _INGEST)


def _scan():
    spec = importlib.util.spec_from_file_location("fran_health_scan", os.path.join(_INGEST, "fran_health_scan.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.findings.clear()
    return m


class Cur:
    """Routes each statement to a canned result by a keyword it contains; records every statement."""

    def __init__(self, routes):
        self.routes, self.log, self._r = routes, [], []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.log.append(s)
        for key, res in self.routes:
            if key in s:
                self._r = list(res(params) if callable(res) else res)
                return
        raise AssertionError("unrouted SQL: " + s[:120])

    def fetchall(self):
        return self._r

    def fetchone(self):
        return self._r[0]


def _sev(m, area):
    return sorted({f[0] for f in m.findings if f[1] == area})


def test_empty_provenance_is_red_by_engine():
    m = _scan()
    c = Cur([("FROM delimp_searches GROUP BY 1", [("spectronaut", 100, 1.0, 1.0, 2222, 1.0, 1.0),
                                                   ("diann", 40, 1.0, 0.5, 1224, 1.0, 0.998)])])
    m.check_search_provenance(c)
    assert _sev(m, "provenance") == ["AMBER", "RED"]   # diann completed_at is only 50% null -> AMBER
    assert any("spectronaut" in f[2] and "search_params_json" in f[2] for f in m.findings)


def test_filled_provenance_is_quiet():
    m = _scan()
    m.check_search_provenance(Cur([("FROM delimp_searches GROUP BY 1", [("diann", 40, 0.0, 0.0, 1224, 0.0, 0.0)])]))
    assert not m.findings


def _junk_cur(prec_rows, n_prot):
    return Cur([("FROM delimp_searches ORDER BY", [("sid1", "diann", "a"), ("sid2", "spectronaut", "b")]),
                ("FROM delimp_precursors", prec_rows),
                ("FROM delimp_proteins", [(n_prot,)])])


def test_literal_nan_protein_group_is_red_and_scoped_by_search():
    m = _scan()
    c = _junk_cur([("sid2", 80729)], 332)
    m.check_junk_protein_group(c)
    assert _sev(m, "junk text") == ["RED"]
    assert all("search_id = ANY" in s for s in c.log if "FROM delimp_precursors" in s or "FROM delimp_proteins" in s)


def test_clean_protein_groups_are_ok():
    m = _scan()
    m.check_junk_protein_group(_junk_cur([], 0))
    assert _sev(m, "junk text") == ["ok"]


def _raw_cur(n_dis, n_dis_new, contra):
    return Cur([("count(DISTINCT platform)", [(n_dis, n_dis_new)]),
                ("FROM raw_files WHERE", [contra])])


def test_new_platform_disagreement_is_red_legacy_is_amber():
    m = _scan(); m.check_raw_file_consistency(_raw_cur(370, 2, (0, 0)))
    assert "RED" in _sev(m, "raw_files")
    m = _scan(); m.check_raw_file_consistency(_raw_cur(370, 0, (0, 0)))
    assert _sev(m, "raw_files") == ["AMBER"]


def test_new_extension_contradiction_is_red():
    m = _scan(); m.check_raw_file_consistency(_raw_cur(0, 0, (92, 3)))
    assert "RED" in _sev(m, "raw_files")
    m = _scan(); m.check_raw_file_consistency(_raw_cur(0, 0, (0, 0)))
    assert not m.findings


def _fill_cur(drop=None):
    cols = {e: list(f) for e, f in _scan().PREC_FILL_FLOORS.items()}

    def prec(params):
        return [(20000,) + tuple(0.0 if (drop and c == drop) else 1.0 for c in cols[prec.eng])]

    def ids(params):
        prec.eng = params[0]
        return [(f"s{i}",) for i in range(10)]
    return Cur([("SELECT id FROM delimp_searches", ids), ("FROM delimp_precursors", prec)])


def test_a_column_that_stopped_being_written_turns_red():
    m = _scan(); c = _fill_cur(drop="ms1_area"); m.check_precursor_fill_by_engine(c)
    red = [f for f in m.findings if f[0] == "RED"]
    assert red and "ms1_area" in red[0][2] and red[0][2].startswith("diann")
    assert all("search_id = %s LIMIT" in s for s in c.log if "FROM delimp_precursors" in s)


def test_healthy_fill_is_ok_for_every_engine():
    m = _scan(); m.check_precursor_fill_by_engine(_fill_cur())
    assert [f[0] for f in m.findings] == ["ok", "ok"]


if __name__ == "__main__":
    fails = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f(); print("PASS", n)
            except Exception as e:  # noqa: BLE001
                fails += 1; print("FAIL", n, type(e).__name__, e)
    sys.exit(1 if fails else 0)
