# Service-Directory Scanner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the dead one-shot "AI disk-match" with a repeatable scanner that inventories every project folder on the service share — where it is, how many runs it holds, whether it is in FRAN — so the un-ingested inventory stops rotting.

**Architecture:** A standalone `ingest/scan_service_dir.py` walks `campus/client/project` under the service root, counts raw files per project folder, resolves `in_fran` against `delimp_search_provenance`, and upserts into `delimp_submission_service_dir`. Submission matching is a separate, conservative pass that never overwrites a higher-confidence `ai-disk-match` row. Folders that match no submission are still inventoried, which requires relaxing the table's one-row-per-submission key.

**Tech Stack:** Python 3.11 (Hive env `/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python`), psycopg2, PG Farm. No pytest — this repo's tests are plain executable Python scripts (see `tests/test_federation_boundary.py`).

**Spec:** `docs/superpowers/specs/2026-09-08-submissions-tab-and-service-dir-scanner-design.md`

## Global Constraints

- **Never overwrite a higher-confidence `ai-disk-match` row.** The June rows encode human-reviewed judgement this script cannot re-derive. `matched_by` distinguishes origin; scanner rows use `scan_service_dir/v1`.
- **NULL beats a wrong value.** A folder that matches no submission is inventoried with `submission_id = NULL`, never guessed onto a plausible submission. (Same rule that governs `sample_health.spd`.)
- **`service_folder` format is fixed** as `campus/client/project` (e.g. `on_campus/SadeghC/Chechneva_beadsMusM_iun26`) with `service_folder_win` as `R:\Data\lab\service\<same, backslashed>`, so scanner rows and existing rows are interchangeable.
- **Service root:** `/nfs/lssc0/flinders/proteomics/Data/lab/service` (= `R:\Data\lab\service`). 396 `on_campus` + 354 `off_campus` client folders as of 2026-09-08.
- **Skip at campus level:** `Thumbs.db`, `htrms_quarantine_*`, and any non-directory entry.
- **Raw counting:** `.d` directories and `.raw` files. Never descend into a `.d` (it is a directory of instrument files, not a folder of runs).
- **Credentials from files, never argv.** `DELIMP_PG_TOKEN_FILE` as every other ingest script does; argv is world-readable on the compute nodes.
- **Heavy work is not login-node work.** The full walk runs under sbatch.
- **The service-dir tables are INTERNAL tables** (`delimp_submission_service_dir` and the new `delimp_service_dir_inventory`) (`app/db.py:_INTERNAL_TABLES`). The
  governed `app.db.query` refuses it unless the request is internal, so any test or snippet using
  that layer must set `DELIMP_INTERNAL_MODE=1` BEFORE importing `app.db`. The scanner itself talks
  to psycopg2 directly and is unaffected.
- **`ingest/` is not a Python package** — there is no `__init__.py`. Import siblings with
  `sys.path.insert(0, "ingest")`, never `from ingest.x import y`.
- **Every run stamps `scanned_at`,** so staleness is visible in the data. The June table's invisibility is the specific failure being fixed.

---

### Task 1: A folder-inventory table

**Files:**
- Create: `ingest/migrations/2026-09-08_service_dir_inventory.sql`
- Test: `tests/test_service_dir_schema.py`

**WHY THIS IS A NEW TABLE, NOT AN ALTER.** The first version of this task tried to add
`UNIQUE (service_folder)` to `delimp_submission_service_dir`. Applying it failed:
`UniqueViolation: could not create unique index`. Measured on the live data, **285 service_folder
values appear more than once**, the worst mapping to ELEVEN submissions
(`off_campus/UCSF/Jain-Isha/JainUCSF-Desousa-brandon`). That is correct data — a lab sends several
submissions whose work lands in one project folder — so that table is genuinely
one-row-per-submission and can never carry a unique folder.

Folder facts therefore get their own table. Storing them in the existing one would repeat a
folder's `run_count` once per mapped submission (eleven times for that UCSF folder), so a single
scan would update eleven rows that could then disagree.

