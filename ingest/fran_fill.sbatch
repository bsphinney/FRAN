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
set -uo pipefail
source /etc/profile.d/modules.sh 2>/dev/null || true
export DELIMP_PG_TOKEN_FILE=/quobyte/proteomics-grp/brett/.pgfarm_token
ING=/quobyte/proteomics-grp/brett/glendon/fran_ingest
export PYTHONPATH=$ING
cd "$ING"
echo "===== fran_fill $(date -Is) on $(hostname) args: $* ====="
/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python -u fran_fill.py "$@"
echo "===== fran_fill done rc=$? $(date -Is) ====="
