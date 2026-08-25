#!/bin/bash
#SBATCH -A publicgrp
#SBATCH -p low -q publicgrp-low-qos
#SBATCH -c 4 --mem=24G -t 12:00:00
#SBATCH -J selfref
#SBATCH -o /quobyte/proteomics-grp/brett/glendon/fran_ingest/selfref_logs/sr_%A_%a.log
#
# Self-reference scoring, one run per array task, on the low partition.
#
# Each task is fully self-contained: convert .raw -> mzparquet, build the cache, extract, compare
# against the search engine's own reported values, record, delete the intermediates. Nothing is
# shared between tasks except the manifest, so a preempted or failed task costs exactly one run and
# can be resubmitted alone.
#
# The low partition is PREEMPTIBLE. That is fine here and is why the work is structured this way:
# there is no partial state to corrupt, --apply commits per run, and a re-run of a completed run
# simply recomputes and overwrites the same numbers. Do NOT restructure this into a job that holds
# state across runs without revisiting that.
#
# Scratch: intermediates are ~1.5 GB per run and are removed at the end of each task, so peak usage
# is roughly (concurrent tasks x 1.5 GB), not (total runs x 1.5 GB). The %N throttle below is
# therefore a disk bound as much as a politeness one.
set -uo pipefail
module load dotnet-core-sdk/8.0.4 2>/dev/null
export DOTNET_ROOT="$(dotnet --list-runtimes 2>/dev/null | head -1 | sed -E 's#.*\[(.*)/shared/.*#\1#')"

ING=/quobyte/proteomics-grp/brett/glendon/fran_ingest
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
MANIFEST="${SELFREF_MANIFEST:-$ING/selfref_manifest.tsv}"
# 12 fragment channels so the comparison is not silently limited to the first 6 the engine reported.
export FRAN_XIC_FRAG_CHANNELS="${FRAN_XIC_FRAG_CHANNELS:-12}"

LINE=$(sed -n "${SLURM_ARRAY_TASK_ID}p" "$MANIFEST")
[ -z "$LINE" ] && { echo "no manifest line ${SLURM_ARRAY_TASK_ID}"; exit 0; }
IFS=$'\t' read -r RUN LANCE RAW MODEL TRACE <<< "$LINE"

WORK="$ING/selfref_work/task_${SLURM_ARRAY_TASK_ID}"
mkdir -p "$WORK"
echo "task ${SLURM_ARRAY_TASK_ID}  $RUN  [$MODEL]"
echo "  raw   $RAW"
echo "  lane  $LANCE"

TRACE_ARG=()
[ "$TRACE" != "-" ] && TRACE_ARG=(--trace-lance "$TRACE")

cd "$ING"
"$PY" selfref_score.py \
  --run "$RUN" --lance "$LANCE" --raw "$RAW" \
  --work-dir "$WORK" --limit-precursors 1500 \
  "${TRACE_ARG[@]}" "$@"
RC=$?

# The work dir is per-task and holds only this run's intermediates; selfref_score already removes
# the cache and mzparquet, so this just clears the empty scaffolding.
rm -rf "$WORK"
exit $RC
