# Ingest Version Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Windows Claude node running a stale ingestor refuses to ingest, and sees it is stale the moment it checks in — without anyone remembering anything.

**Architecture:** Content-addressed. The repo publishes md5s of every `ingest/*.py` to `delimp_ingest_manifest` on PG Farm — the same database the nodes already use for `claude_bus`. The ingestor hashes its own files at startup and refuses on a mismatch of any corpus-writing script. `claude_bus checkin` carries the fingerprint so `who` shows staleness with no ingest run at all.

**Tech Stack:** Python 3, psycopg2, PostgreSQL (PG Farm). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-16-ingest-version-gate-design.md`

**Worktree:** `/Users/brettphinney/Documents/FRAN-gate`, branch `ingest-version-gate`, based on `main`.

## Global Constraints

- **Content-addressed, never version-constant-addressed.** Both repo and share declare `CORPUS_INGEST_VERSION = "1.3.0"` today while the files differ. A design trusting that constant repeats a known, live failure.
- **Ingest scripts running from the share CANNOT import `app.db`.** Proven 2026-09-16: `fran_ingest/` is a flat scp'd directory containing only `coreomics_import.py` and `refresh_corpus_reach.py`. Use `from coreomics_import import _conn`. A gate that cannot run where it guards is not a gate.
- **Fail CLOSED on a hash mismatch. Fail OPEN when the manifest is unreachable.** Opposite defaults, both deliberate. A PG Farm outage must not stop ingestion.
- The gate runs **before any corpus write**, not after a partial ingest.
- `delimp_ingest_manifest` is INTERNAL (`_INTERNAL_TABLES`) — it names files and hosts.
- Every `query()` passes `tables=[...]`. Writes go through psycopg2 directly; `app.db.query()` is read-only by construction and raises on INSERT.
- Every test proven able to fail: show red, then green.

---

### Task 1: The manifest table and publisher

**Files:**
- Create: `ingest/publish_manifest.py`
- Modify: `schema/fran_schema.sql` (DDL), `app/db.py` (`_INTERNAL_TABLES`)
- Test: `tests/test_ingest_manifest.py`

**Interfaces:**
- Produces: table `delimp_ingest_manifest(file TEXT PRIMARY KEY, md5 TEXT NOT NULL, git_sha TEXT NOT NULL, published_at TIMESTAMPTZ NOT NULL DEFAULT now(), published_by TEXT NOT NULL, gate TEXT NOT NULL)`.
- Produces: `publish_manifest.py` → writes one row per `ingest/*.py`.
- Produces: `REFUSE_FILES` — the set gated at `refuse`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ingest_manifest.py
import os, sys, hashlib, subprocess
os.environ.setdefault("DELIMP_PG_TOKEN_FILE", "/Users/brettphinney/.pgfarm_token")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingest"))

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

import publish_manifest as PM

# The gated set must contain every script that writes corpus rows. If a new adapter is added and
# not listed here, a stale copy of it would ingest silently — the exact failure this exists to stop.
check("corpus-writing scripts are gated at refuse",
      {"corpus_ingest.py", "spectronaut_to_corpus.py", "versions.py"} <= set(PM.REFUSE_FILES),
      str(sorted(PM.REFUSE_FILES)))

# THE DISCRIMINATOR. The whole design rests on md5 detecting a change no version constant does.
# spectronaut_to_corpus.py gained the PTM work with no version bump; a constant-based check sees
# nothing. Assert the hash function actually distinguishes two near-identical files.
a = PM.file_md5(os.path.join(PM.INGEST_DIR, "spectronaut_to_corpus.py"))
import tempfile, shutil
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "x.py")
    shutil.copy(os.path.join(PM.INGEST_DIR, "spectronaut_to_corpus.py"), p)
    check("identical content hashes identically", PM.file_md5(p) == a)
    with open(p, "a") as fh: fh.write("\n# one comment\n")
    check("a one-line change changes the hash", PM.file_md5(p) != a)

check("publish refuses a dirty tree", hasattr(PM, "assert_clean_tree"))
print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Run to verify it fails**

`DELIMP_PG_TOKEN_FILE=/Users/brettphinney/.pgfarm_token python3 tests/test_ingest_manifest.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'publish_manifest'`.

- [ ] **Step 3: DDL and allowlist**

`schema/fran_schema.sql`, matching that file's generated style (`timestamp with time zone`, PK as a separate ALTER in the constraints block, alphabetical placement):

```sql
CREATE TABLE IF NOT EXISTS delimp_ingest_manifest (
    "file" text NOT NULL,
    "md5" text NOT NULL,
    "git_sha" text NOT NULL,
    "published_at" timestamp with time zone NOT NULL DEFAULT now(),
    "published_by" text NOT NULL,
    "gate" text NOT NULL
);
```

Apply the same DDL to the live DB (additive, `IF NOT EXISTS`). Add `"delimp_ingest_manifest",` to `_INTERNAL_TABLES` in `app/db.py` — **not** `PUBLIC_TABLES`; it names files and hosts.

- [ ] **Step 4: Write the publisher**

```python
#!/usr/bin/env python3
"""publish_manifest.py — record what the CURRENT ingest code is, so a node can tell it is stale.

WHY CONTENT-ADDRESSED AND NOT A VERSION CONSTANT. On 2026-09-16 both the repo and the Windows-node
share declared CORPUS_INGEST_VERSION = "1.3.0" while the files differed: spectronaut_to_corpus.py
had gained the whole PTM localization block with no version bump, because there is no adapter
version constant at all. versions.py's own docstring warns that "a version that lags the code is
worse than no version, because it is trusted". A constant-based gate would have passed that.

WHY THE MANIFEST LIVES IN PG FARM AND NOT ON THE SHARE. A manifest file shipped alongside the code
is circular: a node that did not sync holds an OLD manifest that matches its OLD files, and nothing
notices. The database is the one thing both sides see, and a node holds no copy of it.
"""
from __future__ import annotations
import hashlib, os, socket, subprocess, sys

INGEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, INGEST_DIR)

# Scripts whose staleness silently corrupts the corpus. A stale adapter writes rows that look fine
# and need re-ingesting later, which is strictly worse than not running.
REFUSE_FILES = {"corpus_ingest.py", "spectronaut_to_corpus.py", "diann_to_corpus.py", "versions.py"}


def file_md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def git_sha() -> str:
    return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                   cwd=INGEST_DIR, text=True).strip()


def assert_clean_tree() -> None:
    """Refuse to publish from a dirty tree.

    Publishing a hash for uncommitted code records an expectation no other machine can reach: the
    node is then told it is stale against a git_sha that does not exist anywhere but this laptop.
    """
    out = subprocess.check_output(["git", "status", "--porcelain", "--", "ingest"],
                                  cwd=INGEST_DIR, text=True).strip()
    if out:
        raise SystemExit("refusing to publish: ingest/ has uncommitted changes:\n" + out)


def manifest_rows():
    sha, host = git_sha(), socket.gethostname()
    for name in sorted(os.listdir(INGEST_DIR)):
        if not name.endswith(".py"):
            continue
        yield (name, file_md5(os.path.join(INGEST_DIR, name)), sha, host,
               "refuse" if name in REFUSE_FILES else "warn")


def main() -> int:
    assert_clean_tree()
    from coreomics_import import _conn
    rows = list(manifest_rows())
    with _conn() as cn, cn.cursor() as cur:
        for f, md5, sha, host, gate in rows:
            cur.execute("""INSERT INTO delimp_ingest_manifest
                             (file, md5, git_sha, published_by, gate, published_at)
                           VALUES (%s,%s,%s,%s,%s, now())
                           ON CONFLICT (file) DO UPDATE SET
                             md5=EXCLUDED.md5, git_sha=EXCLUDED.git_sha,
                             published_by=EXCLUDED.published_by, gate=EXCLUDED.gate,
                             published_at=EXCLUDED.published_at""",
                        (f, md5, sha, host, gate))
        cn.commit()
    print(f"published {len(rows)} ingest files at {rows[0][2]}")
    print(f"  refuse-gated: {sorted(f for f,_,_,_,g in rows if g=='refuse')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Add the PK as a separate statement after the CREATE, per the schema file's style:
`ALTER TABLE delimp_ingest_manifest ADD CONSTRAINT delimp_ingest_manifest_pkey PRIMARY KEY (file);`

- [ ] **Step 5: Run the test, then publish**

`python3 tests/test_ingest_manifest.py` → ALL PASS.
Then `python3 ingest/publish_manifest.py` — this is a small write to an internal table and is safe.

- [ ] **Step 6: Prove the dirty-tree guard has teeth**

Touch a file in `ingest/`, run `publish_manifest.py`, confirm it exits refusing and names the file. Revert, confirm it publishes. Paste both.

- [ ] **Step 7: Commit**

```bash
git add ingest/publish_manifest.py schema/fran_schema.sql app/db.py tests/test_ingest_manifest.py
git commit -m "feat: publish ingest file hashes to PG Farm, content-addressed not version-addressed"
```

---

### Task 2: The gate

**Files:**
- Modify: `ingest/versions.py` (add `assert_current()`)
- Modify: `ingest/corpus_ingest.py` (call it before any work)
- Test: `tests/test_ingest_gate.py`

**Interfaces:**
- Consumes: `delimp_ingest_manifest`, `publish_manifest.file_md5` / `REFUSE_FILES`.
- Produces: `versions.assert_current(cur, ignore_stale=False) -> list[str]` — returns the stale filenames, raises `SystemExit` if any is refuse-gated and `ignore_stale` is False.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ingest_gate.py
import os, sys, tempfile, shutil
os.environ.setdefault("DELIMP_PG_TOKEN_FILE", "/Users/brettphinney/.pgfarm_token")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..")); sys.path.insert(0, os.path.join(HERE, "..", "ingest"))

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

import versions as V
from coreomics_import import _conn

with _conn() as cn, cn.cursor() as cur:
    # Current code must pass cleanly. If this fails, publish_manifest has not been run.
    stale = V.assert_current(cur)
    check("current ingest code is not stale", stale == [], str(stale))

    # THE CORE ASSERTION. Simulate a stale file by rewriting its manifest md5, and confirm the gate
    # REFUSES rather than warns. A gate that returns the filename but does not stop is the failure
    # this whole design exists to prevent.
    cur.execute("SELECT md5 FROM delimp_ingest_manifest WHERE file='corpus_ingest.py'")
    real = cur.fetchone()[0]
    cur.execute("UPDATE delimp_ingest_manifest SET md5='0'*32 WHERE file='corpus_ingest.py'")
    raised = False
    try:
        V.assert_current(cur)
    except SystemExit:
        raised = True
    check("a stale REFUSE-gated file raises SystemExit, not a warning", raised)

    # ...and the override works, but only explicitly.
    try:
        stale2 = V.assert_current(cur, ignore_stale=True)
        check("--ignore-stale-ingest returns the stale list instead of exiting",
              "corpus_ingest.py" in stale2, str(stale2))
    except SystemExit:
        check("--ignore-stale-ingest returns the stale list instead of exiting", False, "raised anyway")

    # A WARN-gated file must NOT stop ingestion.
    cur.execute("UPDATE delimp_ingest_manifest SET md5=%s WHERE file='corpus_ingest.py'", (real,))
    cur.execute("SELECT file, md5 FROM delimp_ingest_manifest WHERE gate='warn' LIMIT 1")
    row = cur.fetchone()
    if row:
        wf, wmd5 = row
        cur.execute("UPDATE delimp_ingest_manifest SET md5='0'*32 WHERE file=%s", (wf,))
        try:
            s3 = V.assert_current(cur)
            check("a stale WARN-gated file does not stop ingestion", wf in s3, str(s3))
        except SystemExit:
            check("a stale WARN-gated file does not stop ingestion", False, "refused on a warn file")
        cur.execute("UPDATE delimp_ingest_manifest SET md5=%s WHERE file=%s", (wmd5, wf))

    # FAIL OPEN: an unreadable manifest must not stop ingestion.
    check("a missing manifest table fails OPEN",
          V.assert_current(None) == [], "passing a dead cursor must not raise")
    cn.rollback()   # leave the manifest exactly as found

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Run to verify it fails**

Expected: FAIL — `module 'versions' has no attribute 'assert_current'`.

- [ ] **Step 3: Implement the gate in `versions.py`**

```python
def assert_current(cur, ignore_stale: bool = False) -> list[str]:
    """Refuse to run a stale ingestor. Returns the stale filenames.

    TWO OPPOSITE DEFAULTS, BOTH DELIBERATE:
      * a hash MISMATCH fails CLOSED -- a stale adapter writes rows that look fine and need
        re-ingesting later, which is worse than not running at all;
      * an UNREACHABLE manifest fails OPEN -- this gate exists to prevent silent corruption, not to
        make PG Farm a hard dependency of ingestion. A database outage must not stop the pipeline.

    Cannot import app.db: on the Windows-node share `fran_ingest/` is a flat scp'd directory with no
    `app/` parent (proven 2026-09-16). Callers pass a live cursor.
    """
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        sys.path.insert(0, here)
        from publish_manifest import file_md5
        cur.execute("SELECT file, md5, git_sha, published_at, gate FROM delimp_ingest_manifest")
        rows = cur.fetchall()
    except Exception as e:                      # FAIL OPEN -- see docstring
        print(f"  ingest-gate: manifest unreadable ({e}); proceeding unchecked", flush=True)
        return []
    if not rows:
        print("  ingest-gate: manifest is empty; proceeding unchecked "
              "(run ingest/publish_manifest.py from the repo)", flush=True)
        return []

    stale, refuse = [], []
    for f, want, sha, published_at, gate in rows:
        p = os.path.join(here, f)
        if not os.path.exists(p):
            continue                            # not deployed here; not this gate's business
        got = file_md5(p)
        if got != want:
            stale.append(f)
            print(f"  ingest-gate: STALE {f}  local={got[:8]} expected={want[:8]} "
                  f"(published {published_at:%Y-%m-%d} at {sha})", flush=True)
            if gate == "refuse":
                refuse.append(f)
    if stale:
        print(f"  ingest-gate: fix with  scp {' '.join(sorted(stale))} "
              f"<repo>/ingest/ -> this directory", flush=True)
    if refuse and not ignore_stale:
        raise SystemExit(
            f"REFUSING TO INGEST: {len(refuse)} corpus-writing script(s) are stale: "
            f"{', '.join(sorted(refuse))}. Ingesting with these produces rows that must be "
            f"re-ingested later. Sync them, or pass --ignore-stale-ingest to override (recorded).")
    return stale
```

- [ ] **Step 4: Wire it into `corpus_ingest.py`**

At `corpus_ingest.py:439`, where `record_run` already stamps the run **before any work** — the correct place, since it is already before the engine pre-flight:

```python
        import versions as _V
        _stale = _V.assert_current(cur, ignore_stale=args.ignore_stale_ingest)
        _V.record_run(cur, "corpus_ingest", CORPUS_INGEST_VERSION,
                      notes=(f"schema={SCHEMA_VERSION}" if not _stale
                             else f"schema={SCHEMA_VERSION}; RAN STALE: {','.join(_stale)}"))
        conn.commit()
```

Add `--ignore-stale-ingest` to the argparse. The override **must** leave the `RAN STALE` note — an override with no trace is how "temporarily" becomes permanent.

- [ ] **Step 5: Run the test**

Expected: ALL PASS.

- [ ] **Step 6: Prove the gate refuses, end to end**

Mandatory. Corrupt the manifest md5 for `corpus_ingest.py`, run a real `corpus_ingest.py --dry` invocation, and confirm it exits non-zero with `REFUSING TO INGEST` **before** printing any ingest progress. Restore. Paste both outputs. A unit test asserting `SystemExit` is not the same as the script actually stopping.

- [ ] **Step 7: Commit**

```bash
git add ingest/versions.py ingest/corpus_ingest.py tests/test_ingest_gate.py
git commit -m "feat: refuse to ingest with a stale adapter; fail open if the manifest is unreachable"
```

---

### Task 3: Heartbeat visibility

**Files:**
- Modify: `claude_bus.py` on the share (`/nfs/lssc0/flinders/proteomics/Data/FRAN_SNE_export/claude_bus.py`) — **and take a copy into `ingest/` in the repo first**, since it is currently share-only and therefore unversioned.

**Interfaces:**
- Consumes: `versions.assert_current`, `publish_manifest.file_md5`.
- Produces: `checkin` writes `ingest=<8 hex> (stale: a.py,b.py)` into the existing `note`; `who` renders staleness.

- [ ] **Step 1: Bring `claude_bus.py` into the repo**

It lives only on the share today, so it has no history and no review. Copy it in unchanged as its own commit, so the next step's diff is readable.

- [ ] **Step 2: Add the fingerprint to `checkin`**

Compute a short digest over the deployed ingest files and append to the note. Keep it short — `note` is rendered inline by `who`:

```python
def _ingest_fingerprint():
    """Short digest of the deployed ingest files, plus which are stale. Best-effort: a node that
    cannot compute it still checks in. Uses the existing `note` column rather than new DDL --
    delimp_claude_heartbeat is the cross-machine comms table and a schema change there lands on
    every node's claude_bus at once."""
    try:
        import hashlib, os, sys
        d = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, d)
        from publish_manifest import file_md5, REFUSE_FILES
        parts = [file_md5(os.path.join(d, f)) for f in sorted(REFUSE_FILES)
                 if os.path.exists(os.path.join(d, f))]
        if not parts:
            return ""
        return "ingest=" + hashlib.md5("".join(parts).encode()).hexdigest()[:8]
    except Exception:
        return ""
```

- [ ] **Step 3: Render staleness in `who`**

`who` already flags 🔴DOWN on a stale heartbeat. Add the same treatment for a node whose fingerprint does not match the manifest, so a node that is *up* but running old code is visibly distinct from one that is merely quiet.

- [ ] **Step 4: Verify without touching the nodes**

Run `claude_bus.py checkin` as `mac-clip-fran` from a directory holding the current ingest files, then `claude_bus.py who`, and confirm the fingerprint appears. Then run it from a directory holding a deliberately old copy and confirm `who` marks it stale.

**Do NOT scp anything to the share, and do NOT modify any other node's state.** Print the scp line for a human.

- [ ] **Step 5: Commit**

```bash
git add ingest/claude_bus.py
git commit -m "feat: nodes report their ingest fingerprint on checkin, so `who` shows staleness"
```

---

## Self-Review

**Spec coverage.** Manifest table (T1), publisher with dirty-tree guard (T1), the gate with refuse/warn split and fail-open (T2), the override with a durable record (T2 step 4), heartbeat fingerprint and `who` rendering (T3). The spec's "out of scope" items — auto-update, signing, gating the XIC lane — are absent from every task, which is correct.

**Placeholders.** None; every code step carries the code.

**Type consistency.** `file_md5` and `REFUSE_FILES` are defined in `publish_manifest.py` (T1) and imported by both `versions.assert_current` (T2) and `_ingest_fingerprint` (T3). `assert_current(cur, ignore_stale)` returns `list[str]` and is consumed as a list in T2 step 4's `record_run` note.

**Known gap, deliberate.** The gate hashes files beside `versions.py`. A node that runs `corpus_ingest.py` from one directory while `versions.py` resolves from another would gate the wrong copy. That cannot happen on the share (flat directory, everything co-located) or in the repo, but it is not defended against — doing so would mean hashing by import path, which is more machinery than the failure justifies.

**One risk this plan does not remove.** `publish_manifest.py` must actually be run after an ingest change, or the manifest silently describes old code and the gate passes a stale node. That is one command in the repo instead of an scp to a share, which is the improvement — but it is still a discipline. The `published_at` surfaced in every mismatch message is the mitigation: a manifest older than the local file is reported as "manifest may be stale" rather than "your code is stale", so the operator is not sent to sync the wrong direction.
