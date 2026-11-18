"""fran_fill.engines_confirming: rounds of searches, each round's basenames de-duplicated and spread
over several connections; the mark only advances past a round that finished. 2026-11-18 the serial
form did 43 searches in 4 h against ~35 new a day (8.8 days behind)."""
import datetime
import os
import sys
import tempfile
import threading
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ingest"))


class Deadlock(Exception):
    pass


class _Cur:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _Cur(self.rows)

    def rollback(self):
        pass

    def close(self):
        pass


def ts(h):
    return datetime.datetime(2026, 11, 10, h, tzinfo=datetime.timezone.utc)


class Parallel(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.calls, self.lock = [], threading.Lock()
        self.fail_on = set()

        def refresh_for(conn, names, verbose=True):
            with self.lock:
                self.calls.append((threading.get_ident(), list(names)))
            if self.fail_on & set(names):
                raise RuntimeError("boom")
            return len(names)

        sys.modules["backfill_engines_confirming"] = types.SimpleNamespace(refresh_for=refresh_for)
        sys.modules["psycopg2"] = types.SimpleNamespace(errors=types.SimpleNamespace(DeadlockDetected=Deadlock))

    def tearDown(self):
        for m in ("backfill_engines_confirming", "psycopg2", "coreomics_import"):
            sys.modules.pop(m, None)

    def _run(self, todo, chunk=2, workers=3, budget=10 ** 6):
        sys.modules["coreomics_import"] = types.SimpleNamespace(_conn=lambda: _Conn(todo))
        import importlib
        import fran_fill as f
        importlib.reload(f)
        f.STATUS_DIR, f.ENGINES_CHUNK, f.ENGINES_WORKERS, f.ENGINES_BUDGET_S = self.d, chunk, workers, budget
        return f.engines_confirming(False), f

    def mark(self):
        return open(os.path.join(self.d, "engines_confirming.watermark")).read().strip()

    def test_each_basename_once_per_round_on_one_worker(self):
        todo = [(ts(1), ["a", "b", "c"]), (ts(2), ["b", "c", "d"]), (ts(3), ["e"])]
        res, _ = self._run(todo, chunk=2)
        self.assertEqual(res["rc"], 0)
        self.assertEqual(res["left"], 0)
        seen = sorted(n for _, names in self.calls for n in names)
        self.assertEqual(seen, ["a", "b", "c", "d", "e"])            # b, c not repeated
        self.assertEqual(self.mark(), ts(3).isoformat())

    def test_a_failed_round_does_not_advance_the_mark_past_it(self):
        todo = [(ts(1), ["a"]), (ts(2), ["b"]), (ts(3), ["c"]), (ts(4), ["d"])]
        self.fail_on = {"c"}
        res, _ = self._run(todo, chunk=2)
        self.assertEqual(res["rc"], 1)
        self.assertIn("boom", res["err_tail"])
        self.assertEqual(self.mark(), ts(2).isoformat())              # round 1 only
        self.assertEqual(res["left"], 2)

    def test_the_budget_stops_between_rounds(self):
        todo = [(ts(1), ["a"]), (ts(2), ["b"])]
        res, _ = self._run(todo, chunk=1, budget=-1)
        self.assertEqual(res["left"], 2)
        self.assertEqual(self.calls, [])

    def test_shares_are_disjoint(self):
        todo = [(ts(1), list("abcdefgh"))]
        self._run(todo, chunk=4, workers=3)
        seen = [n for _, names in self.calls for n in names]
        self.assertEqual(sorted(seen), list("abcdefgh"))
        self.assertEqual(len(self.calls), 3)


if __name__ == "__main__":
    unittest.main()
