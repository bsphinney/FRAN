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
        F.run_child = lambda args, stdin=None: (res, 0)
        try:
            j = dict(sid="s", name="X", action="write", report="/r/x", lance_path=f"{L}/X.lance", corpus_n=corpus_n,
                     corpus_runs=["r1"])
            o = F.do_job(j, False)
        finally:
            F.run_child = orig
        return o, calls

    def test_mismatch_fails(self):
        o, _ = self.run_one(dict(ok=True, n_prec=298432, n_frag=1, md5="m", version=1, n_back=298432, n_runs=1), 90464)
        self.assertEqual(o["status"], "failed")
        self.assertIn("mismatch", o["error"])

    def test_unreadable_back_fails(self):
        o, _ = self.run_one(dict(ok=True, n_prec=1000, n_frag=1, md5="m", version=1, n_back=None, n_runs=1), 1000)
        self.assertEqual(o["status"], "failed")

    def test_exact_passes(self):
        o, _ = self.run_one(dict(ok=True, n_prec=1000, n_frag=1, md5="m", version=1, n_back=1000, n_runs=1), 1000)
        self.assertEqual((o["status"], o["count_check"]), ("verified", "exact"))

    def test_child_killed_is_failure(self):
        o, _ = self.run_one(dict(ok=False, error="child rc=-9 (no result; killed?)"), 1000)
        self.assertEqual(o["status"], "failed")

    def test_path_outside_lane_dir_refused(self):
        j = dict(sid="s", name="X", action="write", report="/r/x", lance_path="/tmp/evil.lance", corpus_n=1,
                 corpus_runs=["r1"])
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
        w = F.child_write(rep, out, "t", 3, False, ["run1"], "J1")
        self.assertEqual((w["n_prec"], w["n_frag"], w["n_back"]), (3, 12, 3))
        self.assertEqual(F.child_describe(out)["md5"], w["md5"])
        self.assertEqual(F.child_describe(out, runs_only=True)["runs"], ["run1"])
        self.assertFalse(os.path.exists(out + ".incoming.J1"))


def synth_report(d, runs=("run1",), n=3):
    """A tiny Spectronaut-shaped parquet report: n precursors per run, 4 fragments each."""
    import pandas as pd
    rows = [{"R.FileName": r, "PEP.StrippedSequence": f"PEPTIDE{i}", "EG.ModifiedSequence": f"_PEPTIDE{i}_",
             "FG.Charge": 2, "EG.Qvalue": 0.001, "F.FrgMz": 100.0 + f, "F.FrgType": "y", "F.FrgNum": f + 1,
             "F.FrgIon": f"y{f + 1}", "F.Charge": 1, "F.FrgLossType": "noloss", "F.PeakArea": 1000.0 * (f + 1),
             "FG.PrecMz": 500.0 + i, "EG.ApexRT": 10.0 + i, "PG.ProteinGroups": "P1"}
            for r in runs for i in range(n) for f in range(4)]
    p = os.path.join(d, "t_Report_FRAN (Normal).parquet")
    pd.DataFrame(rows).to_parquet(p)
    return p


def need_lance(tc):
    try:
        import lance  # noqa: F401
        import pandas  # noqa: F401
    except ImportError:
        tc.skipTest("lance/pandas not installed")


