"""Version recording must work on every ingest host, Windows included.

versions.record() built the host column with os.uname(), which is POSIX-only. On Windows it raised
AttributeError, record_run() swallowed it by design ("version tracking must not be able to fail an
ingest"), and the run went unrecorded. Measured 2026-09-17 on win-2: every ingest printed "could not
record corpus_ingest 1.4.0: module 'os' has no attribute 'uname'" while succeeding otherwise, so
delimp_component_version -- the table that answers "what code has touched this corpus?" -- had no
row for any Windows ingest.
"""
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_INGEST = os.path.join(_HERE, "..", "ingest")
sys.path.insert(0, _INGEST)


def _versions():
    spec = importlib.util.spec_from_file_location("versions", os.path.join(_INGEST, "versions.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _Cur:
    """Minimal cursor: remembers the parameters of the INSERT."""

    def __init__(self):
        self.params = None

    def execute(self, sql, params=None):
        if params is not None and "delimp_component_version" in sql and "INSERT" in sql:
            self.params = params


def test_hostname_is_never_empty_on_this_platform():
    h = _versions().hostname()
    assert isinstance(h, str) and h and len(h) <= 64


def test_record_writes_a_host_without_os_uname():
    # The regression: this raised AttributeError on Windows before hostname() existed.
    v = _versions()
    cur = _Cur()
    v.record(cur, "corpus_ingest", "1.4.0", notes="unit test")
    assert cur.params is not None, "no INSERT into delimp_component_version"
    component, version, _git, host, notes = cur.params
    assert (component, version, notes) == ("corpus_ingest", "1.4.0", "unit test")
    assert host and host != "unknown"


def test_record_run_reports_success():
    v = _versions()
    assert v.record_run(_Cur(), "corpus_ingest", "1.4.0") is True


def test_no_posix_only_uname_left_in_ingest():
    # auto_ingest.py called os.uname() twice as well; on Windows it would crash outright.
    hits = []
    for name in sorted(os.listdir(_INGEST)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(_INGEST, name), encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, 1):
                if "os.uname(" in line:
                    hits.append(f"{name}:{i}")
    assert not hits, f"os.uname() is POSIX-only; use versions.hostname(): {hits}"
