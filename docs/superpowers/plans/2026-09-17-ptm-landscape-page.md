# PTM Landscape Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A public `/#/ptm` page that shows, per search, what fraction of its precursors carry a modification — so a reader can see at a glance which PTM enrichments worked and which did not.

**Architecture:** One cached aggregate in `app/queries.py` over `delimp_search_protein_ptm` (5.26M rows, **measured 1.4 s** for the full per-search GROUP BY), joined to `delimp_searches` for name/date/engine and through `search_raw_files` to `delimp_sample_metadata`/`raw_files` for species and instrument. One `/api/ptm_landscape` endpoint. One `renderPTM()` view in the existing SPA, plus a nav button.

**Tech Stack:** FastAPI + psycopg2 (`app/db.query`, read-only, allowlist-guarded), vanilla-JS SPA in `app/static/app.js`, Chart.js (already loaded), Tailwind classes already in use.

**Spec:** `docs/superpowers/specs/2026-09-16-ptm-landscape-page-design.md`

---

## Pre-flight: two spec defects, resolved here

Both were found by checking the spec against the live schema before any task was written. Implementers should follow THIS plan where it differs from the spec.

**Defect 1 — species and instrument are not on `delimp_searches`.** The spec's column table says
"search name, date, species, instrument | `delimp_searches` join". Verified against
`information_schema`: `delimp_searches` has **no** `organism_name`, `organism`, `species`,
`instrument`, `instrument_model` or `platform`. It has `search_engine` and `completed_at`.

*Resolution:* species and instrument come through `search_raw_files` → `delimp_sample_metadata.organism_name` and `raw_files.instrument_model`. All four tables are in `PUBLIC_TABLES` (`app/db.py:36-40`), and the join path is small — `search_raw_files` 23,871 rows, `raw_files` and `delimp_sample_metadata` 23,935 each. This does **not** violate the spec's real constraint, which is *"Never `delimp_precursors`"* (a >15-minute corpus scan). It does widen the `tables=[...]` list from two to five, which is why it is called out rather than done quietly.

**Defect 2 — the coverage numbers in the spec are stale, and the remaining gap is a different kind of gap.** The spec says "The rollup covers 2 of 2,086 searches at the time of writing; a backfill is running." The backfill has since completed. Measured 2026-09-17: **2,107 of 2,112 searches covered, 5,256,118 rows, 0 pending.**

The 5 uncovered searches are **not** "not yet computed" — they are structurally uncomputable: they hold `delimp_proteins` rows but no `delimp_precursors` row with a non-NULL `protein_group`, so the rollup's GROUP BY produces nothing for them (see the `PENDING` comment in `ingest/refresh_search_ptm.py`). The spec's requirement that the page distinguish "not yet computed" from zero still stands, but the honest wording is now **"cannot be computed from this search's data"**, not "not yet computed". Task 3 must say that.

---

## Global Constraints

- Every `query()` call passes `tables=[...]`. No exceptions.
- **Never read `delimp_precursors`.** The corpus-wide scan is >15 minutes and this page is public and anonymous.
- The page is **public-tier**: no auth gate, and nothing filename-shaped or directory-shaped may reach the response. `privacy.redact()` sanitizes string VALUES, never KEYS — do not rely on it to hide a key you should not have selected.
- The verdict column **must never say "failed"**. FRAN cannot distinguish a failed enrichment from a sample that was never enriched. Allowed labels are exactly: `enriched`, `low for an enrichment`, `incidental`.
- **Name-based intent detection is forbidden.** Do not match `ubiq|phospho` against `search_name` to decide what a search was trying to do.
- A rate whose denominator is NULL or 0 is **omitted**, never rendered as `0`. Absence is not a measurement.
- Every test must be proven able to fail — show the failing output in the task report.
- Cache the aggregate (`SLOW_CACHE`), as every other corpus-wide page does.

---

