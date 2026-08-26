#!/bin/bash
#SBATCH --job-name=fran_sne_ingest
#SBATCH --account=genome-center-grp
#SBATCH --partition=high
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
set -uo pipefail
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true
D=/quobyte/proteomics-grp/de-limp/fran_refresh
CODE=/quobyte/proteomics-grp/brett/glendon/fran_ingest
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
export DELIMP_PG_TOKEN_FILE="$D/.pgfarm_token"
# PG Farm reachability preflight. The old low-partition pinning existed because most compute nodes
# were believed firewalled off PG Farm:5432; on `high` we take whatever node we are given, so check
# before doing any work. Every high-partition node used on 2026-08-25 (hive-dc-7-4/5/6/7-*,
# hive-as-11-3-*) reached it, but "usually" is not a guarantee -- failing here with a clear message
# costs one queue slot, whereas discovering it after an hour of parsing costs the run.
"$PY" - <<'PREFLIGHT' || { echo "ABORT: PG Farm unreachable from $(hostname)"; exit 75; }
import socket, sys
s = socket.socket(); s.settimeout(15)
try:
    s.connect(("pgfarm.library.ucdavis.edu", 5432))
    print("  preflight: PG Farm:5432 reachable")
except Exception as e:
    print("  preflight FAILED: %s" % e); sys.exit(1)
finally:
    s.close()
PREFLIGHT
echo "===== fran .sne ingest $(date '+%F %T') on $(hostname) ====="
"$PY" -u "$CODE/auto_ingest.py" --apply --direct "$CODE/fran_jobs.json" --limit 100 --python "$PY"
echo "===== exit rc=$? $(date '+%F %T') ====="
