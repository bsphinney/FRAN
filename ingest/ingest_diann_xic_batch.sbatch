#!/bin/bash
#SBATCH --job-name=diann_xic_batch
#SBATCH --account=genome-center-grp
#SBATCH --partition=high
#SBATCH --time=16:00:00
#SBATCH --cpus-per-task=4
#SBATCH --mem=64G
#SBATCH --output=/quobyte/proteomics-grp/de-limp/fran_refresh/logs/diann_xic_batch_%j.out
#
# Ingest DIA-NN chromatograms that are ALREADY on disk into the XIC lane. No re-search, no
# extraction -- these searches ran with --xic and the output was simply never ingested.
#
# Job list built by plan_strict.py, which pairs each search ONLY with .xic.parquet under its own
# directory tree. The loose version globbed dirname(d)/*_xic and credited the DEROT search with 318
# files belonging to a sibling; strict pairing drops the "free" set from an apparent 16 searches /
# 496 runs to a real 15 / 483, which is the number worth acting on.
#
# Entries whose file count does not match their run count are SKIPPED, not guessed at: two
# 26june24_he searches have 8 .xic.parquet for 3 runs, which means the pairing is still wrong for
# them and ingesting would attach another run's chromatograms to the search.
set -uo pipefail
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true
D=/quobyte/proteomics-grp/de-limp/fran_refresh
CODE=/quobyte/proteomics-grp/brett/glendon/fran_ingest
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
LANCE=/quobyte/proteomics-grp/brett/glendon/xic_lance
export DELIMP_PG_TOKEN_FILE="$D/.pgfarm_token"

echo "===== diann xic batch $(date '+%F %T') on $(hostname) ====="
"$PY" - "$CODE/diann_xic_jobs.json" <<'PLAN' > /tmp/diann_xic_plan.tsv
import json, sys
jobs = json.load(open(sys.argv[1]))
for j in jobs:
    cov = j.get("coverage", 0)
    if not (0.9 <= cov <= 1.1):
        print("SKIP\t%s\tcoverage %.2f (%d xic / %d runs)" % (j["name"], cov, j["n_xic"], j["n_runs"]),
              file=sys.stderr)
        continue
    print("\t".join([j["search_id"], j["name"], j["dir"], j["report"], str(j["n_runs"])]))
PLAN
n=$(wc -l < /tmp/diann_xic_plan.tsv)
echo "planned: $n search(es)"
ok=0; fail=0
while IFS=$'\t' read -r sid name dir report nrun; do
  safe=$(printf '%s' "$name" | tr -c 'A-Za-z0-9._-' '_' | cut -c1-70)
  out="$LANCE/${safe}__${sid:0:8}.diann.xic.lance"
  echo "--- $name ($nrun runs) -> $(basename "$out")"
  if "$PY" -u "$CODE/diann_xic_to_lance.py" --dir "$dir" --xic-dir "$dir" --report "$report" \
        --out "$out" --search-id "$sid" --search-name "$name" --apply 2>&1 | tail -4; then
    ok=$((ok+1))
  else
    fail=$((fail+1)); echo "    FAILED: $name"
  fi
done < /tmp/diann_xic_plan.tsv
echo "===== done: $ok ingested, $fail failed  $(date '+%F %T') ====="
