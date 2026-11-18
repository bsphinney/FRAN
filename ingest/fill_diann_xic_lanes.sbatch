#!/bin/bash
#SBATCH --job-name=fran_xic_lane
#SBATCH --account=genome-center-grp          # the group's dedicated pool (memory: hive-slurm-accounts)
#SBATCH --partition=high
#SBATCH --qos=genome-center-grp-high-qos
#SBATCH --time=1-00:00:00                    # a run task does ~1/16 of the gap; ~10 min per search
#SBATCH --cpus-per-task=2
#SBATCH --mem=64G                            # one run's long-format .xic.parquet is ~4M rows
#SBATCH --exclude=hive-as-11-2-41            # fails every job launch since 2026-11-13 ~01:00
#SBATCH --output=/quobyte/proteomics-grp/brett/scratch/fran_health/logs/fran_xic_lane_%j.out   # %j is unique per array task too
# DIA-NN XIC lane, daily from fill_diann_xic_lanes_cron.sh (fill_diann_xic_lanes.py docstring).
#   sbatch fill_diann_xic_lanes.sbatch plan          plan the gap, then submit the run array for it
#   sbatch --array=0-N%4 fill_diann_xic_lanes.sbatch run <ntasks> <plan.json>   (submitted by plan)
set +u
source /etc/profile.d/modules.sh 2>/dev/null || true   # under set -u this kills the job at 0 s
set -uo pipefail
export PATH=/cvmfs/hpc.ucdavis.edu/sw/spack/environments/core/view/generic/slurm/bin:$PATH
export DELIMP_PG_TOKEN_FILE=/quobyte/proteomics-grp/brett/.pgfarm_token
ING=/quobyte/proteomics-grp/brett/glendon/fran_ingest
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
export PYTHONPATH=$ING
cd "$ING"
MODE=${1:?plan or run}
echo "===== fran_xic_lane $MODE $(date -Is) on $(hostname) job ${SLURM_JOB_ID:-} ====="
if [ "$MODE" = plan ]; then
    # Yesterday's lanes are written: their drop-box chromatogram copies can go (docstring). Never
    # a reason for the plan to fail.
    "$PY" -u fill_diann_xic_lanes.py prune-drop --apply || echo "prune-drop failed rc=$?"
    OUT=$("$PY" -u fill_diann_xic_lanes.py plan "${@:2}" | tee /dev/stderr | awk '/^PLAN /{print $2, $3}')
    PLAN=${OUT% *}; N=${OUT#* }
    if [ -z "$PLAN" ] || [ "${N:-0}" -eq 0 ]; then echo "nothing to do"; exit 0; fi
    K=$(( N < 16 ? N : 16 ))
    J=$(sbatch --parsable --array=0-$((K-1))%4 "$ING/fill_diann_xic_lanes.sbatch" run "$K" "$PLAN")
    echo "submitted run array $J: $N search(es) over $K task(s)"
    exit 0
fi
"$PY" -u fill_diann_xic_lanes.py run "${SLURM_ARRAY_TASK_ID:?run is an array task}" "$2" "$3"
