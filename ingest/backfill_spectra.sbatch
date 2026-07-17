#!/bin/bash
#SBATCH -A genome-center-grp
#SBATCH -p high
#SBATCH -q genome-center-grp-high-qos
#SBATCH -c 14
#SBATCH --mem=120G
#SBATCH -t 16:00:00
#SBATCH -J spectra_backfill
#SBATCH -o spectra_backfill_%j.log
# Corpus backfill of the observed-spectrum Lance lane. Run on a COMPUTE node (never the login
# node — parallel Arrow/Lance workers OOM-killed the login node). Submit:
#   sbatch backfill_spectra.sbatch <reports_root> <lance_out_dir>
set -euo pipefail
REPORTS_ROOT="${1:-/nfs/lssc0/flinders/proteomics/Data/FRAN_reports}"
LANCE_DIR="${2:-/quobyte/proteomics-grp/brett/glendon/spectra_lance}"
ING=/quobyte/proteomics-grp/brett/glendon/fran_ingest
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
export DELIMP_PG_TOKEN_FILE=/quobyte/proteomics-grp/brett/.pgfarm_token
echo "backfill: $REPORTS_ROOT -> $LANCE_DIR  ($(date))"
cd "$ING"
# 5 workers on a 14-core/120G node (workers recycle per report). Big reports (10M+ fragments)
# build several GB each during assembly, so peak = workers x per-report; 5x keeps us well under
# 120G. Corrupt/0-byte reports are logged and skipped (they go to the regen queue).
"$PY" backfill_fragments.py --scan "$REPORTS_ROOT" --out-dir "$LANCE_DIR" --register --workers 5
echo "done ($(date))"
