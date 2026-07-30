#!/bin/bash
#SBATCH -A genome-center-grp
#SBATCH -p high
#SBATCH -q genome-center-grp-high-qos
#SBATCH -c 2
#SBATCH --mem=8G
#SBATCH -t 12:00:00
#SBATCH -J pg_index
#SBATCH -o pg_index_%j.log
#
# CREATE INDEX CONCURRENTLY on delimp_precursors(protein_group). The work happens inside Postgres,
# so this job is just a durable client holding the connection -- hence the small cpu/mem ask and the
# long wall clock.
#
# sbatch rather than srun-over-ssh specifically because this runs for hours: an ssh timeout would
# kill the client, which CANCELS the build and leaves an INVALID index behind.
set -euo pipefail

ING=/quobyte/proteomics-grp/brett/glendon/fran_ingest
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
export DELIMP_PG_TOKEN_FILE=${DELIMP_PG_TOKEN_FILE:-/quobyte/proteomics-grp/de-limp/fran_refresh/.pgfarm_token}

cd "$ING"
echo "host=$(hostname) start=$(date -Is) args=$*"
"$PY" build_protein_group_index.py "$@"
echo "end=$(date -Is)"