### Task 1: `queries.ptm_landscape()`

**Files:**
- Modify: `app/queries.py` (add one function; follow the `species_showcase` idiom at line ~1588 — inner `_p()` producer, returned through `SLOW_CACHE.get_or_set`)
- Test: `tests/test_ptm_landscape.py` (create)

**Interfaces:**
- Consumes: `app.db.query(sql, params, *, tables=[...])`; `SLOW_CACHE` (already imported at `app/queries.py:19`)
- Produces: `ptm_landscape() -> dict` with exactly these keys, which Tasks 2 and 3 rely on:
  ```
  {
    "coverage": {"n_searches_total": int, "n_searches_covered": int, "n_uncomputable": int},
    "summary":  {"n_searches_any_ptm": int, "n_searches_phospho": int, "n_searches_glygly": int,
                 "n_groups_any_ptm": int, "n_groups_phospho": int, "n_groups_glygly": int},
    "searches": [ {"search_id": str, "search_name": str, "completed_at": str|None,
                   "search_engine": str|None, "organism": str|None, "instrument": str|None,
                   "n_groups": int, "n_ptm": int, "n_phospho": int, "n_glygly": int,
                   "mod_precursors": int, "n_precursors_total": int|None,
                   "modified_rate": float|None, "verdict": str|None} ],
  }
  ```

- [ ] **Step 1: Write the failing test**

Create `tests/test_ptm_landscape.py`:

```python
"""The PTM landscape aggregate must be honest about coverage and never overclaim a verdict."""
import os, sys
os.environ.setdefault("DELIMP_PG_TOKEN_FILE", "/Users/brettphinney/.pgfarm_token")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import queries                                   # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

GG_SEARCH = "5d629050-4f68-5e43-8e1f-48bbc3fc0b8f"   # Bennett_Penn_Ubiq — a working diGly enrichment

d = queries.ptm_landscape()

check("returns the four top-level keys", set(d) == {"coverage", "summary", "searches"} or
      {"coverage", "summary", "searches"} <= set(d), str(sorted(d)))

cov = d["coverage"]
check("coverage counts are present and sane",
      cov["n_searches_covered"] > 0 and cov["n_searches_total"] >= cov["n_searches_covered"],
      str(cov))

rows = {r["search_id"]: r for r in d["searches"]}
check("the Bennett Penn ubiquitin search is present", GG_SEARCH in rows)

if GG_SEARCH in rows:
    r = rows[GG_SEARCH]
    check("it is flagged as carrying GlyGly", r["n_glygly"] > 0, str(r["n_glygly"]))
    check("its modified rate is high (a working enrichment)",
          r["modified_rate"] is not None and r["modified_rate"] > 0.5, str(r.get("modified_rate")))
    check("its verdict is 'enriched'", r["verdict"] == "enriched", str(r.get("verdict")))

# THE OVERCLAIM GUARD. FRAN cannot tell a failed enrichment from a sample never enriched.
check("no verdict anywhere says 'failed'",
      not any("fail" in (r.get("verdict") or "").lower() for r in d["searches"]))

# ABSENCE IS NOT A MEASUREMENT: a search with no usable denominator must have no rate and no
# verdict, rather than a rate of 0.0 that reads as "nothing was modified".
bad = [r for r in d["searches"]
       if not r.get("n_precursors_total") and (r.get("modified_rate") is not None or r.get("verdict"))]
check("searches with no precursor denominator carry neither rate nor verdict",
      not bad, f"{len(bad)} offenders, e.g. {bad[:1]}")

# NO FILENAME/PATH LEAK on a public-tier payload.
import json
blob = json.dumps(d)
check("no output_dir / path-shaped value leaks",
      "output_dir" not in blob and ":\\" not in blob and "/Volumes/" not in blob
      and "/nfs/" not in blob and "/quobyte/" not in blob)

print()
if FAILS: print(f"FAILED ({len(FAILS)}): {', '.join(FAILS)}"); sys.exit(1)
print("all checks passed")
```

