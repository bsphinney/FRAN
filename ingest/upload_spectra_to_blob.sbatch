#!/bin/bash
#SBATCH -A genome-center-grp
#SBATCH -p high
#SBATCH -q genome-center-grp-high-qos
#SBATCH -c 8
#SBATCH --mem=16G
#SBATCH -t 8:00:00
#SBATCH -J fran_blob_upload
#SBATCH -o /quobyte/proteomics-grp/brett/glendon/fran_ingest/blob_upload_%j.log
set -uo pipefail
SRC=/quobyte/proteomics-grp/brett/glendon/spectra_lance
SASFILE=/quobyte/proteomics-grp/brett/.fran_blob_sas
ACCT=franfragments; CONT=spectra
WORK=/quobyte/proteomics-grp/brett/glendon/azcopy_work
mkdir -p "$WORK"; cd "$WORK"
echo "=== node $(hostname) $(date) ==="
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 25 https://${ACCT}.blob.core.windows.net/ 2>/dev/null || echo 000)
echo "egress probe HTTP=$code"
[ "$code" = "000" ] && { echo "NO EGRESS to Azure from $(hostname) — aborting"; exit 3; }
if [ ! -x "$WORK/azcopy" ]; then
  curl -sL https://aka.ms/downloadazcopy-v10-linux -o azcopy.tgz && tar xzf azcopy.tgz
  cp azcopy_linux_amd64_*/azcopy "$WORK/azcopy" && chmod +x "$WORK/azcopy"
fi
"$WORK/azcopy" --version
SAS=$(cat "$SASFILE")
DEST="https://${ACCT}.blob.core.windows.net/${CONT}?${SAS}"
export AZCOPY_LOG_LOCATION="$WORK/logs" AZCOPY_JOB_PLAN_LOCATION="$WORK/plans"
echo "=== azcopy start $(date) ==="
"$WORK/azcopy" copy "$SRC/*" "$DEST" --recursive --overwrite=ifSourceNewer --log-level=ERROR
rc=$?
echo "=== azcopy done rc=$rc $(date) ==="
exit $rc
