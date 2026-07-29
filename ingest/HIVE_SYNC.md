# Keeping Hive and this repo in sync

**The problem this exists to stop:** Hive runs FRAN's pipeline from
`/quobyte/proteomics-grp/brett/glendon/fran_ingest`, which is a **loose copy** of `ingest/` — not a
checkout. Files get there by `scp`. Nothing enforces that what runs is what is committed, and nothing
records which revision produced a given artefact.

That is not hypothetical. As of 2026-07-29, a file-by-file audit found:

| | count | |
|---|---|---|
| identical to git | 27 | fine |
| **differ from git** | 2 | `corpus_ingest.py` (an uncommitted local edit), `xic_extractor.py` (docstring-only; code byte-identical) |
| **exist only on Hive** | 10 | including `upload_spectra_to_blob.sbatch`, the only copy of a real SLURM job, and `diag.py`, the script that found the `content_md5` chunking bug |
| exist only in the repo | 4 | never pushed to Hive |

The 10 Hive-only files have now been committed. The two builders the design docs plan around
(`fran_lance_fragments.py`, `build_xic_trace_lance.py`) had also never been in version control — and
the 6-hour job that timed out and "wrote nothing" was one of them. **Checkpointing cannot be added to
code that is not in the repo.**

There is a second, subtler trap. `fran_ingest` sits *inside* the unrelated `glendon` git repo, so a
bare `git rev-parse` there returns glendon's HEAD — a real sha, from the wrong project, and
indistinguishable from a FRAN one once it is in a database column. `versions.git_sha()` therefore
qualifies it: `glendon@7961e27` vs `FRAN@0d55345`.

## The audit, on demand

```bash
# from the repo root
ssh hive 'cd /quobyte/proteomics-grp/brett/glendon/fran_ingest && md5sum *.py *.sbatch *.sh' \
  | sort -k2 > /tmp/hive.txt
(cd ingest && md5 -r *.py *.sbatch *.sh) | sort -k2 > /tmp/repo.txt
# compare the two; anything differing is code running that nobody can review
```

## Pushing a change

Until the target state below is done, after editing anything in `ingest/`:

```bash
scp ingest/<file>.py hive:/quobyte/proteomics-grp/brett/glendon/fran_ingest/
```

Push **every** module a change touches. A partial push is how you get a Hive tree that matches no
commit — `versions.py` and the lane modules import each other, so pushing one and not the other
produces an `ImportError` on a compute node an hour into a job.

## Target state: make it a checkout

The durable fix is to replace the loose copy with a real clone, so "what is running" and "what is
committed" are the same question:

```bash
ssh hive
cd /quobyte/proteomics-grp/brett/glendon
mv fran_ingest fran_ingest.loose.bak          # keep until the clone is verified
git clone <FRAN remote> fran_ingest
# then: git -C fran_ingest pull, instead of scp, forever after
```

Two things to check before deleting the backup, because the loose copy accumulated state that a
clone will not have:

1. **Untracked run artefacts.** `fran_ingest` holds ~20 `*.log` files from real SLURM jobs, a
   `backfill_plan/` directory and `versions_todo.csv`. These are outputs, not source — keep them out
   of git, but do not lose them by moving the directory away.
2. **`git_sha()` will start reporting `FRAN@<sha>`** instead of `glendon@<sha>` in
   `delimp_component_version`. That change in the log is the signal the migration worked.

Note the sbatch scripts hardcode `ING=/quobyte/proteomics-grp/brett/glendon/fran_ingest`, so cloning
to that exact path means nothing else needs editing.

## What deliberately stays outside this repo

`build_xic_shard.py` lives in `glendon/` and is imported by eight engine-side scripts. Copying it
into FRAN would create exactly the second-copy drift this document is about. `build_xic_trace_lance.py`
depends on it and says so. Reconciling it with `ingest/xic_extractor.py` is Phase 3 work — the two
currently disagree about what a trace is ([9,32] vs [11,32], IM_TOL 0.05 vs 0.030).
