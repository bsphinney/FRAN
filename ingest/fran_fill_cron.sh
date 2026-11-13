#!/bin/bash
# Nightly: submit fran_fill.sbatch (FRAN metadata fillers).
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
# Sunday adds the whole-corpus tissue/blood rescoring.
W=""; [ "$(date +%u)" = 7 ] && W="--weekly"
sbatch --parsable "$ING/fran_fill.sbatch" $W >> "$D/logs/cron_submit.log" 2>&1
