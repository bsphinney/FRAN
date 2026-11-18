"""raw_md5: hashing runs for hours, so the connection is not held across it. 2026-11-18 the nightly
step died at its first flush after 104 min ("SSL SYSCALL error: EOF detected"). Each flush now opens
its own connection and retries once."""
import os
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ingest"))


class OpErr(Exception):
    pass


class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, *a):
        if self.conn.fail:
            raise OpErr("SSL SYSCALL error: EOF detected")
        if sql.lstrip().startswith("SELECT"):
            self.rows = self.conn.world["todo"]

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, world):
        self.world = world
        world["opened"] += 1
        self.fail = world["fail_next"] > 0 and world["opened"] > 1
        if self.fail:
            world["fail_next"] -= 1
        self.closed = False

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.world["commits"] += 1

    def close(self):
        self.closed = True
        self.world["closed"] += 1


class Reconnect(unittest.TestCase):
    def _run(self, fail_next):
        d = tempfile.mkdtemp()
        files = []
        for i in range(3):
            p = os.path.join(d, f"r{i}.raw")
            with open(p, "wb") as fh:
                fh.write(b"x" * (i + 1))
            files.append((p, i + 1))
        world = dict(opened=0, closed=0, commits=0, fail_next=fail_next, todo=files, batches=[])
        sys.modules["coreomics_import"] = types.SimpleNamespace(_conn=lambda: _Conn(world))
        sys.modules["psycopg2"] = types.SimpleNamespace(OperationalError=OpErr)
        sys.modules["psycopg2.extras"] = types.SimpleNamespace(
            execute_values=lambda c, sql, rows, page_size=0: (c.execute(sql), world["batches"].append(list(rows))))
        import importlib
        import raw_md5
        importlib.reload(raw_md5)
        raw_md5.time.sleep = lambda s: None
        sys.argv = ["raw_md5.py", "--apply", "--tsv-dir", d]
        rc = raw_md5.main()
        return rc, world

    def tearDown(self):
        for m in ("coreomics_import", "psycopg2", "psycopg2.extras"):
            sys.modules.pop(m, None)

    def test_the_listing_connection_is_closed_before_hashing(self):
        rc, w = self._run(0)
        self.assertEqual(rc, 0)
        self.assertEqual(w["opened"], w["closed"])        # nothing left open
        self.assertEqual(len(w["batches"]), 1)
        self.assertEqual(len(w["batches"][0]), 3)

    def test_a_dropped_connection_is_retried_and_the_batch_is_kept(self):
        rc, w = self._run(1)
        self.assertEqual(rc, 0)
        self.assertEqual(w["commits"], 1)
        self.assertEqual(len(w["batches"]), 1)            # the failed attempt wrote nothing
        self.assertEqual(w["opened"], w["closed"])

    def test_two_drops_in_a_row_fail_loudly(self):
        with self.assertRaises(OpErr):
            self._run(2)


if __name__ == "__main__":
    unittest.main()
