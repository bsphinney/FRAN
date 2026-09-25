# Deploying the auto-ingest starvation fix to Hive

The cron (`23 */4 * * *` → `cron_auto_ingest.sh` → `fran_auto_ingest.sbatch`) runs
`/quobyte/proteomics-grp/brett/glendon/fran_ingest/auto_ingest.py` from a **loose scp copy**, not a
checkout (`HIVE_SYNC.md`). Nothing about the cron, the sbatch or the wrapper changes.

## What ships — all four together

| file | |
|---|---|
| `auto_ingest.py` | changed: ordering, drop-box identity, attempt memory, pre-ingest corpus re-check + output_dir leases, systemic handling, alerts, crash accounting |
| `auto_ingest_state.py` | **new** — attempt memory, leases, stuck/engine/needs-a-person detector, mkdir lock |
| `auto_ingest_alert.py` | **new** — the Slack post |
| `find_uningested.py` | changed: the drop-box contract (`DROPBOX_ROOT`, `read_manifest`, `qc_reason` / `QC_NAME_RE`); `scan()` skips a staged entry whose manifest `output_dir` is already a corpus path, and never enters `incoming/.excluded/` |

`auto_ingest.py` imports `auto_ingest_state`, `auto_ingest_alert`, `find_uningested` **and
`fran_queue`** at **module top level**, deliberately: a copy that forgets one dies in its first
second rather than hours in, and fran-db's deploy audit sees all four in its import closure, so a
forgotten copy reads `RESULT: NOT SAFE TO INGEST` before anything runs. Forgetting absent files is
exactly how every ingest died on 2026-09-23 (a sync copied the 15 files that *differed* and missed
the 5 that were *missing*). A failure that early cannot write state or alert; the audit is what
stands in front of it. (The functions also `import fran_queue` locally: that reads `sys.modules`,
which is where the tests put their stand-in for its network edge.)

## Steps

**One writer to `fran_ingest` at a time. Coordinate with fran-db before touching it.**
`fran_ingest` is fran-db's deployment directory; whoever copies into it also publishes the manifest.
On 2026-09-25 two agents prepared this same deploy minutes apart, and one copied while the other was
mid-check. Agree who deploys before step 3.

The deploy check is fran-db's `ingest/audit_deploy_sync.py` (on fran-db's branch
`fix/tdf-immutable-opens`, not on `main` yet; already deployed on Hive). One definition: this change
ships no checker of its own. Its verdict is the last line: `RESULT: fully in sync`,
`RESULT: import closure is intact; N non-closure file(s) out of sync` (both SAFE), or
`RESULT: NOT SAFE TO INGEST …`. **A SAFE result is required before AND after the copy.**

**Emit the manifest from the tree Hive ACTUALLY RUNS, plus the change -- not from `main` alone.**
Hive can run a branch ahead of `main`. On 2026-09-25 it ran fran-db's `fix/tdf-immutable-opens`,
including a newer `corpus_ingest.py` (md5 d976cf6: the six-column DIA-NN reader, its migration
already live) that `main` did not have (`main`: cfaa2b3). Checked against plain `main`, step 2 below
reported NOT SAFE with five files that were not this change's. Deploying or publishing from `main`
alone would then have made the ingest gate refuse every ingest, because `corpus_ingest.py` is
refuse-gated.

