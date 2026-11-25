"""win_ingest_all.py — ONE command for a Windows Spectronaut computer: check, find, ingest.

  py win_ingest_all.py            (or double-click FRAN_INGEST.bat in the same folder)

  1. CHECK   win_preflight: packages, token, database, version gate, Spectronaut, report archive,
             free space. Anything failing stops the run before it touches a single experiment.
  2. FIND    find_missed_sne: every .sne on the local drives FRAN has never ingested.
  3. INGEST  sne_one.py on each MISSED experiment, smallest first, one at a time.

  --list-only    stop after step 2 (nothing is ingested)
  --limit N      ingest at most N experiments this run
  --roots ...    scan only these folders instead of every local drive

WHAT IT WILL NOT DO. It never ingests an experiment FRAN already holds under another path
(SAME_NAME), a second copy of the same experiment (COPY), one whose name says QC or test
(QC_OR_TEST), one being written right now (TOO_NEW), or one listed in missed_sne\\exclude.txt.

IT STOPS BY ITSELF when the FIRST experiment fails (nothing has worked on this computer yet, so the
fault is the setup, not the experiment), after 3 failures in a row, or when the export drive is
full or the share is gone. A single failure in the middle is recorded and the run moves on.

SAFE TO RE-RUN at any time: finished experiments are skipped. Only one run at a time per computer.

FOLLOWING IT FROM HIVE (there is no shell into this computer). Everything is under
FRAN_SNE_export/node_reports/<HOST>/ on the share:
    preflight.log   the readiness check          ingest_all.log   one line per experiment
    sne_one.log     every step of every ingest   status.json      where the run is right now
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import node_report  # noqa: E402

LOCK = r"C:\fran_sne_one\win_ingest_all.lock"
STOP_CODES = {5: "the report archive on the share is not reachable", 6: "the export drive is nearly full"}
MAX_IN_A_ROW = 3


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)
    node_report.report("ingest_all", m)


def write_status(**kw):
    """status.json: the one file to look at to know where the run is. Replaced whole, atomically."""
    try:
        d = node_report.report_dir()
        os.makedirs(d, exist_ok=True)
        kw["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        kw["host"] = node_report.host()
        tmp = os.path.join(d, "status.json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(kw, fh, indent=1)
        os.replace(tmp, os.path.join(d, "status.json"))
    except Exception:  # noqa: BLE001 -- status is best-effort
        pass


def keep_awake():
    """Ask Windows not to sleep while this runs (ES_CONTINUOUS | ES_SYSTEM_REQUIRED)."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
        except Exception:  # noqa: BLE001
            pass


def pid_alive(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))      # QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(h)
    return bool(ok) and code.value == 259                                   # STILL_ACTIVE


def take_lock(path=None):
    """One run per computer. A lock whose process is gone (a reboot, a closed window) is replaced."""
    path = path or LOCK
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path) as fh:
            old = int(fh.read().strip() or 0)
        if old and old != os.getpid() and pid_alive(old):
            return False
    except (OSError, ValueError):
        pass
    with open(path, "w") as fh:
        fh.write(str(os.getpid()))
    return True


def plan(items, limit=0):
    """What this run ingests: MISSED only, smallest first (an early small success proves the setup
    before the big ones start)."""
    todo = sorted((it for it in items if it["status"] == "MISSED"),
                  key=lambda it: (it["bytes"] if it["bytes"] >= 0 else float("inf"), it["path"].lower()))
    return todo[:limit] if limit else todo


def run_all(todo, run_one, say=log, status=write_status):
    """Ingest each experiment with run_one(item) -> exit code. Returns (results, stop reason or None).
    Stops on: a STOP_CODES exit, the first experiment failing, or MAX_IN_A_ROW failures in a row."""
    results, in_a_row, done_ok, stop = [], 0, 0, None
    for i, it in enumerate(todo, 1):
        gb = it["bytes"] / 1e9 if it["bytes"] >= 0 else 0
        say(f"[{i}/{len(todo)}] START {it['stem']} ({gb:.1f} GB) {it['path']}")
        status(state="ingesting", n_total=len(todo), n_done=i - 1, n_ok=done_ok,
               n_failed=len(results) - done_ok, current=it["path"])
        t0 = time.time()
        try:
            rc = run_one(it)
        except Exception as e:  # noqa: BLE001
            rc = -1
            say(f"  could not start sne_one.py: {e!r}")
        mins = (time.time() - t0) / 60
        ok = rc == 0
        results.append({"path": it["path"], "name": it["stem"], "rc": rc, "minutes": round(mins, 1)})
        say(f"[{i}/{len(todo)}] {'OK    ' if ok else 'FAILED'} rc={rc} {mins:.0f} min  {it['stem']}")
        if ok:
            done_ok += 1; in_a_row = 0
            continue
        in_a_row += 1
        if rc in STOP_CODES:
            stop = STOP_CODES[rc]
        elif done_ok == 0:
            stop = "the first experiment failed, so the setup is suspect, not the experiment"
        elif in_a_row >= MAX_IN_A_ROW:
            stop = f"{MAX_IN_A_ROW} experiments failed in a row"
        if stop:
            break
    return results, stop


def main():
    ap = argparse.ArgumentParser(description="Check this computer, find the .sne experiments FRAN lacks, ingest them.")
    ap.add_argument("--list-only", action="store_true", help="check and list; ingest nothing")
    ap.add_argument("--limit", type=int, default=0, help="ingest at most N experiments this run")
    ap.add_argument("--roots", nargs="*", default=None, help="folders to scan (default: every local drive)")
    a = ap.parse_args()

    keep_awake()
    log(f"=== win_ingest_all on {node_report.host()} ===")
    write_status(state="checking")
    import win_preflight
    _, bad = win_preflight.check()
    if bad:
        log("NOT READY: " + "; ".join(bad) + " -> nothing scanned, nothing ingested")
        write_status(state="not_ready", failed_checks=bad)
        return 1

    if not take_lock():
        log("another win_ingest_all is already running on this computer -> exit")
        return 1

    write_status(state="scanning")
    import find_missed_sne
    items, tsv, cmd = find_missed_sne.scan(a.roots)
    counts = {s: sum(1 for it in items if it["status"] == s) for s in find_missed_sne.STATUSES}
    todo = plan(items, a.limit)
    log("scan: " + " ".join(f"{k}={v}" for k, v in counts.items()) + f"  list={tsv}")
    if a.list_only or not todo:
        log("list only: nothing ingested" if a.list_only else "nothing to ingest")
        write_status(state="listed" if a.list_only else "finished", counts=counts, list=tsv, n_total=len(todo))
        return 0

    sne_one = os.path.join(HERE, "sne_one.py")

    def run_one(it):
        return subprocess.run([sys.executable, sne_one, "--sne", it["path"], f"--name={it['stem']}"]).returncode

    results, stop = run_all(todo, run_one)
    n_ok = sum(1 for r in results if r["rc"] == 0)
    failed = [r for r in results if r["rc"] != 0]
    log(f"=== {'STOPPED: ' + stop if stop else 'FINISHED'}: {n_ok} ingested, {len(failed)} failed, "
        f"{len(todo) - len(results)} not attempted ===")
    for r in failed:
        log(f"  failed rc={r['rc']}: {r['path']}")
    write_status(state="stopped" if stop else "finished", stop_reason=stop, counts=counts, list=tsv,
                 n_total=len(todo), n_done=len(results), n_ok=n_ok, n_failed=len(failed), failed=failed)
    return 0 if not stop and not failed else 2


if __name__ == "__main__":
    sys.exit(main())
