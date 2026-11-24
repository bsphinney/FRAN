"""fill_spectrum_lanes.py's decisions and its health-scan checks, with no database, no Lance, no HIVE.

What this CANNOT prove: that a real report parses and a real dataset writes. That path
(child_write -> sln.write_lance -> register) is exercised only by running --apply on HIVE.
Here the child process is replaced by a stub, so what is tested is the policy around it: which
action each search gets, that a count mismatch is a failure and is not registered, that a lane is
never written over another search's dataset, and that a search is parked after 3 failures."""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ingest"))

import fill_spectrum_lanes as F  # noqa: E402
import fran_health_scan as H  # noqa: E402

L = F.LANCE


def S(sid, name, n=1000, reports=None, **kw):
    return dict(sid=sid, name=name, corpus_n=n, ingested_at="2026-11-01T00:00:00", output_dir=kw.get("od"),
                names=F.candidate_names(name, None, kw.get("od"), None))


class FindReports(unittest.TestCase):
    def test_newest_export_first_and_only_report_files(self):
        tree = {"/r/A": ["20260101_000000_A", "20260301_000000_A"],
                "/r/A/20260101_000000_A": ["A_Report_FRAN (Normal).parquet", "notes.txt"],
                "/r/A/20260301_000000_A": ["A_Report_FRAN (Normal).tsv"]}
        got = F.find_reports(["A"], None, roots=["/r"], listdir=lambda d: tree.get(d, []),
                             isdir=lambda d: d in tree, isfile=lambda p: True, getsize=lambda p: 5)
        self.assertEqual([os.path.basename(os.path.dirname(p)) for p in got],
                         ["20260301_000000_A", "20260101_000000_A"])

    def test_sne_output_dir_is_not_a_directory_to_search(self):
        got = F.find_reports([], r"D:\x\a.sne", roots=[], listdir=lambda d: ["a_Report_FRAN (Normal).parquet"],
                             isdir=lambda d: True, isfile=lambda p: True, getsize=lambda p: 5)
        self.assertEqual(got, [])

    def test_zero_byte_report_is_ignored(self):
        got = F.find_reports([], "/o", roots=[], listdir=lambda d: ["a_Report_FRAN (Normal).parquet"],
                             isdir=lambda d: d == "/o", isfile=lambda p: True, getsize=lambda p: 0)
        self.assertEqual(got, [])


class Counts(unittest.TestCase):
    def test_exact_near_and_mismatch(self):
        self.assertEqual(F.counts_match(1000, 1000), "exact")
        self.assertEqual(F.counts_match(1000005, 1000000), "near")        # 5 rows of dedup noise
        self.assertIsNone(F.counts_match(1000005, 1000000, strict=True))
        self.assertIsNone(F.counts_match(298432, 90464))                  # the real 3.3x collision
        self.assertIsNone(F.counts_match(5, None))


