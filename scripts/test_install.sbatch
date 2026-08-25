#!/bin/bash
#SBATCH -A genome-center-grp
#SBATCH -p high
#SBATCH -q genome-center-grp-high-qos
#SBATCH -c 4
#SBATCH --mem=16G
#SBATCH -t 2:00:00
#SBATCH -J fran_install_test
#SBATCH -o install_test_%j.log
set -euo pipefail
W=${FRAN_TEST_WORK:-/tmp/fran_install_test}    # image cache (shared storage is fine)
L=${SLURM_TMPDIR:-/tmp}/fran_install_$SLURM_JOB_ID    # PGDATA must be node-local (Quobyte => EINTR)
SIF=$W/postgres16.sif
mkdir -p "$W"; rm -rf "$L"; mkdir -p "$L/data" "$L/run"
[ -f "$SIF" ] || apptainer pull "$SIF" docker://postgres:16
apptainer exec -B "$L/data":/data -B "$L/run":/run/postgresql \
  -B "${FRAN_SQL_DIR:?set FRAN_SQL_DIR to a dir holding fran_schema.sql + install_inner.sh}":/sql "$SIF" \
  bash /sql/install_inner.sh
rm -rf "$L"
echo "INSTALL TEST COMPLETE"
