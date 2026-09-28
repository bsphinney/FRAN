"""corpus_drip_report.py — post the corpus-wide DIA-NN run's progress to Slack.

Fired three times a day by cron. Reports what actually changed, not what the state file claims:
SLURM is asked directly for what is running, and a search counts as complete only when no job from
its jobs.txt is still in the queue.

WHAT IT DELIBERATELY REPORTS, because a progress number alone is not useful over six weeks:
  * completed / in flight / remaining, and the trend since the last report (a run that has stopped
    moving looks identical to a healthy one in a single snapshot);
  * FAILURES AND SKIPS BY REASON. A drip that quietly skips a third of the worklist reports
    excellent progress while doing less and less. The reasons are the point;
  * CPUs running against the ceiling, so a stalled drip is distinguishable from a throttled one --
    0 CPUs with work remaining means something is wrong, 100 CPUs means it is working as designed;
  * the ETA, from the MEASURED rate rather than the 9.7 CPU-h/run estimate, which came from pilot
    searches with a median of 14 runs against a corpus median of 6.

It writes a small history file so the trend is real rather than inferred. It never writes to the
corpus, never submits anything, and a Slack failure is logged and shrugged off -- a broken webhook
must not be able to stop the run it is reporting on.

    python ingest/corpus_drip_report.py            # print, do not post
    python ingest/corpus_drip_report.py --post
"""
import argparse
import collections
import functools
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

print = functools.partial(print, flush=True)                      # noqa: A001

ROOT = "/nfs/lssc0/flinders/proteomics/Data/FRAN_diann"
OUT_ROOT = os.path.join(ROOT, "corpus_2026")
STATE = os.path.join(OUT_ROOT, "state.json")
HISTORY = os.path.join(OUT_ROOT, "report_history.json")
WORKLIST = os.path.join(ROOT, "CORPUS_WORKLIST.tsv")
WEBHOOK_FILE = "/quobyte/proteomics-grp/.config/skill_slack_webhook"
CEILING = 100


def sh(cmd):
    return subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True)


def running_jobs():
    r = sh("squeue -u $USER -h -o '%i' 2>/dev/null")
    return {ln.strip().split("_")[0] for ln in (r.stdout or "").splitlines() if ln.strip()}


def cpus_running():
    r = sh("squeue -u $USER -h -t RUNNING -o '%C' 2>/dev/null")
    n = 0
    for ln in (r.stdout or "").splitlines():
        try:
            n += int(ln.strip())
        except ValueError:
            pass
    return n