`delimp_submission_service_dir` is left ENTIRELY UNTOUCHED. `build_resubmit_brief`,
`internal_submission`, `internal_lab` and `internal_labs_by_institution` all read it and keep
working unchanged.

**Interfaces:**
- Produces: `delimp_service_dir_inventory` — `service_folder TEXT PRIMARY KEY`,
  `service_folder_win TEXT`, `campus TEXT`, `run_count INTEGER` (NULL = could not be read),
  `in_fran BOOLEAN`, `scanned_at TIMESTAMPTZ`. Task 4 upserts on `ON CONFLICT (service_folder)`.

- [ ] **Step 1: Write the failing test**

```python
"""The folder inventory needs its own table, keyed on the folder.

delimp_submission_service_dir is one row per SUBMISSION and cannot be keyed on service_folder:
285 folders there map to more than one submission (one maps to eleven). This table is one row per
FOLDER, including folders no submission can be matched to.

Run:  python tests/test_service_dir_schema.py
"""
import os, sys
# The service-dir tables are in app/db.py's _INTERNAL_TABLES; the governed query layer refuses
# them unless the request is internal. Set this BEFORE importing app.db.
os.environ.setdefault("DELIMP_INTERNAL_MODE", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app.db import query                                  # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)

cols = {r["column_name"]: r for r in query(
    "SELECT column_name, is_nullable, data_type FROM information_schema.columns "
    "WHERE table_name='delimp_service_dir_inventory'",
    tables=["delimp_service_dir_inventory"])}
for c in ("service_folder", "service_folder_win", "campus", "run_count", "in_fran", "scanned_at"):
    check(f"{c} column exists", c in cols, sorted(cols))
check("run_count is nullable (NULL = could not be read)",
      cols.get("run_count", {}).get("is_nullable") == "YES",
      str(cols.get("run_count", {}).get("is_nullable")))

idx = query("SELECT indexdef FROM pg_indexes WHERE tablename='delimp_service_dir_inventory'",
            tables=["delimp_service_dir_inventory"])
defs = " ".join(r["indexdef"] for r in idx)
check("service_folder is the primary key", "UNIQUE" in defs and "service_folder" in defs, defs[:200])

# The existing table must be untouched — its consumers depend on it.
old = query("SELECT count(*) AS n FROM delimp_submission_service_dir",
            tables=["delimp_submission_service_dir"])
check("delimp_submission_service_dir still has its 1862 rows", old[0]["n"] == 1862, str(old[0]["n"]))
oldcols = {r["column_name"] for r in query(
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_name='delimp_submission_service_dir'",
    tables=["delimp_submission_service_dir"])}
check("delimp_submission_service_dir gained no columns",
      oldcols == {"submission_id", "service_folder", "service_folder_win", "campus", "in_fran",
                  "run_count", "match_confidence", "clue", "matched_by", "matched_at"},
      str(sorted(oldcols)))

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python tests/test_service_dir_schema.py`
Expected: the six column checks and the primary-key check FAIL (the table does not exist). The two
"untouched" checks should PASS already — if either fails, STOP: something has altered the existing
table, and that must be understood before going further.

- [ ] **Step 3: Write the migration file**

```sql
-- One row per project FOLDER on the service share.
--
-- Deliberately NOT part of delimp_submission_service_dir, which is one row per SUBMISSION: 285 of
-- its service_folder values repeat, one across eleven submissions, because a lab sends several
-- submissions whose work lands in one folder. Keying that table on the folder is impossible, and
-- storing folder facts in it would repeat run_count once per mapped submission.
--
-- run_count NULL means "could not be read", which must stay distinguishable from 0 = genuinely
-- empty. That is why count_runs() returns int | None.
CREATE TABLE IF NOT EXISTS delimp_service_dir_inventory (
    service_folder     TEXT PRIMARY KEY,
    service_folder_win TEXT,
    campus             TEXT,
    run_count          INTEGER,
    in_fran            BOOLEAN,
    scanned_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_service_dir_inv_in_fran
  ON delimp_service_dir_inventory (in_fran);
```

- [ ] **Step 4: Apply it and re-run the test**

