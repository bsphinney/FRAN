# Deploying the PTM rollup refresh — manual steps

Nothing in this branch reaches Hive on its own. Until these steps are run by hand, the heatmap's
PTM filters stay unavailable on 2,084 of 2,086 searches and the site shows the amber
"has not been computed for this search" banner whenever one is ticked.

**Nothing here has been executed.** No file was copied, no job submitted, no crontab edited.

Precedent worth heeding: `refresh_corpus_reach.py` was committed on 2026-09-10 with its sbatch
payload and never copied across, so that fix has still never run. Editing a tracked file changes
nothing on the cluster.

## 1. Copy the two files

```bash
scp ingest/refresh_search_ptm.py \
    hive:/quobyte/proteomics-grp/brett/glendon/fran_ingest/refresh_search_ptm.py
scp ingest/fran_ptm_refresh.sbatch \
    hive:/quobyte/proteomics-grp/de-limp/fran_refresh/fran_ptm_refresh.sbatch
ssh hive 'chmod +x /quobyte/proteomics-grp/de-limp/fran_refresh/fran_ptm_refresh.sbatch'
```

The script goes to `fran_ingest/` (beside `coreomics_import.py`, which it imports); the sbatch goes
to `fran_refresh/` (beside the token and the other job). That split is deliberate and matches how
`refresh_corpus_reach.py` is already deployed.

**Also still pending from an earlier branch, not this one:** the deployed
`fran_mv_refresh.sbatch` on Hive is the July 1,095-byte one-payload file, so the corpus_reach
payload committed on 2026-09-10 has never executed. Copying `ingest/fran_mv_refresh.sbatch` across
would fix that, but note it goes to a job that already TIMEOUTs in four of its last six runs — a
separate decision, not part of this deployment.

## 2. Run the backfill once, by hand, and watch it

```bash
ssh hive 'sbatch /quobyte/proteomics-grp/de-limp/fran_refresh/fran_ptm_refresh.sbatch'
ssh hive 'squeue -u brettsp -n fran_ptm_refresh'
ssh hive 'tail -f /quobyte/proteomics-grp/de-limp/fran_refresh/logs/ptm_refresh_<JOBID>.out'
```

Expect roughly 0.7 h and `2079 search(es) to process, 50 per batch`. The log must end with
`===== done search_ptm_rc=0`.

## 3. Verify before scheduling it

```bash
# Run this ON HIVE, like the steps above. It deliberately does NOT import app.db -- that
# module does not exist under fran_ingest/, and importing it is the exact failure this
# deployment fixes.
python3 - <<'PY'
import sys
sys.path.insert(0, "/quobyte/proteomics-grp/brett/glendon/fran_ingest")
from coreomics_import import _conn
with _conn() as cn, cn.cursor() as cur:
    cur.execute("SELECT count(DISTINCT search_id) FROM delimp_search_protein_ptm")
    print("searches with rollup rows:", cur.fetchone()[0])
PY
```

Expect **2,081** -- not 2,086, and not the 2,079 printed at step 2. Three different numbers are
in play and conflating them reads as failure:

| number | what it is |
|---|---|
| **2,079** | searches the run PROCESSES -- the pending set, and the string the script prints |
| **2** | already had rows before the backfill (the two fixture searches) |
| **2,081** | `count(DISTINCT search_id)` AFTERWARDS -- this is the number to expect here |
| **5** | permanently excluded, so 2,081 + 5 = 2,086 rather than 2,086 outright |

Seeing 2,081 means it worked. **Five searches are permanently excluded and that is correct** --
`223106d8, 58918226, 90d20943, e7bf2b7b, f0501ee6` hold no `delimp_precursors` row with a
non-NULL `protein_group`, so nothing can be computed for them. They keep reporting
`ptm_rollup_ready: false`, which is the honest answer and never "no modified proteins".

A second run must print `nothing to do — every search that can produce rows has them`. If it
prints `5 search(es) to process` instead, the PENDING predicate has regressed and the job will
never converge — `tests/test_search_ptm_rollup.py` covers exactly that.

## 4. Only then, add the cron entry

Modelled on the existing FRAN entries: own lock file, own schedule, shared submit log. Staggered an
hour after `fran_mv_refresh` so the two do not contend for the pinned nodes.

```cron
# FRAN PTM rollup: delimp_search_protein_ptm, behind the search heatmap's modification filters.
# Its OWN job, not a payload of fran_mv_refresh — that one already TIMEOUTs at its 4h wall in most
# runs with one payload, so a third would usually never start.
17 5 * * 0 flock -n /tmp/fran_ptm_refresh.lock bash -lc 'sbatch /quobyte/proteomics-grp/de-limp/fran_refresh/fran_ptm_refresh.sbatch' >> /quobyte/proteomics-grp/de-limp/fran_refresh/logs/cron_submit.log 2>&1
```

Add it with `ssh hive 'crontab -e'`. Back up first — `crontab -l > ~/crontab.bak.$(date +%Y%m%d)` —
there is a `crontab_backups/` directory on Hive already following that habit.

`bash -lc` is not optional: without a login shell `sbatch` is not on cron's PATH, which is how a
previous FRAN cron failed silently.
