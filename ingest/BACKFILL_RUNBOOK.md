# Runbook — backfill the long-tail Spectronaut searches into the spectrum lane

Copy-paste steps to recover the **544 completed Spectronaut searches (~82.9M precursors)** that
still have no linked Lance dataset (see [`INGEST_STATUS.md`](INGEST_STATUS.md) for the live count).
**All of this runs on Hive** — the planner needs the Flinders/Windows report filesystem, which a
laptop can't see. The backfill itself must run on a **compute node** (parallel Arrow/Lance workers
OOM-kill the login node).

## Fixed paths / env (from `backfill_spectra.sbatch`)
```bash
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python          # alphadia2 env (has lance, pyarrow, psycopg2)
ING=/quobyte/proteomics-grp/brett/glendon/fran_ingest              # where the ingest scripts live on Hive
LANCE_DIR=/quobyte/proteomics-grp/brett/glendon/spectra_lance      # one <search>.lance per search (137 GB so far)
REPORTS_ROOT=/nfs/lssc0/flinders/proteomics/Data/FRAN_reports      # archived Spectronaut reports on Flinders
export DELIMP_PG_TOKEN_FILE=/quobyte/proteomics-grp/brett/.pgfarm_token
cd "$ING"
```

> **First sync the scripts.** `$ING` is a copy of this repo's `ingest/`. Before Step 3/4, pull the
> latest so the **precursor-count tiebreak** added on 2026-07-20 is present:
> `cd "$ING" && git pull`  (or re-copy `link_spectrum_lane.py`, `backfill_fragments.py`,
> `diag_unlinked.py`). Otherwise the linker will leave the 75 count-tiebreak rows unlinked.

---

## Step 1 — coverage plan: which of the long tail have an archived report?
Splits the not-yet-backfilled searches into "report is on disk → backfill now (cheap)" vs "report
missing → must re-export from the `.sne` on Windows".
```bash
"$PY" plan_spectrum_backfill.py \
  --reports-root /nfs/lssc0/flinders/proteomics/Data/FRAN_reports \
  --reports-root /nfs/lssc0/flinders/proteomics/Data/FRAN_SNE_export \
  --out ./backfill_plan
# reads:  backfill_plan/backfill_worklist.txt  (reports ready to ingest)
#         backfill_plan/regen_queue.txt        (searches whose report is still missing)
```
The `--enqueue` flag (writes the missing ones into `delimp_spectrum_regen_queue`) is **optional and
a DB write** — only add it if a Windows ingestor will actually poll that queue. Note the regen queue
is otherwise treated as stale; don't rely on its old contents.

## Step 2 — (only if Step 1 shows missing reports) pull them off Windows
The report usually already exists on `C:\fran_sne_export\` and was just never copied. **Run on a
Windows ingestor** (has both `C:\fran_sne_export\` and the Flinders share):
```bat
python pull_reports_to_hive.py ^
  --src "C:\fran_sne_export" --src "B:\Automatic_SNE_storage" ^
  --dest "\\flinders\proteomics\Data\FRAN_reports" ^
  --update-queue
```
Idempotent (skips files already on Hive with matching size). Only reports with *no* copy anywhere
need a true Spectronaut re-export (`manageSNE -sne <sne> -n <name> -o <out> -rs FRAN.rs`).

## Step 3 — backfill the reports into the Lance lane (compute node)
Resume is the default: it **skips every search that already has a Lance dataset**, so this only
processes the long tail — you do NOT reprocess the 1,539 already done.
```bash
sbatch backfill_spectra.sbatch \
  /nfs/lssc0/flinders/proteomics/Data/FRAN_reports \
  /quobyte/proteomics-grp/brett/glendon/spectra_lance
# tail the log:
#   tail -f spectra_backfill_<jobid>.log
```
The sbatch runs: `backfill_fragments.py --scan <reports> --out-dir <lance> --register --workers 5`
(5 workers / 120G / 16h — sized after the OOM fight; corrupt/0-byte reports are logged + skipped).
To reprocess everything from scratch instead, add `--no-resume` (rarely needed).

## Step 4 — link + verify (login node is fine)
```bash
"$PY" link_spectrum_lane.py                 # fill search_id on the newly-ingested datasets
"$PY" verify_spectrum_lane.py --missing-only # confirm every registered dataset's md5 still matches
"$PY" diag_unlinked.py                       # re-check what (if anything) is still unlinked / long tail
```

## Done when
`diag_unlinked.py` reports the long-tail "no linked lane dataset" count near zero (only genuinely
missing/corrupt reports remain), and `verify_spectrum_lane.py --missing-only` prints no problems.
Refresh the headline numbers in `INGEST_STATUS.md` with the query at the bottom of that file.

## What's left that this runbook can't fix
- **64 ambiguous-duplicate** unlinked datasets (resubmits with identical precursor counts) — need a
  `resubmit_of_search_id`/`parent_chain_depth` canonical-pick, not a report backfill. Low value.
- **51 no-name-match** unlinked datasets — need a manual/provenance name reconciliation.