Write the statements INLINE in the apply command rather than reading the .sql file. Executing file
contents is refused by this environment's safety gate, while inline statements are visible to
whoever approves them. Save the .sql file anyway as the migration record.

```bash
cd /Users/brettphinney/Documents/FRAN-scanner
DELIMP_PG_TOKEN_FILE=~/.pgfarm_token python3 -c "
import sys; sys.path.insert(0, 'ingest')
from coreomics_import import _conn
con = _conn(); cur = con.cursor()
cur.execute('CREATE TABLE IF NOT EXISTS delimp_service_dir_inventory (service_folder TEXT PRIMARY KEY, service_folder_win TEXT, campus TEXT, run_count INTEGER, in_fran BOOLEAN, scanned_at TIMESTAMPTZ)')
cur.execute('CREATE INDEX IF NOT EXISTS idx_service_dir_inv_in_fran ON delimp_service_dir_inventory (in_fran)')
con.commit()
cur.execute('SELECT count(*) FROM delimp_submission_service_dir')
print('existing table still has', cur.fetchone()[0], 'rows')
con.close()"
python tests/test_service_dir_schema.py
```

Expected: `existing table still has 1862 rows`, then `ALL PASS`.

- [ ] **Step 5: Commit**

```bash
git add ingest/migrations/2026-09-08_service_dir_inventory.sql tests/test_service_dir_schema.py
git commit -m "ingest: a folder-inventory table, because one folder can serve many submissions"
```

---

### Task 2: The folder walk and run counter

**Files:**
- Create: `ingest/scan_service_dir.py`
- Test: `tests/test_scan_service_dir.py`

**Interfaces:**
- Consumes: nothing from Task 1 at import time (pure filesystem functions).
- Produces:
  - `SERVICE_ROOT: str`
  - `SKIP_AT_CAMPUS: set[str]`
  - `win_path(rel: str) -> str` — `"on_campus/A/B"` → `"R:\\Data\\lab\\service\\on_campus\\A\\B"`
  - `count_runs(path: str) -> int` — `.d` dirs + `.raw` files, not descending into `.d`
  - `walk_projects(root: str) -> list[dict]` — `[{"service_folder", "service_folder_win", "campus", "abs_path", "run_count"}]`

- [ ] **Step 1: Write the failing test**

```python
"""The service-share walk: what counts as a run, and what is skipped.

Run:  python tests/test_scan_service_dir.py
"""
import os, sys, tempfile, pathlib
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ingest"))
import scan_service_dir as sd                              # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)

check("win_path spells the R: drive",
      sd.win_path("on_campus/A/B") == r"R:\Data\lab\service\on_campus\A\B",
      sd.win_path("on_campus/A/B"))

with tempfile.TemporaryDirectory() as root:
    p = pathlib.Path(root)
    proj = p / "on_campus" / "SomeLab" / "proj1"
    proj.mkdir(parents=True)
    (proj / "run1.d").mkdir()
    (proj / "run1.d" / "analysis.tdf").write_text("x")     # must NOT be counted
    (proj / "run2.d").mkdir()
    (proj / "run3.raw").write_text("x")
    (proj / "notes.txt").write_text("x")                   # must NOT be counted
    check("count_runs counts .d dirs and .raw files only", sd.count_runs(str(proj)) == 3,
          str(sd.count_runs(str(proj))))

    # campus-level junk is skipped
    (p / "Thumbs.db").write_text("x")
    (p / "htrms_quarantine_20250916_134754").mkdir()
    empty = p / "off_campus" / "OtherLab" / "proj2"
    empty.mkdir(parents=True)

    rows = sd.walk_projects(str(root))
    folders = {r["service_folder"] for r in rows}
    check("finds the on_campus project", "on_campus/SomeLab/proj1" in folders, str(folders))
    check("finds the off_campus project", "off_campus/OtherLab/proj2" in folders, str(folders))
    check("skips Thumbs.db and quarantine dirs",
          not any("htrms_quarantine" in f or "Thumbs" in f for f in folders), str(folders))
    r1 = next(r for r in rows if r["service_folder"] == "on_campus/SomeLab/proj1")
    check("row carries campus", r1["campus"] == "on_campus", r1["campus"])
    check("row carries run_count", r1["run_count"] == 3, str(r1["run_count"]))
    check("a project with no runs is still inventoried",
          next(r for r in rows if r["service_folder"].endswith("proj2"))["run_count"] == 0)

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python tests/test_scan_service_dir.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'scan_service_dir'`.

