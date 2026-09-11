# Heatmap Filters + PTM Rollup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a biologist filter the search-page heatmap to the proteins they care about — above all, the ones carrying a modification.

**Architecture:** A precomputed `delimp_search_protein_ptm` table supplies PTM flags (live computation measured at 16–91 s per search, impossible on a public auto-firing endpoint). Four further filters ride the aggregate the matrix already runs, at no extra cost. All filtering happens in SQL *before* the top-N cut.

**Tech Stack:** Python 3.13, FastAPI, psycopg2, PostgreSQL (PG Farm), vanilla JS (no framework, no build step).

**Spec:** `docs/superpowers/specs/2026-09-11-heatmap-filters-and-ptm-rollup-design.md`

**Worktree:** `/Users/brettphinney/Documents/FRAN-filters`, branch `heatmap-filters`, based on `main`.

## Global Constraints

- Every `query()` passes `tables=[...]`. `_assert_allowlisted()` is the first statement of `query()`'s body (`app/db.py:383`).
- `privacy.redact(obj, reveal)` sanitizes string VALUES under `_FILE_KEYS`. It NEVER renames dict KEYS. Nothing filename-shaped may become a dict key.
- Read `modified_seq_proforma`. NEVER `mods` (1.43% populated; its GIN index is dead by construction).
- The matrix endpoint is **PUBLIC, anonymous, and auto-fires on every search-page view**. It is cached in `SLOW_CACHE` (30 min) with key `f"matrix_{search_id}_{mode}_{limit}"`. Any new input to the query MUST enter that key, or two filter states serve each other's results.
- `n_unique_peptides` on `delimp_proteins` is **PER-RUN**. Summing it has overstated a peptide count by up to 56×. Use `max()` and describe it as "in at least one run".
- Filters apply **before** the `LIMIT`, never client-side. Under 1% of proteins carry phospho; filtering the selected 50 would leave two or three.
- **BATCH the backfill; never loop per search.** Measured: 12 searches in one pass = 13.7 s (1.1 s/search); the same searches individually = 16–91 s each.
- Every test must be proven able to fail. Show the red output, then the green.

---

### Task 1: The rollup table and its refresh script

**Files:**
- Create: `ingest/refresh_search_ptm.py`
- Modify: `schema/fran_schema.sql` (add the table DDL)
- Modify: `app/db.py` (add `delimp_search_protein_ptm` to `PUBLIC_TABLES`)
- Test: `tests/test_search_ptm_rollup.py`

**Interfaces:**
- Produces: table `delimp_search_protein_ptm(search_id UUID, protein_group TEXT, has_ptm BOOL NOT NULL, has_phospho BOOL NOT NULL, has_glygly BOOL NOT NULL, n_mod_precursors INT NOT NULL, computed_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (search_id, protein_group))`.
- Produces: `refresh_search_ptm.py --limit N` (incremental, default) and `--rebuild` (all searches).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_search_ptm_rollup.py
import os, sys
os.environ.setdefault("DELIMP_PG_TOKEN_FILE", "/Users/brettphinney/.pgfarm_token")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app.db import query                               # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

PHOS_SEARCH = "2c4911a3-79fd-5367-bdd0-ee85a16cd25b"   # Arabidopsis STY phospho
GG_SEARCH   = "5d629050-4f68-5e43-8e1f-48bbc3fc0b8f"   # Bennett_Penn_Ubiq, 61.6% GlyGly
RS41        = "P92966"                                  # 6 phosphopeptides, 460 precursors

rows = query("SELECT * FROM delimp_search_protein_ptm WHERE search_id=%s AND protein_group=%s",
             (PHOS_SEARCH, RS41), tables=["delimp_search_protein_ptm"])
check("RS41 has a row in the phospho search", len(rows) == 1, str(len(rows)))
if rows:
    check("RS41 is flagged has_phospho", rows[0]["has_phospho"] is True, str(rows[0]))
    check("RS41 is flagged has_ptm", rows[0]["has_ptm"] is True, str(rows[0]))

# THE GLYGLY WITNESS. Historical rows store the literal `[GlyGly (K)]`, NOT [UNIMOD:121]. A
# predicate matching only the normalised form finds ZERO here while the search is 61.6% GG.
n_gg = query("""SELECT count(*) AS n FROM delimp_search_protein_ptm
                 WHERE search_id=%s AND has_glygly""", (GG_SEARCH,),
             tables=["delimp_search_protein_ptm"], fetch="val")