class Decide(unittest.TestCase):
    def d(self, s, reports=(), registry=None, on_disk=None, state=None):
        return F.decide(s, list(reports), registry or {}, on_disk or {}, set(), state or {})

    def test_new_search_with_report_is_written_to_plain_name(self):
        job, skip = self.d(S("sid1aaaa", "X"), ["/r/X/20260101_000000_X/X_Report_FRAN (Normal).parquet"])
        self.assertEqual((job["action"], job["lance_path"]), ("write", f"{L}/X.lance"))

    def test_name_held_by_another_search_gets_suffixed_path_never_overwrite(self):
        reg = {f"{L}/X.lance": (7, "other-sid", 99999)}
        job, _ = self.d(S("sid1aaaa", "X"), ["/r/X/t/X_Report_FRAN (Normal).parquet"], registry=reg)
        self.assertEqual(job["action"], "write")
        self.assertEqual(job["lance_path"], f"{L}/X__sid1aaaa.lance")

    def test_two_searches_same_name_in_one_plan_do_not_share_a_path(self):
        taken = set()
        rep = ["/r/X/t/X_Report_FRAN (Normal).parquet"]
        a, _ = F.decide(S("aaaaaaaa", "X"), rep, {}, {}, taken, {})
        b, _ = F.decide(S("bbbbbbbb", "X"), rep, {}, {}, taken, {})
        self.assertNotEqual(a["lance_path"], b["lance_path"])

    def test_null_search_id_row_with_matching_count_is_linked_not_rewritten(self):
        reg = {f"{L}/X.lance": (7, None, 1000)}
        job, _ = self.d(S("sid1aaaa", "X"), ["/r/X/t/X_Report_FRAN (Normal).parquet"], registry=reg)
        self.assertEqual((job["action"], job["row_id"]), ("link", 7))

    def test_null_row_with_different_count_is_someone_elses_data(self):
        reg = {f"{L}/X.lance": (7, None, 5)}
        job, _ = self.d(S("sid1aaaa", "X"), ["/r/X/t/X_Report_FRAN (Normal).parquet"], registry=reg)
        self.assertEqual(job["action"], "write")
        self.assertIn("__sid1aaaa", job["lance_path"])

    def test_unregistered_dataset_with_matching_count_is_registered_not_rewritten(self):
        job, _ = self.d(S("sid1aaaa", "X"), ["/r/X/t/X_Report_FRAN (Normal).parquet"],
                        on_disk={f"{L}/X.lance": 1000})
        self.assertEqual(job["action"], "register")

    def test_no_report_no_dataset_is_needs_reexport(self):
        job, skip = self.d(S("sid1aaaa", "X", od=r"D:\MRS\X.sne"))
        self.assertIsNone(job)
        self.assertEqual(skip["reason"], "needs_reexport")
        self.assertFalse(skip["report"])

    def test_no_report_but_identical_lane_under_another_search_is_dup(self):
        reg = {f"{L}/X.lance": (7, "other-sid", 1000)}
        _, skip = self.d(S("sid1aaaa", "X"), registry=reg)
        self.assertEqual(skip["reason"], "dup_of_laned")

    def test_parked_is_skipped_and_still_counts_as_having_a_report(self):
        _, skip = self.d(S("sid1aaaa", "X"), ["/r/X/t/X_Report_FRAN (Normal).parquet"],
                         state={"sid1aaaa": {"parked": True, "attempts": 3, "last_error": "boom"}})
        self.assertEqual((skip["reason"], skip["report"]), ("parked", True))

    def test_retry_uses_next_export_and_same_path(self):
        reps = ["/r/X/t2/X_Report_FRAN (Normal).parquet", "/r/X/t1/X_Report_FRAN (Normal).parquet"]
        st = {"sid1aaaa": {"attempts": 1, "lance_path": f"{L}/X__sid1aaaa.lance"}}
        job, _ = self.d(S("sid1aaaa", "X"), reps, state=st)
        self.assertEqual(job["report"], reps[1])
        self.assertEqual(job["lance_path"], f"{L}/X__sid1aaaa.lance")


class State(unittest.TestCase):
    def test_parked_after_three_failures(self):
        st = {}
        for _ in range(3):
            F.record_failure(st, "s", "mismatch", "/p.lance")
        self.assertTrue(st["s"]["parked"])
        self.assertEqual(st["s"]["attempts"], 3)

    def test_death_by_other_job_counts_requeue_of_same_job_does_not(self):
        st = {"a": {"in_flight": "100"}, "b": {"in_flight": "200"}}
        F.settle_in_flight(st, "200")
        self.assertEqual(st["a"]["attempts"], 1)
        self.assertNotIn("in_flight", st["a"])
        self.assertEqual(st["b"].get("attempts", 0), 0)
        self.assertEqual(st["b"]["in_flight"], "200")


class Verification(unittest.TestCase):
    """A count mismatch is a failure and must not reach the registry."""
    def run_one(self, res, corpus_n):
        calls = []
        orig = F.run_child
        F.run_child = lambda args: (res, 0)
        try:
            j = dict(sid="s", name="X", action="write", report="/r/x", lance_path=f"{L}/X.lance", corpus_n=corpus_n)
            o = F.do_job(j, False)
        finally:
            F.run_child = orig
        return o, calls

    def test_mismatch_fails(self):
        o, _ = self.run_one(dict(ok=True, n_prec=298432, n_frag=1, md5="m", version=1, n_back=298432), 90464)
        self.assertEqual(o["status"], "failed")
        self.assertIn("mismatch", o["error"])

    def test_unreadable_back_fails(self):
        o, _ = self.run_one(dict(ok=True, n_prec=1000, n_frag=1, md5="m", version=1, n_back=None), 1000)
        self.assertEqual(o["status"], "failed")

    def test_exact_passes(self):
        o, _ = self.run_one(dict(ok=True, n_prec=1000, n_frag=1, md5="m", version=1, n_back=1000), 1000)
        self.assertEqual((o["status"], o["count_check"]), ("verified", "exact"))

    def test_child_killed_is_failure(self):
        o, _ = self.run_one(dict(ok=False, error="child rc=-9 (no result; killed?)"), 1000)
        self.assertEqual(o["status"], "failed")

    def test_path_outside_lane_dir_refused(self):
        j = dict(sid="s", name="X", action="write", report="/r/x", lance_path="/tmp/evil.lance", corpus_n=1)
        self.assertEqual(F.do_job(j, False)["status"], "failed")