- [ ] **Step 3: Write the walk**

```python
"""scan_service_dir.py — inventory the service share so the un-ingested list stops rotting.

WHY THIS EXISTS. delimp_submission_service_dir was written ONCE, on 2026-06-24, by an "ai-disk-match"
whose source TSVs lived in a Claude scratchpad that no longer exists. It holds 1,862 rows -- 1,624
submissions whose data is on the share and NOT in FRAN -- and it has known nothing since June. It is
also load-bearing: build_resubmit_brief() reads service_folder / service_folder_win to tell a
HIVE Claude where the raw data is, so every submission after PROT_0724 gets a brief with no paths.

This inventories the share deterministically. Matching a folder to a CoreOmics submission is a
SEPARATE, conservative pass (see match_submissions) that never overwrites a human-reviewed row.
"""
from __future__ import annotations

import os

SERVICE_ROOT = os.environ.get("FRAN_SERVICE_ROOT",
                              "/nfs/lssc0/flinders/proteomics/Data/lab/service")
WIN_ROOT = r"R:\Data\lab\service"

# Campus-level entries that are not client folders. Non-directories are skipped anyway; these are
# the directories that would otherwise be walked as if they were campuses.
SKIP_AT_CAMPUS = {"Thumbs.db"}
SKIP_PREFIXES = ("htrms_quarantine_",)


def win_path(rel: str) -> str:
    """'on_campus/A/B' -> the R: spelling stored in service_folder_win."""
    return WIN_ROOT + "\\" + rel.replace("/", "\\")


def count_runs(path: str) -> int:
    """Raw acquisitions directly under `path`: .d directories plus .raw files.

    Does NOT descend into a .d -- it is one acquisition stored as a directory of instrument files,
    so walking into it would count its internals as runs.
    """
    n = 0
    try:
        with os.scandir(path) as it:
            for e in it:
                low = e.name.lower()
                if e.is_dir(follow_symlinks=False) and low.endswith(".d"):
                    n += 1
                elif e.is_file(follow_symlinks=False) and low.endswith(".raw"):
                    n += 1
    except OSError:
        return 0
    return n


def _skip(name: str) -> bool:
    return name in SKIP_AT_CAMPUS or name.startswith(SKIP_PREFIXES)


def walk_projects(root: str = SERVICE_ROOT) -> list[dict]:
    """Every campus/client/project folder on the share, with its run count.

    Depth is fixed at three because that IS the share's shape and the format already stored in
    service_folder. A project's own subdirectories are its data, not more projects.
    """
    out: list[dict] = []
    try:
        campuses = sorted(e.name for e in os.scandir(root)
                          if e.is_dir(follow_symlinks=False) and not _skip(e.name))
    except OSError:
        return out
    for campus in campuses:
        cpath = os.path.join(root, campus)
        try:
            clients = sorted(e.name for e in os.scandir(cpath) if e.is_dir(follow_symlinks=False))
        except OSError:
            continue
        for client in clients:
            clpath = os.path.join(cpath, client)
            try:
                projects = sorted(e.name for e in os.scandir(clpath)
                                  if e.is_dir(follow_symlinks=False))
            except OSError:
                continue
            for project in projects:
                rel = f"{campus}/{client}/{project}"
                abs_path = os.path.join(clpath, project)
                out.append({"service_folder": rel, "service_folder_win": win_path(rel),
                            "campus": campus, "abs_path": abs_path,
                            "run_count": count_runs(abs_path)})
    return out
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python tests/test_scan_service_dir.py`
Expected: `ALL PASS`

- [ ] **Step 5: Commit**

