"""FRAN-QC: staff-only gating, db_health isolation, growth math, empty state. No DB or network needed.

Run:  python -m pytest -q tests/test_qc_page.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ.pop("DELIMP_INTERNAL_MODE", None)

import pytest
from fastapi.testclient import TestClient

from app import db, qc
from app.main import app

client = TestClient(app)
GIB = 1024 ** 3


def test_page_refuses_non_full_with_login_link():
    r = client.get("/qc")
    assert r.status_code == 403
    assert "/login?post=/qc" in r.text
    assert "no-store" in r.headers["cache-control"]


def test_api_refuses_non_full():
    assert client.get("/api/internal/qc").status_code == 404


def test_qc_tables_are_internal_only():
    assert {"delimp_qc_scan", "delimp_qc_event"} <= db._INTERNAL_TABLES
    assert not ({"delimp_qc_scan", "delimp_qc_event"} & db.PUBLIC_TABLES)
    with pytest.raises(db.GovernanceError):
        db._assert_allowlisted(["delimp_qc_scan"])   # public scope


def test_full_caller_gets_page_and_api(monkeypatch):
    monkeypatch.setattr(db, "INTERNAL_MODE", True)
    r = client.get("/qc")
    assert r.status_code == 200 and "FRAN-QC" in r.text and "no-store" in r.headers["cache-control"]
    assert "No scans yet" in r.text   # empty-state element ships in the page
    monkeypatch.setattr(qc, "qc_overview", lambda: {"latest": None, "history": [], "events": [], "growth": {}})
    monkeypatch.setattr(qc, "db_health", lambda: {"unavailable": {}})
    j = client.get("/api/internal/qc").json()
    assert set(j) == {"overview", "db_health"} and j["overview"]["latest"] is None


def test_overview_empty_state_when_tables_missing(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError('relation "delimp_qc_scan" does not exist')
    monkeypatch.setattr(db, "query", boom)
    ov = qc.qc_overview()
    assert ov["latest"] is None and ov["history"] == [] and ov["events"] == []
    assert "no scans yet" in ov["note"]


def test_growth_math():
    now = datetime(2026, 11, 13, tzinfo=timezone.utc)
    hist = [{"scanned_at": now - timedelta(days=10 - i), "size_bytes": (1000 + 10 * i) * GIB} for i in range(11)]
    g = qc.compute_growth(hist, now=now, disk=1600)
    assert g["gb_per_day"] == pytest.approx(10.0, rel=1e-6)
    free = 1600 * GIB - 1100 * GIB - 5 * GIB
    assert g["est_free_bytes"] == pytest.approx(free)
    assert g["days_to_full"] == pytest.approx(free / (10 * GIB), rel=1e-6)


def test_growth_edge_cases():
    now = datetime(2026, 11, 13, tzinfo=timezone.utc)
    assert qc.compute_growth([])["gb_per_day"] is None
    one = qc.compute_growth([{"scanned_at": now, "size_bytes": 5 * GIB}], now=now)
    assert one["gb_per_day"] is None and one["days_to_full"] is None
    shrink = [{"scanned_at": now - timedelta(days=2), "size_bytes": 900 * GIB},
              {"scanned_at": now, "size_bytes": 800 * GIB}]
    g = qc.compute_growth(shrink, now=now)
    assert g["gb_per_day"] < 0 and g["days_to_full"] is None
    # points older than the 14-day window are ignored
    old = [{"scanned_at": now - timedelta(days=40), "size_bytes": 1 * GIB},
           {"scanned_at": now - timedelta(days=1), "size_bytes": 900 * GIB},
           {"scanned_at": now, "size_bytes": 910 * GIB}]
    assert qc.compute_growth(old, now=now)["gb_per_day"] == pytest.approx(10.0, rel=1e-6)


class _FakeCur:
    """Fake cursor: raises for statements matching `fail`, otherwise returns one canned row."""
    def __init__(self, fail=()):
        self.fail, self.sql, self.description = fail, None, [("x",)]

    def execute(self, sql, params=None):
        if sql.lstrip().upper().startswith("SET"):
            return
        if any(f in sql for f in self.fail):
            raise RuntimeError("permission denied for view pg_stat_bgwriter")
        self.sql = sql

    def fetchall(self):
        return [(1,)]

    def close(self):
        pass


def _patch_cursor(monkeypatch, fail):
    from contextlib import contextmanager

    @contextmanager
    def cm():
        yield _FakeCur(fail)
    monkeypatch.setattr(qc, "_pg_cursor", cm)


def test_db_health_tolerates_failing_metrics(monkeypatch):
    _patch_cursor(monkeypatch, fail=("pg_locks", "pg_stat_checkpointer", "pg_stat_bgwriter", "autovacuum_freeze_max_age"))
    h = qc.db_health()
    assert h["waiting_locks"] is None and "locks" in h["unavailable"]
    assert h["checkpoints"] is None and "bgwriter" in h["unavailable"]
    assert "autovacuum_freeze_max_age" in h["unavailable"]
    assert h["relations"] == [{"x": 1}]   # unaffected metrics still returned


def test_db_health_all_fail_and_no_connection(monkeypatch):
    _patch_cursor(monkeypatch, fail=("SELECT",))
    h = qc.db_health()
    assert h["relations"] is None and len(h["unavailable"]) >= 8

    from contextlib import contextmanager

    @contextmanager
    def nocon():
        raise RuntimeError("no credential")
        yield
    monkeypatch.setattr(qc, "_pg_cursor", nocon)
    assert "connection" in qc.db_health()["unavailable"]
