#!/bin/bash
#SBATCH --job-name=fran_health
#SBATCH --account=genome-center-grp
#SBATCH --partition=high
#SBATCH --qos=genome-center-grp-high-qos
#SBATCH --time=02:00:00                      # read-only SQL; the coverage queries take minutes
#SBATCH --cpus-per-task=1
#SBATCH --mem=2G
#SBATCH --exclude=hive-as-11-2-41
#SBATCH --output=/quobyte/proteomics-grp/brett/scratch/fran_health/logs/fran_health_%j.out
# Weekly FRAN health scan (fran_health_scan.py docstring). Submitted by fran_health_cron.sh on login2,
# which first snapshots the crontab -- crontabs are node-local, so a compute node cannot read it.
set -uo pipefail
source /etc/profile.d/modules.sh 2>/dev/null || true
export DELIMP_PG_TOKEN_FILE=/quobyte/proteomics-grp/brett/.pgfarm_token
ING=/quobyte/proteomics-grp/brett/glendon/fran_ingest
export PYTHONPATH=$ING
cd "$ING"
/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python -u fran_health_scan.py "$@"
