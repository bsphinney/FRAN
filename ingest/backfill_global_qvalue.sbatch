#!/bin/bash
#SBATCH --job-name=globalq
#SBATCH --account=genome-center-grp
#SBATCH --partition=high
#SBATCH --array=0-7
#SBATCH --time=12:00:00
#SBATCH --cpus-per-task=2
#SBATCH --mem=24G
#SBATCH --output=/quobyte/proteomics-grp/de-limp/fran_refresh/logs/globalq_%A_%a.out
#
# Fill delimp_precursors.global_q_value for Spectronaut searches whose report is reachable
# (1,873 of 1,966). Sharded because report sizes vary enormously -- some are tens of GB -- and
# STRIDED so the large ones spread across tasks instead of landing on one.
set -uo pipefail
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true
D=/quobyte/proteomics-grp/de-limp/fran_refresh
CODE=/quobyte/proteomics-grp/brett/glendon/fran_ingest
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
export DELIMP_PG_TOKEN_FILE="$D/.pgfarm_token"
echo "===== globalq shard ${SLURM_ARRAY_TASK_ID}/8 $(date '+%F %T') on $(hostname) ====="
"$PY" -u "$CODE/backfill_global_qvalue.py" --apply --shard "${SLURM_ARRAY_TASK_ID}/8"
echo "===== shard ${SLURM_ARRAY_TASK_ID} exit rc=$? $(date '+%F %T') ====="