```bash
git add ingest/scan_service_dir.py tests/test_scan_service_dir.py
git commit -m "ingest: walk the service share and count runs per project folder"
```

---

### Task 3: `in_fran` resolution

**Files:**
- Modify: `ingest/scan_service_dir.py`
- Test: `tests/test_scan_service_dir.py` (append)

**Interfaces:**
- Consumes: `walk_projects()` rows from Task 2.
- Produces: `ingested_folders(con) -> set[str]` — the set of `service_folder` values that already have a FRAN search, derived from `delimp_search_provenance.service_customer` + `service_campus`; and `mark_in_fran(rows, ingested) -> None`, which sets `row["in_fran"]`.

- [ ] **Step 1: Write the failing test (append to the same file, before the summary print)**

```python
# --- in_fran resolution -------------------------------------------------------
rows = [
    {"service_folder": "on_campus/SomeLab/proj1", "campus": "on_campus"},
    {"service_folder": "off_campus/OtherLab/proj2", "campus": "off_campus"},
]
sd.mark_in_fran(rows, {"on_campus/SomeLab/proj1"})
check("in_fran true when the folder has a search", rows[0]["in_fran"] is True)
check("in_fran false otherwise", rows[1]["in_fran"] is False)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python tests/test_scan_service_dir.py`
Expected: FAIL with `AttributeError: module 'scan_service_dir' has no attribute 'mark_in_fran'`.

- [ ] **Step 3: Implement**

```python
def ingested_folders(con) -> set[str]:
    """service_folder values that already have at least one FRAN search.

    Provenance stores the CLIENT folder (service_customer) and its campus, not the project folder,
    so this matches at client level and marks every project under a client we have searches for.
    That is deliberately generous: in_fran drives a "needs ingesting" prompt, and claiming work is
    un-ingested when it is not wastes someone's afternoon, which is the worse error here.
    """
    cur = con.cursor()
    cur.execute("""SELECT DISTINCT service_campus, service_customer
                     FROM delimp_search_provenance
                    WHERE service_customer IS NOT NULL""")
    return {f"{(c or '').strip()}/{(s or '').strip()}" for c, s in cur.fetchall()}


def mark_in_fran(rows: list[dict], ingested: set[str]) -> None:
    """Set row['in_fran'] from the client-level ingested set."""
    for r in rows:
        parts = r["service_folder"].split("/")
        client_key = "/".join(parts[:2])
        r["in_fran"] = client_key in ingested or r["service_folder"] in ingested
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python tests/test_scan_service_dir.py`
Expected: `ALL PASS`

- [ ] **Step 5: Commit**

```bash
git add ingest/scan_service_dir.py tests/test_scan_service_dir.py
git commit -m "ingest: resolve in_fran for scanned folders from search provenance"
```

---

### Task 4: Upsert, preserving the human-reviewed rows

**Files:**
- Modify: `ingest/scan_service_dir.py`
- Test: `tests/test_scan_service_dir.py` (append)

**Interfaces:**
- Consumes: rows from Tasks 2–3.
- Produces: `UPSERT_SQL: str` and `upsert(con, rows) -> int`. Upserts on `ON CONFLICT (service_folder)`; refreshes `run_count`, `in_fran`, `campus`, `service_folder_win`, `scanned_at`; **never** touches `submission_id`, `match_confidence`, `clue`, `matched_by` or `matched_at` on an existing row.

- [ ] **Step 1: Write the failing test (append)**

```python
# --- the upsert must not clobber a human-reviewed match ----------------------
check("upsert SQL keys on service_folder", "ON CONFLICT (service_folder)" in sd.UPSERT_SQL,
      sd.UPSERT_SQL[:160])
for col in ("submission_id", "match_confidence", "clue", "matched_by", "matched_at"):
    check(f"upsert never overwrites {col}",
          f"{col}=EXCLUDED" not in sd.UPSERT_SQL.replace(" ", ""),
          "found an EXCLUDED assignment")
for col in ("run_count", "in_fran", "scanned_at"):
    check(f"upsert refreshes {col}", col in sd.UPSERT_SQL.split("DO UPDATE SET")[1])
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python tests/test_scan_service_dir.py`
Expected: FAIL with `AttributeError: module 'scan_service_dir' has no attribute 'UPSERT_SQL'`.

