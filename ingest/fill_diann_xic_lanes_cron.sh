#!/bin/bash
# Daily: plan + write the DIA-NN XIC lane for searches that have none (fill_diann_xic_lanes.py).
# Runs from cron on login2 under flock. Same preamble as fran_fill_cron.sh: cron gives no login
# shell (sbatch not on PATH) and sets neither LOGNAME nor USER.
export LOGNAME="${LOGNAME:-$(id -un)}"
export USER="${USER:-$LOGNAME}"
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true
command -v sbatch >/dev/null || { echo "$(date '+%F %T') ABORT: sbatch not on PATH" >&2; exit 1; }
ING=/quobyte/proteomics-grp/brett/glendon/fran_ingest
D=/quobyte/proteomics-grp/brett/scratch/fran_health
mkdir -p "$D/logs" "$D/xic_lane"
# Never two at once: yesterday's array may still be writing; one writer per dataset.
if [ -n "$(squeue -u "$(id -un)" -h -n fran_xic_lane -o %i 2>/dev/null)" ]; then
    echo "$(date '+%F %T') fran_xic_lane still queued/running; skip" >> "$D/logs/cron_submit.log"
    exit 0
fi
echo "$(date '+%F %T') fran_xic_lane plan: $(sbatch --parsable "$ING/fill_diann_xic_lanes.sbatch" plan)" \
    >> "$D/logs/cron_submit.log" 2>&1
