"""auto_ingest can no longer be wedged by the candidates at the head of its list.

2026-09-17 .. 09-24: twelve consecutive runs ingested nothing. Each re-tried the same five
alphabetically-first FRAN_reports candidates (three duplicates, a truncated export, a header-only
export) because nothing remembered they had been tried; drop-box searches sorted behind them were
never reached. This pins the fix:

  1. backoff 4 h -> 1 d, quarantine after 3 failures, --clear
  2. a duplicate is resolved once and never re-picked
  3. the five-stuck scenario: WITHOUT memory the same five come back; WITH it fresh ones are reached
  4. the drop box: first, oldest-staged first (staged_at > manifest mtime); identity from a VALID
     manifest; legacy bare-symlink entries; QC policy (pinned vectors shared with the skill);
     manifest-vs-search FASTA cross-check; already-in-corpus at scan time AND at ingest time;
     one ingest per output_dir per run
  5. systemic failures: global ones stop the run, import failures hold back one engine, argparse
     skew is systemic, repeated "systemic" deferrals are eventually charged; queue rows uncharged
  6. state file: atomic replace, torn-read retry, corrupt file moved aside, the mkdir lock (blocks a
     second writer, breaks a stale lock, no lost updates under concurrent writers), identity leases
  7. alerts: stuck / engine / needs-a-person, once per episode; empty queue silent; crash and
     killed runs count; the webhook never reaches stdout, the return value or the posted text
  8. report_problem: header-only, truncated TSV, parquet without footer

Run:  python tests/test_auto_ingest_starvation.py     (no pytest, no database, no network)
"""
import contextlib, io, json, os, shutil, subprocess, sys, tempfile, textwrap, time, types
import urllib.error, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__))
INGEST = os.path.join(HERE, "..", "ingest")
sys.path.insert(0, INGEST)
import auto_ingest as ai                                    # noqa: E402
import auto_ingest_alert as aia                             # noqa: E402
import auto_ingest_state as ais                             # noqa: E402
import find_uningested as fu                                # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)

H = 3600.0
T0 = 1_790_000_000.0                                        # 2026-09-21, a fixed clock
CLOCK = [T0]
ais._clock = lambda: CLOCK[0]

HEADER = "\t".join(["R.FileName", "PG.ProteinGroups", "PEP.StrippedSequence", "EG.ModifiedSequence",
                    "FG.Charge", "EG.Qvalue"] + [f"X.Col{i:03d}" for i in range(120)]) + "\n"
ROW = "\t".join(["run1", "P12345", "PEPTIDEK", "_PEPTIDEK_", "2", "0.001"] + ["1"] * 120) + "\n"

# What corpus_ingest actually printed on Hive, 2026-09-24 (auto_ingest_23988552.out), trimmed.
COLLATION = ('WARNING:  database "delimp" has a collation version mismatch\n'
             'DETAIL:  The database was created using collation version 2.36, but the operating '
             'system provides version 2.41.\n')
OUT = {
    "dup": (0, "  SKIPPED — DUPLICATE of an already-ingested search.\n    this : X\n"
               "    exists: R:\\Data\\lab\\service\\x.sne\n", COLLATION),
    "notnull": (1, "", COLLATION + textwrap.dedent("""\
        Traceback (most recent call last):
          File "/quobyte/proteomics-grp/brett/glendon/fran_ingest/corpus_ingest.py", line 1139, in <module>
            ingest(a.searchdir, a.engine, a.organism_name, a.taxon, a.name, a.dry_run, a.output_dir)
          File "/quobyte/proteomics-grp/brett/glendon/fran_ingest/corpus_ingest.py", line 1056, in _copy_precursors
            cur.copy_expert(f"COPY delimp_precursors ({cols}) FROM STDIN", buf)
        psycopg2.errors.NotNullViolation: null value in column "charge" of relation "delimp_precursors" violates not-null constraint
        DETAIL:  Failing row contains (ce28cbba, NAN, nan, null, A0A1S3DYR5).
        CONTEXT:  COPY delimp_precursors, line 1874407
        """)),
    "noparse": (1, "", COLLATION + "No precursor records parsed (check the report / --engine).\n"),
    "stale": (1, "  ingest-gate: STALE corpus_ingest.py  local=53db7317 expected=6229b50f\n",
              "REFUSING TO INGEST: 1 corpus-writing script(s) are stale: corpus_ingest.py. "
              "Ingesting with these produces rows that must be re-ingested later.\n"),
    "importerr": (1, "", "Traceback (most recent call last):\n"
                         '  File "corpus_ingest.py", line 300, in ingest\n'
                         "    from spectronaut_to_corpus import iter_records\n"
                         "ModuleNotFoundError: No module named 'spectronaut_to_corpus'\n"),
    "ok": (0, "  ingested 12,345 precursors\n", COLLATION),
}

CORPUS = set()      # output_dirs "in the corpus": what the fake _already_ingested answers from


class FakeIngest:
    """Stands in for `corpus_ingest.py`: the outcome is looked up by --name. An "ok" puts the
    output_dir into CORPUS, as a real ingest would."""
    def __init__(self, behaviour):
        self.behaviour, self.calls = behaviour, []
    def __call__(self, cmd, **kw):
        name = cmd[cmd.index("--name") + 1]
        self.calls.append(name)
        outcome = self.behaviour.get(name, "ok")
        rc, out, err = OUT[outcome]
        if outcome == "ok":
            CORPUS.add(cmd[cmd.index("--output-dir") + 1].rstrip("/"))
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr=err)


class FakeConn:
    """psycopg2 does not autocommit; the corpus re-check must close its read transaction."""
    def __init__(self):
        self.commits = 0
    def cursor(self):
        return object()
    def commit(self):
        self.commits += 1
    def rollback(self):
        pass


def install_fake_queue(rows=()):
    """fran_queue talks to PG Farm; replace only its network edge, as test_auto_ingest_queue_xic does."""
    q = types.ModuleType("fran_queue")
    q.calls = []
    q.conn = FakeConn()
    q._conn = lambda: q.conn
    q.claim_batch = lambda con, limit, claimed_by: [dict(r) for r in rows]
    q.mark_done = lambda con, row_id, search_id=None: q.calls.append(("done", row_id))
    q.mark_failed = lambda con, row_id, err: q.calls.append(("failed", row_id)) or "queued"
    q.mark_xic = lambda con, row_id, status, error=None: q.calls.append(("xic", row_id, status))
    q._already_ingested = (lambda cur, od: ("sid-" + od) if str(od).rstrip("/") in CORPUS else None)
    sys.modules["fran_queue"] = q
    return q


def make_export(root, search, export, body=True, truncated=False):
    d = os.path.join(root, search, export)
    os.makedirs(d, exist_ok=True)
    text = HEADER + (ROW * 3 if body else "")
    if truncated:
        text = text[:-40]                                   # ends mid-line, as Nuciser's does
    with open(os.path.join(d, f"{export}_Report_FRAN (Normal).tsv"), "w") as fh:
        fh.write(text)
    return d


def run_main(argv, ingest):
    real = ai.subprocess.run
    ai.subprocess.run = ingest
    buf, old = io.StringIO(), sys.argv
    sys.argv = ["auto_ingest.py"] + argv
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = ai.main()
    finally:
        sys.argv = old
        ai.subprocess.run = real
    return rc, buf.getvalue()


def run_direct(chosen, qcon, ingest, store=None, limit=5, skipped=()):
    """_run() on an explicit candidate list, as main() would call it."""
    real = ai.subprocess.run
    ai.subprocess.run = ingest
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            ai._run(types.SimpleNamespace(apply=True, limit=limit, python=sys.executable,
                                          timeout=60, no_alert=True), chosen, list(skipped), qcon,
                    store=store)
    finally:
        ai.subprocess.run = real
    return buf.getvalue()


def done_line(out):
    return next((ln for ln in out.splitlines() if ln.startswith("===== done")), "")


