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

`auto_ingest.py` imports the other three **at start-up**, deliberately: a copy that forgets them
dies in its first second with `ModuleNotFoundError` rather than hours in. Forgetting absent files is
exactly how every ingest died on 2026-09-23 (a sync copied the 15 files that *differed* and missed the
5 that were *missing*), so the check below reports MISSING separately from DIFFERS. (A failure that
early cannot write state or alert; the check and the smoke test are what stand in front of it.)

## Steps

```bash
# from the repo root, on the laptop, at the merged commit
bash ingest/deploy_auto_ingest_check.sh          # print-only; changes nothing
```

It md5-compares the four shipped files **and the whole import closure of `auto_ingest.py` and of
every script it runs** (`corpus_ingest.py` and whatever it imports, lazily or not) against Hive,
then prints the backup, `scp` and smoke-test commands to run by hand. Run it again after copying:
every row it ships must read `same`, and nothing may read MISSING. It never copies anything else: on
2026-09-24 `raw_metadata.py` already differed on Hive, and that belongs to other work in flight.

Checked on 2026-09-24 (read-only): Hive's `auto_ingest.py` and `find_uningested.py` are
byte-identical to `origin/main`, so copying over them loses no local edit; the two new modules are
MISSING, as expected; nothing else in the closure is missing.
`ingest/audit_deploy_sync.py` (not in the repo yet) is the whole-directory version of this check.

After merge, re-run `ingest/publish_manifest.py` from the repo as usual. `auto_ingest.py` is gated
`warn`, not `refuse`, so until then each ingest only prints `STALE auto_ingest.py`.

### With Brett's OK only — two drop-box entries

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
| **`mkdir` lock (shipped)** | hive-as-11-4-42 + hive-dc-7-5-50 | **0 / 800** | no torn reads, no warnings |
| none (control) | same pair | 580 / 800 | torn reads persisted through 6 retries |

So `flock` does not exclude across nodes on Quobyte, and the shipped lock does. Only one ingest job
runs at a time anyway (`cron_auto_ingest.sh`); this matters when that guard fails, or when someone
runs `--clear` on the login node during a run.

## Behaviour changes worth knowing

* **Every ingest is re-checked against the corpus first** (`fran_queue._already_ingested`) — scan,
  drop-box, queue and `--direct` alike — because `corpus_ingest` deletes and re-inserts an existing
  output_dir. So a `fran_queue.py add --force` registration of an already-ingested output_dir is now
  marked done, not re-ingested; `--direct` opens its own connection for the check; and with no
  connection to check with, the run stops rather than ingest blind.
* **Systemic failures are not charged.** Stale code, `usage:`/`error: unrecognized arguments` from
  `corpus_ingest` (auto_ingest and corpus_ingest out of step), an unreachable database or a failed
  token exchange stop the run. An `ImportError` holds back only that engine's candidates; the
  others carry on, and Slack hears if the block lasts 3 runs. A queue row hit by any of these is
  left claimed to lapse back to `queued` (no attempt burned). A candidate deferred 3 times while
  others succeeded is charged normally after that.
* **Alerts** (one Slack message per run at most, each condition once per episode, again after 24 h):
  3 consecutive runs with work and no progress (a crash, a failed scan and a run killed at the SLURM
  wall all count); an engine blocked 3 runs running; a drop-box entry whose manifest contradicts its
  own search. An empty queue, QC exclusions and malformed-manifest skips are silent.

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

```bash
ssh hive 'cd /quobyte/proteomics-grp/brett/glendon/fran_ingest &&
          cp -p auto_ingest.py.bak.<date> auto_ingest.py &&
          cp -p find_uningested.py.bak.<date> find_uningested.py'
```

The two new modules are inert without the new `auto_ingest.py`; the state file can stay.
