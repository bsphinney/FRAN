#!/bin/bash
#SBATCH --job-name=fran_lane_backup
#SBATCH --account=publicgrp                  # free tier: restartable copy, resumes into the same folder
#SBATCH --partition=low                      # preemptible; a requeue continues the unfinished snapshot
#SBATCH --qos=publicgrp-low-qos
#SBATCH --requeue
#SBATCH --dependency=singleton               # never two copies at once
#SBATCH --time=2-00:00:00
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
#SBATCH --output=/quobyte/proteomics-grp/brett/scratch/fran_health/logs/fran_lane_backup_%j.out
# Dated, versioned copies of the Lance lanes.
#
# WHY. The lanes live on /quobyte group storage, which HPC does not back up (only a purchased
# BACKED-UP/ folder is), and the daily Azure upload is a MIRROR: it copies whatever is newer, so a
# lane that is overwritten or damaged here replaces the good copy there the next morning. 238
# Spectronaut lanes have no source report left and cannot be rebuilt. This keeps every past state.
#
# HOW. One folder per day under $DEST, made with `rsync --link-dest` against the previous one: a
# file that has not changed is a hard link to the copy already there, so a snapshot costs only the
# space of what changed, and every snapshot is a complete tree that can be read or copied back as
# is. Nothing here ever writes to, or deletes from, the lane folders.
#
# RESTORE one lane:  cp -a $DEST/<date>/spectra_lance/<name>.lance  <somewhere>
# (copy it out; do not move it, and do not edit a snapshot in place -- its files are shared by
#  hard link with the other snapshots).
set -uo pipefail
SRC="${FRAN_LANE_SRC:-/quobyte/proteomics-grp/brett/glendon}"
DEST="${FRAN_LANE_BACKUP_DIR:-/nfs/lssc0/flinders/proteomics/Data/FRAN_lane_backups}"
KEEP="${FRAN_LANE_BACKUP_KEEP:-30}"          # snapshots kept
LANES="spectra_lance xic_lance"
MIN_FREE_GB=2000

echo "=== fran_lane_backup $(date -Is) on $(hostname) job ${SLURM_JOB_ID:-none} ==="
mkdir -p "$DEST" || { echo "ABORT: cannot create $DEST"; exit 1; }
avail=$(df -BG --output=avail "$DEST" | tail -1 | tr -dc '0-9')
echo "destination free: ${avail:-?} GB"
[ "${avail:-0}" -ge "$MIN_FREE_GB" ] || { echo "ABORT: under $MIN_FREE_GB GB free at $DEST"; exit 1; }

# An unfinished snapshot (preempted, or failed) is continued, whatever day it was started.
part=$(ls -d "$DEST"/20??????.partial 2>/dev/null | sort | tail -1)
if [ -n "$part" ]; then
  stamp=$(basename "$part" .partial)
else
  stamp=$(date +%Y%m%d)
  part="$DEST/$stamp.partial"
fi
if [ -d "$DEST/$stamp" ]; then echo "snapshot $stamp already complete; nothing to do"; exit 0; fi
prev=$(ls -d "$DEST"/20?????? 2>/dev/null | sort | tail -1)
echo "snapshot $stamp  (previous: ${prev:-none})"
mkdir -p "$part"

rc=0
for lane in $LANES; do
  [ -d "$SRC/$lane" ] || { echo "ABORT: no $SRC/$lane"; exit 1; }
  n_src=$(ls "$SRC/$lane" | wc -l)
  # A lane folder that has lost most of its datasets is a problem to look at, not to snapshot as
  # the new normal: stop, and keep the previous snapshot as the latest.
  if [ -n "$prev" ] && [ -d "$prev/$lane" ]; then
    n_prev=$(ls "$prev/$lane" | wc -l)
    if [ "$n_src" -lt $(( n_prev * 9 / 10 )) ]; then
      echo "ABORT: $lane has $n_src datasets, the last snapshot had $n_prev"; exit 1
    fi
  fi
  link=()
  [ -n "$prev" ] && [ -d "$prev/$lane" ] && link=(--link-dest="$prev/$lane")
  echo "--- $lane: $n_src datasets $(date -Is)"
  rlog="$DEST/logs/rsync_${stamp}_${lane}_${SLURM_JOB_ID:-manual}.log"
  mkdir -p "$DEST/logs"
  rsync -a --stats "${link[@]}" "$SRC/$lane/" "$part/$lane/" > "$rlog" 2>&1
  r=$?
  grep -E "Number of files:|Number of regular files transferred|Total file size|Total transferred file size|rsync error" "$rlog"
  # 24 = a source file vanished mid-copy (a lane being written): the next snapshot picks it up.
  if [ "$r" -ne 0 ] && [ "$r" -ne 24 ]; then echo "FAILED: rsync $lane rc=$r"; rc=1; fi
  echo "COPIED $lane rc=$r datasets=$(ls "$part/$lane" | wc -l) $(date -Is)"
done
if [ "$rc" -ne 0 ]; then echo "=== FAILED: $part left for the next run to continue ==="; exit 1; fi

mv "$part" "$DEST/$stamp" || { echo "FAILED: could not finish $stamp"; exit 1; }
date -Is > "$DEST/LAST_SNAPSHOT.txt"; echo "$stamp" >> "$DEST/LAST_SNAPSHOT.txt"

# Retention: only finished snapshots, only by their own name pattern, never the newest KEEP.
old=$(ls -d "$DEST"/20?????? 2>/dev/null | sort | head -n -"$KEEP")
for d in $old; do
  case "$(basename "$d")" in 20[0-9][0-9][0-9][0-9][0-9][0-9]) echo "pruning $d"; rm -rf -- "$d";; esac
done
echo "kept: $(ls -d "$DEST"/20?????? | wc -l) snapshot(s)"
echo "=== done rc=0 $(date -Is) ==="