class SyntheticRoundTrip(unittest.TestCase):
    """A 3-precursor synthetic report through the real child writer and describer (needs lance+pandas).
    Proves the plumbing and that _describe's md5 equals the write-time md5 (so a registered-after-kill
    dataset matches what verify() later checks). It is NOT a real Spectronaut report."""
    def test_write_then_describe_agree(self):
        try:
            import lance  # noqa: F401
            import pandas as pd
        except ImportError:
            self.skipTest("lance/pandas not installed")
        d = tempfile.mkdtemp()
        rows = [{"R.FileName": "run1", "PEP.StrippedSequence": f"PEPTIDE{i}", "EG.ModifiedSequence": f"_PEPTIDE{i}_",
                 "FG.Charge": 2, "EG.Qvalue": 0.001, "F.FrgMz": 100.0 + f, "F.FrgType": "y", "F.FrgNum": f + 1,
                 "F.FrgIon": f"y{f + 1}", "F.Charge": 1, "F.FrgLossType": "noloss", "F.PeakArea": 1000.0 * (f + 1),
                 "FG.PrecMz": 500.0 + i, "EG.ApexRT": 10.0 + i, "PG.ProteinGroups": "P1"}
                for i in range(3) for f in range(4)]
        rep = os.path.join(d, "t_Report_FRAN (Normal).parquet")
        pd.DataFrame(rows).to_parquet(rep)
        out = os.path.join(d, "t.lance")
        w = F.child_write(rep, out, "t")
        self.assertEqual((w["n_prec"], w["n_frag"], w["n_back"]), (3, 12, 3))
        self.assertEqual(F.child_describe(out)["md5"], w["md5"])


class Budget(unittest.TestCase):
    def test_estimates(self):
        self.assertGreater(F.est_gb("a.tsv", 170e9), F.MEM_BUDGET_GB)
        self.assertLess(F.est_gb("a.parquet", 4e9), F.MEM_BUDGET_GB)


class HealthChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = (H.SPEC_FILL_DIR, list(H.findings))
        H.SPEC_FILL_DIR = self.tmp
        H.findings.clear()

    def tearDown(self):
        H.SPEC_FILL_DIR = self.old[0]
        H.findings[:] = self.old[1]

    def plan(self, **kw):
        base = dict(summary=dict(job="9", unlaned=3, planned={}, skipped={}), skipped=[], report_sids=[])
        base.update(kw)
        json.dump(base, open(os.path.join(self.tmp, "plan_9.json"), "w"))

    class Cur:
        def __init__(self, rows): self.rows = rows
        def execute(self, *a, **k): pass
        def fetchall(self): return self.rows

    def test_report_on_disk_and_still_missing_is_red(self):
        self.plan(report_sids=["a"], skipped=[dict(sid="b", reason="needs_reexport")])
        H.check_spectrum_staleness(self.Cur([("a", "A"), ("b", "B")]))
        self.assertEqual(H.findings[-1][0], "RED")

    def test_only_reexport_searches_is_amber_not_red(self):
        self.plan(skipped=[dict(sid="b", reason="needs_reexport"), dict(sid="c", reason="dup_of_laned")])
        H.check_spectrum_staleness(self.Cur([("b", "B"), ("c", "C")]))
        self.assertEqual(H.findings[-1][0], "AMBER")

    def test_nothing_missing_ok(self):
        self.plan()
        H.check_spectrum_staleness(self.Cur([]))
        self.assertEqual(H.findings[-1][0], "ok")

    def test_never_planned_with_gap_is_red(self):
        H.check_spectrum_staleness(self.Cur([("a", "A")]))
        self.assertEqual(H.findings[-1][0], "RED")

    def test_job_never_planned_red(self):
        H.check_spectrum_job()
        self.assertEqual(H.findings[-1][0], "RED")

    def test_job_failed_writes_amber(self):
        self.plan()
        json.dump(dict(summary=dict(outcomes={"failed": 1}, parked=0),
                       outcomes=[dict(name="X", status="failed")]),
                  open(os.path.join(self.tmp, "result_9.json"), "w"))
        H.check_spectrum_job()
        self.assertEqual(H.findings[-1][0], "AMBER")

    def test_job_ok(self):
        self.plan()
        json.dump(dict(summary=dict(outcomes={"registered": 2}, parked=0), outcomes=[]),
                  open(os.path.join(self.tmp, "result_9.json"), "w"))
        H.check_spectrum_job()
        self.assertEqual(H.findings[-1][0], "ok")

    def test_cron_expects_the_new_wrapper(self):
        self.assertIn("fill_spectrum_lanes_cron.sh", H.EXPECTED_CRON)


if __name__ == "__main__":
    unittest.main()
