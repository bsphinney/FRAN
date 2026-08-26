#!/bin/bash
#SBATCH --job-name=fran_auto_ingest
#SBATCH --account=genome-center-grp
#SBATCH --partition=high
#SBATCH --time=08:00:00
#SBATCH --cpus-per-task=4
#SBATCH --mem=32G
#SBATCH --output=/quobyte/proteomics-grp/de-limp/fran_refresh/logs/auto_ingest_%j.out
#
# Ingest un-ingested searches found on Flinders/Hive, INSIDE SLURM -- never on the login node.
#
# --nodelist is copied from fran_mv_refresh.sbatch and is NOT cosmetic: those hive-dc-7-* nodes are
# the ones empirically confirmed to reach PG Farm:5432. Most compute nodes are firewalled off it, so
# an unpinned job fails at connect() after waiting in the queue.
#
# --limit is deliberately small. The first real scan found 60 un-ingested searches; ingesting them
# all in one unattended pass would hammer a 434M-row table for hours. The cron runs often enough to
# drain the backlog a few at a time, and a re-scan each run means anything ingested by a Windows
# node in the meantime simply stops being a candidate.
set -uo pipefail
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true

D=/quobyte/proteomics-grp/de-limp/fran_refresh
CODE=/quobyte/proteomics-grp/brett/glendon/fran_ingest
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
export DELIMP_PG_TOKEN_FILE="$D/.pgfarm_token"   # _token() exchanges the secret for a fresh JWT

LIMIT="${FRAN_AUTO_LIMIT:-5}"
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
echo "===== fran auto-ingest $(date '+%F %T') on $(hostname) limit=$LIMIT ====="
"$PY" -u "$CODE/auto_ingest.py" --apply --limit "$LIMIT" --python "$PY"
rc=$?
echo "===== auto-ingest exit rc=$rc $(date '+%F %T') ====="
exit $rc
