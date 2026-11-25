#!/bin/bash
#SBATCH --job-name=fran_diann_reg
#SBATCH --account=publicgrp                  # free tier; an interrupted shard re-registers (upsert)
#SBATCH --partition=low
#SBATCH --qos=publicgrp-low-qos
#SBATCH --requeue
#SBATCH --time=1-00:00:00
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=16G
#SBATCH --array=0-5
#SBATCH --output=/quobyte/proteomics-grp/brett/scratch/fran_health/logs/diann_register_%A_%a.out
# Records every file of each listed DIA-NN search in delimp_search_artifact (hashing files up to
# 512 MB). Reads the search folders, writes only to PG Farm.
#   sbatch diann_register_artifacts.sbatch /path/to/list.jsonl
set -uo pipefail
LIST="${1:?usage: sbatch diann_register_artifacts.sbatch list.jsonl}"
ING="${FRAN_INGEST_DIR:-/quobyte/proteomics-grp/brett/glendon/fran_ingest}"
PY="${FRAN_PY:-/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python}"
export DELIMP_PG_TOKEN_FILE="${DELIMP_PG_TOKEN_FILE:-/quobyte/proteomics-grp/brett/.pgfarm_token}"
"$PY" -u "$ING/diann_full_output.py" register --list "$LIST" --shard "${SLURM_ARRAY_TASK_ID}/6" --apply
