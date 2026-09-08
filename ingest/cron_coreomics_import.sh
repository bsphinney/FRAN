#!/bin/bash
# cron_coreomics_import.sh -- refresh the CoreOmics cache, daily.
#
# WHY DAILY, AND WHY ON THE LOGIN NODE. This is a network sync, not compute: ~23 list requests
# plus one detail request per new/changed submission (149 on the first run, typically a handful
# after). It reads no raw files and writes a few thousand small rows, so it does not belong in
# SLURM -- and compute nodes are not reliably able to reach the public internet anyway, which is
# the same class of problem that made fran_auto_ingest.sbatch preflight PG Farm reachability.
# Submissions arrive one or two a day, so daily keeps the cache at most a day behind. The June
# import sat 83 days stale; anything on that scale is what this exists to prevent.
#
# $USER is NOT reliably set under cron. With `set -u` the profile sourcing below dies before the
# log file exists -- totally silently. That is exactly how the Flinders dispatch cron failed for
# months (STAN 2026-06->08 postmortem). Seed LOGNAME/USER and source OUTSIDE set -u.
export LOGNAME="${LOGNAME:-$(id -un)}"
export USER="${USER:-$(id -un)}"
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true
set -uo pipefail

LOG=/quobyte/proteomics-grp/de-limp/fran_refresh/logs/coreomics_import.log
CODE=/quobyte/proteomics-grp/brett/glendon/fran_ingest
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python

export FRAN_COREOMICS_TOKEN_FILE=/quobyte/proteomics-grp/brett/.coreomics_token
export DELIMP_PG_TOKEN_FILE=/quobyte/proteomics-grp/brett/.pgfarm_token

echo "=== $(date '+%F %T') coreomics import starting" >> "$LOG"
"$PY" -u "$CODE/coreomics_import.py" --apply >> "$LOG" 2>&1
rc=$?
echo "=== $(date '+%F %T') coreomics import rc=$rc" >> "$LOG"

# A refresh that stops working must be visible in the DATA, not only in a log nobody opens: the
# whole point is that the last failure mode was invisible. Report the cache's high-water mark so
# `tail` of this log answers "is it current?" without a query.
"$PY" - <<'PY' >> "$LOG" 2>&1
import os, json, urllib.request, psycopg2
def tok():
    pw = open(os.path.expanduser(os.environ["DELIMP_PG_TOKEN_FILE"])).read().strip()
    if pw.startswith("eyJ") and pw.count(".") == 2: return pw
    body = json.dumps({"username": "genome-proteomics-service-account", "secret": pw}).encode()
    r = urllib.request.Request("https://pgfarm.library.ucdavis.edu/auth/service-account/login",
                               data=body, headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=30).read())["access_token"]
c = psycopg2.connect(host="pgfarm.library.ucdavis.edu", port=5432,
                     dbname="uc-davis-genome-center-proteomics-core/delimp",
                     user="genome-proteomics-service-account", password=tok(),
                     sslmode="require", connect_timeout=30,
                     options="-c default_transaction_read_only=on")
cur = c.cursor()
cur.execute("select count(*), max(internal_id), max(imported_at) from coreomics_submissions_cache")
n, mx, at = cur.fetchone()
print(f"    cache: {n} submissions, max {mx}, imported_at {str(at)[:19]}")
c.close()
PY
exit $rc
