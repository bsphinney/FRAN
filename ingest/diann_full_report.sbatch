#!/bin/bash
#SBATCH --job-name=fran_diann_full
#SBATCH --account=publicgrp                  # free tier: short, restartable work (one marker per search)
#SBATCH --partition=low                      # preemptible; a requeued shard skips what it finished
#SBATCH --qos=publicgrp-low-qos
#SBATCH --requeue
#SBATCH --time=1-00:00:00
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8                    # the free tier's per-job ceiling
#SBATCH --mem=96G                            # the cross-run step holds every run's quant in memory
#SBATCH --array=0-7
#SBATCH --output=/quobyte/proteomics-grp/brett/scratch/fran_health/logs/diann_full_%A_%a.out
# Regenerates each DIA-NN search's report from its saved .quant files at --qvalue 0.5 with decoys
# (diann_full_output.py docstring). No raw file is read. Each array task takes every 8th search
# of the list and writes only under FRAN_DIANN_FULL_ROOT/<search_id>/, one writer per folder.
#   sbatch diann_full_report.sbatch /path/to/todo.jsonl
set -uo pipefail
LIST="${1:?usage: sbatch diann_full_report.sbatch todo.jsonl}"
ING="${FRAN_INGEST_DIR:-/quobyte/proteomics-grp/brett/glendon/fran_ingest}"
PY="${FRAN_PY:-/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python}"
"$PY" -u "$ING/diann_full_output.py" regen --list "$LIST" --shard "${SLURM_ARRAY_TASK_ID}/8" ${FRAN_REGEN_ARGS:-}