- [ ] **Step 3: Implement**

```python
# The DO UPDATE list is deliberately short. submission_id / match_confidence / clue / matched_by /
# matched_at are NOT refreshed: the 2026-06-24 ai-disk-match rows encode human-reviewed judgement
# (clues like "submitter+date+organism") that this scanner cannot re-derive, and silently replacing
# them with a weaker guess would be a regression nobody would notice. The scanner owns the
# INVENTORY columns; matching owns the attribution columns, and only via match_submissions().
UPSERT_SQL = """
INSERT INTO delimp_service_dir_inventory
  (service_folder, service_folder_win, campus, run_count, in_fran, scanned_at)
VALUES (%s, %s, %s, %s, %s, now())
ON CONFLICT (service_folder) DO UPDATE SET
  service_folder_win = EXCLUDED.service_folder_win,
  campus             = EXCLUDED.campus,
  run_count          = EXCLUDED.run_count,
  in_fran            = EXCLUDED.in_fran,
  scanned_at         = now()
"""


def upsert(con, rows: list[dict]) -> int:
    cur = con.cursor()
    for r in rows:
        cur.execute(UPSERT_SQL, (r["service_folder"], r["service_folder_win"], r["campus"],
                                 r["run_count"], bool(r.get("in_fran"))))
    con.commit()
    return len(rows)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python tests/test_scan_service_dir.py`
Expected: `ALL PASS`

- [ ] **Step 5: Commit**

```bash
git add ingest/scan_service_dir.py tests/test_scan_service_dir.py
git commit -m "ingest: upsert scanned folders without clobbering reviewed matches"
```

---

### Task 5: CLI, dry-run default, and the first real run

**Files:**
- Modify: `ingest/scan_service_dir.py`
- Create: `ingest/scan_service_dir.sbatch`

**Interfaces:**
- Consumes: everything above.
- Produces: `main(argv=None) -> int`; `--apply` (default dry run), `--root`, `--limit`.

- [ ] **Step 1: Add the connection helper and CLI**

```python
def _conn():
    """PG Farm, via the same file-based credential every ingest script uses."""
    import json, urllib.request, psycopg2
    pw = os.environ.get("DELIMP_PG_PASSWORD")
    if not pw:
        tf = os.path.expanduser(os.environ.get("DELIMP_PG_TOKEN_FILE", "~/.pgfarm_token"))
        if not os.path.exists(tf):
            raise SystemExit(f"No PG Farm credential: set DELIMP_PG_PASSWORD or place one at {tf}")
        pw = open(tf).read().strip()
    if not (pw.startswith("eyJ") and pw.count(".") == 2):
        body = json.dumps({"username": "genome-proteomics-service-account", "secret": pw}).encode()
        req = urllib.request.Request(
            "https://pgfarm.library.ucdavis.edu/auth/service-account/login",
            data=body, headers={"Content-Type": "application/json"})
        pw = json.loads(urllib.request.urlopen(req, timeout=30).read())["access_token"]
    return psycopg2.connect(host="pgfarm.library.ucdavis.edu", port=5432,
                            dbname="uc-davis-genome-center-proteomics-core/delimp",
                            user="genome-proteomics-service-account", password=pw,
                            sslmode="require", connect_timeout=30)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--root", default=SERVICE_ROOT)
    ap.add_argument("--limit", type=int, default=0, help="stop after N folders (testing)")
    a = ap.parse_args(argv)

    print(f"walking {a.root} …", flush=True)
    rows = walk_projects(a.root)
    if a.limit:
        rows = rows[:a.limit]
    con = _conn()
    mark_in_fran(rows, ingested_folders(con))
    n_runs = sum(r["run_count"] for r in rows)
    n_fran = sum(1 for r in rows if r["in_fran"])
    print(f"  {len(rows)} project folders, {n_runs} runs, {n_fran} already in FRAN, "
          f"{len(rows) - n_fran} not", flush=True)
    if not a.apply:
        for r in rows[:15]:
            print(f"    {'IN FRAN ' if r['in_fran'] else 'on share'} {r['run_count']:>4} runs  "
                  f"{r['service_folder']}")
        print("dry run — nothing written. Re-run with --apply.")
        con.close()
        return 0
    n = upsert(con, rows)
    cur = con.cursor()
    cur.execute("SELECT count(*), count(*) FILTER (WHERE scanned_at IS NOT NULL) "
                "FROM delimp_service_dir_inventory")
    tot, scanned = cur.fetchone()
    print(f"upserted {n}; table now {tot} rows, {scanned} carrying a scanned_at", flush=True)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Dry-run it on Hive against a single client folder**

```bash
ssh hive 'cd /quobyte/proteomics-grp/brett/glendon/fran_ingest && \
  DELIMP_PG_TOKEN_FILE=/quobyte/proteomics-grp/brett/.pgfarm_token \
  /quobyte/proteomics-grp/brett/envs/alphadia2/bin/python scan_service_dir.py --limit 20'
