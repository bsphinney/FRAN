"""A queued DIA-NN search that declares its XIC directory gets its chromatograms ingested.

The producer registers a finished search with `fran_queue.py add ... --xic-dir <dir>`; the row
stores xic_dir/lance_dir and `claim_batch()` returns them. auto_ingest must carry them through to
`_run_xic_lane()`, which hands DIA-NN's native *.xic.parquet to diann_xic_to_lance.py after the
precursors commit. Until 2026-09-16 `_claim_queue()` rebuilt each row without those two keys, so the
lane returned early on every queued search and no DIA-NN trace was ever ingested unattended.

Run:  python tests/test_auto_ingest_queue_xic.py     (no pytest, no database)
"""
import os, subprocess, sys, tempfile, types
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingest"))
import auto_ingest as ai                                    # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)

SEARCH_UUID = "0f0e0d0c-0b0a-4908-8706-050403020100"


class FakeCursor:
    def execute(self, sql, params=None):
        self.sql = sql
    def fetchone(self):
        return (SEARCH_UUID,)


class FakeConn:
    def cursor(self):
        return FakeCursor()


def install_fake_queue(rows):
    """fran_queue talks to PG Farm; replace only its network edge. Outcomes are recorded so the
    test can assert what the ingester reported back."""
    q = types.ModuleType("fran_queue")
    q.calls = []
    q._conn = lambda: FakeConn()
    q.claim_batch = lambda con, limit, claimed_by: [dict(r) for r in rows]
    q.mark_done = lambda con, row_id, search_id=None: q.calls.append(("done", row_id))
    q.mark_failed = lambda con, row_id, err: q.calls.append(("failed", row_id)) or "queued"
    q.mark_xic = lambda con, row_id, status, error=None: q.calls.append(("xic", row_id, status))
    sys.modules["fran_queue"] = q
    return q


def queue_row(searchdir, **over):
    row = {"id": 7, "output_dir": searchdir, "searchdir": searchdir, "engine": "diann",
           "search_name": "pilot_search", "organism_name": "Homo sapiens", "taxon": "9606",
           "attempts": 0, "xic_dir": None, "lance_dir": None}
    row.update(over)
    return row


with tempfile.TemporaryDirectory() as tmp:
    sdir = os.path.join(tmp, "search")
    xdir = os.path.join(sdir, "xic")
    ldir = os.path.join(tmp, "lance")
    os.makedirs(xdir)

    args = types.SimpleNamespace(apply=True, limit=5, python=sys.executable, timeout=60)

    # --- 1. the claimed row keeps its XIC declaration ------------------------------------------
    install_fake_queue([queue_row(sdir, xic_dir=xdir, lance_dir=ldir)])
    cands, con = ai._claim_queue(args)
    check("claimed queue row carries xic_dir", cands and cands[0].get("xic_dir") == xdir,
          str(cands[0] if cands else cands))
    check("claimed queue row carries lance_dir", cands and cands[0].get("lance_dir") == ldir,
          str(cands[0] if cands else cands))

    # --- 2. a row that declared no XICs stays precursors-only ----------------------------------
    install_fake_queue([queue_row(sdir)])
    plain, _ = ai._claim_queue(args)
    check("row without xic_dir does not invent one", plain and not plain[0].get("xic_dir"),
          str(plain[0] if plain else plain))

    # --- 3. end to end: an OK DIA-NN ingest hands DIA-NN's XIC dir to diann_xic_to_lance -------
    q = install_fake_queue([queue_row(sdir, xic_dir=xdir, lance_dir=ldir)])
    ran = []
    real_run = subprocess.run

    def fake_run(cmd, **kw):
        ran.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

    ai.subprocess.run = fake_run
    try:
        cands, con = ai._claim_queue(args)
        ai._run(args, cands, [], con)
    finally:
        ai.subprocess.run = real_run

    lane = [c for c in ran if any(str(x).endswith("diann_xic_to_lance.py") for x in c)]
    check("diann_xic_to_lance.py runs after the ingest", len(lane) == 1,
          f"commands run: {[os.path.basename(str(c[1])) for c in ran if len(c) > 1]}")
    if lane:
        argv = lane[0]
        xic_arg = argv[argv.index("--xic-dir") + 1] if "--xic-dir" in argv else None
        out_arg = argv[argv.index("--out") + 1] if "--out" in argv else ""
        check("lane reads the XIC dir the producer declared", xic_arg == xdir, str(xic_arg))
        check("lane writes under the declared lance_dir", out_arg.startswith(ldir + os.sep), out_arg)
        check("lane attaches traces to the ingested search_id",
              "--search-id" in argv and argv[argv.index("--search-id") + 1] == SEARCH_UUID, str(argv))
    check("xic outcome recorded on the queue row", ("xic", 7, "done") in q.calls, str(q.calls))
    check("queue row marked done", ("done", 7) in q.calls, str(q.calls))

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
