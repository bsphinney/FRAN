"""corpus_ingest_drip.py — ingest the corpus-wide DIA-NN searches that have actually finished.

WHY THIS IS SEPARATE from the 4-hourly auto-ingest cron. That one scans FRAN_reports and the drop
box. It does not scan FRAN_diann/corpus_2026, and it must not simply be pointed at it: that
directory is full of chains still in flight, and a chain writes report.parquet while step 5 is
still filling it. Widening the existing scan would ingest half-written reports as finished ones.

THE GATE IS THE POINT, and it is two independent checks, because either alone passes something it
should not:

  1. NO JOB FROM THE CHAIN IS STILL QUEUED. A report.parquet on disk proves nothing about whether
     the search is done.
  2. THE REPORT COVERS EVERY RUN THE SET HAS. A chain that lost array tasks -- to a timeout, a
     node whose Quobyte mount dropped -- still produces a report, covering only the runs that
     survived. On 2026-09-29 three cancelled chains sat on disk with reports holding 1 of 224,
     1 of 220 and 1 of 125 runs. Ingesting those would have recorded a 224-run study as a one-run
     study, silently, and indistinguishably from a study that genuinely had one run. Check 1 alone
     passes all three of them, because a cancelled chain has no queued jobs either.

Nothing here writes to the corpus itself: it selects, writes a task list, and submits ONE sbatch
that does the ingesting on a compute node. It refuses to submit while its own previous job is
still running, so a slow batch cannot stack.

    python ingest/corpus_ingest_drip.py                 # report what is ready, submit nothing
    python ingest/corpus_ingest_drip.py --submit
    python ingest/corpus_ingest_drip.py --submit --batch-max 40
"""
import argparse
import csv
import functools
import os
import subprocess
import sys

print = functools.partial(print, flush=True)                      # noqa: A001

ROOT = "/nfs/lssc0/flinders/proteomics/Data/FRAN_diann"
OUT_ROOT = os.path.join(ROOT, "corpus_2026")
WORKLIST = os.path.join(ROOT, "CORPUS_WORKLIST.tsv")
SCRATCH = "/quobyte/proteomics-grp/brett/scratch"
TASKS = os.path.join(SCRATCH, "corpus_ingest_tasks.txt")
SBATCH = os.path.join(SCRATCH, "corpus_ingest_drip.sbatch")
HERE = "/quobyte/proteomics-grp/brett/glendon/fran_ingest"
PY = "/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python"
JOB_NAME = "corpus_ingest"
BATCH_MAX = 25
# 64G against a measured worst case of 37.1 GB (sacct MaxRSS, the largest pilot search: 3.8M
# precursors over 27 runs) which had already died once at 32G. Headroom is free; an OOM costs the
# whole batch an hour and a half in.
MEM = "64G"


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def expected_runs():
    """setkey[:8] -> n_runs, the number of runs the set is supposed to contain."""
    out = {}
    try:
        with open(WORKLIST) as fh:
            for r in csv.DictReader(fh, delimiter="\t"):
                out[r["setkey"][:8]] = int(r["n_runs"] or 0)
    except (OSError, ValueError) as e:
        print(f"cannot read the worklist ({e})")
    return out


def queued_jobs():
    r = sh(["bash", "-lc", "squeue -u $USER -h -o '%i' 2>/dev/null"])
    return {ln.strip().split("_")[0] for ln in (r.stdout or "").splitlines() if ln.strip()}


def ours_running():
    """True if a previous corpus_ingest batch is still going -- do not stack another."""
    r = sh(["bash", "-lc", f"squeue -u $USER -h -n {JOB_NAME} -o '%i' 2>/dev/null"])
    return bool((r.stdout or "").strip())


def already_in_corpus():
    sys.path.insert(0, HERE)
    import corpus_ingest as ci
    con = ci._conn()
    cur = con.cursor()
    cur.execute("SELECT output_dir FROM delimp_searches WHERE output_dir LIKE %s", (OUT_ROOT + "%",))
    got = {r[0].rstrip("/") for r in cur.fetchall()}
    con.close()
    return got


def report_run_count(path):
    """Distinct runs in a DIA-NN report.parquet, or -1 if it cannot be read."""
    try:
        import pyarrow.parquet as pq
        f = pq.ParquetFile(path)
        names = f.schema_arrow.names
        col = "Run" if "Run" in names else names[0]
        runs = set()
        for b in f.iter_batches(batch_size=200_000, columns=[col]):
            runs.update(b.column(0).to_pylist())
        return len(runs)
    except Exception as e:                                        # noqa: BLE001
        print(f"    unreadable report ({type(e).__name__}: {str(e)[:80]})")
        return -1


