#!/bin/bash
#SBATCH --job-name=raw_census
#SBATCH --account=genome-center-grp
#SBATCH --partition=high
#SBATCH --qos=genome-center-grp-high-qos
#SBATCH --time=12:00:00
#SBATCH --cpus-per-task=8
#SBATCH --mem=16G
#SBATCH --array=0-1
#SBATCH --exclude=hive-as-11-2-41
#SBATCH --output=/quobyte/proteomics-grp/brett/raw_census/logs/raw_census_%A_%a.log
# Every Bruker .d and Thermo .raw on Flinders (task 0, ~1.5 h) and Quobyte (task 1, ~10 min).
# Read-only on raws. Each task writes its own census.tsv (one writer per file on Quobyte) and a
# _COMPLETE marker last; fill_hive_paths_from_census.py reads only a census with every marker.
# Weekly from raw_census_cron.sh. Task 0 also prunes: the newest 4 complete censuses are kept.
set -uo pipefail
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
ING=/quobyte/proteomics-grp/brett/glendon/fran_ingest
BASE=/quobyte/proteomics-grp/brett/raw_census
OUT=$BASE/${CENSUS_DATE:-$(date +%F)}
ROOTS=(/nfs/lssc0/flinders/proteomics /quobyte/proteomics-grp)
NAMES=(flinders quobyte)
i=$SLURM_ARRAY_TASK_ID
echo "start $(date -Is) $(hostname) root=${ROOTS[$i]} out=$OUT"
"$PY" -u "$ING/raw_census.py" --out "$OUT/${NAMES[$i]}" "${ROOTS[$i]}"
rc=$?
if [ "$i" = 0 ] && [ "$rc" = 0 ]; then
    # Keep the newest 4 dated censuses whose parts are all complete; never touch the one in progress.
    ls -d "$BASE"/20??-??-?? 2>/dev/null | sort -r | while read -r d; do
        [ "$d" = "$OUT" ] && continue
        [ -f "$d/flinders/_COMPLETE" ] && [ -f "$d/quobyte/_COMPLETE" ] && echo "$d"
    done | tail -n +4 | while read -r old; do echo "prune $old"; rm -rf -- "$old"; done
fi
echo "end rc=$rc $(date -Is)"
exit $rc
