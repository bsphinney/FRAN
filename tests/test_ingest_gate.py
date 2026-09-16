"""test_ingest_gate.py — the gate must REFUSE, not warn, and must fail OPEN when blind.

Every assertion here is written to catch the gate failing in the SAFE-LOOKING direction. A gate that
never reads the manifest returns exactly what a gate that read it and found everything clean returns
— an empty list — so "current code is not stale" passing proves nothing on its own. Each all-clear
path below is therefore paired with a check that flips the world and demands the opposite result.
"""
import contextlib
import io
import os
import subprocess
import sys

os.environ.setdefault("DELIMP_PG_TOKEN_FILE", "/Users/brettphinney/.pgfarm_token")
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(HERE, "..")
INGEST = os.path.join(REPO, "ingest")
sys.path.insert(0, REPO)
sys.path.insert(0, INGEST)

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def call(cur, **kw):
    """assert_current with stdout captured. Returns (stale, printed, raised_SystemExit)."""
    buf = io.StringIO()
    raised = False
    stale = None
    try:
        with contextlib.redirect_stdout(buf):
            stale = V.assert_current(cur, **kw)
    except SystemExit:
        raised = True
    return stale, buf.getvalue(), raised


import versions as V                                                            # noqa: E402
from coreomics_import import _conn                                              # noqa: E402

ZERO = "0" * 32

# Capture the live manifest BEFORE touching anything, so the restore check at the end compares
# against reality rather than against what this test believes it wrote.
_probe = _conn()
with _probe.cursor() as _c:
    _c.execute("SELECT file, md5 FROM delimp_ingest_manifest ORDER BY file")
    BEFORE = dict(_c.fetchall())
_probe.close()

cn = _conn()
cn.autocommit = False          # every mutation below lives in a savepoint and is rolled back
try:
    cur = cn.cursor()

    # ── the all-clear path ────────────────────────────────────────────────────────────────────
    stale, out, raised = call(cur)
    check("current ingest code is not stale", stale == [] and not raised, f"{stale} {out}")
    # THE DISCRIMINATOR for the all-clear path. Fail-open also returns []; if the gate silently
    # fell into its except branch (a NameError on `sys`, a typo'd table name) this is the only
    # check that can see it.
    check("the all-clear path actually READ the manifest (did not fail open)",
          "unreadable" not in out and "empty" not in out, repr(out))

    # ── a stale REFUSE-gated file must stop the run ───────────────────────────────────────────
    cur.execute("SAVEPOINT s")
    cur.execute("UPDATE delimp_ingest_manifest SET md5=%s WHERE file='corpus_ingest.py'", (ZERO,))
    stale, out, raised = call(cur)
    check("a stale REFUSE-gated file raises SystemExit, not a warning", raised, repr(out))
    check("the refusal names the file and says what to do",
          "corpus_ingest.py" in out and "STALE" in out, repr(out))

    # ...and the override works, but only when asked for explicitly.
    stale, out, raised = call(cur, ignore_stale=True)
    check("--ignore-stale-ingest returns the stale list instead of exiting",
          not raised and stale is not None and "corpus_ingest.py" in stale, f"{raised} {stale}")
    cur.execute("ROLLBACK TO SAVEPOINT s")

    # ── a stale WARN-gated file must NOT stop the run ─────────────────────────────────────────
    cur.execute("SELECT file FROM delimp_ingest_manifest WHERE gate='warn' ORDER BY file LIMIT 1")
    wf = cur.fetchone()[0]
    cur.execute("SAVEPOINT s")
    cur.execute("UPDATE delimp_ingest_manifest SET md5=%s WHERE file=%s", (ZERO, wf))
    stale, out, raised = call(cur)
    check("a stale WARN-gated file does not stop ingestion",
          not raised and stale is not None and wf in stale, f"{raised} {stale}")
    cur.execute("ROLLBACK TO SAVEPOINT s")

    # ── a manifest row for a file that is not deployed here is not this gate's business ───────
    # The Windows node holds a subset of ingest/. Flagging every absent file would make the gate
    # fire constantly and get switched off.
    cur.execute("SAVEPOINT s")
    cur.execute("""INSERT INTO delimp_ingest_manifest (file, md5, git_sha, published_by, gate,
                                                       published_at)
                   VALUES ('zz_not_deployed_here.py', %s, 'test', 'test', 'refuse', now())""",
                (ZERO,))
    stale, out, raised = call(cur)
    check("a manifest file absent from this deployment is skipped, not called stale",
          not raised and stale == [], f"{raised} {stale} {out}")
    cur.execute("ROLLBACK TO SAVEPOINT s")

    # ── FAIL OPEN: blind must mean proceed, never stop ────────────────────────────────────────
    # PG Farm going down must not stop ingestion; this gate prevents silent corruption, it is not
    # a hard dependency of the pipeline.
    stale, out, raised = call(None)
    check("a dead cursor fails OPEN", not raised and stale == [], f"{raised} {stale}")
    check("failing open says so out loud", "unreadable" in out, repr(out))

    dead = _conn()
    dead_cur = dead.cursor()
    dead.close()                                     # a PG Farm outage, mid-run
    stale, out, raised = call(dead_cur)
    check("a closed connection fails OPEN", not raised and stale == [], f"{raised} {stale}")

    cur.execute("SAVEPOINT s")
    cur.execute("DELETE FROM delimp_ingest_manifest")
    stale, out, raised = call(cur)
    check("an empty manifest fails OPEN and says how to fill it",
          not raised and stale == [] and "publish_manifest" in out, f"{raised} {stale} {repr(out)}")
    cur.execute("ROLLBACK TO SAVEPOINT s")

    cn.rollback()
