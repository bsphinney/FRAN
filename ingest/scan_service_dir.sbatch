#!/bin/bash
#SBATCH -A genome-center-grp
#SBATCH -p high
#SBATCH -q genome-center-grp-high-qos
#SBATCH -c 2
#SBATCH --mem=8G
#SBATCH -t 04:00:00
#SBATCH -J fran_svcdir
#SBATCH -o /quobyte/proteomics-grp/brett/logs/fran_svcdir_%j.log
#
# Walks ~750 client folders on the Flinders share. Metadata-heavy over NFS, so not login-node work.
#
# LOGNAME/USER seeded and the profile sourced OUTSIDE set -u: /etc/profile.d/modules.sh
# dereferences LOGNAME, which silently killed the Flinders cron (STAN 2026-06->08 postmortem).
export LOGNAME="${LOGNAME:-$(id -un)}"
export USER="${USER:-$(id -un)}"
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true
set -uo pipefail

export DELIMP_PG_TOKEN_FILE=/quobyte/proteomics-grp/brett/.pgfarm_token
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
cd /quobyte/proteomics-grp/brett/glendon/fran_ingest || exit 1

echo "=== start $(date) on $(hostname) ==="
"$PY" -u scan_service_dir.py --apply
rc=$?
echo "=== end $(date) rc=$rc ==="
exit $rc
