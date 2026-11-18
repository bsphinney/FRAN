"""fran_health_scan.check_coverage measures every lane for every engine, with floors.

2026-11-18: the lanes were checked one engine each (XIC: diann, spectrum: spectronaut) and with no
floor, so 0 of 1,223 DIA-NN searches with a spectrum lane and 2 of 2,222 Spectronaut searches with
an XIC lane never appeared -- a lane that was always empty never shows a drop."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ingest"))

import fran_health_scan as H  # noqa: E402

# (lane table, engine) -> (n_recent, recent fraction, n_base, base fraction)
WORLD = {
    ("delimp_xic_lane", "diann"): (500, 0.95, 300, 0.97),
    ("delimp_xic_lane", "spectronaut"): (120, 0.0, 400, 0.002),
    ("delimp_spectrum_lane", "diann"): (500, 0.0, 300, 0.0),
    ("delimp_spectrum_lane", "spectronaut"): (120, 0.86, 400, 0.72),
}


class Cur:
    def __init__(self):
        self.res = None
        self.seen = []

    def execute(self, sql, params=None):
        self.seen.append((sql, params))
        if "SELECT DISTINCT search_engine" in sql:
            self.res = [("diann",), ("spectronaut",)]
            return
        eng = params[0] if params else None
        lane = next((t for t, _ in WORLD if t in sql), None)
        if lane and eng:
            self.res = [WORLD[(lane, eng)]]
        else:
            self.res = [(500, 0.99, 300, 0.99)]   # the non-lane checks: healthy

    def fetchall(self):
        return self.res

    def fetchone(self):
        return self.res[0]


class Coverage(unittest.TestCase):
    def setUp(self):
        H.findings.clear()
        H.metrics["coverage"] = {}
        self.cur = Cur()
        H.check_coverage(self.cur)
        self.by = {msg.split(":")[0]: sev for sev, area, msg in H.findings if area == "coverage"}

    def test_every_lane_is_measured_for_every_engine(self):
        for lane in ("XIC lane", "spectrum lane"):
            for eng in ("diann", "spectronaut"):
                self.assertIn(f"searches with {lane} ({eng})", H.metrics["coverage"])

    def test_a_lane_that_was_always_empty_is_red(self):
        self.assertEqual(self.by.get("searches with spectrum lane (diann)"), "RED")
        self.assertEqual(self.by.get("searches with XIC lane (spectronaut)"), "RED")

    def test_the_red_says_why(self):
        msg = next(m for s, a, m in H.findings if m.startswith("searches with spectrum lane (diann)"))
        self.assertIn("diann_spectra_to_lance", msg)

    def test_covered_lanes_are_not_flagged(self):
        self.assertNotIn("searches with XIC lane (diann)", self.by)
        self.assertNotIn("searches with spectrum lane (spectronaut)", self.by)

    def test_engine_is_a_parameter_not_pasted_into_sql(self):
        for sql, params in self.cur.seen:
            self.assertNotIn("'diann'", sql)


if __name__ == "__main__":
    unittest.main()
