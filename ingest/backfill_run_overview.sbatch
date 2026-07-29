#!/bin/bash
#SBATCH -A genome-center-grp
#SBATCH -p high
#SBATCH -q genome-center-grp-high-qos
#SBATCH -c 4
#SBATCH --mem=32G
#SBATCH -t 04:00:00
#SBATCH -J run_overview
#SBATCH -o run_overview_%j.log
#
# Harvest Spectronaut's per-run RunOverview.tsv into raw_files (iRT calibration source + measured
# calibration delta, measured gradient, cycle time, instrument, run date).
#
# Use sbatch, NOT srun-over-ssh: an srun launched through an ssh session loses ALL of its stdout when
# the ssh connection times out, while the job keeps running -- so a failure becomes invisible. That
# cost two debugging rounds on this exact script.
#
#   sbatch backfill_run_overview.sbatch            # dry run
#   sbatch backfill_run_overview.sbatch --apply
set -euo pipefail

ING=/quobyte/proteomics-grp/brett/glendon/fran_ingest
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
export DELIMP_PG_TOKEN_FILE=${DELIMP_PG_TOKEN_FILE:-/quobyte/proteomics-grp/de-limp/fran_refresh/.pgfarm_token}

cd "$ING"
echo "host=$(hostname) start=$(date -Is) args=$*"
"$PY" backfill_run_overview.py "$@"
echo "end=$(date -Is)"
