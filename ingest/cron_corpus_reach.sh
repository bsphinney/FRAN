#!/bin/bash
# cron_corpus_reach.sh -- refresh delimp_protein_corpus_reach, weekly.
#
# WHY THE LOGIN NODE, NOT SLURM. This is a single long DATABASE STATEMENT against PG Farm, not
# compute: refresh_corpus_reach.py issues one INSERT ... ON CONFLICT DO UPDATE inside one
# transaction (measured 278,194 genes in 515 s on 2026-09-09). It reads delimp_proteins and writes
# only delimp_protein_corpus_reach -- no raw files, no CPU-bound work -- so it does not belong in
# SLURM, and compute nodes are not reliably able to reach the public internet anyway (the same
# reasoning cron_coreomics_import.sh applies to its own network sync). A failed run leaves the
# table at its previous consistent state: the whole refresh is one transaction, so there is no
# partial-apply to clean up on retry.
#
# $USER is NOT reliably set under cron. With `set -u` the profile sourcing below dies before the
# log file exists -- totally silently. That is exactly how the Flinders dispatch cron failed for
# months (STAN 2026-06->08 postmortem). Seed LOGNAME/USER and source OUTSIDE set -u.
export LOGNAME="${LOGNAME:-$(id -un)}"
export USER="${USER:-$(id -un)}"
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true
set -uo pipefail

LOG=/quobyte/proteomics-grp/de-limp/fran_refresh/logs/corpus_reach.log
CODE=/quobyte/proteomics-grp/brett/glendon/fran_ingest
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
LOCK=/tmp/fran_corpus_reach.lock

export DELIMP_PG_TOKEN_FILE=/quobyte/proteomics-grp/brett/.pgfarm_token

# SELF-LOCK, in addition to the `flock -n` the crontab line carries -- and the belt-and-braces is
# deliberate, not an oversight. The crontab's flock only guards cron against cron; this one also
# guards against the HAND run, which is how this table got its first 278,194 rows and how it will
# be re-run after any schema change. refresh_corpus_reach.py has no lock of its own, and two
# overlapping runs are not merely wasteful: they take row locks on the same 278 K primary keys in
# whatever order GROUP BY's HashAggregate emits them, which is not stable between runs, so they
# can DEADLOCK -- and the winner holds those locks under SET statement_timeout = '3600s', so the
# loser can block for up to an hour before finding out it lost a 515 s run.
#
# Exit 0, not an error: a refresh skipped because the previous one is still running is the lock
# doing its job. The log line below is the signal, and it is deliberately loud -- silence is what
# the STAN postmortem cited above is about.
exec 200>"$LOCK"
if ! flock -n 200; then
  echo "=== $(date '+%F %T') corpus_reach refresh SKIPPED — another run holds $LOCK" >> "$LOG"
  exit 0
fi

echo "=== $(date '+%F %T') corpus_reach refresh starting" >> "$LOG"
"$PY" -u "$CODE/refresh_corpus_reach.py" >> "$LOG" 2>&1
rc=$?
echo "=== $(date '+%F %T') corpus_reach refresh rc=$rc" >> "$LOG"
exit $rc
