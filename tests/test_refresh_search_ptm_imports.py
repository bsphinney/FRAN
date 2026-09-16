"""refresh_search_ptm.py must import on HIVE, not just in the repo.

The repo lies about this by construction: `app/` is a sibling of `ingest/`, so a
`sys.path.insert(0, "..")` plus `from app.db import query` resolves here and raises
ModuleNotFoundError on the cluster, where the script runs from
/quobyte/proteomics-grp/brett/glendon/fran_ingest/ — a flat scp'd directory with no `app/` beside
or above it. Reading the source cannot catch that; only importing it somewhere app-free can.

So: build a sandbox holding exactly the files Hive has in that directory, and import the module
with ONLY that directory on sys.path and a scrubbed environment. Verified against the real cluster
listing on 2026-09-16 — fran_ingest/ holds coreomics_import.py and refresh_corpus_reach.py, and
NOT refresh_leaderboards.py, which lives in fran_refresh/.
"""
import os, shutil, subprocess, sys, tempfile

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ING = os.path.join(REPO, "ingest")

# Exactly what the cluster has beside the script. refresh_leaderboards.py is deliberately absent:
# it is not in fran_ingest/, so importing it would fail there just as app.db does.
HIVE_SIBLINGS = ["refresh_search_ptm.py", "coreomics_import.py", "refresh_corpus_reach.py"]

sandbox = tempfile.mkdtemp(prefix="hive_fran_ingest_")
neutral = tempfile.mkdtemp(prefix="hive_cwd_")
try:
    for f in HIVE_SIBLINGS:
        shutil.copy2(os.path.join(ING, f), sandbox)
    check("the sandbox has no app/ package (it must mimic fran_ingest, not the repo)",
          not os.path.exists(os.path.join(sandbox, "app")))

    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}   # no PYTHONPATH

    def run(code):
        return subprocess.run([sys.executable, "-c", code], cwd=neutral, env=env,
                              capture_output=True, text=True, timeout=120)

    # THE CONTROL. If `import app.db` succeeded from here the sandbox would be leaking the repo and
    # every other assertion in this file would be worthless.
    ctl = run(f"import sys; sys.path.insert(0, {sandbox!r})\n"
              "try:\n import app.db\n print('LEAK')\nexcept ModuleNotFoundError:\n print('APP-FREE')")
    check("control: app.db is genuinely unimportable from the sandbox",
          "APP-FREE" in ctl.stdout, f"{ctl.stdout.strip()} {ctl.stderr.strip()[:200]}")

    # THE TEST. Import the module the way Hive's sbatch does — its own directory on sys.path.
    res = run(f"import sys; sys.path.insert(0, {sandbox!r})\n"
              "import refresh_search_ptm as m\n"
              "leaked = [k for k in sys.modules if k == 'app' or k.startswith('app.')]\n"
              "print('IMPORTED', 'LEAKED:' + ','.join(leaked) if leaked else 'IMPORTED CLEAN')")
    check("refresh_search_ptm imports with only its own Hive directory on sys.path",
          res.returncode == 0, (res.stderr.strip().splitlines() or [""])[-1][:300])
    check("...and pulls in no app.* module on the way",
          "IMPORTED CLEAN" in res.stdout, res.stdout.strip() + res.stderr.strip()[:200])

    # main() must be reachable too, not merely the module body: argparse runs before any DB work.
    helped = subprocess.run([sys.executable, os.path.join(sandbox, "refresh_search_ptm.py"), "--help"],
                            cwd=neutral, env=env, capture_output=True, text=True, timeout=120)
    check("...and runs as a script (argparse reached, no import error at main())",
          helped.returncode == 0 and "--batch" in helped.stdout,
          (helped.stderr.strip().splitlines() or [""])[-1][:300])
finally:
    shutil.rmtree(sandbox, ignore_errors=True)
    shutil.rmtree(neutral, ignore_errors=True)

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
