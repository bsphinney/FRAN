#!/bin/bash
#SBATCH --job-name=fran_db_backup
#SBATCH --account=genome-center-grp
#SBATCH --partition=high
#SBATCH --time=24:00:00
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --output=/quobyte/proteomics-grp/de-limp/fran_refresh/logs/db_backup_%j.out
#
# Full pg_dump of the FRAN corpus to Flinders. PG Farm is not known to keep its own backups, so
# these dumps are believed to be the ONLY copy of a 228 GB / 434M-precursor database.
#
# They had stopped. The three existing dumps (2026-06-19, 06-22, 07-16) were made BY HAND -- no cron
# entry has ever referenced them and no script on the share mentions pg_dump -- and their header says
# they were written by pg_dump 18.4, a client that does not exist anywhere on Hive. When that machine
# went away the backups silently ended, leaving a 40-day gap by 2026-08-25.
#
# pg_dump therefore comes from a container (postgres:16, giving 16.15 >= server 16.14). A client
# OLDER than the server is refused by pg_dump, so the version floor is not optional.
set -uo pipefail
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true

SIF=/quobyte/proteomics-grp/apptainers/postgres16.sif
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
CODE=/quobyte/proteomics-grp/brett/glendon/fran_ingest
DEST=/nfs/lssc0/flinders/proteomics/Data/de-limp-db-backups
KEEP="${FRAN_BACKUP_KEEP:-4}"
STAMP=$(date '+%Y%m%d_%H%M')
OUT="$DEST/delimp_${STAMP}.dump"

echo "===== fran db backup $(date '+%F %T') on $(hostname) ====="
[ -f "$SIF" ] || { echo "ABORT: no $SIF"; exit 1; }
[ -d "$DEST" ] || { echo "ABORT: no $DEST"; exit 1; }

avail=$(df -BG --output=avail "$DEST" 2>/dev/null | tail -1 | tr -dc '0-9')
echo "  destination free: ${avail:-?} GB"
# Refuse rather than half-fill the share. The last dump was 38.6 GB when the database was smaller;
# it is 228 GB now, so demand real headroom before starting a multi-hour write.
if [ -n "${avail:-}" ] && [ "$avail" -lt 300 ]; then
  echo "ABORT: under 300 GB free at $DEST"; exit 1
fi

PGPASSWORD=$("$PY" -c "
import sys; sys.path.insert(0,'$CODE')
from refresh_leaderboards import _token; print(_token())" 2>/dev/null | tail -1)
[ -z "${PGPASSWORD:-}" ] && { echo "ABORT: could not obtain a PG Farm token"; exit 1; }
export PGPASSWORD

# Write to .part and rename only on success: a truncated dump that looks like a finished one is
# worse than no dump, because the retention prune below would count it as a good generation.
t0=$(date +%s)
# --bind is required, not optional: apptainer mounts $HOME and a few defaults, NOT /nfs. Without
# it the container cannot see the destination and pg_dump fails with "No such file or directory"
# even though the job itself can stat the directory perfectly well (the free-space check above
# passes). Bind the share root so the .part, the final file and the .toc all resolve.
apptainer exec --bind /nfs/lssc0/flinders/proteomics \
  --env PGPASSWORD="$PGPASSWORD" --env PGSSLMODE=require "$SIF" \
  pg_dump -h pgfarm.library.ucdavis.edu -p 5432 \
          -U genome-proteomics-service-account \
          -d "uc-davis-genome-center-proteomics-core/delimp" \
          -Fc -Z 6 --no-owner --no-privileges \
          -f "$OUT.part"
rc=$?
el=$(( $(date +%s) - t0 ))
if [ $rc -ne 0 ]; then
  echo "FAILED: pg_dump rc=$rc after ${el}s"; rm -f "$OUT.part"; exit $rc
fi

# A backup nobody can read is not a backup. pg_restore --list parses the archive's table of
# contents, so it fails on a truncated or corrupt file without restoring anything.
if ! apptainer exec --bind /nfs/lssc0/flinders/proteomics "$SIF" \
        pg_restore --list "$OUT.part" > "$OUT.toc" 2>/dev/null; then
  echo "FAILED: dump is not readable by pg_restore — keeping it as $OUT.part.bad for inspection"
  mv "$OUT.part" "$OUT.part.bad"; rm -f "$OUT.toc"; exit 1
fi
mv "$OUT.part" "$OUT"
sz=$(du -h "$OUT" | cut -f1)
echo "  wrote $OUT ($sz) in ${el}s; $(wc -l < "$OUT.toc") archive entries verified"

# Retention. Never prunes below one surviving dump, and only ever removes files it can see are
# complete (.part and .bad are excluded by the glob).
mapfile -t old < <(ls -1t "$DEST"/delimp_*.dump 2>/dev/null | tail -n +$((KEEP+1)))
if [ ${#old[@]} -gt 0 ]; then
  for f in "${old[@]}"; do echo "  pruning $(basename "$f") ($(du -h "$f" | cut -f1))"; rm -f "$f" "$f.toc"; done
fi
echo "  kept: $(ls -1 "$DEST"/delimp_*.dump 2>/dev/null | wc -l) dump(s), $(du -sh "$DEST" | cut -f1) total"
echo "===== done $(date '+%F %T') ====="