check("the Bennett_Penn ubiquitin search has thousands of GlyGly proteins",
      (n_gg or 0) > 1000, f"has_glygly rows={n_gg}")

# DISCRIMINATOR: flags must not be uniformly true. A rollup that set has_ptm on every row would
# pass every check above and make the filter useless.
tot, with_ptm = query("""SELECT count(*) AS t, count(*) FILTER (WHERE has_ptm) AS w
                           FROM delimp_search_protein_ptm WHERE search_id=%s""",
                      (PHOS_SEARCH,), tables=["delimp_search_protein_ptm"], fetch="one").values()
check("has_ptm is selective, not uniformly true", 0 < with_ptm < tot, f"{with_ptm} of {tot}")
check("has_phospho is rarer than has_ptm (phospho is a subset)",
      query("""SELECT count(*) FILTER (WHERE has_phospho) < count(*) FILTER (WHERE has_ptm) AS ok
                 FROM delimp_search_protein_ptm WHERE search_id=%s""",
            (PHOS_SEARCH,), tables=["delimp_search_protein_ptm"], fetch="val") is True)

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `DELIMP_PG_TOKEN_FILE=/Users/brettphinney/.pgfarm_token python3 tests/test_search_ptm_rollup.py`
Expected: FAIL — `GovernanceError: Table 'delimp_search_protein_ptm' is not in the public-layer allowlist.`

- [ ] **Step 3: Add the table to the schema and the allowlist**

In `schema/fran_schema.sql`, alongside the other `CREATE TABLE IF NOT EXISTS` blocks:

```sql
CREATE TABLE IF NOT EXISTS delimp_search_protein_ptm (
    "search_id" uuid NOT NULL,
    "protein_group" text NOT NULL,
    "has_ptm" boolean NOT NULL,
    "has_phospho" boolean NOT NULL,
    "has_glygly" boolean NOT NULL,
    "n_mod_precursors" integer NOT NULL,
    "computed_at" timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY ("search_id", "protein_group")
);
```

In `app/db.py`, add `"delimp_search_protein_ptm",` to `PUBLIC_TABLES`. It holds only ids, booleans and counts — no filename, path, or person — so it is public-safe.

- [ ] **Step 4: Write the refresh script**

