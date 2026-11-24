#!/bin/bash
#SBATCH --job-name=fran_spectrum_lane
#SBATCH --account=publicgrp                  # free tier; the cap is 8 CPUs / 128 GB per job (ucd-hpc)
#SBATCH --partition=low
#SBATCH --qos=publicgrp-low-qos
#SBATCH --requeue                            # low is preemptible; the job is safe to restart (below)
#SBATCH --time=1-00:00:00                    # a night's cap of writes (--max) fits well inside this
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=128G                           # 4 reports parsed at once, each a few to ~25 GB
#SBATCH --exclude=hive-as-11-2-41            # fails every job launch since 2026-11-13 ~01:00
#SBATCH --output=/quobyte/proteomics-grp/brett/scratch/fran_health/logs/fran_spectrum_lane_%j.out
# Observed-spectrum Lance lane for Spectronaut searches that have none (fill_spectrum_lanes.py).
# Submitted daily by fill_spectrum_lanes_cron.sh. Dry run by default in the script; this passes --apply.
#
# REQUEUE-SAFE: the script re-plans from the DB on every start. A dataset written but not yet
# registered is found on disk and verified+registered, never rewritten; a half-written one is not
# registered and is overwritten by its own retry. One writer per dataset (one job, one planner;
# the cron wrapper refuses to submit while a previous job is queued or running).
#   FRAN_SPECTRUM_MAX=N  caps lane WRITES per run (default 40; raise for catch-up)
set +u
source /etc/profile.d/modules.sh 2>/dev/null || true   # under set -u this kills the job at 0 s
set -uo pipefail
export PATH=/cvmfs/hpc.ucdavis.edu/sw/spack/environments/core/view/generic/slurm/bin:$PATH
export DELIMP_PG_TOKEN_FILE=/quobyte/proteomics-grp/brett/.pgfarm_token
ING=/quobyte/proteomics-grp/brett/glendon/fran_ingest
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
export PYTHONPATH=$ING
cd "$ING"
echo "===== fran_spectrum_lane $(date -Is) on $(hostname) job ${SLURM_JOB_ID:-} restart ${SLURM_RESTART_COUNT:-0} ====="
exec "$PY" -u fill_spectrum_lanes.py --apply --max "${FRAN_SPECTRUM_MAX:-40}" --workers 4
