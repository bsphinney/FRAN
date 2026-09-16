#!/bin/bash
#SBATCH --job-name=fran_mv_refresh
#SBATCH --account=genome-center-grp
#SBATCH --partition=high
#SBATCH --qos=genome-center-grp-high-qos
# ACCOUNT/PARTITION, changed 2026-09-16. Was publicgrp/low/publicgrp-low-qos with an explicit
# --nodelist of three dc-7 nodes. That combination stopped being schedulable: on 2026-09-16 the
# `low` partition held 440 pending jobs (400 from a single user) and THIS job, queued since the
# 13th, had an estimated start of the 22nd -- the weekly matview refresh had not run in three days
# and would not for six more. publicgrp/high was no better: capped at 128 CPUs for the whole group
# and already saturated (QOSGrpCpuLimit).
#
# genome-center-grp carries 64 DEDICATED CPUs per user against a 1303 group ceiling, and is the
# account this work should have been under all along.
#
# THE --nodelist IS GONE, AND THAT NEEDED CHECKING RATHER THAN ASSUMING. It pinned
# hive-dc-7-8-54/7-9-44/7-9-51 as "empirically confirmed to reach PG Farm:5432 (most compute nodes
# are firewalled off it)". Those three are in the `low` partition ONLY, so keeping the pin while
# moving to `high` would have made the job unschedulable outright. 71 other dc-7 nodes ARE in
# `high`, and reachability was re-verified there rather than inferred from the rack name: a probe
# job (23500310) landed on hive-dc-7-8-62 and reported "PG FARM REACHABLE". Left unpinned so the
# scheduler can use any of the 71; if a node ever turns out to be firewalled the symptom is a
# connection failure in the log, not silent wrong data.
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

# Every rc, not `$?`. With more than one payload a bare `$?` reports only the last, so a failed
# matview refresh would be logged as a success whenever the later refresh happened to succeed —
# precisely the silent-failure mode this job's schedule exists to make noisy.
#
# THE PTM ROLLUP IS NOT A THIRD PAYLOAD HERE, and that is deliberate. It briefly was; `sacct` shows
# this job already TIMEOUTs at its own 4-hour wall in four of its last six runs carrying just the
# one payload above, so a third would usually never start — and when SLURM kills the job mid-payload
# this very echo never executes, making the rc reporting unreachable exactly when it matters. It
# lives in ingest/fran_ptm_refresh.sbatch with its own wall and its own cron entry.
echo "===== done mv_rc=$rc_mv corpus_reach_rc=$rc_reach $(date '+%F %T') ====="
exit $(( rc_mv | rc_reach ))