```bash
# 1. BEFORE: find the baseline -- the commit Hive runs today -- and prove it. Try main, and any branch
#    fran-db has deployed from. The baseline is the one whose manifest checks SAFE ("fully in sync")
#    against Hive before anything is copied. (On 2026-09-25: fran-db's b9a9d0b.)
git archive <baseline> | tar -x -C /tmp/base
python3 /tmp/base/ingest/audit_deploy_sync.py --emit /tmp/ingest_manifest_before.json
scp /tmp/ingest_manifest_before.json brettsp@hive.hpc.ucdavis.edu:/tmp/
#    on Hive -- must be SAFE:
cd /quobyte/proteomics-grp/brett/glendon/fran_ingest
python3 audit_deploy_sync.py --check /tmp/ingest_manifest_before.json \
    --target /quobyte/proteomics-grp/brett/glendon/fran_ingest

# 2. The deploy tree = the baseline WITH the change merged in (on 2026-09-25: fran-db merged main,
#    carrying this change, into its branch as 3eae82e). Emit its manifest. Checked before copying it
#    is NOT SAFE and must name exactly this change's files -- here auto_ingest_state.py and
#    auto_ingest_alert.py MISSING, auto_ingest.py and find_uningested.py STALE. Anything else: stop.
git archive <union> | tar -x -C /tmp/union
python3 /tmp/union/ingest/audit_deploy_sync.py --emit /tmp/ingest_manifest_after.json
scp /tmp/ingest_manifest_after.json brettsp@hive.hpc.ucdavis.edu:/tmp/

# 3. Keep the running copies OUTSIDE fran_ingest (files there that are not in the repo clutter the
#    audit's "extra on target"), then copy the change's files TOGETHER from the union tree:
mkdir -p ~/fran_rollback/<date> && cp -p /quobyte/proteomics-grp/brett/glendon/fran_ingest/{auto_ingest,find_uningested}.py ~/fran_rollback/<date>/
scp /tmp/union/ingest/{auto_ingest,auto_ingest_state,auto_ingest_alert,find_uningested}.py \
    brettsp@hive.hpc.ucdavis.edu:/quobyte/proteomics-grp/brett/glendon/fran_ingest/

# 4. AFTER: on Hive, against the union manifest -- must be SAFE:
python3 audit_deploy_sync.py --check /tmp/ingest_manifest_after.json \
    --target /quobyte/proteomics-grp/brett/glendon/fran_ingest

# 5. Smoke test on Hive -- imports and the memory CLI only: no scan, no database, no ingest, and no
#    bytecode written into fran_ingest:
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
PYTHONDONTWRITEBYTECODE=1 $PY -c "import auto_ingest, auto_ingest_state, auto_ingest_alert, find_uningested; print('imports ok')"
PYTHONDONTWRITEBYTECODE=1 $PY auto_ingest.py --list-quarantine

# 6. Publish the manifest from the UNION tree (the deployer does this -- fran-db on 2026-09-25), so
#    delimp_ingest_manifest carries the two NEW modules and every md5 Hive now runs. Never from main
#    alone while Hive runs a branch ahead of it.
python3 /tmp/union/ingest/publish_manifest.py
```

Until step 6, each ingest prints `STALE auto_ingest.py` / `STALE find_uningested.py`: both are gated
`warn`, not `refuse`, so nothing stops. The audit also reports files that exist on Hive but not in
the repo ("extra on target": 79 on 2026-09-25). Those are for a human to look at; do not sweep them
as part of a deploy.

### Deployed 2026-09-25

fran-db synced Hive from its union tree `3eae82e` (`main` with this change, merged into
`fix/tdf-immutable-opens`) at 09:52 and published the manifest at `3eae82e` (94 files; gate 94/94).
Independent read-only verification afterwards:

* `audit_deploy_sync.py --check` against the `3eae82e` manifest: `RESULT: fully in sync` -- 94
  present, 0 missing, 0 differing, 79 extra.
* smoke test on `python3` 3.10 and on the production interpreter (alphadia2, 3.11): imports ok,
  `--list-quarantine` empty; nothing written to `__pycache__`.
* a DB-free dry run of the deployed selection over `incoming/` (known corpus paths given, no ingest):
  Gallegos x2 skipped as already in the corpus; `search__9ff203cf` skipped (`qc: manifest says qc:
  true`); then `PROT_0793 mouse_mousecont` (its manifest repaired to the mouse FASTA DIA-NN ran),
  Dupanloup dog CSF, `Silva_LRS_JPH_Kv21_RyR` (msalemi), and the two Siegel `DIA-NN_2.6.0` entries.

**Rollback copies** of the two files this deploy replaced -- the versions Hive ran until 09:52 --
are kept outside `fran_ingest`, read-only, at
`~brettsp/fran_nan_diag_20260924/rollback/auto_ingest.py.pre0359978` (md5 a43fe64c) and
`…/find_uningested.py.pre0359978` (md5 304afaef). Both equal git `e8e2f0c` (and fran-db's
`b9a9d0b`) and were verified by md5 on Hive. The deploy itself made no `.bak` files in `fran_ingest`.