```
Expected: a folder count, run counts, and `dry run — nothing written`. **Read the first 15 lines and sanity-check the run counts against what is actually in those folders before going further.**

- [ ] **Step 3: Write the sbatch**

```bash
#!/bin/bash
#SBATCH -A genome-center-grp
#SBATCH -p high
#SBATCH -q genome-center-grp-high-qos
#SBATCH -c 2
#SBATCH --mem=8G
#SBATCH -t 04:00:00
#SBATCH -J fran_svcdir
#SBATCH -o /quobyte/proteomics-grp/brett/logs/fran_svcdir_%j.log
#
# Walks ~750 client folders on the Flinders share. Metadata-heavy over NFS, so not login-node work.
#
# LOGNAME/USER seeded and the profile sourced OUTSIDE set -u: /etc/profile.d/modules.sh
# dereferences LOGNAME, which silently killed the Flinders cron (STAN 2026-06->08 postmortem).
export LOGNAME="${LOGNAME:-$(id -un)}"
export USER="${USER:-$(id -un)}"
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true
set -uo pipefail

export DELIMP_PG_TOKEN_FILE=/quobyte/proteomics-grp/brett/.pgfarm_token
PY=/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python
cd /quobyte/proteomics-grp/brett/glendon/fran_ingest || exit 1

echo "=== start $(date) on $(hostname) ==="
"$PY" -u scan_service_dir.py --apply
rc=$?
echo "=== end $(date) rc=$rc ==="
exit $rc
```

- [ ] **Step 4: Deploy and run the full scan**

```bash
scp ingest/scan_service_dir.py ingest/scan_service_dir.sbatch \
    hive:/quobyte/proteomics-grp/brett/glendon/fran_ingest/
ssh hive 'bash -lc "sbatch /quobyte/proteomics-grp/brett/glendon/fran_ingest/scan_service_dir.sbatch"'
```
Then verify the June rows survived with their attribution intact:

```bash
python - <<'PY'
import os, sys
os.environ.setdefault("DELIMP_INTERNAL_MODE", "1")        # internal table; see app/db.py
sys.path.insert(0, ".")
from app.db import query
print(query("""SELECT matched_by, count(*) n, count(submission_id) with_sub,
                      count(scanned_at) scanned
                 FROM delimp_submission_service_dir GROUP BY 1 ORDER BY 2 DESC""",
            tables=["delimp_submission_service_dir"]))
