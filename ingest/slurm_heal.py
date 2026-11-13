"""slurm_heal.py - release jobs that a broken compute node put on hold, and keep them off it.

WHY. When a node cannot set up a job's private /tmp (Hive, 2026-11-13: hive-as-11-2-41,
`namespace_p_join: open failed for /local/scratch/<jobid>/.ns`), Slurm requeues the job and HOLDS
it: squeue reason "launch failed requeued held". A held job never runs again on its own, and the
node stays in service (state `mixed`), so it keeps catching new jobs. 195 FRAN jobs were held that
way by mid-morning; the search drip counted their sets as in flight and stopped submitting.

WHAT. For each of this user's jobs held for a launch failure: read from sacct which node(s) it
failed on, add them to the job's ExcNodeList (keeping any exclusions already there), and release
it. Idempotent; a job that is not held is never touched. It also prints which nodes failed
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    user = os.environ.get("USER") or sh(["id", "-un"]).stdout.strip()   # cron sets no USER
    held = held_jobs(user)
    if not held:
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
    print(f"{time.strftime('%F %T')} heal: {released}/{len(held)} held job(s) released; failing "
          f"node(s): {dict(seen) or 'unknown'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
