# The ingest version gate — nodes refuse to run a stale ingestor

**Date:** 2026-09-16
**Status:** design, approved in chat (spec, then implement). Decision taken: **refuse, not warn.**

## The problem, stated from evidence

Brett: *"I want there to be a mechanism where the winclaudes will automatically be aware of new
versions of the ingestor so they never use an old one."*

Today they cannot be. Measured on 2026-09-16:

- The Windows nodes run scripts from the share (`R:\Data\FRAN_SNE_export`, =
  `/nfs/lssc0/flinders/proteomics/Data/FRAN_SNE_export`). That copy was last synced **8 September**.
- Five files diverge from the repo. Three of them — `corpus_ingest.py`,
  `spectronaut_to_corpus.py`, `versions.py` — are on the critical path for DIA-NN and Spectronaut
  ingestion.
- The share's `spectronaut_to_corpus.py` contains **none** of the PTM work:
  `ptm_localization`, `GlyGly`, `site_localization_probability`, `_parse_localization_min` all
  return 0 matches. Ingesting today silently produces rows that need re-ingesting later.

**And the existing version constant does not detect any of this.** Both copies declare
`CORPUS_INGEST_VERSION = "1.3.0"` while the files differ, because `spectronaut_to_corpus.py` changed
and no constant tracks it — there is no adapter version at all. `versions.py`'s own docstring says:

> *"A version that lags the code is worse than no version, because it is trusted."*

That has already happened. **So the gate must be content-addressed, not constant-addressed.** A
mechanism that depends on remembering to bump a number has the same failure mode as the scp
discipline it is meant to replace, and this project has now watched both lapse.

## Why the obvious designs do not work

| design | why it fails |
|---|---|
| Bump a version constant, compare | Already failed. Requires the discipline that lapsed. |
| Manifest file shipped on the share | Circular: an un-synced node holds an old manifest that matches its old files. Nothing notices. |
| Compare against git | The nodes have no checkout. The share is a loose scp copy — established 2026-08-20. |
| Warn on mismatch | Rejected by Brett, and correctly: a warning in a long ingest log is not read, and the cost of proceeding is corrupt rows discovered weeks later. |

**The one thing both sides can see is PG Farm** — the same database `claude_bus.py` already uses for
`delimp_claude_heartbeat` and `delimp_claude_messages`, and where `delimp_component_version` already
lives. Putting the expected state there breaks the circularity: **a node cannot hold a stale
manifest, because it holds no manifest at all.**

## Design

### 1. `delimp_ingest_manifest` — the expected state, in the comms DB

```sql
CREATE TABLE IF NOT EXISTS delimp_ingest_manifest (
    "file"          text PRIMARY KEY,   -- e.g. 'spectronaut_to_corpus.py'
    "md5"           text NOT NULL,
    "git_sha"       text NOT NULL,
    "published_at"  timestamptz NOT NULL DEFAULT now(),
    "published_by"  text NOT NULL,      -- host that published
    "gate"          text NOT NULL       -- 'refuse' | 'warn'
);
```

`gate` is per-file, because the cost of staleness is not uniform:

- **`refuse`** — `corpus_ingest.py`, `spectronaut_to_corpus.py`, `versions.py`, `diann_to_corpus.py`
  (any adapter that writes corpus rows). A stale one silently produces data needing re-ingest.
- **`warn`** — everything else in `ingest/`. Stale, but not corpus-corrupting.

Default for a file absent from the table is **warn**, not refuse: a new script nobody has published
yet must not brick ingestion on a remote node.

### 2. `ingest/publish_manifest.py` — run from the repo

Hashes every `ingest/*.py` at HEAD, writes the manifest, records the git sha. One command, run
where the developer already is, rather than an scp to a share that may not be mounted.

Refuses to publish from a dirty working tree — publishing a hash for uncommitted code records an
expectation nobody else can reach.

### 3. The gate itself — `versions.py:assert_current()`

Called at the top of `corpus_ingest.main()`, before any work:

1. Hash the ingest files actually on disk beside the running script.
2. Read the manifest from PG Farm.
3. For each mismatch: report the file, both hashes short-form, the manifest's `git_sha` and
   `published_at`, and the exact `scp` that fixes it.
