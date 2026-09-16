#!/bin/bash
#SBATCH --job-name=fran_ptm_refresh
#SBATCH --account=publicgrp
#SBATCH --partition=low
#SBATCH --qos=publicgrp-low-qos
#SBATCH --nodelist=hive-dc-7-8-54,hive-dc-7-9-44,hive-dc-7-9-51
#SBATCH --time=04:00:00
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --output=/quobyte/proteomics-grp/de-limp/fran_refresh/logs/ptm_refresh_%j.out
# Rebuild delimp_search_protein_ptm — the per-(search, protein_group) modification flags behind the
# search heatmap's PTM filters. Computing them live costs 16.4 s on a 10-sample search and 90.9 s
# on a 21-sample one, against a PUBLIC endpoint that auto-fires on every search-page view, so they
# must be precomputed or the filter cannot exist at all.
#
# WHY THIS IS ITS OWN JOB, reversing the earlier ruling that it belonged in fran_mv_refresh.sbatch.
# That ruling was right that the firewall premise for a login-node cron was false — the --nodelist
# pinning below is the deployed answer to it — and wrong that the existing weekly job was therefore
# the right home, because nobody checked whether that job had room. It does not. `sacct` for
# fran_mv_refresh, verified 2026-09-16, with ONE payload against its own --time=04:00:00:
#
#     TIMEOUT 04:00:05 · TIMEOUT 04:00:05 · COMPLETED 02:56:32
#     TIMEOUT 04:00:19 · TIMEOUT 04:00:09 · COMPLETED 02:49:21
#
# Four of six runs already hit the wall. Adding this payload (~0.7 h for the first backfill) would
# mean it usually never starts, and — worse — when SLURM kills the job mid-payload the final
# `echo "done ... rc=..."` never runs, so the rc reporting that exists to make failures noisy is
# itself unreachable. A separate job gets its own wall, fails independently, and cannot push an
# already-timing-out job over its limit.
#
# --time=04:00:00 is ~6x the measured 0.7 h first-run backfill and is a limit already proven
# schedulable on this partition/QoS. Every run after the first is incremental and takes minutes.
#
# NODE PINNING AND TOKEN CONVENTION ARE COPIED, NOT REINVENTED: the hive-dc-7-* nodes are the ones
# empirically confirmed to reach PG Farm:5432 (most compute nodes are firewalled off it), and the
# token lives beside the other refresh jobs' token in fran_refresh/ even though the script itself
# lives in fran_ingest/. One secret, one location.
#
# TRACKED IN THE REPO, and Hive's copy is a deployed copy: scp after editing. The sibling job ran
# for months as a loose file with no version-controlled copy, and its corpus_reach payload was
# committed on 2026-09-10 and never copied across — so that fix has still never executed. Do not
# repeat that here: editing this file changes nothing until it is copied.
set -uo pipefail
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true
D=/quobyte/proteomics-grp/de-limp/fran_refresh
export DELIMP_PG_TOKEN_FILE="$D/.pgfarm_token"   # FRAN secret; the token helper exchanges it fresh
echo "===== fran PTM rollup refresh $(date '+%F %T') on $(hostname) ====="

# INCREMENTAL by default: only searches with no rows yet AND with precursors that can produce rows.
# The first run is the backfill (~0.7 h for 2,086 searches) and dominates this job exactly once.
#
# --batch 50 is not a tuning knob, it is the design. Measured: 12 searches in ONE batched query take
# 13.7 s (1.1 s each); the SAME searches queried individually take 16-91 s EACH. One batched pass is
# a single sequential scan of delimp_precursors, a per-search loop is thousands of index lookups.
# Lowering this toward 1 reverts a ~40-minute job to a 9-53 HOUR one.
#
# Invoked by absolute path so its own directory lands on sys.path — refresh_search_ptm.py does
# `from coreomics_import import _conn`, and coreomics_import.py sits beside it in fran_ingest/.
# It must NOT import app.*: there is no app/ on this cluster path. See the script's docstring.
/quobyte/proteomics-grp/brett/stan_venv/bin/python -u \
    /quobyte/proteomics-grp/brett/glendon/fran_ingest/refresh_search_ptm.py --batch 50
rc_ptm=$?

echo "===== done search_ptm_rc=$rc_ptm $(date '+%F %T') ====="
exit $rc_ptm