```python
#!/usr/bin/env python3
"""refresh_search_ptm.py — per-(search, protein_group) modification flags for the heatmap filter.

WHY THIS TABLE EXISTS: computing "does this protein carry a modification" live costs 16.4 s on a
10-sample search and 90.9 s on a 21-sample one. The matrix endpoint it would serve is PUBLIC,
anonymous, auto-fires on every search-page view and already costs 2.8-8.9 s. So the flags are
precomputed here.

WHY IT BATCHES. Measured: 12 searches in ONE pass = 13.7 s (1.1 s each). The SAME searches queried
individually = 16-91 s each. One batched pass is a single sequential scan of delimp_precursors;
a per-search loop is thousands of index lookups and sorts. A naive loop over 2,086 searches would
run 9-53 HOURS against ~40 minutes batched. Do not "simplify" this into a loop.

Run from ingest/fran_mv_refresh.sbatch on the existing weekly schedule. Do NOT add a new cron.
"""
from __future__ import annotations
import argparse, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app.db import query                                          # noqa: E402

# GlyGly matches BOTH spellings on purpose. Historical Spectronaut rows store the literal
# `[GlyGly (K)]` because ingest/spectronaut_to_corpus.py's _MOD_UNIMOD lacked GlyGly until
# 2026-09-10; only re-ingested rows carry [UNIMOD:121]. Matching one form silently misses
# ~750,000 ubiquitin remnants in the Bennett_Penn searches.
SQL = """
INSERT INTO delimp_search_protein_ptm
      (search_id, protein_group, has_ptm, has_phospho, has_glygly, n_mod_precursors, computed_at)
SELECT p.search_id,
       p.protein_group,
       bool_or(p.n_mods > 0),
       bool_or(p.modified_seq_proforma LIKE '%%UNIMOD:21%%'),
       bool_or(p.modified_seq_proforma ILIKE '%%glygly%%'
            OR p.modified_seq_proforma LIKE '%%UNIMOD:121%%'),
       count(*) FILTER (WHERE p.n_mods > 0),
       now()
  FROM delimp_precursors p
 WHERE p.search_id = ANY(%(ids)s::uuid[])
   AND p.protein_group IS NOT NULL
 GROUP BY p.search_id, p.protein_group
ON CONFLICT (search_id, protein_group) DO UPDATE SET
       has_ptm          = EXCLUDED.has_ptm,
       has_phospho      = EXCLUDED.has_phospho,
       has_glygly       = EXCLUDED.has_glygly,
       n_mod_precursors = EXCLUDED.n_mod_precursors,
       computed_at      = EXCLUDED.computed_at
"""

PENDING = """
SELECT s.id FROM delimp_searches s
 WHERE NOT EXISTS (SELECT 1 FROM delimp_search_protein_ptm t WHERE t.search_id = s.id)
 LIMIT %(lim)s
"""

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=50, help="searches per batched query")
    ap.add_argument("--limit", type=int, default=100000, help="max searches this run")
    ap.add_argument("--rebuild", action="store_true", help="reprocess every search, not just new ones")
    a = ap.parse_args()

    if a.rebuild:
        ids = [r["id"] for r in query("SELECT id FROM delimp_searches", tables=["delimp_searches"])]
    else:
        ids = [r["id"] for r in query(PENDING, {"lim": a.limit}, tables=["delimp_searches",
                                      "delimp_search_protein_ptm"])]
    if not ids:
        print("nothing to do — every search already has rows"); return 0

    print(f"{len(ids)} search(es) to process, {a.batch} per batch")
    done = 0
    for i in range(0, len(ids), a.batch):
        chunk = ids[i:i + a.batch]
        t = time.time()
        query(SQL, {"ids": chunk}, tables=["delimp_search_protein_ptm", "delimp_precursors"],
              fetch=None, timeout_ms=900_000)
        done += len(chunk)
        print(f"  {done}/{len(ids)}  ({time.time() - t:.1f}s for {len(chunk)})", flush=True)
    print("done")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Backfill the two fixture searches only, then run the test**

Do NOT run the full backfill yet — that is Task 4, and it belongs on Hive.

```bash
DELIMP_PG_TOKEN_FILE=/Users/brettphinney/.pgfarm_token python3 - <<'PY'
import sys; sys.path.insert(0, ".")
from ingest.refresh_search_ptm import SQL
from app.db import query
query(SQL, {"ids": ["2c4911a3-79fd-5367-bdd0-ee85a16cd25b",
                    "5d629050-4f68-5e43-8e1f-48bbc3fc0b8f"]},
      tables=["delimp_search_protein_ptm", "delimp_precursors"], fetch=None, timeout_ms=900_000)