finally:
    cn.rollback()
    cn.close()

# The restore is itself a claim that has to be proven, on a connection that never saw the test's
# transaction. A rollback that silently did not happen would leave the live manifest corrupted and
# every ingest refusing.
_probe = _conn()
with _probe.cursor() as _c:
    _c.execute("SELECT file, md5 FROM delimp_ingest_manifest ORDER BY file")
    AFTER = dict(_c.fetchall())
_probe.close()
check("the live manifest is exactly as it was found", AFTER == BEFORE,
      f"{len(BEFORE)} rows before, {len(AFTER)} after; "
      f"changed={[k for k in BEFORE if BEFORE[k] != AFTER.get(k)]}")

# ── the gate must run where it guards ─────────────────────────────────────────────────────────
# versions.py is deployed to the Windows-node share and to Hive's fran_ingest/ as a FLAT directory
# with no app/ parent. An `import app.db` anywhere in this path breaks it there and nowhere here.
flat = subprocess.run(
    [sys.executable, "-c",
     # cwd is /, PYTHONPATH is empty and the repo root is nowhere on sys.path, so ingest/ is the
     # only part of FRAN this interpreter can see -- the Windows-node share and Hive's
     # fran_ingest/ exactly. Prove app/ really is out of reach before trusting what follows.
     "import sys; sys.path.insert(0, r'%s')\n"
     "try:\n"
     "    import app\n"
     "    sys.exit('app/ is importable here, so this proves nothing about the flat deployment')\n"
     "except ImportError:\n"
     "    pass\n"
     "import versions, coreomics_import\n"
     "cn = coreomics_import._conn()\n"
     "print('STALE:', versions.assert_current(cn.cursor()))\n"
     "print('BLIND:', versions.assert_current(None))\n"
     "assert 'app' not in sys.modules, sorted(m for m in sys.modules if m.startswith('app'))\n"
     "print('OK')" % INGEST],
    cwd="/", capture_output=True, text=True, env={**os.environ, "PYTHONPATH": ""})
check("versions.assert_current works with ONLY ingest/ on sys.path (no app/ parent)",
      flat.returncode == 0 and "OK" in flat.stdout,
      (flat.stdout + flat.stderr)[-600:])
check("the flat-directory run reports no stale files either",
      "STALE: []" in flat.stdout, flat.stdout.strip())

# ── the wiring ────────────────────────────────────────────────────────────────────────────────
h = subprocess.run([sys.executable, os.path.join(INGEST, "corpus_ingest.py"), "--help"],
                   capture_output=True, text=True)
check("corpus_ingest exposes --ignore-stale-ingest", "--ignore-stale-ingest" in h.stdout,
      h.stdout[-400:] + h.stderr[-400:])

src = open(os.path.join(INGEST, "corpus_ingest.py"), encoding="utf-8").read()
check("corpus_ingest calls the gate", "assert_current(" in src)
check("an override leaves a durable RAN STALE note", "RAN STALE" in src)

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
