#!/bin/bash
#SBATCH --job-name=fran_sne_ingest
#SBATCH --account=publicgrp
#SBATCH --partition=low
#SBATCH --qos=publicgrp-low-qos
#SBATCH --nodelist=hive-dc-7-8-54,hive-dc-7-9-44,hive-dc-7-9-51
#SBATCH --time=16:00:00
#SBATCH --cpus-per-task=4
#SBATCH --mem=48G
#SBATCH --output=/quobyte/proteomics-grp/de-limp/fran_refresh/logs/sne_ingest_%j.out
#
# Ingest the .sne experiments whose FRAN.rs report is ALREADY on disk -- no Windows round trip.
# Job list built by find_uningested.py --find-sne, filtered to FRAN.rs reports only and deduped so
# one report can never be claimed by two search identities.
#
# FRAN.rs only, deliberately: 121 of the reports found are BGS Factory exports, which carry no
# PG.Genes, no EG.IonMobility and no F.* fragment columns. They would ingest without error and land
# without genes, without ion mobility and without an observed-spectrum lane. Those are queued for
# re-export on Windows instead.
#
# Pinned to hive-dc-7-* : the nodes confirmed to reach PG Farm:5432.
set -uo pipefail
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true
D=/quobyte/proteomics-grp/de-limp/fran_refresh
CODE=/quobyte/proteomics-grp/brett/glendon/fran_ingest
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
export DELIMP_PG_TOKEN_FILE="$D/.pgfarm_token"
echo "===== fran .sne ingest $(date '+%F %T') on $(hostname) ====="
"$PY" -u "$CODE/auto_ingest.py" --apply --direct "$CODE/fran_jobs.json" --limit 100 --python "$PY"
echo "===== exit rc=$? $(date '+%F %T') ====="
