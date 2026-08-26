#!/bin/bash
#SBATCH --job-name=engconf
#SBATCH --account=genome-center-grp
#SBATCH --partition=high
#SBATCH --array=0-7
#SBATCH --time=04:00:00
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
#SBATCH --output=/quobyte/proteomics-grp/de-limp/fran_refresh/logs/engconf_%A_%a.out
#
# Populate delimp_precursors.n_engines_confirming across the 764 multi-engine acquisitions.
# ~29s each serially is ~6 hours; strided across 8 array tasks it is well under an hour.
#
# STRIDED, not contiguous: cost scales with how many precursors an acquisition holds, so
# contiguous blocks would hand one task every large one. Tasks touch disjoint acquisitions, so the
# per-acquisition UPDATEs never contend for the same rows.
set -uo pipefail
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true
D=/quobyte/proteomics-grp/de-limp/fran_refresh
CODE=/quobyte/proteomics-grp/brett/glendon/fran_ingest
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
export DELIMP_PG_TOKEN_FILE="$D/.pgfarm_token"
echo "===== engconf shard ${SLURM_ARRAY_TASK_ID}/8 $(date '+%F %T') on $(hostname) ====="
"$PY" -u "$CODE/backfill_engines_confirming.py" --apply --shard "${SLURM_ARRAY_TASK_ID}/8"
echo "===== shard ${SLURM_ARRAY_TASK_ID} exit rc=$? $(date '+%F %T') ====="
