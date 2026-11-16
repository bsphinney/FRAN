#!/bin/bash
#SBATCH --job-name=fran_blob_upload
#SBATCH --account=genome-center-grp
#SBATCH --partition=high
#SBATCH --qos=genome-center-grp-high-qos
#SBATCH --time=2-00:00:00                    # first run moves ~1 TB; later runs only new datasets
#SBATCH --cpus-per-task=8
#SBATCH --mem=16G
#SBATCH --exclude=hive-as-11-2-41
#SBATCH --output=/quobyte/proteomics-grp/brett/scratch/fran_health/logs/fran_blob_upload_%j.out
# Copy FRAN's Lance lanes to the franfragments blob account the website reads:
#   glendon/xic_lance     -> container xic      (peptide XIC panel, app/observed_xic.py)
#   glendon/spectra_lance -> container spectra  (measured-spectrum panel, app/observed_spectrum.py)
# Credentials: one SAS per container in .fran_blob_sas_<container> (600), each bound to the stored
# access policy `hive-upload` (no delete permission; revoke by deleting the policy). The old single
# .fran_blob_sas expired 2026-07-23 and spectrum uploads silently stopped then.
# azcopy copy --overwrite=ifSourceNewer: re-runs skip what is already there, so this is safe daily.
set +u
source /etc/profile.d/modules.sh 2>/dev/null || true
set -uo pipefail
export PATH=/cvmfs/hpc.ucdavis.edu/sw/spack/environments/core/view/generic/slurm/bin:$PATH
OTHER=$(squeue -u "$(id -un)" -h -n fran_blob_upload -t RUNNING -o %i 2>/dev/null | grep -vx "${SLURM_JOB_ID:-x}" || true)
[ -n "$OTHER" ] && { echo "another fran_blob_upload is running ($OTHER); exiting"; exit 0; }
G=/quobyte/proteomics-grp/brett/glendon
B=/quobyte/proteomics-grp/brett
ACCT=franfragments
WORK=$G/azcopy_work
mkdir -p "$WORK"; cd "$WORK"
echo "=== fran_blob_upload $(date -Is) on $(hostname) ==="
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 25 https://${ACCT}.blob.core.windows.net/ 2>/dev/null || echo 000)
[ "$code" = "000" ] && { echo "NO EGRESS to Azure from $(hostname)"; exit 3; }
if [ ! -x "$WORK/azcopy" ]; then
  curl -sL https://aka.ms/downloadazcopy-v10-linux -o azcopy.tgz && tar xzf azcopy.tgz
  cp azcopy_linux_amd64_*/azcopy "$WORK/azcopy" && chmod +x "$WORK/azcopy"
fi
export AZCOPY_LOG_LOCATION="$WORK/logs" AZCOPY_JOB_PLAN_LOCATION="$WORK/plans" AZCOPY_CONCURRENCY_VALUE=32
rc_all=0
for pair in "xic_lance:xic" "spectra_lance:spectra"; do
  SRC=$G/${pair%%:*}; CONT=${pair##*:}; SASF=$B/.fran_blob_sas_$CONT
  if [ ! -s "$SASF" ]; then echo "UPLOAD $CONT rc=9 no SAS file $SASF"; rc_all=9; continue; fi
  n=$(ls "$SRC" | wc -l)
  echo "--- $CONT: $n datasets from $SRC ($(date -Is))"
  "$WORK/azcopy" copy "$SRC/*" "https://${ACCT}.blob.core.windows.net/${CONT}?$(cat "$SASF")" \
      --recursive --overwrite=ifSourceNewer --log-level=ERROR --output-level=quiet
  rc=$?
  echo "UPLOAD $CONT rc=$rc datasets=$n $(date -Is)"     # fran_health_scan greps these lines
  [ $rc -ne 0 ] && rc_all=$rc
done
"$WORK/azcopy" jobs clean >/dev/null 2>&1 || true
echo "=== done rc=$rc_all $(date -Is) ==="
exit $rc_all