def candidates(a):
    """(ready, short, running). Gate 1 is free; gate 2 is not, so it is applied LAZILY.

    Gate 2 opens a parquet and walks one column, and this runs on a login node where heavy IO is
    not allowed. Applying it to the whole backlog would mean reading every un-ingested report on
    every firing -- 120 of them at the time of writing, and the backlog is the thing that grows
    when ingestion falls behind, so the cost would rise exactly when it should not. Stopping as
    soon as the batch is full bounds it to batch_max reads plus however many are held back.
    """
    want = expected_runs()
    live = queued_jobs()
    have = already_in_corpus()
    ready, short, running, waiting = [], [], 0, 0
    for d in sorted(os.listdir(OUT_ROOT)):
        sd = os.path.join(OUT_ROOT, d, "output", "search")
        rp = os.path.join(sd, "report.parquet")
        if not os.path.exists(rp) or sd.rstrip("/") in have:
            continue
        jf = os.path.join(sd, "jobs.txt")
        jobs = []
        if os.path.exists(jf):
            jobs = [ln.strip() for ln in open(jf) if ln.strip()]
        if any(j.split("_")[0] in live for j in jobs):
            running += 1
            continue                                   # gate 1: the chain has not finished
        if len(ready) >= a.batch_max:
            waiting += 1                               # past gate 1, not read this firing
            continue
        exp = want.get(d.rsplit("__", 1)[-1][:8], 0)
        got = report_run_count(rp)
        if exp and got >= exp:
            ready.append(sd)                           # gate 2: it covers every run
        else:
            short.append((d, got, exp))
    if waiting:
        print(f"(+{waiting} more past gate 1, not read this firing — batch is full)")
    return ready, short, running


def write_sbatch(n):
    body = f"""#!/bin/bash -l
#SBATCH --job-name={JOB_NAME}
#SBATCH --cpus-per-task=4
#SBATCH --mem={MEM}
#SBATCH --time=12:00:00
#SBATCH --partition=high
#SBATCH --account=genome-center-grp
#SBATCH --qos=genome-center-grp-high-qos
#SBATCH -o {SCRATCH}/corpus_ingest_%j.log
#
# Generated by corpus_ingest_drip.py -- do not hand-edit; it is rewritten every firing.
# Ingests {n} corpus-wide search(es) whose chains have finished AND whose reports cover every run
# of their set. Both checks happen at SELECTION time, in the drip, not here.
set -uo pipefail
export DELIMP_PG_TOKEN_FILE=/quobyte/proteomics-grp/brett/.pgfarm_token
cd {HERE}
ok=0; fail=0
while read -r d; do
  [ -n "$d" ] || continue
  echo "=== $(basename $(dirname $(dirname "$d")))"
  if {PY} corpus_ingest.py "$d" --engine diann --output-dir "$d" --bulk-copy --no-fragments 2>&1 \\
       | grep -vE "collation version|^DETAIL:|^HINT:" | sed 's/^/    /'; then
    ok=$((ok+1))
  else
    fail=$((fail+1)); echo "    *** FAILED"
  fi
done < {TASKS}
echo; echo "=== done: $ok ingested, $fail failed ==="; date
"""
    with open(SBATCH, "w") as fh:
        fh.write(body)
    os.chmod(SBATCH, 0o755)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--submit", action="store_true", help="submit the batch (otherwise just report)")
    ap.add_argument("--batch-max", type=int, default=BATCH_MAX)
    a = ap.parse_args()

    if a.submit and ours_running():
        print(f"a {JOB_NAME} batch is still running — nothing submitted")
        return 0

    ready, short, running = candidates(a)
    print(f"chains still in flight : {running}")
    print(f"ready to ingest        : {len(ready)}")
    if short:
        print(f"HELD BACK (report does not cover the set): {len(short)}")
        for d, got, exp in short[:10]:
            print(f"    {d[:52]:<54} {got}/{exp} runs")
    if not ready:
        print("nothing ready")
        return 0

    batch = ready[: a.batch_max]
    if not a.submit:
        print(f"(dry run — {len(batch)} would be submitted, nothing written)")
        return 0

    with open(TASKS, "w") as fh:
        fh.write("\n".join(batch) + "\n")
    write_sbatch(len(batch))
    r = sh(["bash", "-lc", f"sbatch --parsable {SBATCH}"])
    jid = (r.stdout or "").strip().split(";")[0]
    if r.returncode != 0 or not jid.isdigit():
        print(f"sbatch failed rc={r.returncode}: {(r.stderr or '')[:200]}")
        return 1
    print(f"submitted job {jid} to ingest {len(batch)} search(es)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