class PathSafety(unittest.TestCase):
    """Items 1, 2, 6: the job cannot put anything on a path it did not create for that search."""
    def setUp(self):
        self.old = (F.LANCE, F.D)
        self.lane = tempfile.mkdtemp()
        F.LANCE, F.D = self.lane, tempfile.mkdtemp()

    def tearDown(self):
        F.LANCE, F.D = self.old

    def test_two_same_name_searches_never_share_a_path_even_with_state_pointing_at_plain(self):
        P = f"{self.lane}/X.lance"
        st = {"aaaaaaaa": {"attempts": 1, "lance_path": P}, "bbbbbbbb": {"attempts": 1, "lance_path": P}}
        taken = set()
        rep = ["/r/X/t/X_Report_FRAN (Normal).parquet"]
        a, _ = F.decide(S("aaaaaaaa", "X"), rep, {}, {}, taken, st)
        b, _ = F.decide(S("bbbbbbbb", "X"), rep, {}, {}, taken, st)
        self.assertNotEqual(a["lance_path"], b["lance_path"])
        self.assertEqual(len({a["lance_path"], b["lance_path"]}), 2)

    def test_anything_existing_at_the_path_makes_it_occupied_even_if_unopenable(self):
        os.makedirs(f"{self.lane}/X.lance")                       # a directory that is not a Lance dataset
        open(f"{self.lane}/X.lance/junk", "w").write("x")
        job, _ = F.decide(S("sid1aaaa", "X"), ["/r/X/t/X_Report_FRAN (Normal).parquet"], {}, {}, set(), {})
        self.assertEqual(job["lance_path"], f"{self.lane}/X__sid1aaaa.lance")

    def test_both_plain_and_suffixed_occupied_is_refused_not_overwritten(self):
        for n in ("X.lance", "X__sid1aaaa.lance"):
            os.makedirs(f"{self.lane}/{n}")
        job, skip = F.decide(S("sid1aaaa", "X"), ["/r/X/t/X_Report_FRAN (Normal).parquet"], {}, {}, set(), {})
        self.assertIsNone(job)
        self.assertEqual(skip["reason"], "path_occupied")

    def test_child_refuses_foreign_directory_and_leaves_it_untouched(self):
        need_lance(self)
        d = tempfile.mkdtemp()
        rep = synth_report(d)
        out = f"{self.lane}/X.lance"
        os.makedirs(out)
        open(f"{out}/precious", "wb").write(b"irreplaceable")
        r = F.child_write(rep, out, "X", 3, True, ["run1"], "J9")
        self.assertFalse(r["ok"])
        self.assertIn("refusing", r["error"])
        self.assertEqual(os.listdir(out), ["precious"])
        self.assertEqual(open(f"{out}/precious", "rb").read(), b"irreplaceable")
        self.assertEqual(os.listdir(self.lane), ["X.lance"])      # no .incoming left behind either

    def test_run_set_mismatch_fails_before_any_write(self):
        need_lance(self)
        d = tempfile.mkdtemp()
        rep = synth_report(d, runs=("run1", "other"))
        out = f"{self.lane}/X.lance"
        r = F.child_write(rep, out, "X", 6, True, ["run1", "run2"], "J9")
        self.assertFalse(r["ok"])
        self.assertIn("identity", r["error"])
        self.assertEqual(os.listdir(self.lane), [])               # nothing at the path, nothing incoming

    def test_count_mismatch_fails_before_any_write(self):
        need_lance(self)
        d = tempfile.mkdtemp()
        r = F.child_write(synth_report(d), f"{self.lane}/X.lance", "X", 9999, True, ["run1"], "J9")
        self.assertFalse(r["ok"])
        self.assertEqual(os.listdir(self.lane), [])

    def test_suffixed_path_requires_exact_count_plain_accepts_near(self):
        need_lance(self)
        d = tempfile.mkdtemp()
        rep = synth_report(d)                                      # 3 precursors; corpus says 8 (within the 10-row floor)
        r = F.child_write(rep, f"{self.lane}/X__sid1aaaa.lance", "X", 8, False, ["run1"], "J9")
        self.assertFalse(r["ok"])
        r = F.child_write(rep, f"{self.lane}/X.lance", "X", 8, True, ["run1"], "J9")
        self.assertTrue(r["ok"])

    def test_failed_write_leaves_nothing_at_the_real_path(self):
        need_lance(self)
        import spectrum_lance as sln
        d = tempfile.mkdtemp()
        rep = synth_report(d)
        real = sln.write_lance

        def boom(tbl, path, mode="overwrite"):
            os.makedirs(path)                                      # half-written incoming directory
            open(os.path.join(path, "partial"), "w").write("x")
            raise RuntimeError("disk full")
        sln.write_lance = boom
        try:
            r = F.child_write(rep, f"{self.lane}/X.lance", "X", 3, True, ["run1"], "J9")
        finally:
            sln.write_lance = real
        self.assertFalse(r["ok"])
        self.assertEqual(os.listdir(self.lane), [])

    def test_quarantine_moves_an_unregisterable_dataset_off_the_real_path(self):
        p = f"{self.lane}/X.lance"
        os.makedirs(p)
        F.quarantine(dict(action="write", lance_path=p), "J9")
        self.assertFalse(os.path.exists(p))
        self.assertTrue(os.path.isdir(p + ".failed.J9"))
        # and the planner can never match it: it only builds <name>.lance paths
        self.assertNotIn(p + ".failed.J9", {f"{self.lane}/X.lance"})

    def test_ambiguous_link_is_left_alone(self):
        reg = {f"{self.lane}/X.lance": (7, None, 1000)}
        claims = {f"{self.lane}/X.lance": ["a", "b"]}
        job, skip = F.decide(S("a", "X"), [], reg, {}, set(), {}, claims=claims)
        self.assertIsNone(job)
        self.assertEqual(skip["reason"], "ambiguous_link")


