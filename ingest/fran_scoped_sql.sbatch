#!/bin/bash
#SBATCH -A genome-center-grp
#SBATCH -p high
#SBATCH -q genome-center-grp-high-qos
#SBATCH -c 8
#SBATCH --mem=96G
#SBATCH -t 02:00:00
#SBATCH -J fran_gate
#SBATCH -o fran_gate_%j.log
#
# The Phase 1 gate. Run via sbatch, NOT srun-over-ssh: an srun launched through an ssh session loses
# all of its stdout when the ssh connection times out, and the result is unrecoverable even though
# the job keeps running.
#
#   sbatch fran_scoped_sql.sbatch --legacy-grep              # control: reproduces 10.57 s
#   sbatch fran_scoped_sql.sbatch --grad-lo 18 --grad-hi 22  # the SQL-selected cohort
set -euo pipefail

ING=/quobyte/proteomics-grp/brett/glendon/fran_ingest
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
export DELIMP_PG_TOKEN_FILE=${DELIMP_PG_TOKEN_FILE:-/quobyte/proteomics-grp/de-limp/fran_refresh/.pgfarm_token}

cd "$ING"
echo "host=$(hostname) start=$(date -Is) args=$*"
"$PY" fran_scoped_sql.py "$@"
echo "end=$(date -Is)"