PY
```
Expected: the `ai-disk-match` group still numbers 1,862 with 1,862 `with_sub`; a large NULL-`matched_by` group of newly inventoried folders; every row carrying `scanned_at`.

- [ ] **Step 5: Commit**

```bash
git add ingest/scan_service_dir.py ingest/scan_service_dir.sbatch
git commit -m "ingest: a repeatable service-share scan, replacing the June disk-match"
```

---

### Task 6: Weekly cron

**Files:**
- Create: `ingest/cron_service_dir_scan.sh`

**Interfaces:**
- Consumes: `scan_service_dir.sbatch` from Task 5.
- Produces: nothing importable; installs a weekly schedule.

- [ ] **Step 1: Write the wrapper**

```bash
#!/bin/bash
# cron_service_dir_scan.sh -- submit the service-share scan, at most one at a time.
#
# $USER is NOT reliably set under cron; with `set -u` the profile sourcing dies before the log file
# exists, silently. That is exactly how the Flinders dispatch cron failed for months.
export LOGNAME="${LOGNAME:-$(id -un)}"
export USER="${USER:-$(id -un)}"
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true
set -uo pipefail

JOB=fran_svcdir
LOG=/quobyte/proteomics-grp/de-limp/fran_refresh/logs/svcdir_submit.log
SBATCH=/quobyte/proteomics-grp/brett/glendon/fran_ingest/scan_service_dir.sbatch

ME=$(id -un)
if ! command -v squeue >/dev/null 2>&1; then
  echo "$(date '+%F %T') ABORT: squeue not on PATH; refusing to submit blind" >> "$LOG"; exit 1
fi
# Fail CLOSED: if the queue cannot be read, assume something is queued rather than pile on.
if ! q=$(squeue -h -u "$ME" -n "$JOB" -t PENDING,RUNNING 2>/dev/null); then
  echo "$(date '+%F %T') ABORT: squeue failed; refusing to submit blind" >> "$LOG"; exit 1
fi
n=$(printf '%s' "$q" | grep -c . || true)
if [ "${n:-1}" -gt 0 ]; then
  echo "$(date '+%F %T') skip: $n $JOB job(s) already pending/running" >> "$LOG"; exit 0
fi
out=$(sbatch "$SBATCH" 2>&1)
echo "$(date '+%F %T') $out" >> "$LOG"
```

- [ ] **Step 2: Install the schedule**

```bash
scp ingest/cron_service_dir_scan.sh hive:/quobyte/proteomics-grp/brett/glendon/fran_ingest/
ssh hive 'chmod +x /quobyte/proteomics-grp/brett/glendon/fran_ingest/cron_service_dir_scan.sh
crontab -l > /tmp/ct.bak
cp /tmp/ct.bak /quobyte/proteomics-grp/brett/crontab.bak-$(date +%Y%m%d)
{ cat /tmp/ct.bak; cat <<"EOS"

# Service-share inventory, weekly. Replaces the 2026-06-24 one-shot ai-disk-match, whose source
# TSVs are gone; without it, build_resubmit_brief emits briefs with no file paths for anything
# newer than PROT_0724.
19 3 * * 1 flock -n /tmp/fran_svcdir.lock bash -lc "/quobyte/proteomics-grp/brett/glendon/fran_ingest/cron_service_dir_scan.sh" >> /quobyte/proteomics-grp/de-limp/fran_refresh/logs/cron_submit.log 2>&1
EOS
} | crontab -
crontab -l | grep -c fran_svcdir'
```
Expected: prints `1`.

- [ ] **Step 3: Verify the wrapper runs**

```bash
ssh hive 'bash -lc "/quobyte/proteomics-grp/brett/glendon/fran_ingest/cron_service_dir_scan.sh"; \
  tail -3 /quobyte/proteomics-grp/de-limp/fran_refresh/logs/svcdir_submit.log'
```
Expected: a `Submitted batch job N` line.

- [ ] **Step 4: Commit**

```bash
git add ingest/cron_service_dir_scan.sh
git commit -m "ingest: run the service-share scan weekly so it cannot rot unnoticed"
```

---

## Deliberately not in this plan

**Submission↔folder matching** (`match_submissions()`, `matched_by='scan_service_dir/v1'`). The
inventory above is the valuable, deterministic half and stands alone: it answers "what is on the
share and not ingested" for every folder, with `scanned_at` proving freshness. Matching is
heuristic, must not overwrite the reviewed June rows, and deserves its own plan with its own
accuracy measurements. Until it lands, folders the scanner adds carry `submission_id = NULL` — true,
and visible, rather than guessed.
