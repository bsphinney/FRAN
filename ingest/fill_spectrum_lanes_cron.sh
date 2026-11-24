#!/bin/bash
# Daily: submit the spectrum-lane filler (fill_spectrum_lanes.py) for Spectronaut searches with no lane.
# Runs from cron on login2 under flock. Same preamble as fill_diann_xic_lanes_cron.sh: cron gives no
# login shell (sbatch not on PATH) and sets neither LOGNAME nor USER. This only SUBMITS (seconds);
# all work is in the sbatch. Suggested crontab line (NOT installed by this change):
#   47 4 * * * flock -n /tmp/fran_spectrum_lane.lock /quobyte/proteomics-grp/brett/glendon/fran_ingest/fill_spectrum_lanes_cron.sh
export LOGNAME="${LOGNAME:-$(id -un)}"
export USER="${USER:-$LOGNAME}"
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true
command -v sbatch >/dev/null || { echo "$(date '+%F %T') ABORT: sbatch not on PATH" >&2; exit 1; }
ING=/quobyte/proteomics-grp/brett/glendon/fran_ingest
D=/quobyte/proteomics-grp/brett/scratch/fran_health
mkdir -p "$D/logs" "$D/spectrum_lane"
# Never two at once: last night's job may still be writing; one writer per dataset.
if [ -n "$(squeue -u "$(id -un)" -h -n fran_spectrum_lane -o %i 2>/dev/null)" ]; then
    echo "$(date '+%F %T') fran_spectrum_lane still queued/running; skip" >> "$D/logs/cron_submit.log"
    exit 0
fi
echo "$(date '+%F %T') fran_spectrum_lane: $(sbatch --parsable "$ING/fill_spectrum_lanes.sbatch")" \
    >> "$D/logs/cron_submit.log" 2>&1