4. If any mismatching file is `gate='refuse'` → **exit non-zero before ingesting anything.**

Escape hatch: `--ignore-stale-ingest`, which refuses silently-skipping and instead prints what is
stale and records `notes='ran stale: <files>'` in `delimp_component_version`. An override that
leaves no trace is how "temporarily" becomes permanent.

**If the manifest cannot be read, the gate FAILS OPEN with a warning, not closed.** A PG Farm
outage must not stop ingestion — the gate exists to prevent silent corruption, not to add a
dependency whose failure is worse than the problem.

### 4. Heartbeat integration — this is what makes it *automatic*

`claude_bus.py checkin` gains the node's ingest fingerprint, so `claude_bus.py who` shows staleness
**without anyone running an ingest**. A Windows Claude comes online, checks in, and immediately sees
it is behind — which is precisely what Brett asked for.

Store it in the existing `note` column rather than adding a column: `delimp_claude_heartbeat` is the
cross-machine comms table, DDL on it affects every node's `claude_bus.py` at once, and the payload is
a short `ingest=<8-char digest> (stale: spectronaut_to_corpus.py)`. A column can come later if this
proves load-bearing.

`who` then renders a node running stale ingest code distinctly, the same way it already renders
🔴DOWN.

## Constraints

- **Content-addressed, never version-constant-addressed.** The constant already lied; a design that
  trusts it repeats a known failure.
- The gate runs **before** any corpus write, not after a partial ingest.
- **Fail open on an unreachable manifest**, fail closed on a mismatch. Those are opposite defaults
  and both are deliberate.
- `publish_manifest.py` refuses a dirty tree.
- The override must leave a durable record.
- Every `query()` passes `tables=[...]`; the new table is INTERNAL — it names files and hosts, not
  corpus data, and nothing public should read it.
- **Ingest scripts on the share cannot import `app.db`** — proven on 2026-09-16, it is not on
  `sys.path` there. The gate must use the `coreomics_import._conn` route like every other script
  that runs from `fran_ingest/`. A gate that cannot run in the place it guards is not a gate.
- Every test proven able to fail.

## Risks

| risk | mitigation |
|---|---|
| The gate blocks a legitimate urgent ingest | `--ignore-stale-ingest`, recorded in the component log |
| PG Farm unreachable → ingestion stops | Fails open with a warning; explicitly not a hard dependency |
| Publishing is forgotten, so the manifest goes stale and blocks current code | `published_at` is surfaced in the mismatch message; a manifest older than the file it describes is reported as "manifest may be stale", not as "your code is stale" |
| A node runs from a directory the gate does not hash | The gate hashes files beside the *running script*, resolved from `__file__`, not a hardcoded path |
| DDL on the comms heartbeat breaks other nodes | Reuse the existing `note` column; no DDL on `delimp_claude_heartbeat` |

## Deliberately out of scope

- Auto-updating the share. The gate detects and refuses; it does not pull code. Self-updating
  ingest code on a machine nobody is watching is a larger decision than this.
- Signing or verifying provenance beyond an md5. This guards against staleness, not tampering.
- Gating the XIC lane scripts (`sne_xic_ingest.py`, `xic_lance.py`). Those genuinely diverge
  bidirectionally and need a manual merge first; gating them now would refuse on a file that has no
  correct version yet.

## The immediate fix this enables

Independent of the gate, three files should be copied repo → share before the Windows nodes ingest
anything. Audited 2026-09-16, all three cleanly repo-ahead with no share-side work to lose:

```bash
scp ingest/corpus_ingest.py ingest/spectronaut_to_corpus.py ingest/versions.py \
    hive:/nfs/lssc0/flinders/proteomics/Data/FRAN_SNE_export/
```

`versions.py` and `xic_lance.py` must move **together** if either moves — 1.2.0 declares the
streamed `dataset_md5` that `xic_lance.py` implements. **Do not copy `sne_xic_ingest.py`** in either
direction: the share holds `iter_record_batches` / `_meas_quant` / `_upsert_xic`, the RAM-bounded
path that exists because the whole-table version OOM-killed the 224-run PROT_0793 lane at 48 GB.