class TrialNarrowing(unittest.TestCase):
    def test_claims_are_over_all_unlaned_searches_so_a_trial_cannot_resolve_an_ambiguity(self):
        reg = {f"{L}/X.lance": (7, None, 1000)}
        both = [S("a", "X"), S("b", "X")]
        self.assertEqual(sorted(F.link_claims(both, reg)[f"{L}/X.lance"]), ["a", "b"])
        # planning only "a" (a trial) with claims from the full list still sees the ambiguity
        job, skip = F.decide(S("a", "X"), [], reg, {}, set(), {}, claims=F.link_claims(both, reg))
        self.assertEqual(skip["reason"], "ambiguous_link")


class Registration(unittest.TestCase):
    """Item 3: the fill's own conditional upsert, and zero rows is a failure."""
    class Cur:
        def __init__(self, rc): self.rc, self.sql = rc, []
        def execute(self, sql, params=None): self.sql.append((sql, params))
        def fetchone(self): return None

        @property
        def rowcount(self): return self.rc

    class Conn:
        def __init__(self, rc): self.c, self.commits, self.rollbacks = Registration.Cur(rc), 0, 0
        def cursor(self): return self.c
        def commit(self): self.commits += 1
        def rollback(self): self.rollbacks += 1

    J = dict(sid="s", name="X", lance_path="/l/X.lance", row_id=7)
    R = dict(n_prec=1, n_frag=2, md5="m", version=1)

    def test_sql_only_updates_unlinked_or_own_rows_and_never_coalesces(self):
        sql = " ".join(F.UPSERT_SQL.split())
        self.assertIn("ON CONFLICT (lance_path) DO UPDATE", sql)
        self.assertIn("WHERE delimp_spectrum_lane.search_id IS NULL OR delimp_spectrum_lane.search_id = EXCLUDED.search_id", sql)
        self.assertNotIn("COALESCE", sql)
        self.assertEqual(sql.count("%"), sql.count("%s"))          # psycopg2 trap: a bare percent breaks execute()

    def test_zero_rows_is_a_failure_and_nothing_is_committed(self):
        conn = self.Conn(0)
        with self.assertRaises(RuntimeError):
            F.register_row(conn, self.J, self.R)
        self.assertEqual((conn.commits, conn.rollbacks), (0, 1))

    def test_one_row_commits(self):
        conn = self.Conn(1)
        F.register_row(conn, self.J, self.R)
        self.assertEqual(conn.commits, 1)

    def test_link_only_touches_a_still_unlinked_row_at_that_path(self):
        self.assertIn("search_id IS NULL", F.LINK_SQL)
        self.assertIn("lance_path = %s", F.LINK_SQL)
        with self.assertRaises(RuntimeError):
            F.link_row(self.Conn(0), self.J)


class InFlight(unittest.TestCase):
    """Item 4: only the search whose child is running is in_flight."""
    def setUp(self):
        self.old_d = F.D
        F.D = tempfile.mkdtemp()

    def tearDown(self):
        F.D = self.old_d

    def test_in_flight_is_per_search_and_persisted_before_the_child_starts(self):
        seen = []
        old = F.do_job

        def spy(j, strict, jid="x"):
            st = json.load(open(os.path.join(F.D, "state.json")))
            seen.append((j["sid"], sorted(k for k, v in st.items() if v.get("in_flight"))))
            return dict(sid=j["sid"], name=j["name"], action="write", status="failed", error="stub")
        F.do_job = spy
        try:
            jobs = [dict(sid=f"s{i}", name=f"n{i}", action="write", lance_path=f"{L}/n{i}.lance", est_gb=1.0)
                    for i in range(5)]
            class A: workers, strict = 1, False
            state = {}
            F.apply_jobs(None, jobs, state, A, "J1")
        finally:
            F.do_job = old
        for sid, flying in seen:
            self.assertEqual(flying, [sid])                         # never the whole batch up front

    def test_kill_charges_only_the_searches_that_were_running(self):
        state = {}
        F.mark_in_flight(state, "s1", "J1")                         # J1 started s1, then was OOM-killed
        state.setdefault("s2", {})                                  # s2..s244 never started
        F.settle_in_flight(state, "J2")                             # next night's job
        self.assertEqual(state["s1"]["attempts"], 1)
        self.assertEqual(state["s2"].get("attempts", 0), 0)
        for _ in range(2):
            state["s1"]["in_flight"] = "Jx"
            F.settle_in_flight(state, "Jy")
        self.assertTrue(state["s1"]["parked"])
        self.assertFalse(state["s2"].get("parked"))

    def test_state_file_is_written_atomically(self):
        F.save_state({"a": {}})
        self.assertEqual(sorted(os.listdir(F.D)), ["state.json"])