**The audit's subprocess gap is closed.** fran-db's `8040bb6` ("the deploy audit missed every file
reached by subprocess") is in `3eae82e`: its closure (30 files, 7 entry points) now includes
`diann_xic_to_lance.py` and `ingest_perrun_xic.py`, which `auto_ingest.py` reaches only by running
them. It will be on `main` once `fix/tdf-immutable-opens` merges.

### With Brett's OK only — two drop-box entries

**Status 2026-09-25: both resolved without these steps.** `search__9ff203cf` was NOT moved: its manifest
now says `qc: true` / `exclude: true` (set through fran_deposit), so it is excluded by the flag. The
`search_mouse_mousecont` manifest was repaired to `mouse_UP000000589_mousecont.fasta`, which matches the
search's own DIA-NN log, so it is eligible. Kept below as the record of what was proposed.

**1. Set gabrig's QC run aside** (reversible; the ingester never moves or deletes anything itself):

```bash
cd /quobyte/proteomics-grp/fran/incoming
mkdir -p .excluded && mv search__9ff203cf .excluded/        # undo: mv .excluded/search__9ff203cf .
```

Without this it is still never ingested — `qc_reason` excludes it by name every run and logs
`skipped (qc: search_name 'chkLUppm_HeLa50_2026 Lumos QC' matches QC_NAME_RE)` — but moved, it stops
being looked at. `scan()` never enters `incoming/.excluded/`.

**2. Repair `search_mouse_mousecont__9ad24935`'s manifest** (fran-backfill has the repaired file).
Its manifest names `human_UP000005640.fasta`; DIA-NN's own log for that search ran
`mouse_UP000000589_mousecont.fasta`. Until it is repaired the ingester skips it every run —
`skipped (manifest_fasta_mismatch: manifest=human_UP000005640.fasta search=mouse_UP000000589_mousecont.fasta)`
— without charging it, and Slack is told once (then once a day while it persists) that it needs a
person. Once repaired it is ingested on the next run.

### Every current `incoming/` entry (2026-09-24 14:00, for Brett to eyeball)

| entry | search_name | staged_by | organism | manifest FASTA | what the new code does |
|---|---|---|---|---|---|
| `GallPlasCer__5b11a0d9` | diann261_gallegos_plasma_Ceres_24file_matched | brettsp | Homo sapiens | — | skipped by the scan: already in the corpus (queue Q6, 09-08) |
| `GallPlasStrap__74bf2f97` | diann261_gallegos_plasma_STrap_24file_matched | brettsp | Homo sapiens | — | skipped by the scan: already in the corpus (queue Q7, 09-08) |
| `search__59698462` | Silva_LRS_JPH_Kv21_RyR | msalemi | Mus musculus | — | ingested (staged 09-24 13:16) |
| `search__9ff203cf` | chkLUppm_HeLa50_2026 Lumos QC | gabrig | Homo sapiens | — | excluded as QC (step 1 above) |
| `search_mouse_mousecont__9ad24935` | PROT_0793 mouse liver — mouse_tissue contaminants (re-search) | brettsp | Mus musculus | human_UP000005640.fasta | skipped: manifest FASTA contradicts the search (step 2) |
| `search_out__e14aac29` | Dupanloup Jin dog CSF SRMA vs bacterial meningitis (DIA-NN 2.7.0) | brettsp | Canis lupus familiaris | dog_UP000805418_opg_plus_universal_contam.fasta | ingested first (staged 09-21); its FASTA matches the search's |

None carries `staged_at`, `qc` or `exclude` yet, so staging order is each manifest's mtime. Verified
with a read-only sbatch probe of the final code over the real drop box (job 23991857, no database,
the Gallegos output_dirs given as known corpus paths).

## Nothing else to set up

* **State**: `/quobyte/proteomics-grp/de-limp/fran_refresh/state/auto_ingest_attempts.json`
  (+ a `.lockd` lock directory while a write is in progress), created on the first `--apply` run.
  `fran_refresh/` is group-writable. Override with `--state-file` or `FRAN_AUTO_INGEST_STATE`. A dry
  run never writes it.
* **Slack**: the webhook is read at run time from
  `/quobyte/proteomics-grp/.config/skill_slack_webhook` (brettsp can read it; mode 0640). Override
  with `FRAN_SLACK_WEBHOOK_FILE`. It is never printed; a non-`hooks.slack.com` value is refused.

## The lock, measured on Quobyte (2026-09-24)

Two SLURM jobs on different nodes, each writing 400 distinct keys to one state file on `/quobyte`
after meeting at a barrier, with a no-lock control each time (logs kept in
`~brettsp/fran_nan_diag_20260924/lock{A,B}_*.out`):

| lock | nodes | lost updates | notes |
|---|---|---|---|
| `flock()` (first design) | hive-as-11-2-48 + hive-as-11-4-42 | **578 / 800** | no better than the control (567); readers saw torn JSON |
| `mkdir` lock | hive-as-11-4-42 + hive-dc-7-5-50 | **0 / 800** | no torn reads, no warnings |
| none (control) | same pair | 580 / 800 | torn reads persisted through 6 retries |
| **`mkdir` lock + owner token (shipped)** | hive-as-11-3-51 + hive-dc-7-7-26 | **0 / 800** | no warnings, no stray lock dirs; control lost 657 |

So `flock` does not exclude across nodes on Quobyte, and the shipped lock does. Each lock carries a
unique owner token: a stale lock is broken by judge → rename → verify-the-owner (restored if it
changed hands in between), and a run only ever removes a lock that is still its own. An OWNER-LESS
stale lock (its holder died before writing the owner file) is broken only if, on a second look 1 s
later, it is still owner-less and still stale -- otherwise it may be a fresh lock whose owner is
about to be written. Only one ingest job
runs at a time anyway (`cron_auto_ingest.sh`); this matters when that guard fails, or when someone
runs `--clear` on the login node during a run.

## Behaviour changes worth knowing

* **Every ingest is re-checked against the corpus first** (`fran_queue._already_ingested`) — scan,
  drop-box, queue and `--direct` alike — because `corpus_ingest` deletes and re-inserts an existing
  output_dir. So a `fran_queue.py add --force` registration of an already-ingested output_dir is now
  marked done, not re-ingested; `--direct` opens its own connection for the check, and so does a
  run whose queue claim failed (the scan half carries on, as `_claim_queue` promises); only with no
  connection at all does the run stop rather than ingest blind.
* **Systemic failures are not charged.** Stale code, argparse's `error: unrecognized arguments` /
  `error: the following arguments are required` from `corpus_ingest` (the two files out of step),
  an unreachable database or a failed token exchange stop the run; any other argparse error (a bad
  value in the candidate's data) is charged normally. An `ImportError` holds back only that
  engine's candidates; the others carry on, and Slack hears if the block lasts 3 runs. A queue row
  hit by any of these is left claimed to lapse back to `queued` (no attempt burned). A candidate
  deferred 3 times while others succeeded is charged normally after that -- for an import failure,
  only if its OWN engine succeeded meanwhile.
* **Alerts** (one Slack message per run at most, each condition once per episode, again after 24 h):
  3 consecutive runs with work and no progress (a crash, a failed scan and a run killed at the SLURM
  wall all count); an engine blocked 3 runs running (only a REAL ingest of that engine ends its
  block -- resolving a duplicate does not); a drop-box entry whose manifest contradicts its own
  search. An empty queue, QC exclusions and malformed-manifest skips are silent.

If the ingest manifest gate refuses (`REFUSING TO INGEST … stale`), the run stops after the first
candidate, charges nobody, and after 3 such runs posts one Slack message. **Do not** add
`--ignore-stale-ingest`; fix the sync.

## Operating it

```bash
cd /quobyte/proteomics-grp/brett/glendon/fran_ingest
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
$PY auto_ingest.py --list-quarantine            # no scan, no database
$PY auto_ingest.py --clear 'Nuciser'            # exact key or a UNIQUE part of one
```

The run's last line now reads
`===== done: 0 ingested, 3 duplicate-skipped, 2 failed, 181 still queued, 0 quarantined, 2 backed-off — …`
(the original prefix is unchanged; nothing in the repo or on Hive parses it).

It also skips as unusable, instead of spending a run on each: 4 truncated TSV exports (Nuciser,
Plasma_liver2, MouseBiology_Brain-rerun, Cameron80), 5 parquet exports with no PAR1 footer, and 4
searches whose only non-empty export is header-only (including `20220330_153755_Chicken_DIA…` and
`MuckeRat-PhosAcety`, both in the live candidate list). All 9 truncated files also have a 0-byte
`.params`, the export's own record of not finishing (read-only probe, job 23991422).

## Rollback

Coordinate with fran-db first (one writer to `fran_ingest`). Restore the two files from the copies
kept outside `fran_ingest` -- `install -m 644`, not `cp -p`, because the copies are read-only and a
read-only file in `fran_ingest` would make the next `scp` fail:

```bash
ssh brettsp@hive.hpc.ucdavis.edu 'cd /quobyte/proteomics-grp/brett/glendon/fran_ingest &&
    R=~/fran_nan_diag_20260924/rollback &&
    install -m 644 $R/auto_ingest.py.pre0359978 auto_ingest.py &&
    install -m 644 $R/find_uningested.py.pre0359978 find_uningested.py &&
    md5sum auto_ingest.py find_uningested.py'      # expect a43fe64c... / 304afaef...
```

Then re-run the audit against the manifest of the tree Hive now runs, and have the manifest
re-published from that tree. The two new modules are inert without the new `auto_ingest.py`; the
state file can stay.
