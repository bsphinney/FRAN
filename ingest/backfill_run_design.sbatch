#!/bin/bash
#SBATCH -A genome-center-grp
#SBATCH -p high
#SBATCH -q genome-center-grp-high-qos
#SBATCH -c 4
#SBATCH --mem=48G
#SBATCH -t 08:00:00
#SBATCH -J run_design
#SBATCH -o run_design_%j.log
#
# Harvest per-run experimental design (R_Condition / R_Replicate / R_Fraction / R_Label) from the
# archived Spectronaut reports into delimp_run_design.
#
# Cheap BECAUSE Parquet prunes columns: five string columns over 2,024 reports is ~72 min, against
# days for the fragment re-parse over the same 464 GB archive. Do not merge the two jobs.
#
# Memory is generous because a single report can hold ~26M rows and group_by materialises.
# Checkpointed per report, so a kill costs one report and a re-run skips what is stored.
set -euo pipefail

ING=/quobyte/proteomics-grp/brett/glendon/fran_ingest
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
export DELIMP_PG_TOKEN_FILE=${DELIMP_PG_TOKEN_FILE:-/quobyte/proteomics-grp/de-limp/fran_refresh/.pgfarm_token}

cd "$ING"
echo "host=$(hostname) start=$(date -Is) args=$*"
"$PY" backfill_run_design.py "$@"
echo "end=$(date -Is)"