- [ ] **Step 2: Run it to watch it fail**

Run: `python3 tests/test_ptm_landscape.py`
Expected: `AttributeError: module 'app.queries' has no attribute 'ptm_landscape'`. **Record this output in your report** — it is the proof the test can fail.

- [ ] **Step 3: Implement `ptm_landscape()`**

Add to `app/queries.py`. Note `n_precursors_total` lives on `delimp_searches`; the rollup's `n_mod_precursors` is the numerator.

```python
def ptm_landscape() -> dict[str, Any]:
    """Per-search modified-precursor rate — which PTM enrichments actually worked.

    Reads ONLY the rollup + search metadata. NEVER delimp_precursors: computing this live is a
    >15-minute corpus scan and this page is public and anonymous. The full per-search GROUP BY
    over the 5.26M-row rollup measured 1.4 s on 2026-09-17.

    The rollup flags only has_ptm / has_phospho / has_glygly, so this CANNOT distinguish
    oxidation from acetyl from deamidation. The page says so; do not let a caller infer that an
    absent flag means an absent modification.
    """
    def _p() -> dict[str, Any]:
        rows = query(
            """
            WITH agg AS (
              SELECT search_id,
                     COUNT(*)                                AS n_groups,
                     COUNT(*) FILTER (WHERE has_ptm)         AS n_ptm,
                     COUNT(*) FILTER (WHERE has_phospho)     AS n_phospho,
                     COUNT(*) FILTER (WHERE has_glygly)      AS n_glygly,
                     SUM(n_mod_precursors)                   AS mod_precursors
                FROM delimp_search_protein_ptm
               GROUP BY search_id
            ),
            -- species/instrument are NOT on delimp_searches (verified 2026-09-17); they come
            -- through the run join. Bounded by runs (23,871 rows), not precursors.
            meta AS (
              SELECT srf.search_id,
                     MODE() WITHIN GROUP (ORDER BY sm.organism_name)   AS organism,
                     MODE() WITHIN GROUP (ORDER BY rf.instrument_model) AS instrument
                FROM search_raw_files srf
                LEFT JOIN delimp_sample_metadata sm ON sm.raw_path = srf.raw_path
                LEFT JOIN raw_files rf              ON rf.raw_path = srf.raw_path
               GROUP BY srf.search_id
            )
            SELECT a.search_id, s.search_name, s.completed_at, s.search_engine,
                   m.organism, m.instrument,
                   a.n_groups, a.n_ptm, a.n_phospho, a.n_glygly, a.mod_precursors,
                   s.n_precursors_total
              FROM agg a
              JOIN delimp_searches s ON s.id = a.search_id
              LEFT JOIN meta m       ON m.search_id = a.search_id
             ORDER BY a.n_ptm DESC
            """,
            tables=["delimp_search_protein_ptm", "delimp_searches",
                    "search_raw_files", "delimp_sample_metadata", "raw_files"],
        )

        searches = []
        for r in rows:
            total = r.get("n_precursors_total")
            modp = int(r.get("mod_precursors") or 0)
            # A rate needs a real denominator. No denominator -> no rate AND no verdict: a 0.0
            # here would read as "nothing was modified", which is a claim we cannot make.
            rate = (modp / total) if (total and total > 0) else None
            searches.append({
                "search_id": str(r["search_id"]),
                "search_name": r.get("search_name"),
                "completed_at": r["completed_at"].isoformat() if r.get("completed_at") else None,
                "search_engine": r.get("search_engine"),
                "organism": r.get("organism"),
                "instrument": (r.get("instrument") or "").strip() or None,
                "n_groups": int(r.get("n_groups") or 0),
                "n_ptm": int(r.get("n_ptm") or 0),
                "n_phospho": int(r.get("n_phospho") or 0),
                "n_glygly": int(r.get("n_glygly") or 0),
                "mod_precursors": modp,
                "n_precursors_total": int(total) if total else None,
                "modified_rate": rate,
                "verdict": _ptm_verdict(rate),
            })
        searches.sort(key=lambda x: (x["modified_rate"] is None, -(x["modified_rate"] or 0)))

        n_total = query("SELECT COUNT(*) FROM delimp_searches", tables=["delimp_searches"],
                        fetch="val")
        return {
            "coverage": {
                "n_searches_total": int(n_total or 0),
                "n_searches_covered": len(searches),
                "n_uncomputable": max(int(n_total or 0) - len(searches), 0),
            },
            "summary": {
                "n_searches_any_ptm": sum(1 for s in searches if s["n_ptm"]),
                "n_searches_phospho": sum(1 for s in searches if s["n_phospho"]),
                "n_searches_glygly": sum(1 for s in searches if s["n_glygly"]),
                "n_groups_any_ptm": sum(s["n_ptm"] for s in searches),
                "n_groups_phospho": sum(s["n_phospho"] for s in searches),
                "n_groups_glygly": sum(s["n_glygly"] for s in searches),
            },
            "searches": searches,
        }
    return SLOW_CACHE.get_or_set("ptm_landscape", _p)


def _ptm_verdict(rate: float | None) -> str | None:
    """Rate-only classification. NEVER 'failed'.

    FRAN cannot distinguish a failed enrichment from a sample that was never enriched, and
    inferring intent from search_name is a guess about a human's naming habits. So this reads the
    rate and nothing else, and the UI states that basis next to the label.
    """
    if rate is None:
        return None
    if rate >= 0.50:
        return "enriched"
    if rate >= 0.05:
        return "low for an enrichment"
    return "incidental"
```

