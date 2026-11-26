#!/bin/bash
#SBATCH --job-name=fran_spectrum_lane
#SBATCH --account=publicgrp                  # free tier; the cap is 8 CPUs / 128 GB per job (ucd-hpc)
#SBATCH --partition=low
#SBATCH --qos=publicgrp-low-qos
#SBATCH --dependency=singleton               # never two jobs of this name at once (belt and braces with the cron check)
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
#   FRAN_SPECTRUM_MAX=N  caps lane WRITES per run (default 40 = nightly; 0 = NO CAP, the one-off catch-up).
#   The first log line prints the cap in force.
set +u
source /etc/profile.d/modules.sh 2>/dev/null || true   # under set -u this kills the job at 0 s
set -uo pipefail
export PATH=/cvmfs/hpc.ucdavis.edu/sw/spack/environments/core/view/generic/slurm/bin:$PATH
export DELIMP_PG_TOKEN_FILE=/quobyte/proteomics-grp/brett/.pgfarm_token
ING=${FRAN_ING:-/quobyte/proteomics-grp/brett/glendon/fran_ingest}   # FRAN_ING: run a staged copy (a trial)
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
export PYTHONPATH=$ING
cd "$ING"
echo "===== fran_spectrum_lane $(date -Is) on $(hostname) job ${SLURM_JOB_ID:-} restart ${SLURM_RESTART_COUNT:-0} ====="
# FRAN_SPECTRUM_ARGS: extra flags, e.g. "--only-sids <id> <id>" for a trial
"$PY" -u fill_spectrum_lanes.py --apply --preflight --max "${FRAN_SPECTRUM_MAX:-40}" --workers 4 ${FRAN_SPECTRUM_ARGS:-}
rc=$?
# Then the chromatogram (XIC) lane for Spectronaut searches whose XIC dbs sne_one.py archived on
# Flinders. ON HOLD by default: the lane format (every trace vs identified-only, compression) is
# not settled until real exports have been measured, so this step only LISTS what is waiting.
# FRAN_SN_XIC_BUILD=1 makes it write lanes (its own state and lane folder, one lane at a time).
# The XIC dbs on the share are never deleted here unless FRAN_SN_XIC_FREE_DBS=1 is also set.
echo "===== spectronaut XIC lanes $(date -Is) ====="
if [ "${FRAN_SN_XIC_BUILD:-0}" = "1" ]; then
  "$PY" -u fill_spectronaut_xic_lanes.py --apply --max "${FRAN_SN_XIC_MAX:-0}" --budget-hours "${FRAN_SN_XIC_BUDGET_H:-8}"
else
  echo "on hold (FRAN_SN_XIC_BUILD is not 1): listing only, no lane is written"
  "$PY" -u fill_spectronaut_xic_lanes.py
fi
rc2=$?
[ "$rc" -ne 0 ] && exit "$rc"
exit "$rc2"