class Robustness(unittest.TestCase):
    """Second review: connection loss, empty run sets, quarantine after rename, state file, lock."""
    def setUp(self):
        self.old = (F.D, F.LANCE)
        F.D, F.LANCE = tempfile.mkdtemp(), tempfile.mkdtemp()

    def tearDown(self):
        F.D, F.LANCE = self.old

    # -- 1. connection
    class FakeConn:
        def __init__(self, fail_first=0, closed=0):
            self.closed, self.fail, self.calls, self.commits = closed, fail_first, 0, 0
        def close(self): self.closed = 1
        def rollback(self): pass
        def commit(self): self.commits += 1

    def job(self, name="X", action="write"):
        p = os.path.join(F.LANCE, name + ".lance")
        return dict(sid="s1", name=name, action=action, lance_path=p, est_gb=1.0, corpus_n=3,
                    corpus_runs=["r1"], report="/r/x", row_id=1)

    def run_apply(self, db, job, do_job_result):
        old = F.do_job
        F.do_job = lambda j, strict, jid="x": dict(do_job_result)
        old_reg = F.register_row
        try:
            class A: workers, strict = 1, False
            state = {}
            outs, aborted = F.apply_jobs(db, [job], state, A, "J1", os.path.join(F.D, "o.jsonl"))
        finally:
            F.do_job, F.register_row = old, old_reg
        return outs, aborted, state

    def verified(self, j):
        return dict(sid=j["sid"], name=j["name"], action=j["action"], lance_path=j["lance_path"], status="verified",
                    count_check="exact", res=dict(n_prec=3, n_frag=1, md5="m", version=1))

    def test_connection_lost_once_reconnects_and_registers(self):
        import psycopg2
        j = self.job()
        os.makedirs(j["lance_path"])
        conns = [self.FakeConn(), self.FakeConn()]
        db = F.Db(lambda: conns.pop(0))
        seen = []

        def reg(c, jj, r):
            seen.append(c)
            if len(seen) == 1:
                raise psycopg2.OperationalError("server closed the connection")
        F.register_row = reg
        old = F.do_job
        F.do_job = lambda jj, strict, jid="x": self.verified(jj)
        try:
            class A: workers, strict = 1, False
            outs, aborted = F.apply_jobs(db, [j], {}, A, "J1")
        finally:
            F.do_job = old
        self.assertEqual(outs[0]["status"], "registered")
        self.assertIsNone(aborted)
        self.assertEqual(len(seen), 2)
        self.assertIsNot(seen[0], seen[1])                       # reconnected
        self.assertTrue(os.path.isdir(j["lance_path"]))          # not quarantined

    def test_connection_cannot_be_restored_stops_job_keeps_dataset_and_records_pending(self):
        import psycopg2
        j = self.job()
        os.makedirs(j["lance_path"])
        n = []

        def connect():
            n.append(1)
            if len(n) > 1:
                raise psycopg2.OperationalError("no route")
            return self.FakeConn()
        db = F.Db(connect)
        F.register_row = lambda c, jj, r: (_ for _ in ()).throw(psycopg2.OperationalError("gone"))
        outs, aborted, state = None, None, {}
        old = F.do_job
        F.do_job = lambda jj, strict, jid="x": self.verified(jj)
        try:
            class A: workers, strict = 1, False
            outs, aborted = F.apply_jobs(db, [j], state, A, "J1")
        finally:
            F.do_job = old
        self.assertEqual(outs[0]["status"], "pending_register")
        self.assertIn("connection lost", aborted)
        self.assertTrue(os.path.isdir(j["lance_path"]))          # verified dataset stays at its final path
        self.assertFalse(os.path.exists(j["lance_path"] + ".failed.J1"))
        self.assertEqual(state["s1"]["pending_register"]["lance_path"], j["lance_path"])
        self.assertEqual(state["s1"].get("attempts", 0), 0)      # no attempt charged

    def test_closed_connection_is_reopened_before_use(self):
        conns = [self.FakeConn(closed=1), self.FakeConn()]
        db = F.Db(lambda: conns.pop(0))
        db.conn.closed = 1
        self.assertEqual(db.run(lambda c: c.closed), 0)

    # -- 2. empty sets
    def test_empty_run_sets_fail_in_do_job(self):
        for runs, n_runs in (([], 0), (["r1"], 1)):
            old = F.run_child
            F.run_child = lambda args, stdin=None: (dict(ok=True, n_prec=3, n_frag=1, md5="m", version=1,
                                                         n_back=3, n_runs=n_runs, runs=runs), 0)
            try:
                j = self.job(action="register")
                j["corpus_runs"] = [] if runs else ["r1"]
                o = F.do_job(j, False)
            finally:
                F.run_child = old
            self.assertEqual(o["status"], "failed")
            self.assertIn("identity", o["error"])

    def test_empty_expected_runs_fail_in_child_before_writing(self):
        need_lance(self)
        d = tempfile.mkdtemp()
        out = os.path.join(F.LANCE, "X.lance")
        r = F.child_write(synth_report(d), out, "X", 3, True, [], "J9")
        self.assertFalse(r["ok"])
        self.assertEqual(os.listdir(F.LANCE), [])

    # -- 3. quarantine after rename
    def test_failure_after_the_rename_quarantines(self):
        j = self.job()
        os.makedirs(j["lance_path"])
        old = F.run_child
        F.run_child = lambda args, stdin=None: (dict(ok=True, n_prec=3, n_frag=1, md5="m", version=1,
                                                     n_back=2, n_runs=1), 0)           # reopens short
        try:
            o = F.do_job(j, False, "J1")
        finally:
            F.run_child = old
        self.assertEqual(o["status"], "failed")
        self.assertFalse(os.path.exists(j["lance_path"]))
        self.assertTrue(os.path.isdir(j["lance_path"] + ".failed.J1"))

    def test_count_mismatch_after_the_rename_quarantines(self):
        j = self.job()
        j["corpus_n"] = 90000
        os.makedirs(j["lance_path"])
        old = F.run_child
        F.run_child = lambda args, stdin=None: (dict(ok=True, n_prec=3, n_frag=1, md5="m", version=1,
                                                     n_back=3, n_runs=1), 0)
        try:
            o = F.do_job(j, False, "J1")
        finally:
            F.run_child = old
        self.assertEqual(o["status"], "failed")
        self.assertFalse(os.path.exists(j["lance_path"]))

    # -- 4. incremental outcomes
    def test_each_outcome_is_appended_as_it_finishes(self):
        db = F.Db(lambda: self.FakeConn())
        j = self.job()
        old = F.do_job
        F.do_job = lambda jj, strict, jid="x": dict(sid=jj["sid"], name=jj["name"], action="write", status="failed", error="e")
        try:
            class A: workers, strict = 1, False
            F.apply_jobs(db, [j], {}, A, "J1", os.path.join(F.D, "o.jsonl"))
        finally:
            F.do_job = old
        lines = open(os.path.join(F.D, "o.jsonl")).read().splitlines()
        self.assertEqual(json.loads(lines[0])["sid"], "s1")

    # -- 5. state file and lock
    def test_corrupt_state_stops_instead_of_loading_empty(self):
        open(os.path.join(F.D, "state.json"), "w").write("{not json")
        with self.assertRaises(F.StateError):
            F.load_state()

    def test_missing_state_is_a_fresh_start(self):
        self.assertEqual(F.load_state(), {})

    def test_state_temp_name_has_host_pid_job(self):
        seen = []
        real = os.replace
        os.replace = lambda a, b: (seen.append(a), real(a, b))
        try:
            os.environ["SLURM_JOB_ID"] = "777"
            F.save_state({"a": {}})
        finally:
            os.replace = real
            os.environ.pop("SLURM_JOB_ID", None)
        self.assertIn(os.uname().nodename, seen[0])
        self.assertIn(str(os.getpid()), seen[0])
        self.assertTrue(seen[0].endswith(".777"))

    def test_second_apply_cannot_take_a_live_lock(self):
        F.acquire_lock("100", job_state=lambda j: "RUNNING")
        with self.assertRaises(F.StateError):
            F.acquire_lock("200", job_state=lambda j: "RUNNING")

    def test_stale_lock_is_taken_over(self):
        F.acquire_lock("100", job_state=lambda j: "RUNNING")
        lock = F.acquire_lock("200", job_state=lambda j: "")        # squeue no longer knows job 100
        self.assertEqual(open(os.path.join(lock, "job")).read(), "200")

    def test_unverifiable_lock_is_not_taken(self):
        F.acquire_lock("100", job_state=lambda j: "RUNNING")
        with self.assertRaises(F.StateError):
            F.acquire_lock("200", job_state=lambda j: None)         # squeue itself failed

    def test_same_job_requeue_keeps_its_lock_and_release_frees_it(self):
        lock = F.acquire_lock("100")
        self.assertEqual(F.acquire_lock("100"), lock)
        F.release_lock(lock, "100")
        self.assertFalse(os.path.exists(lock))

    # -- 6. in_flight for a search the new plan dropped
    def test_in_flight_of_a_dropped_search_is_cleared_without_charge(self):
        st = {"gone": {"in_flight": "J1"}, "still": {"in_flight": "J1"}}
        F.settle_in_flight(st, "J2", active={"still"})
        self.assertNotIn("in_flight", st["gone"])
        self.assertEqual(st["gone"].get("attempts", 0), 0)
        self.assertEqual(st["still"]["attempts"], 1)