print("two fixture searches populated")
PY
DELIMP_PG_TOKEN_FILE=/Users/brettphinney/.pgfarm_token python3 tests/test_search_ptm_rollup.py
```
Expected: ALL PASS.

- [ ] **Step 6: Prove the GlyGly witness has teeth**

Mandatory. Temporarily drop the `ILIKE '%%glygly%%'` arm, leaving only `UNIMOD:121`; re-run the
two-search backfill and the test.
Expected: `the Bennett_Penn ubiquitin search has thousands of GlyGly proteins` FAILS with
`has_glygly rows=0`. Restore, re-run, confirm PASS. Paste both outputs.

- [ ] **Step 7: Commit**

```bash
git add ingest/refresh_search_ptm.py schema/fran_schema.sql app/db.py tests/test_search_ptm_rollup.py
git commit -m "feat: per-search protein PTM flags, batched because per-search is 20-80x slower"
```

---

### Task 2: Filters in the matrix query

**Files:**
- Modify: `app/queries.py` — `search_protein_matrix()`
- Modify: `app/main.py` — `api_search_matrix`
- Test: `tests/test_matrix_filters.py`

**Interfaces:**
- Consumes: `delimp_search_protein_ptm` from Task 1.
- Produces: `search_protein_matrix(search_id, mode, limit, filters="")` where `filters` is a comma-separated subset of `{ptm, phospho, glygly, noncontam, multipeptide, complete, patchy, unique}`. Unknown tokens are ignored. Response gains `"filters"` (the normalized list applied) and `"n_rankable"` continues to mean *after* filtering.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_matrix_filters.py
import os, sys
os.environ.setdefault("DELIMP_PG_TOKEN_FILE", "/Users/brettphinney/.pgfarm_token")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import queries                                # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

PHOS = "2c4911a3-79fd-5367-bdd0-ee85a16cd25b"

base = queries.search_protein_matrix(PHOS, mode="abundance", limit=50)
phos = queries.search_protein_matrix(PHOS, mode="abundance", limit=50, filters="phospho")

check("unfiltered returns rows", len(base["proteins"]) > 0, str(len(base["proteins"])))
check("phospho filter returns rows", len(phos["proteins"]) > 0, str(len(phos["proteins"])))

# THE FILTER MUST ACTUALLY CHANGE THE SET. A filter applied after the top-N cut, or ignored
# entirely, yields the same genes — and every other assertion here would still pass.
check("the phospho filter changes which genes are returned",
      {p["gene"] for p in base["proteins"]} != {p["gene"] for p in phos["proteins"]},
      "identical gene sets — filter had no effect")

# APPLIED BEFORE THE LIMIT, not after. Only 62 of 6,284 protein groups carry phospho, so filtering
# the already-selected 50 would leave a handful. Anything close to 50 proves pre-LIMIT filtering.
check("phospho filter fills the page (pre-LIMIT), not a remnant of it",
      len(phos["proteins"]) >= 20, f'{len(phos["proteins"])} rows')

# n_rankable must describe the FILTERED population, or the header lies.
check("n_rankable shrinks under a filter",
      phos.get("n_rankable", 0) < base.get("n_rankable", 0),
      f'{phos.get("n_rankable")} vs {base.get("n_rankable")}')

nc = queries.search_protein_matrix(PHOS, mode="abundance", limit=50, filters="noncontam")
check("noncontam returns no contaminants",
      all(not p.get("is_contaminant") for p in nc["proteins"]),
      str([p["gene"] for p in nc["proteins"] if p.get("is_contaminant")][:3]))

comp = queries.search_protein_matrix(PHOS, mode="abundance", limit=50, filters="complete")
n_tot = comp["n_samples_total"]
check("complete returns only proteins seen in every sample",
      all(p["n_samples"] == n_tot for p in comp["proteins"]),
      str([(p["gene"], p["n_samples"]) for p in comp["proteins"] if p["n_samples"] != n_tot][:3]))

patchy = queries.search_protein_matrix(PHOS, mode="abundance", limit=50, filters="patchy")
check("patchy and complete are disjoint",
      not ({p["gene"] for p in patchy["proteins"]} & {p["gene"] for p in comp["proteins"]}))

check("an unknown filter token is ignored, not fatal",
      len(queries.search_protein_matrix(PHOS, mode="abundance", limit=5,
                                        filters="not_a_filter")["proteins"]) > 0)

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `DELIMP_PG_TOKEN_FILE=/Users/brettphinney/.pgfarm_token python3 tests/test_matrix_filters.py`
Expected: FAIL — `search_protein_matrix() got an unexpected keyword argument 'filters'`.

- [ ] **Step 3: Implement the filters**

In `search_protein_matrix()`, add a `filters: str = ""` parameter. Normalize it FIRST, before it
reaches the cache key:

```python
_MATRIX_FILTERS = ("ptm", "phospho", "glygly", "noncontam",
                   "multipeptide", "complete", "patchy", "unique")

# Normalized, deduped, sorted -> a bounded key space. The raw string must never enter the cache
# key: SLOW_CACHE has no eviction, so a caller-controlled free-text dimension is unbounded growth.
_f = sorted({t for t in (filters or "").lower().split(",") if t in _MATRIX_FILTERS})
key = f"matrix_{search_id}_{mode}_{limit}_{'+'.join(_f)}"
```

`per_sample` gains `max(n_unique_peptides) AS max_pep` (free — same scan). `agg` gains
`max(max_pep) AS max_peptides`. The PTM flags join in at the `agg` level:

```sql
        agg AS (
          SELECT gene,
                 max(protein_group)                       AS protein_group,
                 count(*)                                 AS n_samples,
                 avg(v)                                   AS mean_int,
                 stddev_pop(v) / NULLIF(avg(v), 0)        AS cv,
                 bool_or(is_contaminant)                  AS is_contaminant,
                 max(max_pep)                             AS max_peptides
            FROM per_sample
           GROUP BY gene
          HAVING count(*) >= %(floor)s)
        SELECT a.*, r.n_searches AS reach,
               CASE WHEN r.n_pct_searches >= %(minpct)s THEN r.mean_pct_rank END AS mean_pct_rank,
               count(*) OVER ()                          AS n_rankable
          FROM agg a
          LEFT JOIN delimp_protein_corpus_reach r ON r.gene = upper(a.gene)
          {ptm_join}
         WHERE TRUE {filter_sql}
         ORDER BY {order}
         LIMIT %(limit)s
