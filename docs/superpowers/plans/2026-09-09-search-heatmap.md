# Search Heatmap and Peptide Map Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a search page show its result — a protein × sample heatmap below the existing Runs table, and a per-protein peptide map comparing this experiment against the whole FRAN corpus.

**Architecture:** `delimp_proteins` is already a protein × sample matrix, so the heatmap is a query plus a renderer, not new data. One precomputed table (`delimp_protein_corpus_reach`) supplies the two corpus-wide facts that are too slow to compute live. Coverage extends the existing `/api/protein/{pg}/coverage` endpoint with an optional `search_id` scope rather than being rebuilt.

**Tech Stack:** Python 3 / FastAPI / psycopg2 / PostgreSQL (PG Farm), vanilla JS in `app/static/app.js`, Tailwind-style utility classes already in the page. No new dependencies, no build step, no JS test framework.

**Spec:** `docs/superpowers/specs/2026-09-09-search-heatmap-and-coverage-design.md`

## Global Constraints

- **USE `intensity`, NOT `normalized_intensity`.** Measured 2026-09-09: `normalized_intensity` is populated for **75 of 2,086 searches (4%)**; `intensity` for **2,084 (100%)**. A panel keyed on the normalized column renders for 4% of searches and is blank for the rest, while looking perfect on `PROT_0793_search_mouse`, which has both. If you add any ranking or colour that reads a column, state which column and check it against this fact.
- **Corpus reach is keyed on `upper(gene)`, never on `protein_group` and never case-sensitively.** Measured: 47% of `protein_group` strings are seen in exactly one search (they vary between FASTAs); and `Aldoa`=278 vs `ALDOA`=1,278, `Actb`=93 vs `ACTB`=1,007, `Calm1`=1 vs `CALM1`=30, because symbols are capitalised per species.
- **Every ranking applies a presence floor of `>= 0.2 * n_samples`.** Without it `Or6c75` (1 of 222 samples, 85.4 billion mean intensity) outranks albumin (219 samples).
- **Absence is never zero.** A protein with no reach row renders "not computed", never "seen in 0 searches". A (protein, sample) pair with no row renders "not identified", never 0.
- **Every `query()` call passes `tables=[...]` naming every table it touches.** That list is the governance allowlist check, not documentation.
- **Do not break the three exports:** `/api/export/diann_report/{search_id}` (report.parquet → DE-LIMP/limpa), `/api/export/research_brief/{search_id}`, `/api/export/resubmit_brief/{submission_id}`.
- **Match existing UI vocabulary exactly:** `glass` + `card`, 18px radii, `text-accent-400` (#FFCF40), `kpi-num`, the existing `table()`/`stat()`/`esc()`/`fmt()` helpers. The heatmap's own colour scale must be colour-blind safe (viridis-like, never red/green).
- **PROVE EVERY TEST CAN FAIL.** Break the behaviour, run, paste the FAIL, restore, confirm PASS. Every teeth-proof must END with `git diff` showing no unintended production change — an earlier plan in this repo shipped a bug by restoring only half a `WHERE` clause while the suite stayed green.
- **Run tests as:** `DELIMP_PG_TOKEN_FILE=/Users/brettphinney/.pgfarm_token python3 tests/<name>.py` from the repo root. Use the ABSOLUTE token path; Python does not expand `~`. Read-only against the live PG Farm database. **Never write to it** except the one migration in Task 1 and the refresh in Task 2.

---

## File Structure

| file | responsibility |
|---|---|
| `ingest/migrations/2026-09-09_protein_corpus_reach.sql` | DDL record for the new table (applied inline, see Task 1) |
| `ingest/refresh_corpus_reach.py` | builds/refreshes `delimp_protein_corpus_reach`; mirrors `ingest/refresh_leaderboards.py` |
| `app/queries.py` | `search_protein_matrix()`; `protein_coverage_peptides()` gains `search_id` |
| `app/main.py` | `GET /api/search/{search_id}/matrix`; `coverage` gains `?search_id=` |
| `app/static/app.js` | `renderSearchMatrix()` + `renderPeptideMap()`, called from `renderSearchDetail` |
| `tests/test_corpus_reach.py` | Task 1 |
| `tests/test_search_matrix.py` | Tasks 2–3 |
| `tests/test_coverage_scope.py` | Task 5 |

---

### Task 1: The corpus-reach table

**Files:**
- Create: `ingest/migrations/2026-09-09_protein_corpus_reach.sql`
- Create: `tests/test_corpus_reach.py`

**Interfaces:**
- Produces: table `delimp_protein_corpus_reach` — `gene TEXT PRIMARY KEY`, `n_searches INTEGER`, `n_samples INTEGER`, `mean_pct_rank REAL`, `n_pct_searches INTEGER`, `computed_at TIMESTAMPTZ`. `gene` holds the **UPPERCASED** symbol. Task 2 joins on `upper(p.gene) = r.gene`.

- [ ] **Step 1: Write the failing test**

```python
"""delimp_protein_corpus_reach — the two corpus-wide facts the heatmap needs.

Keyed on UPPERCASED gene, not protein_group and not raw gene. Measured 2026-09-09:
47% of protein_group strings are seen in exactly one search (they vary between FASTAs), and
Aldoa=278 vs ALDOA=1278 because symbols are capitalised per species. Either mistake makes
"rarity" measure annotation style instead of biology.

Run:  DELIMP_PG_TOKEN_FILE=/Users/brettphinney/.pgfarm_token python3 tests/test_corpus_reach.py
"""
import os, sys
os.environ.setdefault("DELIMP_INTERNAL_MODE", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app.db import query                                    # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)

T = "delimp_protein_corpus_reach"
cols = {r["column_name"]: r for r in query(
    "SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name=%s",
    (T,), tables=[T])}
for c in ("gene", "n_searches", "n_samples", "mean_pct_rank", "n_pct_searches", "computed_at"):
    check(f"{c} column exists", c in cols, sorted(cols))

idx = query("SELECT indexdef FROM pg_indexes WHERE tablename=%s", (T,), tables=[T])
check("gene is the primary key",
      any("UNIQUE" in r["indexdef"] and "(gene)" in r["indexdef"] for r in idx),
      " | ".join(r["indexdef"] for r in idx)[:200])

n = query(f"SELECT count(*) AS n FROM {T}", tables=[T], fetch="val") or 0
check("table is populated", n > 100_000, str(n))

# Keys are UPPERCASE. A single lowercase key means the build forgot upper().
lower = query(f"SELECT count(*) AS n FROM {T} WHERE gene <> upper(gene)", tables=[T], fetch="val") or 0
check("every key is uppercased", lower == 0, f"{lower} non-uppercase keys")

# Case-merging actually happened: ALB must exceed either spelling alone (273 / 1789 measured).
alb = query(f"SELECT n_searches FROM {T} WHERE gene='ALB'", tables=[T], fetch="val")
check("ALB reach is case-merged (>1000, not ~273)", (alb or 0) > 1000, str(alb))

# ...and genuinely mouse-specific genes stay rare — the merge must not flatten everything.
mup2 = query(f"SELECT n_searches FROM {T} WHERE gene='MUP2'", tables=[T], fetch="val")
check("MUP2 stays rare after merging (<200)", 0 < (mup2 or 0) < 200, str(mup2))

# The percentile is only meaningful with enough contributing searches.
alb_pct = query(f"SELECT mean_pct_rank FROM {T} WHERE gene='ALB'", tables=[T], fetch="val")
check("ALB sits near the top of a typical run (mean_pct_rank > 0.8)", (alb_pct or 0) > 0.8, str(alb_pct))

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `DELIMP_PG_TOKEN_FILE=/Users/brettphinney/.pgfarm_token python3 tests/test_corpus_reach.py`
Expected: every check FAILS — the table does not exist. If any check PASSES, stop: something already created this table and you must understand what before proceeding.

- [ ] **Step 3: Write the migration file (record only)**

```sql
-- Two corpus-wide facts per gene, precomputed because both are far too slow to compute live:
-- the reach scan is 637 s over protein_group / 121 s over gene, and the percentile is 97 s.
--
-- KEYED ON upper(gene). Not protein_group: 47% of those strings are seen in exactly one search
-- because they vary between FASTAs, so rarity keyed on them measures FASTA style, not biology.
-- Not raw gene: symbols are capitalised per species (Aldoa=278 searches, ALDOA=1278), so a
-- mouse search would read as uniformly "rare" against a mostly-human corpus.
CREATE TABLE IF NOT EXISTS delimp_protein_corpus_reach (
    gene           TEXT PRIMARY KEY,   -- UPPERCASED symbol
    n_searches     INTEGER,            -- distinct searches that reported this gene
    n_samples      INTEGER,            -- distinct raw files
    mean_pct_rank  REAL,               -- mean percent_rank() of its intensity WITHIN a search
    n_pct_searches INTEGER,            -- searches contributing to mean_pct_rank
    computed_at    TIMESTAMPTZ
);
```

- [ ] **Step 4: Apply it inline and re-run the test**

Write the DDL INLINE in the apply command rather than executing the .sql file — this environment's
safety gate refuses executing file contents, while inline statements are visible to whoever approves
them. Save the .sql file anyway as the migration record.

```bash
cd /Users/brettphinney/Documents/FRAN
DELIMP_PG_TOKEN_FILE=/Users/brettphinney/.pgfarm_token python3 -c "
import sys; sys.path.insert(0, 'ingest')
from coreomics_import import _conn
con = _conn(); cur = con.cursor()
cur.execute('''CREATE TABLE IF NOT EXISTS delimp_protein_corpus_reach (
    gene TEXT PRIMARY KEY, n_searches INTEGER, n_samples INTEGER,
    mean_pct_rank REAL, n_pct_searches INTEGER, computed_at TIMESTAMPTZ)''')
con.commit(); print('created')
con.close()"
```

The population step is Task 2; the test still fails on "table is populated" after this. That is
expected — do not proceed past Task 2 with it failing.

- [ ] **Step 5: Add the table to the governance allowlist**

`app/db.py` gates `query()` on the table NAME before any SQL runs, so without this entry every
query against the new table raises `GovernanceError` — Step 2's predicted "every check FAILS" does
not happen; the test crashes uncaught instead. Add `"delimp_protein_corpus_reach"` to
**`PUBLIC_TABLES`**, in the same style as the neighbouring `delimp_mv_*` entries.

`PUBLIC_TABLES`, not `_INTERNAL_TABLES`, and the reason is not incidental: this table derives solely
from `delimp_proteins`, which is already public, so it creates no confidentiality that does not
already exist. Putting it in the internal list would break the read path for the public search page
this feature lives on.

- [ ] **Step 6: Commit**

```bash
git add ingest/migrations/2026-09-09_protein_corpus_reach.sql tests/test_corpus_reach.py app/db.py
git commit -m "ingest: a corpus-reach table keyed on the uppercased gene"
```

---

### Task 2: The refresh script that populates it

**Files:**
- Create: `ingest/refresh_corpus_reach.py`

**Interfaces:**
- Consumes: table from Task 1.
- Produces: a populated `delimp_protein_corpus_reach`, and `python ingest/refresh_corpus_reach.py` as the command a cron will run.

- [ ] **Step 1: Write the script**

Mirror `ingest/refresh_leaderboards.py` — same `_token()` helper, same long `statement_timeout`, same
"slow but offline" posture. Read it first; do not invent a different connection idiom.

```python
"""Refresh delimp_protein_corpus_reach — the corpus-wide facts behind the search heatmap.

Two things per gene, both far too slow for a web request (measured 2026-09-09 on PG Farm):
  n_searches / n_samples   121 s over 298,391 genes
  mean_pct_rank             97 s
Run on the same weekly schedule as refresh_leaderboards.py, after new searches land.

KEYED ON upper(gene). See the migration comment for why protein_group and raw gene are both wrong.
USES intensity, NOT normalized_intensity: the latter exists for 75 of 2,086 searches (4%).

Usage:  python refresh_corpus_reach.py
"""
from __future__ import annotations

import os
import sys
import time

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from coreomics_import import _conn                           # noqa: E402

# mean_pct_rank is meaningless off one search — a gene seen once scores a spurious 1.000 (measured:
# OR6C75 1.000 from one search, GM6133 0.033 from one). Readers must apply their own floor too; this
# column records how many searches contributed so they can.
SQL = """
WITH per AS (
  SELECT search_id, upper(gene) AS g,
         avg(intensity)          AS ai,
         count(DISTINCT raw_path) AS ns
    FROM delimp_proteins
   WHERE intensity > 0 AND NULLIF(gene,'') IS NOT NULL
   GROUP BY 1, 2),
rk AS (
  SELECT search_id, g, ns,
         percent_rank() OVER (PARTITION BY search_id ORDER BY ai) AS pr
    FROM per)
INSERT INTO delimp_protein_corpus_reach
      (gene, n_searches, n_samples, mean_pct_rank, n_pct_searches, computed_at)
SELECT g, count(*), sum(ns), avg(pr)::real, count(*), now()
  FROM rk GROUP BY g
ON CONFLICT (gene) DO UPDATE SET
  n_searches     = EXCLUDED.n_searches,
  n_samples      = EXCLUDED.n_samples,
  mean_pct_rank  = EXCLUDED.mean_pct_rank,
  n_pct_searches = EXCLUDED.n_pct_searches,
  computed_at    = EXCLUDED.computed_at;
"""


def main() -> int:
    con = _conn()
    con.autocommit = False
    with con.cursor() as cur:
        cur.execute("SET statement_timeout = '3600s'")
        t = time.time()
        cur.execute(SQL)
        n = cur.rowcount
        con.commit()
        print(f"delimp_protein_corpus_reach: {n:,} genes in {time.time() - t:.0f}s")
        cur.execute("SELECT count(*), max(computed_at) FROM delimp_protein_corpus_reach")
        total, when = cur.fetchone()
        print(f"table now holds {total:,} genes, computed_at {when}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run it**

```bash
cd /Users/brettphinney/Documents/FRAN
DELIMP_PG_TOKEN_FILE=/Users/brettphinney/.pgfarm_token python3 ingest/refresh_corpus_reach.py
```

Expected: roughly 3–5 minutes, and a count near 298,391 genes. Report the real number and the real
elapsed time in your report — do not repeat the estimate.

- [ ] **Step 3: Run the Task 1 test — it must now fully pass**

Run: `DELIMP_PG_TOKEN_FILE=/Users/brettphinney/.pgfarm_token python3 tests/test_corpus_reach.py`
Expected: `ALL PASS`, including `ALB` > 1000, `MUP2` < 200, and `ALB.mean_pct_rank` > 0.8.

If `MUP2` came back large, the build merged more than case — stop and investigate before continuing.

- [ ] **Step 4: Prove the test has teeth**

Run the SQL again with `upper(gene)` replaced by `gene` in the `per` CTE, into a scratch copy of the
script under `/tmp`, writing to a temp table, and confirm `ALB` there is ~273 not ~1789 — i.e. the
"case-merged" check would fail against a case-sensitive build. Paste both numbers. Do NOT write the
scratch build into `delimp_protein_corpus_reach`.

- [ ] **Step 5: Commit**

```bash
git add ingest/refresh_corpus_reach.py
git commit -m "ingest: build the corpus-reach table, case-merged and intensity-based"
```

---

### Task 3: `search_protein_matrix()`

**Files:**
- Modify: `app/queries.py` (add one function; touch nothing else)
- Create: `tests/test_search_matrix.py`

**Interfaces:**
- Consumes: `delimp_protein_corpus_reach` from Tasks 1–2.
- Produces:

```python
search_protein_matrix(search_id: str, mode: str = "cv", limit: int = 50) -> dict
# {"proteins": [{"gene", "protein_group", "n_samples", "is_contaminant",
#                "reach", "reach_pct_rank", "cells": {sample_id: float}}],
#  "samples": [{"id"}],
# NOTE: cells are keyed by the OPAQUE sample id ("s0", "s1", ...), never by a filename, and a
# sample row is that id and NOTHING else. privacy.redact() sanitises string VALUES under
# privacy._FILE_KEYS; it never renames KEYS, so a filename used as a dict key reaches the public
# tier untouched — that leak was found during Task 4. The endpoint is PUBLIC, so the follow-up at
# the merge gate went further and dropped raw_path/raw_basename/acquisition_date from the row
# entirely (nothing rendered them; acquisition_date is used only for the server-side sort). The
# response is now structurally incapable of carrying a filename rather than dependent on the
# sanitiser continuing to know the right key names. Anything added back here must be covered by
# privacy._FILE_KEYS AND by the real-path component scan in tests/test_internal_route_gate.py.
#  "mode", "limit", "n_proteins_total", "n_samples_total",
#  "floor_pct", "reach_computed_at"}
# mode ∈ {"cv", "abundance", "rarity", "corpus_abundance"}
```

- [ ] **Step 1: Write the failing test**

```python
"""search_protein_matrix — the heatmap's data.

Fixture: PROT_0793_search_mouse, 8221f5fc-492e-5c9d-a08d-542cfdb48791 — 480,123 rows,
6,388 proteins x 222 samples, every intensity populated. The largest search in the corpus.

Run:  DELIMP_PG_TOKEN_FILE=/Users/brettphinney/.pgfarm_token python3 tests/test_search_matrix.py
"""
import os, sys
os.environ.setdefault("DELIMP_INTERNAL_MODE", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import queries                                      # noqa: E402

SID = "8221f5fc-492e-5c9d-a08d-542cfdb48791"
FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)

d = queries.search_protein_matrix(SID, mode="cv", limit=50)
prots, samps = d.get("proteins") or [], d.get("samples") or []
check("returns 50 proteins", len(prots) == 50, str(len(prots)))
check("returns all 222 samples", len(samps) == 222, str(len(samps)))
check("reports the true protein total", d.get("n_proteins_total") == 6388, str(d.get("n_proteins_total")))
check("reports when reach was computed", d.get("reach_computed_at") is not None)

# THE PRESENCE FLOOR. Without it Or6c75 (1 of 222 samples, 85.4e9 mean) outranks albumin.
# Assert the floor held, not that some arbitrary gene is absent.
floor = 0.2 * len(samps)
worst = min(p["n_samples"] for p in prots)
check(f"every row clears the {int(floor)}-sample floor", worst >= floor, f"worst row has {worst}")

# ABUNDANCE MODE must be led by real abundant proteins, not sparse ones.
a = queries.search_protein_matrix(SID, mode="abundance", limit=50)
names = [p["gene"] for p in a["proteins"]]
check("abundance mode contains albumin", "Alb" in names, str(names[:8]))
check("abundance mode excludes the 1-sample outlier Or6c75", "Or6c75" not in names, str(names[:8]))

# CV MODE is ordered by variance, descending, over the whole returned page.
c = queries.search_protein_matrix(SID, mode="cv", limit=50)
cvs = [p.get("cv") for p in c["proteins"]]
check("cv mode is sorted descending across the whole page",
      all(cvs[i] >= cvs[i + 1] for i in range(len(cvs) - 1)), str(cvs[:4]))

# RARITY MODE must be corpus-scoped and ascending.
r = queries.search_protein_matrix(SID, mode="rarity", limit=50)
reach = [p.get("reach") for p in r["proteins"]]
check("rarity mode is sorted ascending by corpus reach",
      all((reach[i] or 0) <= (reach[i + 1] or 0) for i in range(len(reach) - 1)), str(reach[:6]))

# CORPUS-ABUNDANCE mode is the mean within-search percentile, descending, and must be led by
# genuinely abundant proteins. Measured corpus-wide: H4c1 .909, Hsp90ab1 .906, Gapdh .897, Alb .873.
ca = queries.search_protein_matrix(SID, mode="corpus_abundance", limit=50)
canames = [p["gene"] for p in ca["proteins"]]
check("corpus-abundance mode is led by housekeeping proteins",
      any(g in canames[:15] for g in ("Hsp90ab1", "Gapdh", "Alb", "Atp5f1a")), str(canames[:8]))

# CELLS. A missing (protein, sample) pair must be ABSENT, never 0 — absence is not zero.
zeros = sum(1 for p in prots for v in p["cells"].values() if v == 0)
check("no cell is stored as a literal zero", zeros == 0, f"{zeros} zero cells")
check("cells reference real sample keys",
      all(k in {s["basename"] for s in samps} for p in prots for k in p["cells"]),
      "a cell key is not a returned sample")

# CONTAMINANT + REACH annotations are present on every row.
check("every row carries is_contaminant", all("is_contaminant" in p for p in prots))
check("every row carries reach (may be None)", all("reach" in p for p in prots))

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Run it to verify it fails**

Expected: `AttributeError: module 'app.queries' has no attribute 'search_protein_matrix'`.

- [ ] **Step 3: Implement**

Add to `app/queries.py`. Follow the file's existing idiom: module-level function, `query(...)` with an
explicit `tables=[...]`, parameters bound with `%(name)s`, docstring carrying the measurements.

```python
_MATRIX_MODES = {
    # ORDER BY fragment -> applied after the presence floor. Every one reads `intensity`, never
    # `normalized_intensity`: the latter is populated for 75 of 2,086 searches (4%), so a matrix
    # keyed on it renders for 4% of searches and is blank for the rest.
    "cv":               "cv DESC NULLS LAST",
    "abundance":        "mean_int DESC NULLS LAST",
    "rarity":           "reach ASC NULLS LAST, n_samples DESC",
    "corpus_abundance": "mean_pct_rank DESC NULLS LAST",
}
_MATRIX_FLOOR = 0.2          # of the search's sample count
_MATRIX_MIN_PCT_SEARCHES = 20  # before mean_pct_rank is trusted


def search_protein_matrix(search_id: str, mode: str = "cv", limit: int = 50) -> dict[str, Any]:
    """PRIVATE-SAFE: the protein x sample matrix behind a search page's heatmap.

    delimp_proteins is already one row per (search, sample, protein), so this is a read, not a
    derivation. Four ranking modes: two scoped to this search (cv, abundance) and two corpus-wide
    (rarity, corpus_abundance) served from delimp_protein_corpus_reach because they take 121 s and
    97 s live.

    PRESENCE FLOOR of 20% of samples applies to every mode. Without it mean intensity puts Or6c75 —
    an olfactory receptor in 1 of 222 samples at 85.4e9 — above albumin in 219.

    Reads `intensity`. NOT `normalized_intensity`, which exists for only 4% of searches.
    """
    if mode not in _MATRIX_MODES:
        mode = "cv"
    order = _MATRIX_MODES[mode]

    n_samples_total = query(
        "SELECT count(DISTINCT raw_path) AS n FROM delimp_proteins WHERE search_id=%(sid)s",
        {"sid": search_id}, tables=["delimp_proteins"], fetch="val") or 0
    n_proteins_total = query(
        "SELECT count(DISTINCT protein_group) AS n FROM delimp_proteins WHERE search_id=%(sid)s",
        {"sid": search_id}, tables=["delimp_proteins"], fetch="val") or 0
    if not n_samples_total:
        return {"proteins": [], "samples": [], "mode": mode, "limit": limit,
                "n_proteins_total": 0, "n_samples_total": 0,
                "floor_pct": int(_MATRIX_FLOOR * 100), "reach_computed_at": None}

    params = {"sid": search_id, "limit": int(limit),
              "floor": _MATRIX_FLOOR * n_samples_total,
              "minpct": _MATRIX_MIN_PCT_SEARCHES}
    rows = query(
        f"""
        WITH agg AS (
          SELECT p.gene,
                 max(p.protein_group)                     AS protein_group,
                 count(DISTINCT p.raw_path)               AS n_samples,
                 avg(p.intensity)                         AS mean_int,
                 stddev_pop(p.intensity)
                   / NULLIF(avg(p.intensity), 0)          AS cv,
                 bool_or(p.is_contaminant)                AS is_contaminant
            FROM delimp_proteins p
           WHERE p.search_id = %(sid)s AND p.intensity > 0
             AND NULLIF(p.gene,'') IS NOT NULL
           GROUP BY p.gene
          HAVING count(DISTINCT p.raw_path) >= %(floor)s)
        SELECT a.*, r.n_searches AS reach,
               CASE WHEN r.n_pct_searches >= %(minpct)s THEN r.mean_pct_rank END AS mean_pct_rank
          FROM agg a
          LEFT JOIN delimp_protein_corpus_reach r ON r.gene = upper(a.gene)
         ORDER BY {order}
         LIMIT %(limit)s
        """,
        params, tables=["delimp_proteins", "delimp_protein_corpus_reach"])

    genes = [r["gene"] for r in rows]
    cells = query(
        """SELECT gene, raw_path, avg(intensity) AS v
             FROM delimp_proteins
            WHERE search_id=%(sid)s AND gene = ANY(%(genes)s) AND intensity > 0
            GROUP BY gene, raw_path""",
        {"sid": search_id, "genes": genes}, tables=["delimp_proteins"]) if genes else []

    samples = query(
        """SELECT DISTINCT p.raw_path, rf.acquisition_date
             FROM delimp_proteins p
             LEFT JOIN raw_files rf ON rf.raw_path = p.raw_path
            WHERE p.search_id=%(sid)s""",
        {"sid": search_id}, tables=["delimp_proteins", "raw_files"])

    def _base(rp: str) -> str:
        return (rp or "").rstrip("/").split("/")[-1].removesuffix(".d").removesuffix(".raw")

    # Acquisition order where known, name order otherwise: batch drift then reads as vertical bands.
    samples.sort(key=lambda s: (s["acquisition_date"] is None, s["acquisition_date"], s["raw_path"]))
    sample_rows = [{"raw_path": s["raw_path"], "basename": _base(s["raw_path"]),
                    "acquisition_date": s["acquisition_date"]} for s in samples]

    by_gene: dict[str, dict[str, float]] = {}
    for c in cells:
        by_gene.setdefault(c["gene"], {})[_base(c["raw_path"])] = float(c["v"])

    reaches = sorted(r["reach"] for r in rows if r["reach"] is not None)
    def _pct(v):
        # Percentile against the DISPLAYED rows, not the corpus: globally 40% of genes are seen once,
        # which would flatten the scale to a single bin.
        if v is None or len(reaches) < 2:
            return None
        return reaches.index(v) / (len(reaches) - 1)

    proteins = [{"gene": r["gene"], "protein_group": r["protein_group"],
                 "n_samples": r["n_samples"], "is_contaminant": bool(r["is_contaminant"]),
                 "cv": float(r["cv"]) if r["cv"] is not None else None,
                 "mean_int": float(r["mean_int"]) if r["mean_int"] is not None else None,
                 "reach": r["reach"],
                 "mean_pct_rank": float(r["mean_pct_rank"]) if r["mean_pct_rank"] is not None else None,
                 "reach_pct_rank": _pct(r["reach"]),
                 "cells": by_gene.get(r["gene"], {})} for r in rows]

    as_of = query("SELECT max(computed_at) AS d FROM delimp_protein_corpus_reach",
                  tables=["delimp_protein_corpus_reach"], fetch="val")
    return {"proteins": proteins, "samples": sample_rows, "mode": mode, "limit": int(limit),
            "n_proteins_total": n_proteins_total, "n_samples_total": n_samples_total,
            "floor_pct": int(_MATRIX_FLOOR * 100), "reach_computed_at": as_of}
```

- [ ] **Step 4: Run the test to verify it passes**

Expected: `ALL PASS`. Report the wall-clock time of the `cv` call — the spec requires this be
measured on this search (222 samples, 6,388 proteins), not assumed.

- [ ] **Step 5: Prove the teeth of the three checks that guard a real defect**

Each break, then restore, then `git diff app/queries.py` showing empty. Paste all three FAIL outputs.

1. Remove the `HAVING count(DISTINCT p.raw_path) >= %(floor)s` line → the floor check must FAIL and
   `Or6c75` must appear in abundance mode.
2. Change `LEFT JOIN ... ON r.gene = upper(a.gene)` to `ON r.gene = a.gene` → rarity ordering
   degrades; report what the check does. If NEITHER rarity check fails, say so plainly — that means
   the check cannot see the case bug and needs strengthening before you move on.
3. Flip `cv DESC NULLS LAST` to `cv ASC` → the whole-page sort check must FAIL.

- [ ] **Step 6: Commit**

```bash
git add app/queries.py tests/test_search_matrix.py
git commit -m "site: the protein x sample matrix behind a search heatmap"
```

---

### Task 4: The matrix endpoint

**Files:**
- Modify: `app/main.py` (add one route beside `@app.get("/api/search/{search_id}")` at line 1027)
- Modify: `tests/test_search_matrix.py` (append)

**Interfaces:**
- Consumes: `queries.search_protein_matrix(search_id, mode, limit)`.
- Produces: `GET /api/search/{search_id}/matrix?mode=cv|abundance|rarity|corpus_abundance&limit=50`.

- [ ] **Step 1: Append the failing test**

```python
# --- the endpoint ----------------------------------------------------------------------------
from fastapi.testclient import TestClient                    # noqa: E402
from app.main import app                                     # noqa: E402

client = TestClient(app)
rr = client.get(f"/api/search/{SID}/matrix", params={"mode": "cv", "limit": 10})
check("matrix endpoint returns 200", rr.status_code == 200, str(rr.status_code))
body = rr.json()
body = body.get("data", body)
check("endpoint returns 10 proteins", len(body.get("proteins") or []) == 10,
      str(len(body.get("proteins") or [])))
check("endpoint echoes the mode", body.get("mode") == "cv", str(body.get("mode")))

bad = client.get(f"/api/search/{SID}/matrix", params={"mode": "; DROP TABLE delimp_proteins --"})
check("an unknown mode falls back rather than erroring", bad.status_code == 200, str(bad.status_code))
check("...and falls back to cv", (bad.json().get("data") or bad.json()).get("mode") == "cv")

missing = client.get("/api/search/00000000-0000-0000-0000-000000000000/matrix")
check("an unknown search returns 200 with an empty matrix",
      missing.status_code == 200
      and not ((missing.json().get("data") or missing.json()).get("proteins")),
      str(missing.status_code))
```

- [ ] **Step 2: Run it to verify it fails**

Expected: 404 on the matrix route — it does not exist yet.

- [ ] **Step 3: Implement the route**

Insert immediately after `api_search_detail` in `app/main.py`. The matrix is public-tier data
(`delimp_proteins` is in `PUBLIC_TABLES`), so it takes **no** `db.is_full()` gate — unlike
`/api/internal/*`. Do not add one; do not remove the gates from the internal routes either.

```python
@app.get("/api/search/{search_id}/matrix")
def api_search_matrix(search_id: str, mode: str = "cv", limit: int = 50):
    """The protein x sample matrix for a search page's heatmap.

    Public-tier: every table it reads is in PUBLIC_TABLES. `mode` is validated inside
    search_protein_matrix() against a fixed dict and falls back to "cv", so an unknown value can
    never reach SQL.
    """
    return ok(queries.search_protein_matrix(search_id, mode=mode, limit=max(1, min(int(limit), 200))))
```

- [ ] **Step 4: Run the test to verify it passes**

Expected: `ALL PASS` for the whole file.

- [ ] **Step 5: Prove the mode guard has teeth**

Change `if mode not in _MATRIX_MODES: mode = "cv"` in `app/queries.py` to interpolate `mode`
directly into the `ORDER BY`, re-run, and confirm the injection check FAILS. Restore, confirm PASS,
`git diff` empty. Paste both outputs. This is the one place user input reaches SQL structure.

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_search_matrix.py
git commit -m "site: GET /api/search/{id}/matrix"
```

---

### Task 5: Search-scoped coverage

**Files:**
- Modify: `app/queries.py` (`protein_coverage_peptides`, currently at line 2876)
- Modify: `app/main.py` (`api_protein_coverage`, currently at line 755)
- Create: `tests/test_coverage_scope.py`

**Interfaces:**
- Produces: `protein_coverage_peptides(protein_group, limit=4000, search_id=None)`; each returned
  peptide gains `"here": bool` when `search_id` is given. `GET /api/protein/{pg}/coverage?search_id=…`.

- [ ] **Step 1: Write the failing test**

```python
"""Coverage gains an optional search scope — WITHOUT changing its unscoped behaviour.

Fixtures measured 2026-09-09 against the live endpoint:
  Fabp1  P12710  127 aa  38 corpus peptides, 20 in PROT_0793_search_mouse, 18 corpus-only
  Mup2   P11589  180 aa  26 corpus peptides, 22 here, 4 corpus-only

Run:  DELIMP_PG_TOKEN_FILE=/Users/brettphinney/.pgfarm_token python3 tests/test_coverage_scope.py
"""
import os, sys
os.environ.setdefault("DELIMP_INTERNAL_MODE", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import queries                                      # noqa: E402

SID = "8221f5fc-492e-5c9d-a08d-542cfdb48791"
FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)

# BACKWARD COMPATIBILITY. Existing callers pass no search_id and must see exactly what they saw.
un = queries.protein_coverage_peptides("P12710")
check("unscoped call still returns peptides", len(un.get("peptides") or []) == 38,
      str(len(un.get("peptides") or [])))
check("unscoped peptides carry NO 'here' key",
      all("here" not in p for p in un["peptides"]),
      "adding 'here' unconditionally would change the shape for every existing caller")

sc = queries.protein_coverage_peptides("P12710", search_id=SID)
check("scoped call returns the same corpus peptide set", len(sc["peptides"]) == 38,
      str(len(sc["peptides"])))
check("scoped peptides all carry 'here'", all("here" in p for p in sc["peptides"]))
here = sum(1 for p in sc["peptides"] if p["here"])
check("Fabp1: 20 of 38 peptides were found in this search", here == 20, str(here))

sc2 = queries.protein_coverage_peptides("P11589", search_id=SID)
here2 = sum(1 for p in sc2["peptides"] if p["here"])
check("Mup2: 22 of 26 peptides were found in this search",
      here2 == 22 and len(sc2["peptides"]) == 26, f"{here2}/{len(sc2['peptides'])}")

# A search that never saw this protein must mark everything corpus-only, not crash.
none = queries.protein_coverage_peptides("P12710", search_id="00000000-0000-0000-0000-000000000000")
check("an unrelated search marks every peptide corpus-only",
      none["peptides"] and not any(p["here"] for p in none["peptides"]))

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Run it to verify it fails**

Expected: `TypeError: protein_coverage_peptides() got an unexpected keyword argument 'search_id'`.

- [ ] **Step 3: Implement**

`protein_coverage_peptides` is CACHED via `CACHE.get_or_set(f"covpep_{pg}", ...)`. The cache key must
include the scope or a scoped call will poison the unscoped entry and vice versa. Read the existing
function's docstring before changing it — it records why the cache exists.

```python
def protein_coverage_peptides(protein_group: str, limit: int = 4000,
                              search_id: str | None = None) -> dict[str, Any]:
    """(existing docstring — keep it, then add:)

    With search_id, each peptide gains "here": whether THIS search saw it. Without it the return is
    unchanged, so every existing caller is untouched. The cache key includes the scope; sharing one
    key between scoped and unscoped calls would serve one shape to a caller expecting the other.
    """
    pg = (protein_group or "").strip()
    key = f"covpep_{pg}" if not search_id else f"covpep_{pg}_{search_id}"
    cached = CACHE.get_or_set(key, lambda: _protein_coverage_peptides(pg, limit, search_id))
    return cached or {"gene": None, "peptides": []}
```

In `_protein_coverage_peptides`, after the existing corpus peptide query returns `peps`, add:

```python
    if search_id and peps:
        # One extra indexed lookup, not a re-derivation: idx_prec_protein_group already covers this.
        seen = {r["stripped_seq"] for r in query(
            """SELECT DISTINCT stripped_seq FROM delimp_precursors
                WHERE protein_group=%s AND search_id=%s AND stripped_seq = ANY(%s)""",
            (pg, search_id, [p["stripped_seq"] for p in peps]),
            tables=["delimp_precursors"])}
        for p in peps:
            p["here"] = p["stripped_seq"] in seen
```

Then widen the endpoint in `app/main.py`:

```python
@app.get("/api/protein/{protein_group:path}/coverage")
def api_protein_coverage(protein_group: str, search_id: str | None = None):
    """(existing docstring) With search_id, each peptide carries "here" — whether that search saw it."""
    from . import coverage as cov
    data = queries.protein_coverage_peptides(protein_group, search_id=search_id)
    ...  # rest unchanged
```

- [ ] **Step 4: Run the test to verify it passes**

Expected: `ALL PASS`, including the two backward-compatibility checks.

- [ ] **Step 5: Prove the cache-key check has teeth**

Set `key = f"covpep_{pg}"` unconditionally (ignoring the scope), then in one process call the
unscoped form first and the scoped form second. The scoped call will return the cached unscoped
result with no `"here"` keys, so `"scoped peptides all carry 'here'"` must FAIL. Restore, confirm
PASS, `git diff app/queries.py` empty. Paste both outputs.

- [ ] **Step 6: Commit**

```bash
git add app/queries.py app/main.py tests/test_coverage_scope.py
git commit -m "site: coverage can be scoped to one search, without changing the unscoped shape"
```

---

### Task 6: The heatmap and peptide map in the page

**Files:**
- Modify: `app/static/app.js` (`renderSearchDetail`, and two new functions)

**Interfaces:**
- Consumes: `GET /api/search/{id}/matrix`, `GET /api/protein/{pg}/coverage?search_id=…`.
- Produces: `renderSearchMatrix(searchId)` and `renderPeptideMap(pg, gene, searchId)` in `app.js`.

There is no JS test framework in this repo. Verification is `node --check` plus a real browser
session — tests passing is not available here, so looking at it is not optional.

- [ ] **Step 1: Add the container and the call**

In `renderSearchDetail`, immediately after the `Runs (${d.runs.length})` card's closing `</div>` and
before the template's final backtick, append:

```javascript
    <div class="glass card p-5 fade-in mt-4" id="matrixCard">
      <div class="skeleton h-64 rounded-xl"></div></div>
    <div class="glass card p-5 fade-in mt-4" id="pepmapCard" style="display:none"></div>`;
  renderSearchMatrix(id);
```

- [ ] **Step 2: Implement `renderSearchMatrix`**

Requirements, each from a measured finding — do not simplify any of them away:

- Four mode buttons, **each labelled with its own scope**, and no group-level scope label:
  `Varies most — your samples` (default), `Most abundant — your samples`,
  `Rarest — across the corpus`, `Most abundant — across the corpus`.
- **Legend ABOVE the grid.**
- Cells: `viridis`-style ramp over log2 `intensity`, **z-scored per row**. A sample with no value for
  that protein gets the distinct "not identified" colour `#111a2b`, never the zero end of the ramp.
- **Two row-annotation strips** between the gene name and the cells: corpus rarity (using
  `reach_pct_rank`, which the server already percentiled against the returned rows) and
  `is_contaminant`. Rarity must NOT colour the cells — it is constant across a row and would waste
  the x-axis.
- A row whose `reach` is `null` renders the rarity strip in a neutral "not computed" colour and its
  tooltip says so. Never render it as the rare extreme.
- Gene name click calls `renderPeptideMap(...)`. It is **not** a link — the y-axis is for scanning,
  and making it navigate loses the sort.
- Caption states the row count, the sample count, the active mode, and the presence floor.

- [ ] **Step 3: Implement `renderPeptideMap`**

- Fetch `/api/protein/${pg}/coverage?search_id=${searchId}`.
- Keep the existing custom-construct and `!sequence_available` branches from the current coverage
  renderer (`app.js:1696-1698`) — copy them, do not drop them.
- Sequence in wrapped rows of 60 with residue numbering; below each row, every peptide as its own
  bar, **lane-packed** so overlaps stack rather than hide. Gold `#FFCF40` = `here`, teal `#5eead4` =
  corpus-only. Residues never covered stay grey `#475569`.
- Header shows both percentages: this experiment, and including corpus.
- Header carries a link to the gene page: `go('gene', <symbol>)`.
- Peptide bar click opens an inline detail card — residue range, and this-experiment vs corpus
  columns. The peptide sequence in that card links to `go('peptide', <stripped_seq>)`.

- [ ] **Step 4: Verify it parses and renders**

```bash
cd /Users/brettphinney/Documents/FRAN
node --check app/static/app.js
DELIMP_PG_TOKEN_FILE=/Users/brettphinney/.pgfarm_token DELIMP_INTERNAL_MODE=1 \
  python3 -m uvicorn app.main:app --port 8899 --host 127.0.0.1
```

Then in a browser at `#/run/8221f5fc-492e-5c9d-a08d-542cfdb48791`, confirm and paste evidence for
each: all four modes change the row set; the legend is above the grid; a gene click opens the peptide
map; a peptide click opens the detail card; the gene-page and peptide-page links navigate; and the
three export buttons on that page still work. Kill the server afterwards.

- [ ] **Step 5: Confirm nothing regressed**

```bash
for t in test_corpus_reach test_search_matrix test_coverage_scope \
         test_submission_lookup test_collaborator_recency test_internal_route_gate; do
  printf "%-34s " "$t"
  DELIMP_PG_TOKEN_FILE=/Users/brettphinney/.pgfarm_token python3 tests/$t.py 2>&1 | tail -1
done
```

Expected: `ALL PASS` on every line.

- [ ] **Step 6: Commit**

```bash
git add app/static/app.js
git commit -m "site: a protein x sample heatmap and peptide map on the search page"
```

---

### Task 7: Schedule the refresh

**Files:**
- Create: `ingest/cron_corpus_reach.sh`

**Interfaces:**
- Consumes: `ingest/refresh_corpus_reach.py` from Task 2.

- [ ] **Step 1: Write the wrapper**

Read `ingest/cron_coreomics_import.sh` first and mirror it exactly — the token path, the log
location, and the `bash -lc` invocation are all established there and differ from what you would
guess. Note the known gotcha: a non-login shell has neither the module environment nor the PATH this
needs.

- [ ] **Step 2: Run the wrapper by hand once**

Confirm it completes and that `computed_at` in the table advances. Paste the before and after values.

- [ ] **Step 3: Do NOT install the crontab entry**

Print the exact `crontab -e` line for a weekly run alongside the existing refresh jobs, and hand it
to Brett. Installing a recurring job on shared infrastructure is his call, not yours.

- [ ] **Step 4: Commit**

```bash
git add ingest/cron_corpus_reach.sh
git commit -m "ingest: weekly refresh wrapper for the corpus-reach table"
```

---

## Self-Review

**Spec coverage.** §1 reach table → Tasks 1–2. §1b(a) case-insensitive → Task 1 test + Task 2 SQL.
(b) presence floor → Task 3 constant + test. (c) rarity as annotation → Task 6 Step 2. (d) contaminant
strip → Task 3 returns it, Task 6 renders it. (e) peptide map → Task 6 Step 3. (f) modified forms →
Task 6 Step 3 detail card. (g) `intensity` → Global Constraints + Task 3. (h) corpus abundance →
Task 2 `mean_pct_rank` + Task 3 mode. §2 heatmap → Tasks 3–4, 6. §3 coverage + navigation → Tasks 5–6.
Staleness → `reach_computed_at` returned in Task 3, rendered in Task 6.

**Gap found and accepted:** the spec's "acquisition order makes batch drift visible" is implemented
(Task 3 sorts on `acquisition_date`) but has no test, because the fixture's acquisition dates were
never verified to be populated. Task 3's implementer should report how many of the 222 samples carry
one; if it is low, say so rather than claiming the ordering works.

**Placeholder scan:** no TBDs. Task 6 has no code block for the renderers — deliberate, and the one
place this plan states requirements rather than code: it is ~200 lines of DOM construction with no
test framework to pin it, and the mockup at
`/private/tmp/claude-501/-Users-brettphinney-Documents-FRAN/bcffa69b-2ea9-4009-887a-173552b87f49/scratchpad/heatmap_mockup.html`
is a complete working reference the implementer should read first.

**Type consistency:** `search_protein_matrix` returns `reach_pct_rank` (Task 3) and Task 6 consumes
`reach_pct_rank`. `protein_coverage_peptides` adds `here` (Task 5) and Task 6 consumes `here`. The
reach table's key is `gene` UPPERCASED in Tasks 1, 2 and 3's join.