- [ ] **Step 4: Run the test to green**

Run: `python3 tests/test_ptm_landscape.py`
Expected: `all checks passed`.

- [ ] **Step 5: Prove the overclaim guard bites**

Temporarily make `_ptm_verdict` return `"failed enrichment"` for `rate < 0.05`, re-run, and confirm the `no verdict anywhere says 'failed'` check FAILS. Revert. Paste both outputs in your report.

- [ ] **Step 6: Commit**

```bash
git add app/queries.py tests/test_ptm_landscape.py
git commit -m "ptm: per-search modified-precursor rate aggregate for the landscape page"
```

---

### Task 2: `/api/ptm_landscape` endpoint

**Files:**
- Modify: `app/main.py` (follow `api_species_showcase` at line ~542)
- Test: `tests/test_ptm_landscape_api.py` (create)

**Interfaces:**
- Consumes: `queries.ptm_landscape()` from Task 1, `ok()`, `_safe()` (both already in `app/main.py`)
- Produces: `GET /api/ptm_landscape` → `{"ok": true, "landscape": {...}}`

- [ ] **Step 1: Write the failing test**

```python
"""The PTM landscape endpoint is PUBLIC — it must serve anonymously and leak no paths."""
import os, sys
os.environ.setdefault("DELIMP_PG_TOKEN_FILE", "/Users/brettphinney/.pgfarm_token")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from fastapi.testclient import TestClient                  # noqa: E402
from app.main import app                                   # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond: FAILS.append(name)

c = TestClient(app)
r = c.get("/api/ptm_landscape")
check("200 for an anonymous caller", r.status_code == 200, str(r.status_code))
j = r.json()
check("ok envelope", j.get("ok") is True, str(j)[:200])
lc = j.get("landscape") or {}
check("carries coverage + summary + searches",
      {"coverage", "summary", "searches"} <= set(lc), str(sorted(lc)))
check("searches is non-empty", len(lc.get("searches") or []) > 0)
body = r.text
check("no path-shaped value in the public payload",
      "output_dir" not in body and ":\\" not in body and "/Volumes/" not in body
      and "/nfs/" not in body and "/quobyte/" not in body)
check("no verdict says failed", "fail" not in body.lower().replace("failed_", ""))

print()
if FAILS: print(f"FAILED ({len(FAILS)}): {', '.join(FAILS)}"); sys.exit(1)
print("all checks passed")
```

