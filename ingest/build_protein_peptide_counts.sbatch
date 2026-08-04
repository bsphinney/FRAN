#!/bin/bash
#SBATCH -A genome-center-grp
#SBATCH -p high
#SBATCH -q genome-center-grp-high-qos
#SBATCH -c 2
#SBATCH --mem=8G
#SBATCH -t 12:00:00
#SBATCH -J pep_counts
#SBATCH -o pep_counts_%j.log
#
# Precompute COUNT(DISTINCT stripped_seq) per protein_group so the gene page reads a 522k-row table
# instead of aggregating a 138 GB heap per request. All the work happens inside Postgres, so this
# job is just a durable client holding the connection -- hence the small cpu/mem ask.
#
# sbatch rather than srun-over-ssh because it runs for a long time and an ssh timeout would kill the
# client mid-run. The job is chunked and resumable, so a kill only costs the batch in flight:
# re-running skips everything already stored.
set -euo pipefail

ING=/quobyte/proteomics-grp/brett/glendon/fran_ingest
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
export DELIMP_PG_TOKEN_FILE=${DELIMP_PG_TOKEN_FILE:-/quobyte/proteomics-grp/de-limp/fran_refresh/.pgfarm_token}

cd "$ING"
echo "host=$(hostname) start=$(date -Is) args=$*"
"$PY" build_protein_peptide_counts.py "$@"
echo "end=$(date -Is)"
