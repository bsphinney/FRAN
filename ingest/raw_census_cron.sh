#!/bin/bash
# Weekly (Saturday evening): submit raw_census.sbatch so Sunday's 01:41 fran_fill has a fresh census.
# Same preamble as fran_fill_cron.sh: cron gives no login shell and sets neither LOGNAME nor USER.
export LOGNAME="${LOGNAME:-$(id -un)}"
export USER="${USER:-$LOGNAME}"
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true
command -v sbatch >/dev/null || { echo "$(date '+%F %T') ABORT: sbatch not on PATH" >&2; exit 1; }
ING=/quobyte/proteomics-grp/brett/glendon/fran_ingest
mkdir -p /quobyte/proteomics-grp/brett/raw_census/logs
# One date for both array tasks, even if one starts after midnight.
CENSUS_DATE=$(date +%F) sbatch --parsable --export=ALL "$ING/raw_census.sbatch" \
    >> /quobyte/proteomics-grp/brett/scratch/fran_health/logs/cron_submit.log 2>&1
