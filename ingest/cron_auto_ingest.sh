#!/bin/bash
# cron_auto_ingest.sh — submit the FRAN auto-ingest job, at most one at a time.
#
# Runs on the Hive LOGIN node from cron, so it must stay trivial: it submits an sbatch and exits.
# All real work happens inside SLURM (see fran_auto_ingest.sbatch).
#
# WHY THE QUEUE CHECK. The job runs in partition=low, which is regularly backed up -- test
# submissions sat PENDING(Priority) for minutes. sbatch returns immediately, so `flock` around the
# submit only prevents two CRON TICKS overlapping; it does nothing about a job still sitting in the
# queue from the previous tick. Without this check a busy queue would accumulate one auto-ingest job
# per tick, and they would then all start together and ingest the same candidates concurrently.
set -uo pipefail
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true

JOB=fran_auto_ingest
LOG=/quobyte/proteomics-grp/de-limp/fran_refresh/logs/auto_ingest_submit.log
SBATCH=/quobyte/proteomics-grp/brett/glendon/fran_ingest/fran_auto_ingest.sbatch

n=$(squeue -h -u "$USER" -n "$JOB" -t PENDING,RUNNING 2>/dev/null | wc -l)
if [ "${n:-0}" -gt 0 ]; then
  echo "$(date '+%F %T') skip: $n $JOB job(s) already pending/running" >> "$LOG"
  exit 0
fi
out=$(sbatch "$SBATCH" 2>&1)
echo "$(date '+%F %T') $out" >> "$LOG"
