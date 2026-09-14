#!/bin/bash
#SBATCH --job-name=fran_mv_refresh
#SBATCH --account=publicgrp
#SBATCH --partition=low
#SBATCH --qos=publicgrp-low-qos
#SBATCH --nodelist=hive-dc-7-8-54,hive-dc-7-9-44,hive-dc-7-9-51
#SBATCH --time=04:00:00
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --output=/quobyte/proteomics-grp/de-limp/fran_refresh/logs/refresh_%j.out
# Rebuild FRAN leaderboard matviews INSIDE SLURM (not the login node). Pinned to hive-dc-7-* nodes,
# the ones empirically confirmed to reach PG Farm:5432 (most compute nodes are firewalled off it).
# Light job: PG Farm does the GROUP BY; this process just issues REFRESH and waits.
#
# TRACKED IN THE REPO as of 2026-09-10. It ran for months as a loose file on Hive with no copy under
# version control, which is the same trap that left ingest/cron_corpus_reach.sh uncommitted. Hive's
# copy is still what crond executes (`17 4 * * 0 ... sbatch /quobyte/.../fran_mv_refresh.sbatch`),
# so this file is the source and Hive's is a deployed copy: scp after editing.
set -uo pipefail
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true
D=/quobyte/proteomics-grp/de-limp/fran_refresh
export DELIMP_PG_TOKEN_FILE="$D/.pgfarm_token"   # FRAN secret; _token() exchanges it fresh each run
echo "===== fran MV refresh $(date '+%F %T') on $(hostname) ====="
/quobyte/proteomics-grp/brett/stan_venv/bin/python -u "$D/refresh_leaderboards.py"
rc_mv=$?

# delimp_protein_corpus_reach, the corpus-wide table behind the search heatmap's `rarity` and
# `corpus_abundance` ranking modes. It belongs HERE, not in a cron of its own:
#
#   - refresh_corpus_reach.py's own header says "Run on the same weekly schedule as
#     refresh_leaderboards.py, after new searches land", and the design spec pre-specified the same
#     home ("refreshed by the existing weekly Hive cron alongside the matviews").
#   - It is the identical kind of work this job already does: one long statement that PG Farm
#     executes while this process waits (measured 515 s for 278,194 genes on 2026-09-09).
#   - The standalone wrapper argued for the login node because "compute nodes are not reliably able
#     to reach the public internet". That premise was borrowed from cron_coreomics_import.sh, which
#     really does call a public API — but this refresh only needs PG Farm:5432, and the --nodelist
#     pinning above is the deployed solution to exactly that firewall problem.
#   - This job already has the lock (the crontab's `flock -n /tmp/fran_mv_refresh.lock`), the
#     schedule, the pinning and the token. A separate cron recreated all four, worse.
#
# Invoked by absolute path so its own directory lands on sys.path — refresh_corpus_reach.py does
# `from coreomics_import import _conn`, and coreomics_import.py sits beside it in fran_ingest/.
# It inherits DELIMP_PG_TOKEN_FILE from the export above; no second token convention.
/quobyte/proteomics-grp/brett/stan_venv/bin/python -u \
    /quobyte/proteomics-grp/brett/glendon/fran_ingest/refresh_corpus_reach.py
rc_reach=$?

# delimp_search_protein_ptm, the per-(search, protein_group) modification flags behind the search
# heatmap's PTM filters. Same home, same reasoning as corpus_reach above, plus one of its own:
# computing these live costs 16.4 s on a 10-sample search and 90.9 s on a 21-sample one, against a
# PUBLIC endpoint that auto-fires on every search-page view — so they must be precomputed or the
# filter cannot exist.
#
# INCREMENTAL by default: it processes only searches with no rows yet, so the weekly run is cheap
# once the first backfill has landed. The first run is the backfill (~0.7 h for 2,086 searches) and
# will dominate this job's wall-clock exactly once.
#
# --batch 50 is not a tuning knob, it is the design. Measured: 12 searches in ONE batched query take
# 13.7 s (1.1 s each); the SAME searches queried individually take 16-91 s EACH. One batched pass is
# a single sequential scan of delimp_precursors, a per-search loop is thousands of index lookups.
# Lowering this toward 1 reverts a ~40-minute job to a 9-53 HOUR one.
/quobyte/proteomics-grp/brett/stan_venv/bin/python -u \
    /quobyte/proteomics-grp/brett/glendon/fran_ingest/refresh_search_ptm.py --batch 50
rc_ptm=$?

# Every rc, not `$?`. With three payloads a bare `$?` reports only the last, so a failed matview
# refresh would be logged as a success whenever the later refreshes happened to succeed — precisely
# the silent-failure mode this job's schedule exists to make noisy.
echo "===== done mv_rc=$rc_mv corpus_reach_rc=$rc_reach search_ptm_rc=$rc_ptm $(date '+%F %T') ====="
exit $(( rc_mv | rc_reach | rc_ptm ))