with tempfile.TemporaryDirectory() as tmp:
    reports = os.path.join(tmp, "FRAN_reports")

    # ============ 1. backoff progression and quarantine =======================================
    print("\n1. backoff and quarantine")
    st = ais.AttemptStore(os.path.join(tmp, "s1", "state.json"))
    k = "/x/FRAN_reports/bad/export"
    r = st.record(k, "fail", "boom", now=T0)
    check("1st failure backs off 4 h", r["status"] == "backoff" and r["attempts"] == 1, str(r))
    check("  still held at +3 h 59 m", st.status(k, T0 + 4 * H - 60)[0] == "backoff")
    check("  eligible again at +4 h", st.status(k, T0 + 4 * H + 1)[0] == "eligible")
    r = st.record(k, "fail", "boom", now=T0 + 5 * H)
    check("2nd failure backs off 1 d", st.status(k, T0 + 5 * H + 23 * H)[0] == "backoff"
          and st.status(k, T0 + 5 * H + 24 * H + 1)[0] == "eligible", str(r))
    r = st.record(k, "fail", "boom\nlast line of the error", now=T0 + 40 * H)
    check("3rd failure quarantines", r["status"] == "quarantined", str(r))
    check("  quarantine does not expire", st.status(k, T0 + 400 * 24 * H)[0] == "quarantined")
    check("  error tail kept for the human", "last line of the error" in r.get("last_error_tail", ""))
    st5 = ais.AttemptStore(os.path.join(tmp, "s1b", "state.json"), backoff_hours=(1, 2),
                           max_failures=5)
    for n in range(4):
        st5.record("k", "fail", now=T0)
    check("backoff beyond the schedule repeats its last step",
          st5.status("k", T0 + 2 * H - 60)[0] == "backoff" and
          st5.status("k", T0 + 2 * H + 1)[0] == "eligible")
    check("timeout is charged like a failure", st5.record("k2", "timeout", now=T0)["attempts"] == 1)

    # ============ 2 + 3. the five-stuck scenario =============================================
    print("\n2/3. the five candidates that wedged the head, 2026-09-17 .. 09-24")
    bad = [  # (search, export, outcome) -- oldest-staged AND alphabetically first, as on Hive
        ("20201111_113616_talbot_smoker", "20260626_035518_talbot smoker", "dup"),
        ("20201210_133218_Set_1_swabs", "20260826_201031_20201210_133218_Set_1_swabs", "dup"),
        ("20220207_164923_Nuciser_Plate", "20260826_202000_20220207_164923_Nuciser_Plate", "notnull"),
        ("20220330_125626_Chicken_DIA", "20260826_054319_20220330_125626_Chicken_DIA", "dup"),
        ("20220330_153755_Chicken_DIA", "20260826_055612_20220330_153755_Chicken_DIA", "noparse"),
    ]
    fresh = [
        ("20230215_155135_SpN_MuckeRatEx", "20260827_194712_20230215_155135_SpN_MuckeRatEx", "ok"),
        ("SpN_DWang-81to260Peps-HoSa", "20260830_154757_SpN_DWang-81to260Peps-HoSa", "ok"),
        ("Short_Course_2024_Data", "20260830_201044_Short Course 2024 Data", "ok"),
    ]
    cands = [{"dir": make_export(reports, s, e), "engine": "spectronaut", "real": None}
             for s, e, _ in bad + fresh]
    behaviour = {s: o for s, _, o in bad + fresh}
    cjson = os.path.join(tmp, "cands.json")
    json.dump(cands, open(cjson, "w"))

    # WITHOUT memory (store=None: the pre-fix behaviour) the same five come back every run.
    install_fake_queue()
    legacy = []
    for _ in range(2):
        fake = FakeIngest(behaviour)
        run_direct(ai.select(cands)[0], FakeConn(), fake)
        legacy.append(fake.calls)
    check("reproduced: without memory, run 2 retries exactly run 1's five",
          legacy[0] == legacy[1] and set(legacy[0]) == {s for s, _, _ in bad}, str(legacy))

    # WITH memory, through main() as the cron runs it.
    state = os.path.join(tmp, "state", "attempts.json")
    base = ["--apply", "--candidates", cjson, "--state-file", state, "--limit", "5", "--no-alert",
            "--timeout", "60"]
    CLOCK[0] = T0
    fake1 = FakeIngest(behaviour)
    rc, out1 = run_main(base, fake1)
    check("run 1 tries the same five (nothing remembered yet)",
          set(fake1.calls) == {s for s, _, _ in bad} and len(fake1.calls) == 5, str(fake1.calls))
    check("run 1 summary: 0 ingested, 3 duplicates, 2 failed",
          "0 ingested, 3 duplicate-skipped, 2 failed" in done_line(out1), done_line(out1))
    check("run 1 done line carries quarantined/backed-off counts",
          "0 quarantined, 2 backed-off" in done_line(out1), done_line(out1))

    CLOCK[0] = T0 + 1 * H                                  # an early re-run: failures still backed off
    fake2 = FakeIngest(behaviour)
    rc, out2 = run_main(base, fake2)
    check("next run reaches the fresh candidates at once", fake2.calls == [s for s, _, _ in fresh],
          str(fake2.calls))
    check("duplicates are never re-picked", not (set(fake2.calls) & {s for s, _, o in bad if o == "dup"}))
    check("failed head candidates do not block the rest", "3 ingested" in done_line(out2),
          done_line(out2))

    CLOCK[0] = T0 + 4 * H + 60                             # backoff over: each failure gets attempt 2
    fake3 = FakeIngest(behaviour)
    run_main(base, fake3)
    check("after the 4 h backoff the two failures are retried (and only they)",
          sorted(fake3.calls) == sorted(s for s, _, o in bad if o in ("notnull", "noparse")),
          str(fake3.calls))
    CLOCK[0] = T0 + 8 * H + 120
    fake4 = FakeIngest(behaviour)
    run_main(base, fake4)
    check("second failure holds them for a day", fake4.calls == [], str(fake4.calls))
    CLOCK[0] = T0 + 4 * H + 60 + 24 * H + 60
    fake5 = FakeIngest(behaviour)
    rc, out5 = run_main(base, fake5)
    check("third failure quarantines both", "2 quarantined, 0 backed-off" in done_line(out5),
          done_line(out5))
    CLOCK[0] += 100 * 24 * H
    fake6 = FakeIngest(behaviour)
    run_main(base, fake6)
    check("quarantined candidates are never retried on their own", fake6.calls == [], str(fake6.calls))

    rc, lq = run_main(["--list-quarantine", "--state-file", state], FakeIngest({}))
    check("--list-quarantine lists both, with the error",
          "2 quarantined" in lq and "Nuciser" in lq and 'null value in column "charge"' in lq, lq)
    rc, cl = run_main(["--clear", "Chicken", "--state-file", state], FakeIngest({}))
    check("--clear with an ambiguous fragment clears nothing", rc == 1 and "nothing cleared" in cl, cl)
    rc, cl = run_main(["--clear", "Nuciser", "--state-file", state], FakeIngest({}))
    check("--clear with a unique fragment clears exactly one", rc == 0 and "cleared:" in cl, cl)
    fake7 = FakeIngest(behaviour)
    run_main(base, fake7)
    check("a cleared candidate is tried again", fake7.calls == ["20220207_164923_Nuciser_Plate"],
          str(fake7.calls))

    dry_state = os.path.join(tmp, "dry", "attempts.json")
    run_main(["--candidates", cjson, "--state-file", dry_state], FakeIngest(behaviour))
    check("dry run writes no state file", not os.path.exists(dry_state))

    # ============ 4. the drop box ==============================================================
    print("\n4. the drop box")
    check("the drop box is one of the roots find_uningested scans", fu.DROPBOX_ROOT in fu.DEFAULT_ROOTS)
    real_root = fu.DROPBOX_ROOT
    fu.DROPBOX_ROOT = os.path.join(tmp, "incoming")
    try:
        def drop(entry, name, mtime, raw=None, log=None, prov=None, **man):
            """A drop-box entry as fran_deposit.py stages it: a real directory holding the manifest
            and a link to the report. `raw` writes the manifest text verbatim (malformed cases);
            `log` / `prov` give the real search a report.log.txt / search_provenance.json."""
            real = os.path.join(tmp, "real", entry)
            os.makedirs(real, exist_ok=True)
            open(os.path.join(real, "report.parquet"), "wb").write(b"PAR1" + b"0" * 2000 + b"PAR1")
            if log is not None:
                open(os.path.join(real, "report.log.txt"), "w").write(log)
            if prov is not None:
                json.dump(prov, open(os.path.join(real, "search_provenance.json"), "w"))
            d = os.path.join(fu.DROPBOX_ROOT, entry)
            os.makedirs(d, exist_ok=True)
            if not os.path.exists(os.path.join(d, "report.parquet")):
                os.symlink(os.path.join(real, "report.parquet"), os.path.join(d, "report.parquet"))
            mp = os.path.join(d, fu.MANIFEST)
            if raw is not None:
                open(mp, "w").write(raw)
            elif man.pop("_no_manifest", None) is None:
                m = {"fran_manifest_version": 1, "output_dir": real, "engine": "diann",
                     "search_name": name, "organism": "Canis lupus familiaris", "taxon": 9615}
                m.update(man)
                json.dump({k: v for k, v in m.items() if v is not None}, open(mp, "w"))
            if os.path.exists(mp):
                os.utime(mp, (mtime, mtime))
            return {"dir": d, "engine": "diann", "real": None}, real

        # --- ordering and identity ---
        c_new, real_new = drop("search_out__e14aac29", "Dupanloup dog CSF (DIA-NN 2.7.0)", T0 - 3 * 24 * H)
        c_old, real_old = drop("GallPlasCer__5b11a0d9", "diann261_gallegos_plasma_Ceres", T0 - 29 * 24 * H)
        c_decl, _ = drop("declared__3", "declared_oldest", T0, staged_at="2026-01-01T00:00:00+00:00")
        c_badts, _ = drop("bad_ts__4", "bad_staged_at", T0 - 5 * 24 * H, staged_at="last tuesday")
        os.utime(c_new["dir"], (T0 - 100 * 24 * H,) * 2)       # the DIRECTORY's mtime must not rank
        undated = make_export(reports, "unzipped_NIST_Salmon", "unzipped_NIST_Salmon")
        os.utime(undated, (T0 - 365 * 24 * H, T0 - 365 * 24 * H))
        chosen, skipped = ai.select([c_new] + cands + [{"dir": undated, "engine": "spectronaut"},
                                                       c_old, c_decl, c_badts])
        order = [c["search"] for c in chosen]
        check("drop box first, oldest-staged first: staged_at, else the manifest's mtime",
              order[:4] == ["declared_oldest", "diann261_gallegos_plasma_Ceres", "bad_staged_at",
                            "Dupanloup dog CSF (DIA-NN 2.7.0)"], str(order[:5]))
        check("then FRAN_reports, oldest export first (undated falls back to mtime)",
              order[4] == "unzipped_NIST_Salmon" and order[5] == "20201111_113616_talbot_smoker"
              and order[-1] == "Short_Course_2024_Data", str(order))
        g = next(c for c in chosen if c["search"].startswith("diann261"))
        check("drop-box identity is the manifest's output_dir, not the incoming/ path",
              g["identity"] == real_old and g["dir"] == real_old, str(g))
        check("drop-box organism and taxon come from the manifest",
              g.get("organism") == "Canis lupus familiaris" and g.get("taxon") == "9615", str(g))
        check("attempts are remembered under the staged entry",
              g["attempt_key"] == os.path.realpath(c_old["dir"]), g["attempt_key"])

        # --- defensive manifest validation: skipped with the reason, never charged ---
        malformed = {
            "wrong_engine__1": drop("wrong_engine__1", "x", T0, engine="spectronaut")[0],
            "no_output_dir__2": drop("no_output_dir__2", "y", T0, output_dir=None)[0],
            "relative_dir__5": drop("relative_dir__5", "z", T0, output_dir="search_out")[0],
            "bad_taxon__6": drop("bad_taxon__6", "t", T0, taxon="dog")[0],
            "not_json__7": drop("not_json__7", "j", T0, raw="{ this is not json")[0],
            "a_list__8": drop("a_list__8", "l", T0, raw="[1, 2]")[0],
            "future_version__9": drop("future_version__9", "v", T0, fran_manifest_version=2)[0],
            "no_manifest__10": drop("no_manifest__10", "n", T0, _no_manifest=True)[0],
            "bad_exclude__16": drop("bad_exclude__16", "e", T0, exclude="yes")[0],
        }
        why = dict(ai.select(list(malformed.values()))[1])
        for entry, want in (("wrong_engine__1", "engine"), ("no_output_dir__2", "output_dir"),
                            ("relative_dir__5", "output_dir"), ("bad_taxon__6", "taxon"),
                            ("not_json__7", "unreadable"), ("a_list__8", "not a JSON object"),
                            ("future_version__9", "version"), ("no_manifest__10", "no fran_manifest"),
                            ("bad_exclude__16", "exclude")):
            check(f"malformed manifest is skipped with its reason: {entry}",
                  want in why.get(entry, "") and why[entry].startswith("drop box:"), why.get(entry))
        djson = os.path.join(tmp, "drop.json")
        json.dump([c_new] + list(malformed.values()), open(djson, "w"))
        dstate = os.path.join(tmp, "s4", "attempts.json")
        install_fake_queue()
        fake = FakeIngest({})
        CLOCK[0] = T0
        rc, od = run_main(["--apply", "--candidates", djson, "--state-file", dstate, "--no-alert"],
                          fake)
        recs = json.load(open(dstate))["candidates"] if os.path.exists(dstate) else {}
        check("malformed entries are not attempted and leave no attempt record",
              fake.calls == ["Dupanloup dog CSF (DIA-NN 2.7.0)"] and
              not any(k.endswith(tuple(malformed)) for k in recs), f"{fake.calls} {list(recs)}")
        check("  ...and each is logged as a SKIP line", all(f"SKIP {e}" in od for e in malformed),
              od[-600:])

        # --- the ORIGINAL staging format: a bare symlink to the output dir, no manifest ---
        legacy_real = os.path.join(tmp, "real", "legacy_search")
        os.makedirs(legacy_real, exist_ok=True)
        open(os.path.join(legacy_real, "report.parquet"), "wb").write(b"PAR1" + b"0" * 2000 + b"PAR1")
        legacy_link = os.path.join(fu.DROPBOX_ROOT, "legacy_search")
        os.symlink(legacy_real, legacy_link)
        ch, sk = ai.select([{"dir": legacy_link, "engine": "diann", "real": legacy_real}])
        check("a legacy bare-symlink entry needs no manifest: identity is the link's target",
              len(ch) == 1 and ch[0]["identity"] == os.path.realpath(legacy_real)
              and ch[0].get("identity_from") != "manifest", f"{ch} {sk}")

        # --- QC precedence: qc/exclude: true > DEFAULT_EXCLUDES path > qc: false > QC_NAME_RE
        # (vectors and precedence pinned in both repos)
        glendon = "/quobyte/proteomics-grp/brett/glendon/scratch/search_out"
        for label, args, want in (
                ("qc: true wins over everything", (glendon, "plain", True, None), "qc: true"),
                ("exclude: true excludes", ("/a/b/c", "plain", None, True), "exclude: true"),
                ("a QC/scratch ROOT beats qc: false", (glendon, "plain", False, None),
                 "DEFAULT_EXCLUDES"),
                ("qc: false beats the NAME rule", ("/a/b/c", "Lumos QC", False, None), None),
                ("the ROOT itself matches its pattern (no trailing slash)",
                 ("/quobyte/proteomics-grp/brett/glendon", "plain", False, None), "brett/glendon/"),
                ("the laptop SMB spelling /Volumes/proteomics-grp/... maps to /quobyte/...",
                 ("/Volumes/proteomics-grp/brett/glendon/scratch/x", "plain", False, None),
                 "brett/glendon/"),
                ("an unrelated /Volumes path is not excluded",
                 ("/Volumes/other-share/brett/glendon_like/x", "plain", False, None), None),
                ("no flag: the name rule", ("/a/b/c", "Lumos QC", None, None), "QC_NAME_RE")):
            got = fu.qc_reason(*args)
            check(f"QC precedence: {label}", (got is None) if want is None else (want in (got or "")),
                  str(got))
        for n in ("chkLUppm_HeLa50_2026 Lumos QC", "QC_run_01", "hela_qc_2", "Exploris QC2"):
            check(f"QC_NAME_RE excludes {n!r}", fu.qc_reason("/a/b/c", n) is not None)
        for n in ("HeLa_digest_timecourse", "aqc_buffer_study", "QCM_study", "Plasma_liver2"):
            check(f"QC_NAME_RE keeps {n!r}", fu.qc_reason("/a/b/c", n) is None)
        check("the regex is the one the skill mirrors",
              fu.QC_NAME_RE.pattern == r"(?i)(?<![a-z0-9])qc(?![a-z])")
        qc_entry, _ = drop("search__9ff203cf", "chkLUppm_HeLa50_2026 Lumos QC", T0 - 24 * H)
        hela, _ = drop("hela_study__11", "Smith HeLa phospho knockdown", T0 - 24 * H)
        flagged, _ = drop("flagged__12", "plain name", T0 - 24 * H, qc=True)
        excl, _ = drop("excluded__17", "plain name 2", T0 - 24 * H, exclude=True)
        unflagged, _ = drop("unflagged__13", "Lumos QC but the producer says not", T0 - 24 * H,
                            qc=False)
        stan, _ = drop("stan__15", "s", T0 - 24 * H, output_dir="/quobyte/proteomics-grp/STAN/proc/x")
        scratch, _ = drop("scratch_qcfalse__18", "a scratch search", T0 - 24 * H, qc=False,
                          output_dir=glendon)
        chosen_q, skipped_q = ai.select([qc_entry, hela, flagged, excl, unflagged, stan, scratch])
        why = dict(skipped_q)
        names_q = [c["search"] for c in chosen_q]
        check("QC: gabrig's search__9ff203cf is excluded",
              why.get("search__9ff203cf", "").startswith("qc: "), str(why))
        check("QC: a customer study OF HeLa is kept", "Smith HeLa phospho knockdown" in names_q)
        check("QC: manifest qc: true excludes", "qc: true" in why.get("flagged__12", ""), str(why))
        check("QC: manifest exclude: true excludes", "exclude: true" in why.get("excluded__17", ""))
        check("QC: manifest qc: false keeps a QC-looking name",
              "Lumos QC but the producer says not" in names_q, str(names_q))
        check("QC: an output_dir under DEFAULT_EXCLUDES is excluded (no flag)",
              "DEFAULT_EXCLUDES" in why.get("stan__15", ""), str(why))
        check("QC: a qc: false manifest staged from brett/glendon/ is still excluded, with the path "
              "reason (the skill writes qc: false into every manifest)",
              why.get("scratch_qcfalse__18", "") ==
              "qc: output_dir is under /quobyte/proteomics-grp/brett/glendon/ (DEFAULT_EXCLUDES)",
              str(why))
        qjson = os.path.join(tmp, "qc.json")
        json.dump([qc_entry], open(qjson, "w"))
        qstate = os.path.join(tmp, "s4", "qc_attempts.json")
        fake, outs_q = FakeIngest({}), []
        for i in range(4):
            CLOCK[0] = T0 + i * 4 * H
            outs_q.append(run_main(["--apply", "--candidates", qjson, "--state-file", qstate,
                                    "--no-alert"], fake)[1])
        qs = json.load(open(qstate))
        check("QC: never attempted, never charged", fake.calls == [] and qs["candidates"] == {})
        check("QC: never counts toward the stuck alert",
              qs["runs"]["consecutive_zero"] == 0 and not any("stuck-watch" in o for o in outs_q))
        check("QC: logged as 'skipped (qc: <reason>)'",
              "SKIP search__9ff203cf  skipped (qc: " in outs_q[0], outs_q[0][-400:])
        check("QC: the entry stays in incoming/", os.path.isfile(os.path.join(qc_entry["dir"],
                                                                              fu.MANIFEST)))

        # --- manifest vs the search's own FASTA record ---
        diann_cmd = ("diann-linux --f a.d --lib x --fasta {} --fasta-search --threads 32\n"
                     "DIA-NN 2.7.0 ...\n")
        mm, _ = drop("mouse_mismatch__20", "PROT_0793 mouse", T0 - 24 * H, organism="Mus musculus",
                     taxon=10090, fasta_path="/quobyte/proteomics-grp/brett/PROT_0793/human_UP000005640.fasta",
                     log=diann_cmd.format("/quobyte/proteomics-grp/brett/PROT_0793/mouse_UP000000589_mousecont.fasta"))
        mt, _ = drop("dog_match__21", "dog CSF", T0 - 24 * H,
                     fasta_path="/Users/b/sessions/input/dog_UP000805418_opg_plus_universal_contam.fasta",
                     log=diann_cmd.format("/quobyte/SERVICE/input/dog_UP000805418_opg_plus_universal_contam.fasta"))
        ab, _ = drop("no_record__22", "no record", T0 - 24 * H, fasta_path="/x/whatever.fasta")
        pv, _ = drop("prov_mismatch__23", "prov", T0 - 24 * H, fasta_path="/x/human.fasta",
                     prov={"engine": "diann", "fasta": "/nfs/x/input/search.fasta"})
        og, _ = drop("org_mismatch__24", "org", T0 - 24 * H, organism="Homo sapiens",
                     prov={"engine": "diann", "organism": "Mus musculus"})
        chosen_f, skipped_f = ai.select([mm, mt, ab, pv, og])
        why, names_f = dict(skipped_f), [c["search"] for c in chosen_f]
        check("FASTA mismatch (the real PROT_0793 case) is skipped as manifest_fasta_mismatch",
              why.get("mouse_mismatch__20") == "manifest_fasta_mismatch: manifest=human_UP000005640.fasta"
              " search=mouse_UP000000589_mousecont.fasta", str(why))
        check("FASTA match by basename (laptop path vs Hive path) proceeds", "dog CSF" in names_f)
        check("no record of its own: the manifest stands", "no record" in names_f)
        check("search_provenance.json 'fasta' is a record too",
              why.get("prov_mismatch__23", "").startswith("manifest_fasta_mismatch"), str(why))
        check("--fasta-search is not mistaken for a --fasta argument",
              "--fasta-search" not in why.get("mouse_mismatch__20", ""))
        check("a recorded organism that disagrees is skipped",
              why.get("org_mismatch__24", "").startswith("manifest_organism_mismatch"), str(why))
        mjson = os.path.join(tmp, "mm.json")
        json.dump([mm], open(mjson, "w"))
        rc, om = run_main(["--apply", "--candidates", mjson, "--state-file",
                           os.path.join(tmp, "s4", "mm.json"), "--no-alert"], FakeIngest({}))
        check("a mismatch is logged as 'skipped (manifest_fasta_mismatch: ...)', not charged",
              "skipped (manifest_fasta_mismatch: manifest=human_UP000005640.fasta" in om and
              json.load(open(os.path.join(tmp, "s4", "mm.json")))["candidates"] == {}, om[-500:])
        check("  ...and is due as an alert that needs a person",
              "ALERT DUE (human:mouse_mismatch__20)" in om, om[-300:])

        # --- the ingest command carries the manifest's identity ---
        CORPUS.clear()
        install_fake_queue()
        fake, cmds = FakeIngest({}), []
        out = run_direct([g], FakeConn(), lambda cmd, **kw: (cmds.append(cmd), fake(cmd, **kw))[1],
                         limit=1)
        cmd = cmds[0] if cmds else []
        check("the ingest command carries the manifest's identity",
              cmd and cmd[cmd.index("--output-dir") + 1] == real_old and
              cmd[cmd.index("--name") + 1] == "diann261_gallegos_plasma_Ceres" and
              cmd[cmd.index("--organism-name") + 1] == "Canis lupus familiaris", str(cmd) + out)

        # --- already in the corpus: at SCAN time (known paths) ... ---
        paths = {fu.norm_path(real_old)}
        with contextlib.redirect_stdout(io.StringIO()) as w:
            found, _ = fu.scan([fu.DROPBOX_ROOT], paths, set(), set())
        dirs = {os.path.basename(f["dir"]) for f in found}
        check("scan: a staged entry whose manifest output_dir is a corpus path is skipped (Gallegos)",
              "GallPlasCer__5b11a0d9" not in dirs and "already in the corpus" in w.getvalue(),
              f"{sorted(dirs)}")
        check("scan: an entry whose output_dir is new is still a candidate",
              "search_out__e14aac29" in dirs, str(sorted(dirs)))
        os.makedirs(os.path.join(fu.DROPBOX_ROOT, ".excluded"), exist_ok=True)
        shutil.move(qc_entry["dir"], os.path.join(fu.DROPBOX_ROOT, ".excluded", "search__9ff203cf"))
        with contextlib.redirect_stdout(io.StringIO()):
            found, _ = fu.scan([fu.DROPBOX_ROOT], set(), set(), set())
        check("scan: incoming/.excluded/ (set aside by a person) is never scanned",
              not any(".excluded" in f["dir"] for f in found), str([f["dir"] for f in found]))

        # --- ... AND at INGEST time: the corpus can change between scan and ingest ---
        CORPUS.clear()
        CORPUS.add(real_old)                            # appears after the scan chose it
        store4 = ais.AttemptStore(os.path.join(tmp, "s4", "ingest_time.json"))
        fake = FakeIngest({})
        out = run_direct([g], FakeConn(), fake, store=store4)
        check("ingest time: already in the corpus -> NOT re-ingested (Gallegos: delete+insert)",
              fake.calls == [] and "ALREADY IN THE CORPUS" in out, out[-400:])
        check("  ...remembered as resolved", store4.status(g["attempt_key"])[0] == "done")
        fr = ai.select([cands[-1]])[0][0]               # a FRAN_reports candidate
        CORPUS.add(fr["identity"])
        fake = FakeIngest({})
        out = run_direct([fr], FakeConn(), fake)
        check("ingest time: the re-check covers FRAN_reports candidates too", fake.calls == [], out)
        out = run_direct([g], None, FakeIngest({}),
                         store=ais.AttemptStore(os.path.join(tmp, "s4", "noconn.json")))
        check("ingest time: no connection to re-check with -> systemic stop, never blind",
              "SYSTEMIC FAILURE" in out and "re-check the corpus" in out, out[-400:])

        # --- a queue row and a staged entry for the SAME output_dir, in one run ---
        CORPUS.clear()
        q = install_fake_queue([{"id": 51, "output_dir": real_new, "searchdir": real_new,
                                 "engine": "diann", "search_name": "Dupanloup dog CSF (queue)",
                                 "attempts": 0}])
        sjson = os.path.join(tmp, "same.json")
        json.dump([c_new], open(sjson, "w"))
        fake = FakeIngest({})
        rc, os_ = run_main(["--apply", "--candidates", sjson, "--state-file",
                            os.path.join(tmp, "s4", "same.json"), "--no-alert"], fake)
        check("queued + staged, same output_dir, one run: corpus_ingest runs ONCE",
              fake.calls == ["Dupanloup dog CSF (queue)"], f"{fake.calls}\n{os_[-600:]}")
        check("  ...the second is recognised, not ingested",
              "already handled earlier in this run" in os_ or "ALREADY IN THE CORPUS" in os_)
        install_fake_queue()
    finally:
        fu.DROPBOX_ROOT = real_root

    # ============ 5. systemic failures ========================================================
    print("\n5. systemic failures are not the candidate's fault")
    cls = ais.classify_failure
    check("stale-code refusal is global", cls(*OUT["stale"][1:])[0] == "global")
    check("the real NotNullViolation is NOT systemic", cls(*OUT["notnull"][1:]) == (None, None))
    check("'No precursor records parsed' is NOT systemic", cls(*OUT["noparse"][1:])[0] is None)
    s, why = cls(*OUT["importerr"][1:])
    check("a missing module is ENGINE-scoped, and names the module",
          s == "engine" and "spectronaut_to_corpus" in why, str((s, why)))
    check("an unreachable database is global",
          cls("", "Traceback (most recent call last):\n  File \"x.py\", line 9, in _conn\n"
                  "psycopg2.OperationalError: connection to server at \"pgfarm.library.ucdavis.edu\" "
                  "(169.237.1.1), port 5432 failed: Connection refused\n")[0] == "global")
    check("a failed token exchange is global",
          cls("", "Traceback (most recent call last):\n  File \"r.py\", line 40, in _token\n"
                  "    raise\nurllib.error.HTTPError: HTTP Error 401: Unauthorized\n")[0] == "global")
    check("argparse 'unrecognized arguments' (auto_ingest/corpus_ingest skew) is global",
          cls("", "usage: corpus_ingest.py [-h] [--engine ENGINE] searchdir\n"
                  "corpus_ingest.py: error: unrecognized arguments: --bulk-copy\n")[0] == "global")
    check("argparse 'invalid value' after a usage: line is NOT systemic (bad data in the candidate)",
          cls("", "usage: corpus_ingest.py [-h] [--taxon TAXON] searchdir\n"
                  "corpus_ingest.py: error: argument --taxon: invalid int value: 'dog'\n")
          == (None, None))
    check("argparse 'the following arguments are required' is global",
          cls("", "corpus_ingest.py: error: the following arguments are required: --output-dir\n")[0]
          == "global")
    check("a mid-COPY disconnect is NOT systemic (a huge candidate can cause it)",
          cls("", "Traceback (most recent call last):\n  File \"c.py\", line 1056, in _copy\n"
                  "psycopg2.OperationalError: server closed the connection unexpectedly\n")[0] is None)
    check("an 'ImportError' warning earlier in the output does not make a real failure systemic",
          cls("  [warn] optional pyteomics: ImportError: No module named pyteomics; using fallback\n"
              + "x\n" * 10, OUT["notnull"][2])[0] is None)

    # global: stop the run; a queue row is left claimed, uncharged
    sys_state = os.path.join(tmp, "sys", "attempts.json")
    CLOCK[0] = T0
    CORPUS.clear()
    q = install_fake_queue([{"id": 41, "output_dir": "/q/a", "searchdir": "/q/a", "engine": "diann",
                             "search_name": "registered_one", "attempts": 0}])
    rc, outs = run_main(["--apply", "--candidates", cjson, "--state-file", sys_state, "--limit", "5",
                         "--no-alert"], FakeIngest({"registered_one": "stale"}))
    check("a global systemic failure stops the run after the first candidate",
          "stopped by a systemic error" in done_line(outs) and outs.count("SYSTEMIC FAILURE") == 1,
          done_line(outs))
    check("  ...logged prominently, saying it was not charged",
          "### SYSTEMIC FAILURE" in outs and "NOT charged" in outs, outs[-800:])
    check("  ...a queue row hit by it is NOT charged (left claimed to lapse)",
          ("failed", 41) not in q.calls, str(q.calls))
    install_fake_queue()
    stale_all = FakeIngest({s: "stale" for s, _, _ in bad + fresh})
    CLOCK[0] = T0 + 60
    run_main(["--apply", "--candidates", cjson, "--state-file", sys_state, "--limit", "5",
              "--no-alert"], stale_all)
    recs = json.load(open(sys_state))["candidates"]
    check("scan candidates hit by it are deferred, not charged",
          any(r.get("status") == "deferred" for r in recs.values())
          and not [r for r in recs.values() if r.get("attempts")], str(recs))
    check("  ...and it was the head candidate", stale_all.calls == ["20201111_113616_talbot_smoker"],
          str(stale_all.calls))
    fake_next = FakeIngest({s: "stale" for s, _, _ in bad + fresh})
    CLOCK[0] = T0 + 120
    run_main(["--apply", "--candidates", cjson, "--state-file", sys_state, "--limit", "5",
              "--no-alert"], fake_next)
    check("the deferred candidate does not hold the head next run",
          fake_next.calls and fake_next.calls[0] != "20201111_113616_talbot_smoker", str(fake_next.calls))

    # a QUEUE-only fault (no table, no grant, a claim_batch error) must not halt the scan half
    CORPUS.clear()
    q = install_fake_queue()
    q.conns = []
    def new_conn():
        q.conns.append(FakeConn())
        return q.conns[-1]
    def claim_fails(con, limit, claimed_by):
        raise RuntimeError('relation "delimp_ingest_queue" does not exist')
    q._conn, q.claim_batch = new_conn, claim_fails
    one = FakeIngest({})
    rc, oq = run_main(["--apply", "--candidates", cjson, "--state-file",
                       os.path.join(tmp, "sys", "queuefault.json"), "--limit", "2", "--no-alert"], one)
    check("a failed queue claim does not stop the scan half: its candidates still ingest",
          len(one.calls) == 2 and "SYSTEMIC" not in oq and "queue unavailable" in oq,
          f"{one.calls}\n{oq[-500:]}")
    check("  ...re-checking the corpus on a separate, fallback connection",
          len(q.conns) == 2 and q.conns[1].commits >= 2, f"{len(q.conns)} conns")
    def no_db():
        raise OSError("connection to server at pgfarm... failed: timeout expired")
    q._conn = no_db
    none_ = FakeIngest({})
    rc, on = run_main(["--apply", "--candidates", cjson, "--state-file",
                       os.path.join(tmp, "sys", "queuefault2.json"), "--no-alert"], none_)
    check("  ...and only if that connection fails too is it a systemic stop (never a blind ingest)",
          none_.calls == [] and "SYSTEMIC FAILURE" in on and "re-check the corpus" in on, on[-500:])
    install_fake_queue()

    # engine: hold back that engine only; the others carry on; its block alerts on its own
    CORPUS.clear()
    install_fake_queue()
    ddirs = []
    for n in ("dsearch1", "dsearch2"):
        d = os.path.join(tmp, "diann_searches", n)
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "report.parquet"), "wb").write(b"PAR1" + b"0" * 2000 + b"PAR1")
        ddirs.append(d)
    s1 = make_export(reports, "eng_sn_one", "20260101_000000_eng_sn_one")
    s2 = make_export(reports, "eng_sn_two", "20260101_000100_eng_sn_two")
    ejson = os.path.join(tmp, "engine.json")
    json.dump([{"dir": s1, "engine": "spectronaut"}, {"dir": s2, "engine": "spectronaut"}] +
              [{"dir": d, "engine": "diann"} for d in ddirs], open(ejson, "w"))
    eng = FakeIngest({"eng_sn_one": "importerr", "eng_sn_two": "importerr"})
    estate = os.path.join(tmp, "sys", "engine.json")
    outs_e = []
    for t in (T0, T0 + 4 * H + 60, T0 + 8 * H + 120):
        CLOCK[0] = t
        outs_e.append(run_main(["--apply", "--candidates", ejson, "--state-file", estate,
                                "--no-alert"], eng)[1])
    check("an import failure holds back ITS engine only: the other engine's candidates run",
          eng.calls[:3] == ["eng_sn_one", "dsearch1", "dsearch2"], str(eng.calls))
    check("  ...the rest of that engine is skipped this run, not charged",
          "spectronaut candidates are held back this run" in outs_e[0]
          and "held back: spectronaut" in done_line(outs_e[0]), done_line(outs_e[0]))
    check("  ...and a block that persists alerts on its own (3 runs), whatever else progressed",
          "ALERT DUE (engine:spectronaut)" in outs_e[2], outs_e[2][-300:])

    st6 = ais.AttemptStore(os.path.join(tmp, "sys", "repeat.json"))
    for i in range(3):
        st6.record("rk", "systemic", "x", now=T0 + i * 5 * H, reason="module 'x' failed")
    st6.record_run(2, 5, {}, now=T0 + 16 * H)            # others succeed meanwhile
    r = st6.record("rk", "systemic", "x", now=T0 + 20 * H, reason="module 'x' failed")
    check("a candidate deferred 3x as 'systemic' while others succeed is then charged",
          r["attempts"] == 1 and r["last_outcome"] == "systemic-repeat", str(r))
    st8 = ais.AttemptStore(os.path.join(tmp, "sys", "repeat3.json"))
    meta = {"engine": "spectronaut", "search": "s"}
    for i in range(3):
        st8.record("sk", "systemic", "x", meta=meta, now=T0 + i * 5 * H, scope="engine")
    st8.record_run(2, 5, {}, now=T0 + 16 * H, engines_progressed=["diann"])
    r = st8.record("sk", "systemic", "x", meta=meta, now=T0 + 20 * H, scope="engine")
    check("an ENGINE-scoped deferral is not charged on another engine's successes (a missing "
          "adapter would otherwise quarantine its engine's heads one by one)",
          r["attempts"] == 0 and r["status"] == "deferred", str(r))
    st8.record_run(1, 5, {}, now=T0 + 21 * H, engines_progressed=["spectronaut"])
    r = st8.record("sk", "systemic", "x", meta=meta, now=T0 + 25 * H, scope="engine")
    check("  ...but is, once its OWN engine succeeds in the meantime",
          r["attempts"] == 1 and r["last_outcome"] == "systemic-repeat", str(r))
    st7 = ais.AttemptStore(os.path.join(tmp, "sys", "repeat2.json"))
    for i in range(5):
        r = st7.record("rk", "systemic", "x", now=T0 + i * 5 * H)
    check("  ...but never while nothing else succeeds (a real outage)",
          r["attempts"] == 0 and r["status"] == "deferred", str(r))

    # ============ 6. state file ===============================================================
    print("\n6. state file: atomicity, torn reads, the lock, leases")
    sp = os.path.join(tmp, "s6", "state.json")
    st = ais.AttemptStore(sp)
    st.record("a", "duplicate", now=T0)
    before = open(sp, "rb").read()
    real_fsync = ais.os.fsync
    def boom(fd):
        raise OSError(28, "No space left on device")
    ais.os.fsync = boom
    try:
        with contextlib.redirect_stdout(io.StringIO()) as w:
            r = st.record("b", "fail", "x", now=T0)
    finally:
        ais.os.fsync = real_fsync
    check("a failed write leaves the previous file byte-identical",
          os.path.exists(sp) and open(sp, "rb").read() == before)
    check("  ...and no temp file behind",
          [f for f in os.listdir(os.path.dirname(sp)) if ".tmp." in f] == [])
    check("  ...is reported, and the run continues from memory",
          st.degraded and "NOT remembered" in w.getvalue() and r["attempts"] == 1, w.getvalue())

    st_t = ais.AttemptStore(sp)
    real_read, n_torn = st_t._read_once, [0]
    def torn_twice():
        if n_torn[0] < 2:
            n_torn[0] += 1
            raise ais._Torn("read 1213 of 16975 bytes")
        return real_read()
    st_t._read_once = torn_twice
    with contextlib.redirect_stdout(io.StringIO()) as w:
        data = st_t.load()
    check("a TORN read (as measured cross-node on Quobyte) is retried, not called corruption",
          "a" in data["candidates"] and not st_t.degraded and os.path.exists(sp)
          and not any(".corrupt-" in f for f in os.listdir(os.path.dirname(sp))), w.getvalue())

    open(sp, "w").write('{"candidates": {"a": {"status": "do')        # torn for good
    with contextlib.redirect_stdout(io.StringIO()) as w:
        data = ais.AttemptStore(sp).load()
    aside = [f for f in os.listdir(os.path.dirname(sp)) if ".corrupt-" in f]
    check("a file that STAYS unparseable is moved aside (kept, not deleted) and the run starts clean",
          data["candidates"] == {} and aside, w.getvalue())

    lp = os.path.join(tmp, "s6", "lock.json")
    worker = (f"import sys; sys.path.insert(0, {INGEST!r}); import auto_ingest_state as s; "
              f"st = s.AttemptStore({lp!r}, lock_wait=30); w = sys.argv[1]; "
              f"[st.record(f'k{{w}}_{{i}}', 'duplicate') for i in range(25)]")
    ais.AttemptStore(lp).record("seed", "duplicate")
    os.mkdir(lp + ".lockd")                                     # another process holds the lock
    p = subprocess.Popen([sys.executable, "-c", worker, "blocked"], stdout=subprocess.DEVNULL)
    time.sleep(1.5)
    blocked = p.poll() is None and "kblocked_0" not in json.load(open(lp))["candidates"]
    shutil.rmtree(lp + ".lockd")
    p.wait(timeout=60)
    check("the mkdir lock makes a second writer wait", blocked)
    check("  ...and it writes once the lock is released",
          "kblocked_24" in json.load(open(lp))["candidates"])
    procs = [subprocess.Popen([sys.executable, "-c", worker, str(w)], stdout=subprocess.DEVNULL)
             for w in range(6)]
    for p in procs:
        p.wait(timeout=120)
    keys = json.load(open(lp))["candidates"]
    n = sum(1 for k in keys if k.startswith("k") and not k.startswith("kblocked"))
    check("6 concurrent writers x 25 updates: none lost", n == 150, f"{n} of 150")
    os.mkdir(lp + ".lockd")
    os.utime(lp + ".lockd", (time.time() - 3600,) * 2)          # its holder died an hour ago
    with contextlib.redirect_stdout(io.StringIO()) as w:
        ais.AttemptStore(lp, lock_wait=10).record("after_stale", "duplicate")
    check("a STALE lock (holder dead) is broken and the write goes through",
          "after_stale" in json.load(open(lp))["candidates"] and "broke a lock" in w.getvalue()
          and not os.path.exists(lp + ".lockd"), w.getvalue())
    os.mkdir(lp + ".lockd")                                     # fresh: a live holder
    try:
        with contextlib.redirect_stdout(io.StringIO()) as w:
            ais.AttemptStore(lp, lock_wait=0.3).record("impatient", "duplicate")
    finally:
        shutil.rmtree(lp + ".lockd", ignore_errors=True)
    check("a live lock held too long is waited out, then written unlocked with a warning",
          "impatient" in json.load(open(lp))["candidates"] and "unlocked" in w.getvalue(),
          w.getvalue())

    # A/B/C: A died holding the lock. B judges A's lock stale -- and, before B renames it, C breaks
    # A's lock and takes a FRESH one. B's rename then moves C's LIVE lock; B must notice (the moved
    # directory's owner is C, not the A it judged), put it back, and wait like anyone else.
    lk = os.path.join(tmp, "s6", "abc.json")
    lkd = lk + ".lockd"
    os.makedirs(lkd)
    open(os.path.join(lkd, "owner"), "w").write("A:dead:1\n")
    os.utime(lkd, (time.time() - 3600,) * 2)
    b_store = ais.AttemptStore(lk, lock_wait=0.5)
    def c_interleaves(judged):
        if b_store._break_hook is None:
            return
        b_store._break_hook = None                 # once
        os.rename(lkd, lkd + ".c_grave")           # C breaks A's stale lock...
        shutil.rmtree(lkd + ".c_grave")
        os.mkdir(lkd)                              # ...and takes a fresh one
        open(os.path.join(lkd, "owner"), "w").write("C:live:2\n")
    b_store._break_hook = c_interleaves
    with contextlib.redirect_stdout(io.StringIO()) as w:
        b_store.record("from_b", "duplicate")
    owner_now = open(os.path.join(lkd, "owner")).read().strip() if os.path.isdir(lkd) else None
    check("lock race A/B/C: B restores C's live lock instead of destroying it",
          owner_now == "C:live:2" and "restored it to its live holder" in w.getvalue(),
          f"{owner_now}\n{w.getvalue()}")
    check("  ...waits for it rather than taking it, and leaves no stray directory behind",
          "still held" in w.getvalue() and
          [f for f in os.listdir(os.path.dirname(lk)) if f.startswith("abc.json.lockd.")] == [],
          os.listdir(os.path.dirname(lk)))
    shutil.rmtree(lkd)
    x = ais.AttemptStore(lk)
    assert x._lock()
    open(os.path.join(lkd, "owner"), "w").write("someone-else\n")   # broken + re-taken meanwhile
    with contextlib.redirect_stdout(io.StringIO()) as w:
        x._unlock()
    check("_unlock removes the lock only if it is still ours",
          os.path.isdir(lkd) and "left in place" in w.getvalue(), w.getvalue())
    shutil.rmtree(lkd)
    y = ais.AttemptStore(lk)
    assert y._lock()
    y._unlock()
    check("  ...and does remove its own", not os.path.exists(lkd))

    st = ais.AttemptStore(os.path.join(tmp, "s6", "lease.json"))
    got1 = st.claim("/od/x", "hostA:1", 600, attempt_key="k1", now=T0)
    got2 = st.claim("/od/x", "hostB:2", 600, now=T0 + 10)         # a queue row: no attempt_key
    check("leases are by OUTPUT_DIR: a queue row is kept off an output_dir a scan run holds",
          got1 == (True, None) and got2[0] is False, str((got1, got2)))
    e, hld = st.gate([{"identity": "/od/x", "attempt_key": "other"}], now=T0 + 20)
    check("  ...and the gate holds it as leased", hld and hld[0][1] == "leased", str(hld))
    e, hld = st.gate([{"identity": "/od/x/", "attempt_key": "other"}], now=T0 + 20)
    check("  ...a trailing slash does not make a leased output_dir look free (identity_of)",
          hld and hld[0][1] == "leased", str(hld))
    got3 = st.claim("/od/x", "hostB:2", 600, attempt_key="k1", now=T0 + 700)
    rec = st.load()["candidates"]["k1"]
    check("an expired lease with no outcome is charged as an abandoned attempt",
          got3[0] is False and rec["attempts"] == 1 and rec["last_outcome"] == "abandoned" and
          rec["status"] == "backoff", str((got3, rec)))
    st.claim("/od/y", "hostC:3", 600, now=T0)
    st.release("/od/y", "hostC:3")
    check("release frees the output_dir", "/od/y" not in st.load()["leases"])

    # ============ 7. alerts ===================================================================
    print("\n7. alerts")
    ap = os.path.join(tmp, "s7", "state.json")
    st = ais.AttemptStore(ap)
    zero = {"ok": 0, "dup": 0, "fail": 2, "systemic": 0}
    due = [st.record_run(0, 12, zero, stuck_runs=3, now=T0 + i * 4 * H)["alert_due"] for i in range(3)]
    check("stuck: due on the 3rd consecutive no-progress run, not before", due == [False, False, True],
          str(due))
    st.mark_alert_sent(now=T0 + 8 * H)
    check("stuck: not again in the same episode",
          not st.record_run(0, 12, zero, now=T0 + 12 * H)["alert_due"])
    check("stuck: again once 24 h have passed", st.record_run(0, 12, zero, now=T0 + 33 * H)["alert_due"])
    st.mark_alert_sent(now=T0 + 33 * H)
    r = st.record_run(1, 12, {"ok": 1, "dup": 0, "fail": 0, "systemic": 0}, now=T0 + 37 * H)
    check("stuck: progress ends the episode", r["consecutive_zero"] == 0 and not r["alert_due"])
    due = [st.record_run(0, 12, zero, now=T0 + (41 + 4 * i) * H)["alert_due"] for i in range(3)]
    check("stuck: a new episode alerts again", due == [False, False, True], str(due))
    st2 = ais.AttemptStore(os.path.join(tmp, "s7", "empty.json"))
    due = [st2.record_run(0, 0, zero, now=T0 + i * H)["alert_due"] for i in range(10)]
    check("an empty queue stays silent", not any(due) and
          st2.load()["runs"]["consecutive_zero"] == 0, str(due))
    st2.record_run(0, 5, zero, now=T0 + 20 * H)
    st2.record_run(0, 5, zero, now=T0 + 24 * H)
    r = st2.record_run(3, 5, {"ok": 0, "dup": 3, "fail": 2, "systemic": 0}, now=T0 + 28 * H)
    check("resolving duplicates counts as progress", r["consecutive_zero"] == 0 and not r["alert_due"])
    st3 = ais.AttemptStore(os.path.join(tmp, "s7", "human.json"))
    nh = {"mouse_mismatch__20": "manifest_fasta_mismatch: manifest=h.fasta search=m.fasta"}
    d1 = st3.record_run(1, 3, {}, now=T0, needs_human=nh)["due"]
    st3.mark_alert_sent(now=T0, keys=[d["key"] for d in d1])
    d2 = st3.record_run(1, 3, {}, now=T0 + 4 * H, needs_human=nh)["due"]
    d3 = st3.record_run(1, 3, {}, now=T0 + 25 * H, needs_human=nh)["due"]
    st3.record_run(1, 3, {}, now=T0 + 26 * H, needs_human={})
    d4 = st3.record_run(1, 3, {}, now=T0 + 27 * H, needs_human=nh)["due"]
    check("needs-a-person: due at first sight even while other work progresses",
          [d["key"] for d in d1] == ["human:mouse_mismatch__20"], str(d1))
    check("  ...not again within 24 h, again after", d2 == [] and len(d3) == 1, str((d2, d3)))
    check("  ...and cleared once resolved (so a recurrence alerts afresh)", len(d4) == 1, str(d4))

    hook = "https://hooks.slack.com/services/T000FAKE/B000FAKE/secretsecretsecret"
    hookfile = os.path.join(tmp, "s7", "skill_slack_webhook")
    open(hookfile, "w").write(hook + "\n")
    real_file, real_open = aia.WEBHOOK_FILE, aia.urllib.request.urlopen
    posts = []

    class Resp:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def ok_open(req, timeout=None):
        posts.append((req.full_url, json.loads(req.data.decode()), timeout))
        return Resp()

    aia.WEBHOOK_FILE = hookfile
    aia.urllib.request.urlopen = ok_open
    try:
        alert_state = os.path.join(tmp, "s7", "e2e.json")
        stuck = FakeIngest({s: "notnull" for s, _, _ in bad + fresh})
        CORPUS.clear()
        install_fake_queue()
        outs = []
        for i in range(4):
            CLOCK[0] = T0 + i * 4 * H
            rc, o = run_main(["--apply", "--candidates", cjson, "--state-file", alert_state,
                              "--limit", "2", "--max-failures", "9"], stuck)
            outs.append(o)
        check("end to end: exactly ONE post after 3 no-progress runs (and not on the 4th)",
              len(posts) == 1 and "SENT" in outs[2] and "SENT" not in outs[3], str(len(posts)))
        if posts:
            url, body, timeout = posts[0]
            check("  posted to the webhook, with a timeout of at most 10 s",
                  url == hook and timeout is not None and timeout <= 10, str(timeout))
            check("  the message says what is wrong on its first line",
                  body["text"].splitlines()[0].startswith(":warning: FRAN auto-ingest is stuck: 3 "),
                  body["text"])
            check("  the message body does not contain the webhook", hook not in body["text"])
        check("the webhook never appears in the run's output", all(hook not in o for o in outs))

        empty_state = os.path.join(tmp, "s7", "empty_e2e.json")
        ejson0 = os.path.join(tmp, "empty.json")
        json.dump([], open(ejson0, "w"))
        n0 = len(posts)
        for i in range(5):
            CLOCK[0] = T0 + i * 4 * H
            run_main(["--apply", "--candidates", ejson0, "--state-file", empty_state], FakeIngest({}))
        check("end to end: an empty queue never posts", len(posts) == n0)

        scan_state = os.path.join(tmp, "s7", "scanfail.json")
        def scan_fails(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="psycopg2.OperationalError: x")
        n0, outs = len(posts), []
        for i in range(3):
            CLOCK[0] = T0 + i * 4 * H
            outs.append(run_main(["--apply", "--state-file", scan_state], scan_fails)[1])
        check("a scan that fails every run is 'stuck' too (queue size unknown): one post on the 3rd",
              len(posts) == n0 + 1 and "SENT" in outs[2] and "unknown (the scan failed)"
              in posts[-1][1]["text"], outs[2][-400:])

        crash_state = os.path.join(tmp, "s7", "crash.json")
        real_select, n0, crashed = ai.select, len(posts), []
        def broken_select(*a, **k):
            raise AttributeError("module 'find_uningested' has no attribute 'read_manifest'")
        ai.select = broken_select
        try:
            for i in range(3):
                CLOCK[0] = T0 + i * 4 * H
                try:
                    run_main(["--apply", "--candidates", cjson, "--state-file", crash_state], stuck)
                except AttributeError:
                    crashed.append(True)
        finally:
            ai.select = real_select
        cs = json.load(open(crash_state))
        check("a CRASH (the 09-23 partial-deploy class) is re-raised, not swallowed", len(crashed) == 3)
        check("  ...counted as a no-progress run, and the 3rd posts once, naming the crash",
              cs["runs"]["consecutive_zero"] == 3 and len(posts) == n0 + 1 and
              "crashed: AttributeError" in posts[-1][1]["text"], str(cs["runs"]))

        killed_state = os.path.join(tmp, "s7", "killed.json")
        ais.AttemptStore(killed_state).begin_run("hive-dc-7-4-46:4242", now=T0)   # never finishes
        CLOCK[0] = T0 + 4 * H
        rc, ok_ = run_main(["--apply", "--candidates", ejson0, "--state-file", killed_state,
                            "--no-alert"], FakeIngest({}))
        ks = json.load(open(killed_state))["runs"]
        check("a run killed before it finished is counted as a no-progress run by the next one",
              ks["consecutive_zero"] == 1 and any(h.get("killed") == "hive-dc-7-4-46:4242"
                                                  for h in ks["history"])
              and "never finished" in ok_, str(ks))
        check("  ...and the finished run leaves nothing 'in progress'", ks["in_progress"] == {},
              str(ks["in_progress"]))

        def failing_open(req, timeout=None):
            raise urllib.error.URLError(f"cannot reach {req.full_url}: [Errno 8] nodename")
        aia.urllib.request.urlopen = failing_open
        with contextlib.redirect_stdout(io.StringIO()):
            sent, note = aia.post("hello")
        check("a failed post returns (False, note) and never raises", sent is False)
        check("  ...and the URL is scrubbed from the note", hook not in note and "<webhook>" in note,
              note)
        check("a bare /services/T…/B…/… path is scrubbed too",
              "secretsecretsecret" not in aia.scrub("body: /services/T0A1/B0B2/secretsecretsecret")
              and "B000FAKE/secret" not in aia.scrub("echo /services/T000FAKE/B000FAKE/secretsecretsecret", hook))

        retry_state = os.path.join(tmp, "s7", "retry.json")
        for i in range(3):
            CLOCK[0] = T0 + i * 4 * H
            rc, o = run_main(["--apply", "--candidates", cjson, "--state-file", retry_state,
                              "--limit", "1", "--max-failures", "9"], stuck)
        check("an unsent alert prints the reason without the URL",
              "NOT sent" in o and hook not in o, o[-300:])
        check("  ...and is not recorded as sent, so the next run retries it",
              (json.load(open(retry_state))["alerts"].get("stuck") or {}).get("last_sent") is None)

        aia.urllib.request.urlopen = ok_open
        n0 = len(posts)
        open(hookfile, "w").write("https://evil.example.com/collect\n")
        sent, note = aia.post("hello")
        check("a non-Slack URL in the webhook file is refused",
              sent is False and len(posts) == n0 and "evil" not in note, note)
        os.unlink(hookfile)
        sent, note = aia.post("hello")
        check("a missing webhook file is 'not configured', not an error", sent is False, note)
    finally:
        aia.WEBHOOK_FILE, aia.urllib.request.urlopen = real_file, real_open

    # ============ 8. unusable exports are skipped before they cost a run ======================
    print("\n8. report_problem")
    rp = ai.report_problem
    check("a normal export is usable", rp(make_export(reports, "ok_s", "ok_e"), "spectronaut") is None)
    check("header-only export is rejected",
          "header only" in (rp(make_export(reports, "ho", "ho_e", body=False), "spectronaut") or ""))
    check("an export that ends mid-line is rejected as truncated",
          "truncated" in (rp(make_export(reports, "tr", "tr_e", truncated=True), "spectronaut") or ""))
    pq = os.path.join(reports, "pq", "pq_e")
    os.makedirs(pq)
    open(os.path.join(pq, "pq_e_Report_FRAN (Normal).parquet"), "wb").write(b"PAR1" + b"x" * 5000)
    check("a parquet export without its PAR1 footer is rejected",
          "parquet" in (rp(pq, "spectronaut") or ""))
    chosen, skipped = ai.select([{"dir": make_export(reports, "two", "20260801_000000_two"),
                                  "engine": "spectronaut"},
                                 {"dir": make_export(reports, "two", "20260901_000000_two",
                                                     truncated=True), "engine": "spectronaut"}])
    check("a truncated NEWEST export falls back to the older complete one",
          chosen and chosen[0]["dir"].endswith("20260801_000000_two"), str(chosen))

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
