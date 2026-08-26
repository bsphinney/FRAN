#!/bin/bash
# cron_db_backup.sh -- submit the FRAN/delimp pg_dump job, at most one at a time.
#
# Runs on the Hive LOGIN node from cron, so it must stay trivial: it submits an sbatch and exits.
# All real work happens inside SLURM (see fran_db_backup.sbatch) -- a 228 GB pg_dump is emphatically
# not login-node work.
#
# WHY THE QUEUE CHECK. sbatch returns immediately, so `flock` around the submit only prevents two
# CRON TICKS overlapping; it does nothing about a job still sitting in the queue -- or still
# dumping -- from the previous tick. The dump runs for hours (38.6 GB took 5.5 h in July, and the
# database has grown since), so a weekly tick can easily land on top of a run that has not finished.
# Two concurrent pg_dumps would double the load on PG Farm and race over the same retention window.
set -uo pipefail
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true

JOB=fran_db_backup
LOG=/quobyte/proteomics-grp/de-limp/fran_refresh/logs/db_backup_submit.log
SBATCH=/quobyte/proteomics-grp/brett/glendon/fran_ingest/fran_db_backup.sbatch

# $USER is NOT reliably set under cron. With `set -u` the subshell below then dies, n comes back
# empty, ${n:-0} becomes 0 and the guard SUBMITS -- it failed open, which is the wrong direction for
# an anti-stacking guard. That is exactly what happened at 16:23:02: a second job was queued while
# the first was still PENDING. id -un does not depend on the environment.
ME=$(id -un)
if ! command -v squeue >/dev/null 2>&1; then
  echo "$(date '+%F %T') ABORT: squeue not on PATH; refusing to submit blind" >> "$LOG"
  exit 1
fi
# Fail CLOSED: if the queue cannot be read, assume something is already queued rather than pile on.
if ! q=$(squeue -h -u "$ME" -n "$JOB" -t PENDING,RUNNING 2>/dev/null); then
  echo "$(date '+%F %T') ABORT: squeue failed; refusing to submit blind" >> "$LOG"
  exit 1
fi
n=$(printf '%s' "$q" | grep -c . || true)
if [ "${n:-1}" -gt 0 ]; then
  echo "$(date '+%F %T') skip: $n $JOB job(s) already pending/running" >> "$LOG"
  exit 0
fi
out=$(sbatch "$SBATCH" 2>&1)
echo "$(date '+%F %T') $out" >> "$LOG"
