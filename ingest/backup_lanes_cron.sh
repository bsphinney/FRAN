#!/bin/bash
# Submit the weekly lane snapshot. Runs on the login node: it only submits.
#   17 6 * * 0 flock -n /tmp/fran_lane_backup.lock /quobyte/proteomics-grp/brett/glendon/fran_ingest/backup_lanes_cron.sh
set -uo pipefail
SBIN=/cvmfs/hpc.ucdavis.edu/sw/spack/environments/core/view/generic/slurm/bin
HERE="$(cd "$(dirname "$0")" && pwd)"
LOG=/quobyte/proteomics-grp/brett/scratch/fran_health/logs/cron_submit.log
ME=$(id -un)                       # cron has no $USER
if ! q=$("$SBIN/squeue" -h -u "$ME" -n fran_lane_backup -t PENDING,RUNNING 2>&1); then
  echo "$(date '+%F %T') lane_backup ABORT: squeue failed ($q); not submitting blind" >> "$LOG"; exit 1
fi
if [ -n "$q" ]; then echo "$(date '+%F %T') lane_backup skip: one already queued or running" >> "$LOG"; exit 0; fi
echo "$(date '+%F %T') lane_backup $("$SBIN/sbatch" "$HERE/backup_lanes.sbatch" 2>&1)" >> "$LOG"
