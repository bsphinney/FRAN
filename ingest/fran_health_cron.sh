#!/bin/bash
# Weekly: snapshot the crontab, then submit the FRAN health scan with --heal --post.
# Runs from cron on login2 under flock. Same preamble as corpus_drip.sh, for the same reasons:
# cron gives no login shell (sbatch not on PATH) and sets neither LOGNAME nor USER.
export LOGNAME="${LOGNAME:-$(id -un)}"
export USER="${USER:-$LOGNAME}"
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true
command -v sbatch >/dev/null || { echo "$(date '+%F %T') ABORT: sbatch not on PATH" >&2; exit 1; }
ING=/quobyte/proteomics-grp/brett/glendon/fran_ingest
D=/quobyte/proteomics-grp/brett/scratch/fran_health
mkdir -p "$D/logs"
# Crontabs are node-local (memory: hive-crontab-is-node-local); the scan runs on a compute node and
# reads this snapshot to check that the FRAN cron lines are still live.
crontab -l > "$D/crontab_login2.txt" 2>&1
sbatch --parsable "$ING/fran_health.sbatch" --heal --post >> "$D/logs/cron_submit.log" 2>&1
