#!/bin/bash
#SBATCH -A genome-center-grp
#SBATCH -p high
#SBATCH -q genome-center-grp-high-qos
#SBATCH -c 4
#SBATCH --mem=64G
#SBATCH -t 06:00:00
#SBATCH -J org_lance
#SBATCH -o org_lance_%j.log
set -euo pipefail
ING=/quobyte/proteomics-grp/brett/glendon/fran_ingest
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
export DELIMP_PG_TOKEN_FILE=${DELIMP_PG_TOKEN_FILE:-/quobyte/proteomics-grp/de-limp/fran_refresh/.pgfarm_token}
cd "$ING"
echo "host=$(hostname) start=$(date -Is) args=$*"
"$PY" backfill_organism_from_lance.py "$@"
echo "end=$(date -Is)"