```

where, built from `_f` (never from raw input):

```python
    ptm_join, conds = "", []
    if {"ptm", "phospho", "glygly"} & set(_f):
        # One row per (search, protein_group); a gene may map to several, so EXISTS over the
        # gene's groups rather than a join that would multiply rows.
        ptm_join = ""
        col = {"ptm": "has_ptm", "phospho": "has_phospho", "glygly": "has_glygly"}
        for t in ("ptm", "phospho", "glygly"):
            if t in _f:
                conds.append(f"""EXISTS (SELECT 1 FROM delimp_search_protein_ptm m
                                          WHERE m.search_id = %(sid)s
                                            AND m.protein_group = a.protein_group
                                            AND m.{col[t]})""")
    if "noncontam" in _f:    conds.append("NOT a.is_contaminant")
    if "multipeptide" in _f: conds.append("a.max_peptides >= 2")
    if "complete" in _f:     conds.append("a.n_samples = %(ntot)s")
    if "patchy" in _f:       conds.append("a.n_samples < %(ntot)s")
    if "unique" in _f:       conds.append("r.n_searches = 1")
    filter_sql = ("AND " + " AND ".join(conds)) if conds else ""
```

Add `"ntot": n_samples_total` to `params`, and `"delimp_search_protein_ptm"` to the `tables=[...]`
list whenever a PTM filter is active. Return `"filters": _f` in the result dict.

In `app/main.py`, `api_search_matrix` gains `filters: str = ""` and passes it through. It is
normalized inside the query function, so the route needs no validation of its own — but it must
NOT be interpolated anywhere.

- [ ] **Step 4: Run the test**

Expected: ALL PASS.

- [ ] **Step 5: Prove the pre-LIMIT assertion has teeth**

Mandatory, and it is the one that matters. Temporarily move the filtering client-side — apply
`filters` to the returned `proteins` list AFTER the query instead of inside it.
Expected: `phospho filter fills the page (pre-LIMIT), not a remnant of it` FAILS with a single-digit
row count, while "changes which genes are returned" still PASSES. That contrast is the point:
post-filtering looks like it works. Restore and confirm green. Paste both.

- [ ] **Step 6: Prove the cache key includes the filter state**

```bash
DELIMP_PG_TOKEN_FILE=/Users/brettphinney/.pgfarm_token python3 - <<'PY'
import sys; sys.path.insert(0, ".")
from app import queries
S = "2c4911a3-79fd-5367-bdd0-ee85a16cd25b"
a = queries.search_protein_matrix(S, mode="abundance", limit=50)
b = queries.search_protein_matrix(S, mode="abundance", limit=50, filters="phospho")
assert {p["gene"] for p in a["proteins"]} != {p["gene"] for p in b["proteins"]}, \
    "CACHE COLLISION: the filtered call returned the unfiltered cached result"
print("cache key separates filter states")
PY
```

- [ ] **Step 7: Commit**

```bash
git add app/queries.py app/main.py tests/test_matrix_filters.py
git commit -m "feat: filter the heatmap in SQL, before the top-N cut"
```

---

### Task 3: The filter UI

**Files:**
- Modify: `app/static/app.js` — `renderSearchMatrix()`

**Interfaces:**
- Consumes: the `filters` query parameter and the `filters` / `n_rankable` response fields from Task 2.

- [ ] **Step 1: Add the filter state and control**

A module-level `let _hmFilters = new Set();` beside the existing `_hmMode`. A "Filter" button next
to the ranking-mode buttons, showing a count when any are active (`Filter · 2`). Clicking toggles a
panel of checkboxes, grouped:

```
MODIFICATIONS   [ ] Has any modification   [ ] Phospho   [ ] Ubiquitin (GlyGly)
CONFIDENCE      [ ] Hide contaminants      [ ] ≥2 peptides (in at least one run)
DETECTION       [ ] In every sample        [ ] Patchy (not in all)
CORPUS          [ ] Unique to this experiment
```

Each checkbox calls `renderSearchMatrix(searchId, _hmMode)` after updating the set, exactly as the
mode buttons already do. Use `escJs()` for any interpolated id.

- [ ] **Step 2: Send the filters and report the population honestly**

Append `&filters=${[..._hmFilters].join(',')}` to the fetch. Change the footer to state the
filtered population:
- no filters: unchanged from today.
- filters active: `Showing 50 of 62 matching · 4,005 rankable` — the matching count is
  `n_rankable` from the filtered response.

- [ ] **Step 3: Handle the empty result without lying**

If a filter combination returns zero proteins, say which filters are active and offer to clear
them. It must never render as "this search has no proteins" — that is the absent-data-looks-like-
clean-data failure this codebase keeps hitting.

- [ ] **Step 4: Verify in the browser**

Serve this worktree on port 8891 (8893-8899 are in use):

```bash
cd /Users/brettphinney/Documents/FRAN-filters
DELIMP_PG_TOKEN_FILE=/Users/brettphinney/.pgfarm_token DELIMP_INTERNAL_MODE=1 \
  python3 -m uvicorn app.main:app --port 8891 --host 127.0.0.1
