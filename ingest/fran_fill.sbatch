#!/bin/bash
#SBATCH --job-name=fran_fill
#SBATCH --account=genome-center-grp          # the group's dedicated pool (memory: hive-slurm-accounts)
#SBATCH --partition=high                     # not preemptible: a filler killed mid-pass just repeats work
#SBATCH --qos=genome-center-grp-high-qos
#SBATCH --time=1-00:00:00                    # first pass over the Sep->Nov backlog probes thousands of raws
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
#SBATCH --exclude=hive-as-11-2-41            # fails every job launch since 2026-11-13 ~01:00
#SBATCH --output=/quobyte/proteomics-grp/brett/scratch/fran_health/logs/fran_fill_%j.out
# Runs FRAN's metadata fillers in order (fran_fill.py docstring). Nightly from fran_fill_cron.sh;
# pass --weekly through to add the whole-corpus tissue/blood rescoring:  sbatch fran_fill.sbatch --weekly
# modules.sh dereferences unset variables, and under set -u that kills the job with no message
# (exit 1 at 0 s, found by test 2026-11-13) -- the same trap corpus_drip.sh documents.
set +u
source /etc/profile.d/modules.sh 2>/dev/null || true
# The Thermo steps (record_raw_metadata --thermo, backfill_thermo_resolution, load_run_windows)
# load a .NET runtime. The module puts `dotnet` on PATH but leaves DOTNET_ROOT empty, and without
# it pythonnet cannot create the runtime: on 2026-11-13 a window load without this block recorded
# all 5,269 .raw files as "coreclr unavailable". Same block as record_raw_metadata.sbatch.
if command -v module >/dev/null 2>&1; then
    module load dotnet-core-sdk/8.0.4 2>/dev/null || echo "WARN: dotnet module not loaded"
fi
if [ -z "${DOTNET_ROOT:-}" ] && command -v dotnet >/dev/null 2>&1; then
    export DOTNET_ROOT="$(dotnet --list-runtimes 2>/dev/null \
        | sed -n 's|.*\[\(.*\)/shared/Microsoft.NETCore.App\]|\1|p' | head -1)"
fi
echo "DOTNET_ROOT=${DOTNET_ROOT:-<unset>}"
set -uo pipefail
export PATH=/cvmfs/hpc.ucdavis.edu/sw/spack/environments/core/view/generic/slurm/bin:$PATH
export DELIMP_PG_TOKEN_FILE=/quobyte/proteomics-grp/brett/.pgfarm_token
ING=/quobyte/proteomics-grp/brett/glendon/fran_ingest
export PYTHONPATH=$ING
cd "$ING"
echo "===== fran_fill $(date -Is) on $(hostname) args: $* ====="
/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python -u fran_fill.py "$@"
echo "===== fran_fill done rc=$? $(date -Is) ====="
