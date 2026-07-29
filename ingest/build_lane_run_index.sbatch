#!/bin/bash
#SBATCH -A genome-center-grp
#SBATCH -p high
#SBATCH -q genome-center-grp-high-qos
#SBATCH -c 8
#SBATCH --mem=96G
#SBATCH -t 12:00:00
#SBATCH -J lane_run_index
#SBATCH -o lane_run_index_%j.log
#
# Scans all 1,553 spectrum-lane datasets for their distinct (run, raw_path) pairs and materialises
# delimp_spectrum_lane_runs -- the join that makes "select comparable runs, then aggregate"
# expressible. Compute node only: parallel Arrow/Lance work has OOM-killed the Hive login node.
#
# The job is CHECKPOINTED (commits per dataset, resumes by skipping indexed lance_paths), so if it
# hits the wall clock, just resubmit -- unlike the prior corpus-scale job here, which wrote one
# pickle at the end, ran 6:00:14 against a 6-hour wall, and produced nothing.
set -euo pipefail

ING=/quobyte/proteomics-grp/brett/glendon/fran_ingest
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
export DELIMP_PG_TOKEN_FILE=${DELIMP_PG_TOKEN_FILE:-/quobyte/proteomics-grp/de-limp/fran_refresh/.pgfarm_token}

cd "$ING"
echo "host=$(hostname) start=$(date -Is)"
"$PY" build_lane_run_index.py --apply "$@"
echo "end=$(date -Is)"