```

Open `http://127.0.0.1:8891/#/run/2c4911a3-79fd-5367-bdd0-ee85a16cd25b`, tick **Phospho**, and
confirm the heatmap fills with phosphoproteins (RS41, SR45, SCL30, RS2Z33 should be among them).

**Prove the served JS is the new code in the SAME check as the measurement** — a browser will
happily serve a cached `app.js` and every observation will describe the old file. Assert that a
string you just added is present in the live function source in the same `javascript_exec` call
that counts the rendered rows.

- [ ] **Step 5: Commit**

```bash
git add app/static/app.js
git commit -m "site: filter button on the heatmap"
```

---

### Task 4: Backfill and schedule

**Files:**
- Modify: `ingest/fran_mv_refresh.sbatch` (add the refresh call)

- [ ] **Step 1: Add the refresh to the existing weekly job**

`ingest/fran_mv_refresh.sbatch` already runs weekly on Hive with node pinning, a lock and a working
environment. Add `refresh_search_ptm.py` to its payload, matching how `refresh_corpus_reach.py` is
invoked there. Report its return code alongside the others — that file already had a bug where two
payloads reported only the last one's status.

**Do NOT create a new cron. Do NOT install or modify anything on Hive.** Print the single `scp`
line for a human to run.

- [ ] **Step 2: Run the backfill as a batched job**

Measured rate: 1.1 s per search batched, ~40 minutes for all 2,086. This is a WRITE against
production and must be run deliberately, not as a side effect of a test. Print the command for a
human to run rather than running it:

```bash
python3 ingest/refresh_search_ptm.py --batch 50
```

- [ ] **Step 3: Commit**

```bash
git add ingest/fran_mv_refresh.sbatch
git commit -m "ingest: refresh the PTM rollup on the existing weekly job"
```

---

## Self-Review

**Spec coverage.** Rollup table (Task 1), the four free filters and the three PTM filters (Task 2), pre-LIMIT filtering (Task 2 Step 5), the honest header (Task 3 Step 2), the empty-result behaviour (Task 3 Step 3), `has_glygly` matching both spellings (Task 1, with its own teeth-proof), batching (Task 1 Step 4 and Task 4 Step 2), and the refresh riding the existing job (Task 4). All present.

**Placeholders.** None — every code step carries the actual code.

**Type consistency.** `filters` is a comma-separated string end to end: UI → query param → `search_protein_matrix(filters=...)` → normalized list → response `"filters"`. `max_peptides` is produced in `per_sample` as `max_pep`, rolled up in `agg` as `max_peptides`, and read in the filter condition as `a.max_peptides`. The rollup is keyed `(search_id, protein_group)` and consumed by `EXISTS` on `a.protein_group`, which is `max(protein_group)` per gene — see the known gap below.

**Known gap, deliberate.** `agg.protein_group` is `max(protein_group)` — one representative when a gene maps to several groups (1.8% of genes on the 21-sample fixture). A gene whose phospho sits on a non-representative group will be missed by the `EXISTS`. The honest fix is to roll the flags up by gene inside `per_sample`, which costs a join at query time; it is not done here because the filter is a discovery aid rather than a quantitative claim, and a missed row is visible (the gene is simply absent) rather than wrong. If the filter is later used to *count* phosphoproteins, fix this first.