- [ ] **Step 2: Run it, watch it 404**

Run: `python3 tests/test_ptm_landscape_api.py`
Expected: the `200 for an anonymous caller` check FAILS with `404`. Record it.

- [ ] **Step 3: Add the endpoint**

In `app/main.py`, beside the other public showcase endpoints:

```python
@app.get("/api/ptm_landscape")
def api_ptm_landscape():
    """PTM landscape — per-search modified-precursor rate, so a reader can see which PTM
    enrichments worked. PUBLIC and anonymous, like the other showcase pages.

    Reads the precomputed rollup only, never delimp_precursors (a >15-minute corpus scan)."""
    return ok({"landscape": _safe(queries.ptm_landscape, {})})
```

- [ ] **Step 4: Run to green**

Run: `python3 tests/test_ptm_landscape_api.py` → `all checks passed`.

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_ptm_landscape_api.py
git commit -m "ptm: public /api/ptm_landscape endpoint"
```

---

### Task 3: The page

**Files:**
- Modify: `app/static/app.js` (router `switch` at ~line 60; add `renderPTM()` near `renderSpeciesShowcase`)
- Modify: `app/templates/index.html` (nav button, after the `engines` button at line 78)

**Interfaces:**
- Consumes: `GET /api/ptm_landscape` from Task 2; existing helpers `api()`, `esc()`, `fmt()`, `table()`, `go()`
- Produces: route `#/ptm`, nav button `data-view="ptm"`

- [ ] **Step 1: Register the route and nav**

`app/templates/index.html`, immediately after the Engines button (line 78):

```html
        <button data-view="ptm" class="navbtn px-3 py-1.5 rounded-lg font-medium text-slate-300 hover:text-white">PTM</button>
```

`app/static/app.js`, in the `route()` switch, after `case 'engines':`:

```javascript
    case 'ptm': return renderPTM();
```

- [ ] **Step 2: Implement `renderPTM()`**

Three sections, in the spec's order. The caption and the coverage line are **required**, not decoration — they are what stops the page reading as a complete modification census.