class Identity(unittest.TestCase):
    def test_norm_run_spellings(self):
        self.assertEqual(F.norm_run(r"K:\Data\x\a.sne\21Dec24_MA13_1_10305.d"), "21dec24_ma13_1_10305")
        self.assertEqual(F.norm_run("21Dec24_MA13_1_10305"), "21dec24_ma13_1_10305")
        self.assertEqual(F.norm_run("/p/Run_A.raw"), "run_a")


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


def test_dot_sne_directory_is_named_by_its_folder():
    import fill_spectrum_lanes as f
    assert "My Exp" in f.candidate_names("Custom Name", None, r"B:\\proj\\My Exp\\.sne", None)
    assert "My Exp" in f.candidate_names("Custom Name", None, r"B:\\proj\\My Exp.sne", None)
    assert "" not in f.candidate_names("n", None, r"B:\\proj\\My Exp\\.sne", None)


def test_xic_plan_writes_only_complete_sets_at_a_path_with_the_search_id():
    import fill_spectronaut_xic_lanes as X
    searches = [dict(sid="aaaaaaaa-1", name="20250101_010101_Exp A", output_dir=r"B:\\p\\Exp A.sne", n=10),
                dict(sid="bbbbbbbb-2", name="Exp B", output_dir=r"B:\\p\\Exp B.sne", n=10),
                dict(sid="cccccccc-3", name="Exp C", output_dir=r"B:\\p\\Exp C.sne", n=10),
                dict(sid="dddddddd-4", name="Exp D", output_dir=r"B:\\p\\Exp D.sne", n=10)]
    reports = {"Exp A": ["/r/Exp A/ts/x_Report_FRAN (Normal).tsv"], "Exp B": ["/r/Exp B/ts/x_Report_FRAN (Normal).tsv"],
               "Exp C": ["/r/Exp C/ts/x_Report_FRAN (Normal).tsv"], "Exp D": ["/r/Exp D/ts/x_Report_FRAN (Normal).tsv"]}
    find = lambda names, od: next((v for k, v in reports.items() if k in names), [])
    has = lambda r: None if "Exp B" in r else os.path.dirname(r) + "/xic_dbs"
    jobs, skipped = X.plan(searches, {"cccccccc-3": {"attempts": 3}}, find_reports=find, has_set=has,
                           exists=lambda p: "Exp_D" in p)
    assert [j["sid"] for j in jobs] == ["aaaaaaaa-1"]
    assert jobs[0]["out"].endswith("/Exp_A__aaaaaaaa.xic.lance")
    assert skipped == {"no_xic_dbs_on_flinders": 1, "parked": 1, "path_occupied_unregistered": 1}


def test_xic_set_needs_the_complete_marker(tmp_path=None):
    import fill_spectronaut_xic_lanes as X
    d = tempfile.mkdtemp()
    rep = os.path.join(d, "x_Report_FRAN (Normal).tsv")
    os.makedirs(os.path.join(d, "xic_dbs"))
    assert X.xic_set_for(rep) is None
    open(os.path.join(d, "xic_dbs", "COMPLETE.txt"), "w").write("1 files")
    assert X.xic_set_for(rep) == os.path.join(d, "xic_dbs")