def load(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--post", action="store_true", help="send to Slack (otherwise just print)")
    a = ap.parse_args()

    st = load(STATE, {})
    total = sum(1 for _ in open(WORKLIST)) - 1 if os.path.exists(WORKLIST) else 0
    live = running_jobs()

    # A search is complete when NOTHING from its chain is still queued. Deliberately not
    # "report.parquet exists": that file exists while step 5 is still writing it.
    done = inflight = 0
    for v in st.values():
        if v.get("status") == "submitted":
            if any(str(j).split("_")[0] in live for j in (v.get("jobs") or [])):
                inflight += 1
            else:
                done += 1
        elif v.get("status") == "complete":
            done += 1

    skipped = collections.Counter(
        (v.get("reason") or "unspecified").split(" -- ")[0][:64]
        for v in st.values() if v.get("status") == "skipped")
    failed = sum(1 for v in st.values() if v.get("status") == "failed")

    # SETTLED must stay identical to the drip's own done-set (corpus_diann_drip.py, `done = {...}`).
    # Anything outside it is picked again on a later firing, so it is still work in front of us.
    # Counting every state entry as started -- which this did -- made `generated` and `failed`
    # invisible: not done, not in flight, and subtracted from remaining anyway. Seven sets sat in
    # `generated` through the first day and appeared in no line of the report.
    SETTLED = ("complete", "submitted", "skipped")
    settled = sum(1 for v in st.values() if v.get("status") in SETTLED)
    retry = collections.Counter(v.get("status") or "?"
                                for v in st.values() if v.get("status") not in SETTLED)
    remaining = max(total - settled, 0)
    runs_done = sum(int(v.get("n_runs") or 0) for v in st.values()
                    if v.get("status") in ("complete", "submitted")
                    and not any(str(j).split("_")[0] in live for j in (v.get("jobs") or [])))

    hist = load(HISTORY, [])
    prev = hist[-1] if hist else None
    now = time.time()
    delta = ""
    eta = ""
    # A stall is only meaningful over a real interval. The first --post after a manual run
    # compared against a history entry 0.0 h old and cried stall, which is the surest way to
    # train everyone to ignore the alert. Nothing is said about the trend until enough time has
    # passed for a search to plausibly have finished.
    MIN_TREND_HOURS = 3.0
    if prev:
        dd = done - prev.get("done", 0)
        hrs = (now - prev.get("t", now)) / 3600.0
        if hrs >= MIN_TREND_HOURS:
            delta = f"  (+{dd} in {hrs:.1f} h)"
            if dd > 0 and remaining + inflight > 0:
                rate = dd / hrs
                eta_h = (remaining + inflight) / rate
                eta = (f"\n• *ETA* ~{eta_h/24:.0f} days at the measured {rate:.1f} searches/h"
                       if eta_h > 48 else f"\n• *ETA* ~{eta_h:.0f} h at {rate:.1f} searches/h")
            elif dd == 0 and inflight == 0 and remaining > 0:
                # Nothing finished AND nothing is running: that is a stall. Nothing finished
                # while chains are still running is just a long search.
                eta = "\n• ⚠️ *nothing completed and nothing running* — the drip has stalled"
            elif dd == 0:
                eta = f"\n• nothing completed in {hrs:.0f} h ({inflight} still running)"

    cpus = cpus_running()
    # Every RUNNING CPU this user holds, not just the corpus run's -- deliberately, because that is
    # the number corpus_diann_drip.cpus_in_flight() gates submission on. Read as "the corpus run is
    # using 650 CPUs" it is wrong; read as "nothing new is submitted until this drops under 100" it
    # is right, and that is the thing worth knowing when progress looks frozen.
    if cpus == 0 and remaining:
        cpu_note = "  ⚠️ *idle with work remaining*"
    elif cpus >= CEILING and remaining:
        cpu_note = "  — *submission paused* until it drops"
    else:
        cpu_note = ""
    pct = (100.0 * done / total) if total else 0.0
    lines = [
        f"*FRAN corpus-wide DIA-NN 2.7.0* — {done:,}/{total:,} searches ({pct:.1f}%){delta}",
        f"• in flight {inflight} · remaining {remaining:,} · runs searched {runs_done:,}",
        f"• {cpus} CPUs running, all jobs / drip ceiling {CEILING}{cpu_note}",
    ]
    if failed:
        lines.append(f"• ⚠️ *{failed} failed*")
    if skipped:
        top = ", ".join(f"{n}× {r}" for r, n in skipped.most_common(3))
        lines.append(f"• skipped {sum(skipped.values())}: {top}")
    if retry:
        lines.append("• awaiting a later firing: "
                     + ", ".join(f"{n}× {s}" for s, n in retry.most_common()))
    lines.append(eta.lstrip("\n") if eta else "")
    text = "\n".join(x for x in lines if x)

    print(text)
    if not a.post:
        # A dry run is not a report. Writing history here is what produced the first real post's
        # "(+0 since 0.0 h ago) -- check for a stall": the preview had just stamped an entry, so the
        # post compared against itself. MIN_TREND_HOURS bounds that damage; this removes the cause.
        return 0

    hist.append({"t": now, "done": done, "inflight": inflight, "remaining": remaining})
    try:
        os.makedirs(OUT_ROOT, exist_ok=True)
        with open(HISTORY, "w") as fh:
            json.dump(hist[-200:], fh)
    except OSError as e:
        print(f"(could not write history: {e})")
    try:
        hook = open(WEBHOOK_FILE).read().strip()
    except OSError as e:
        print(f"no webhook ({e}) — not posted")
        return 0
    try:
        req = urllib.request.Request(
            hook, data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20).read()
        print("posted to Slack")
    except (urllib.error.URLError, OSError) as e:
        # Never fatal. A broken webhook must not be able to stop the run it reports on.
        print(f"Slack post failed ({str(e)[:90]}) — progress is unaffected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