```javascript
async function renderPTM(){
  const app = $('#app');
  app.innerHTML = `<div class="glass card p-5 fade-in"><h2 class="text-xl font-bold text-white">PTM landscape</h2>
    <div class="text-slate-400 text-sm mt-2">Loading…</div></div>`;
  let d;
  try { d = (await api('/api/ptm_landscape')).landscape; }
  catch(e){ app.innerHTML = `<div class="glass card p-5"><h2 class="text-xl font-bold text-white">PTM landscape</h2>
    <div class="text-rose-300 text-sm mt-2">Could not load the PTM aggregate.</div></div>`; return; }

  const cov = d.coverage||{}, sm = d.summary||{}, rows = d.searches||[];
  // COVERAGE IS NOT DECORATION. The 5 uncovered searches cannot be computed from their own data
  // (they carry no precursor with a protein group) — that is different from "not computed yet",
  // and saying it wrong turns an absence into an implied zero.
  const covLine = cov.n_uncomputable
    ? `Covering <b>${fmt(cov.n_searches_covered)}</b> of ${fmt(cov.n_searches_total)} searches.
       ${fmt(cov.n_uncomputable)} cannot be computed from their own data (no precursor carries a protein group).`
    : `Covering all ${fmt(cov.n_searches_covered)} searches.`;

  const verdictChip = (v, rate) => {
    if(!v) return `<span class="text-slate-500" title="No precursor total recorded for this search, so no rate can be computed. This is an absence, not a zero.">—</span>`;
    const cls = v==='enriched' ? 'bg-emerald-500/20 text-emerald-300'
              : v==='low for an enrichment' ? 'bg-amber-500/20 text-amber-300'
              : 'bg-slate-600/30 text-slate-300';
    const tip = `${(rate*100).toFixed(1)}% of precursors carry a modification. Classified by rate alone — FRAN cannot tell a failed enrichment from a sample that was never enriched.`;
    return `<span class="px-2 py-0.5 rounded text-xs font-semibold ${cls}" title="${esc(tip)}">${esc(v)}</span>`;
  };

  app.innerHTML = `
    <div class="glass card p-5 fade-in">
      <h2 class="text-xl font-bold text-white">PTM landscape</h2>
      <div class="mt-2 text-sm text-amber-200/90 bg-amber-500/10 border border-amber-500/20 rounded-lg px-3 py-2">
        <b>Phospho and GlyGly only.</b> This table records whether a protein carried
        <i>any</i> modification, phospho, or GlyGly — it does not distinguish oxidation,
        acetyl or deamidation. Read it as a view of those three, not a census of every modification.
      </div>
      <div class="mt-2 text-xs text-slate-400">${covLine}</div>
      <div class="grid grid-cols-2 sm:grid-cols-3 gap-4 mt-4">
        ${stat('Searches with a modification', fmt(sm.n_searches_any_ptm))}
        ${stat('Searches with phospho', fmt(sm.n_searches_phospho))}
        ${stat('Searches with GlyGly', fmt(sm.n_searches_glygly))}
        ${stat('Protein groups modified', fmt(sm.n_groups_any_ptm))}
        ${stat('…with phospho', fmt(sm.n_groups_phospho))}
        ${stat('…with GlyGly', fmt(sm.n_groups_glygly))}
      </div>
    </div>

    <div class="glass card p-5 fade-in mt-4">
      <h3 class="font-bold text-white mb-1">Modified-precursor rate by search</h3>
      <div class="text-xs text-slate-400 mb-3">Sorted by rate. A high rate on an enrichment means it worked; a low one means it did not enrich, or was never meant to.</div>
      <div class="overflow-x-auto">
      ${table(['Search','Date','Species','Instrument','Groups w/ mod','Phospho','GlyGly','Modified rate','Verdict'],
        rows.slice(0,400).map(r=>[
          `<a class="text-accent-300 hover:underline cursor-pointer" onclick="go('run','${esc(r.search_id)}')">${esc(r.search_name||r.search_id)}</a>`,
          esc((r.completed_at||'').slice(0,10)),
          esc(r.organism||'—'),
          esc(r.instrument||'—'),
          fmt(r.n_ptm), fmt(r.n_phospho), fmt(r.n_glygly),
          r.modified_rate==null ? '<span class="text-slate-500">—</span>' : (r.modified_rate*100).toFixed(1)+'%',
          verdictChip(r.verdict, r.modified_rate),
        ]))}
      </div>
    </div>`;
}
```

- [ ] **Step 3: Verify in a browser**

Start the app (`uvicorn app.main:app --port 8899`), open `http://127.0.0.1:8899/#/ptm`.
Confirm, and state each in your report:
1. the amber "Phospho and GlyGly only" caption is visible **above** the table, not in a footnote;
2. the coverage line names both numbers;
3. the top rows are high-rate searches and the Bennett Penn ubiquitin search shows `enriched`;
4. a row with no rate shows `—` in **both** the rate and verdict columns, never `0.0%`;
5. no verdict anywhere reads "failed".

**Beware a cached `app.js`.** A previous session measured a stale cached copy and drew a wrong conclusion from it. Hard-reload, and confirm `renderPTM` exists in the page's live JS before trusting what you see.

- [ ] **Step 4: Commit**

```bash
git add app/static/app.js app/templates/index.html
git commit -m "ptm: the landscape page — which enrichments actually worked"
```
