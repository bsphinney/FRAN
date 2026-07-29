#!/bin/bash
#SBATCH -A genome-center-grp
#SBATCH -p high
#SBATCH -q genome-center-grp-high-qos
#SBATCH -c 4
#SBATCH --mem=16G
#SBATCH -t 08:00:00
#SBATCH -J raw_metadata
#SBATCH -o raw_metadata_%j.log
#
# Phase 0 step 3: fill the raw-header fields corpus-wide. Reads analysis.tdf (Bruker) or runs
# ThermoRawFileParser (Thermo) for every located raw and writes instrument_model, instrument_serial,
# acquisition_method, acquisition_date, mobility_min/max, frame counts and mass range.
#
# acquisition_date matters beyond provenance: with column_id/column_age_injections dropped (no
# upstream source), date IS the column-aging proxy -- runs close in time share a column.
#
# Compute node: ~12.5k SQLite header reads over Quobyte is too much IO for the shared login node.
# Commits every 500 rows, so a wall-clock kill keeps everything done so far.
#
#   sbatch record_raw_metadata.sbatch --bruker --apply --only-missing
#   sbatch record_raw_metadata.sbatch --thermo --apply --only-missing   # needs dotnet + DOTNET_ROOT
set -euo pipefail

ING=/quobyte/proteomics-grp/brett/glendon/fran_ingest
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
export DELIMP_PG_TOKEN_FILE=${DELIMP_PG_TOKEN_FILE:-/quobyte/proteomics-grp/de-limp/fran_refresh/.pgfarm_token}

cd "$ING"
echo "host=$(hostname) start=$(date -Is) args=$*"
"$PY" record_raw_metadata.py "$@"
echo "end=$(date -Is)"
