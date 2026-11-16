"""slurm_heal.py - release jobs that a broken compute node put on hold, and keep them off it.

WHY. When a node cannot set up a job's private /tmp (Hive, 2026-11-13: hive-as-11-2-41,
`namespace_p_join: open failed for /local/scratch/<jobid>/.ns`), Slurm requeues the job and HOLDS
it: squeue reason "launch failed requeued held". A held job never runs again on its own, and the
node stays in service (state `mixed`), so it keeps catching new jobs. 195 FRAN jobs were held that
way by mid-morning; the search drip counted their sets as in flight and stopped submitting.

WHAT. For each of this user's jobs held for a launch failure: read from sacct which node(s) it
failed on, add them to the job's ExcNodeList (keeping any exclusions already there), and release
it. Then add every node that failed a launch in the last 24 h to the ExcNodeList of all other
PENDING jobs, so they never land there. Idempotent; a job that is not held is never touched. It also prints which nodes failed
launches, so a node that keeps doing it can be reported to hpc-help@ucdavis.edu.

It acts on ALL of this user's held jobs, not only FRAN's: the hold is never something a job wants,
and the remedy (avoid the node that failed) is the same for any job.

    python slurm_heal.py            # release and exclude (what cron runs)
    python slurm_heal.py --dry-run  # say what it would do

Run from corpus_drip.sh before the drip, every 20 minutes. Needs the Slurm commands on PATH.
"""
import argparse
import os
import subprocess
import sys
import time
from collections import Counter

HELD_REASON = "launch failed requeued held"
LOG = os.environ.get("FRAN_HEAL_LOG",
                     "/quobyte/proteomics-grp/brett/scratch/corpus_drip_logs/slurm_heal_nodes.tsv")
# One JSON line per firing that DID something; fran_health_scan.py imports these into
# delimp_qc_event for the FRAN-QC page. Single writer: only the drip's cron firing runs this.
EVENTS = os.environ.get("FRAN_HEAL_EVENTS",
                        "/quobyte/proteomics-grp/brett/scratch/corpus_drip_logs/slurm_heal_events.jsonl")


def log_event(kind, summary, **detail):
    try:
        import json
        with open(EVENTS, "a") as fh:
            fh.write(json.dumps({"at": time.strftime("%FT%T%z"), "kind": kind,
                                 "summary": summary, "detail": detail}) + "\n")
    except OSError as e:
        print(f"heal: could not append {EVENTS}: {e}")


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def held_jobs(user):
    r = sh(["squeue", "-u", user, "-h", "-t", "PD", "-o", "%i|%r|%j"])
    out = []
    for line in r.stdout.splitlines():
        jid, reason, name = (line.split("|", 2) + ["", ""])[:3]
        if reason.strip() == HELD_REASON:
            out.append((jid.strip(), name.strip()))
    return out


def failed_nodes(jid):
    """Nodes this job was launched on and then requeued from (sacct keeps one row per attempt)."""
    r = sh(["sacct", "-j", jid, "-X", "-D", "-n", "-P", "-o", "State,NodeList"])
    nodes = set()
    for line in r.stdout.splitlines():
        state, _, node = line.partition("|")
        if state.split()[0] in ("REQUEUED", "NODE_FAIL", "BOOT_FAIL") and node not in ("", "None assigned"):
            nodes.update(n for n in node.split(",") if n)
    return nodes


def current_exclusions(jid):
    r = sh(["scontrol", "show", "job", jid])
    for tok in r.stdout.split():
        if tok.startswith("ExcNodeList="):
            v = tok.split("=", 1)[1]
            return set() if v in ("(null)", "") else set(v.split(","))
    return set()


def recent_bad_nodes(hours=24):
    """Nodes this healer has seen fail a launch in the last `hours` (from its own log)."""
    cutoff = time.strftime("%FT%T", time.localtime(time.time() - hours * 3600))
    bad = set()
    try:
        for line in open(LOG):
            p = line.rstrip("\n").split("\t")
            if len(p) == 3 and p[0] != "time" and p[0] >= cutoff:
                bad.add(p[1])
    except OSError:
        pass
    return bad


def steer_pending(user, bad, dry):
    """Add recently-bad nodes to every PENDING job's ExcNodeList BEFORE it can land there.

    Releasing alone is not enough (2026-11-13): new jobs kept being scheduled onto the broken node
    every 20 minutes -- 8 more held between two firings -- because Slurm never drained it. A pending
    job's exclusion list can be changed freely; one that already excludes the node is not touched.
    """
    if not bad:
        return 0
    r = sh(["squeue", "-u", user, "-h", "-t", "PD", "-o", "%i|%r"])
    n = 0
    for line in r.stdout.splitlines():
        jid, _, reason = line.partition("|")
        jid = jid.strip()
        if reason.strip() == HELD_REASON:
            continue                      # handled by the release pass
        base = jid.split("_[")[0]         # a pending array prints as 123_[0-8%20]
        exc = current_exclusions(base)
        if bad <= exc:
            continue
        if not dry:
            sh(["scontrol", "update", "JobId=%s" % base, "ExcNodeList=%s" % ",".join(sorted(exc | bad))])
        n += 1
    if n:
        print(f"{time.strftime('%F %T')} heal: {'would steer' if dry else 'steered'} {n} pending "
              f"job(s) off {','.join(sorted(bad))}")
        if not dry:
            log_event("steered", f"steered {n} pending job(s) off {', '.join(sorted(bad))}",
                      n=n, nodes=sorted(bad))
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    user = os.environ.get("USER") or sh(["id", "-un"]).stdout.strip()   # cron sets no USER
    held = held_jobs(user)
    if not held:
        steer_pending(user, recent_bad_nodes(), a.dry_run)
        return 0
    seen = Counter()
    released = 0
    for jid, name in held:
        bad = failed_nodes(jid)
        seen.update(bad)
        exc = current_exclusions(jid) | bad
        act = "would release" if a.dry_run else "released"
        if not a.dry_run:
            if exc:
                sh(["scontrol", "update", "JobId=%s" % jid, "ExcNodeList=%s" % ",".join(sorted(exc))])
            if sh(["scontrol", "release", jid]).returncode == 0:
                released += 1
            else:
                act = "FAILED to release"
        print(f"{time.strftime('%F %T')} heal: {act} {jid} ({name}); excluding {','.join(sorted(exc)) or '-'}")
    if not a.dry_run and seen:
        try:
            new = not os.path.exists(LOG)
            with open(LOG, "a") as fh:   # one writer: only the drip's cron firing runs this
                if new:
                    fh.write("time\tnode\tn_jobs\n")
                for node, n in seen.items():
                    fh.write(f"{time.strftime('%FT%T')}\t{node}\t{n}\n")
        except OSError as e:
            print(f"heal: could not append {LOG}: {e}")
    if not a.dry_run and released:
        log_event("released", f"released {released} held job(s); failing node(s): "
                              f"{', '.join(f'{k} ({v})' for k, v in seen.items()) or 'unknown'}",
                  n=released, jobs=[j for j, _ in held][:200], nodes=dict(seen))
    print(f"{time.strftime('%F %T')} heal: {released}/{len(held)} held job(s) released; failing "
          f"node(s): {dict(seen) or 'unknown'}")
    steer_pending(user, recent_bad_nodes() | set(seen), a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
