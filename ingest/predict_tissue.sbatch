#!/bin/bash
#SBATCH -A genome-center-grp
#SBATCH -p high
#SBATCH -q genome-center-grp-high-qos
#SBATCH -c 4
#SBATCH --mem=32G
#SBATCH -t 04:00:00
#SBATCH -J tissue_pred
#SBATCH -o tissue_pred_%j.log
#
# sbatch, NOT srun-over-ssh: srun's stdout is lost when the ssh client is backgrounded, which has
# now cost debugging time twice on this project. All the work happens inside Postgres.
set -euo pipefail
ING=/quobyte/proteomics-grp/brett/glendon/fran_ingest
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
export DELIMP_PG_TOKEN_FILE=${DELIMP_PG_TOKEN_FILE:-/quobyte/proteomics-grp/de-limp/fran_refresh/.pgfarm_token}
cd "$ING"
echo "host=$(hostname) start=$(date -Is) args=$*"
"$PY" predict_tissue.py "$@"
echo "end=$(date -Is)"
