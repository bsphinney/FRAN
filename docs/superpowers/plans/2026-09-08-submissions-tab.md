# Submissions Tab & Collaborator Simplification — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a submission number a first-class way to navigate FRAN — `PROT_0793` resolves to a page, appears in search, and lists alongside every other submission — and turn the collaborators page into the lookup it is actually used as.

**Architecture:** Almost everything needed already exists. `internal_submission()` returns the CoreOmics record, samples, linked searches and the share location, and `renderSubmission()` already draws that page; it is simply unreachable by anything but a hex id and has no list in front of it. This plan adds a number→hex resolver, a list query and endpoint, a tab, and repairs two specific defects in `internal_people_search`. No schema changes, no writes, nothing that needs a database migration.

**Tech Stack:** FastAPI (`app/main.py`), psycopg2 via the governed `app/db.py` query layer, vanilla JS + Tailwind CDN (`app/static/app.js`). Tests are plain executable Python scripts — **no pytest** (see `tests/test_federation_boundary.py`).

**Spec:** `docs/superpowers/specs/2026-09-08-submissions-tab-and-service-dir-scanner-design.md`

## Global Constraints

- **PROVE EVERY TEST CAN FAIL.** The scanner plan produced FIVE tests whose names described a bug they could not detect — assertions that passed identically with the fix reverted. Before committing any test that guards a behaviour, deliberately break that behaviour, run the suite, confirm the test FAILS, restore it, confirm it passes, and paste both outputs in your report. A test you have not seen fail is decoration.
- **The internal tables are gated.** `coreomics_submissions_cache`, `coreomics_samples_cache`, `delimp_search_provenance`, `delimp_submission_service_dir`, `delimp_pi_profile` and `delimp_lab_institute_override` live in `app/db.py:_INTERNAL_TABLES`. The governed `query()` refuses them unless the request is internal. Any test touching them must set `os.environ["DELIMP_INTERNAL_MODE"] = "1"` **before** importing `app.db`.
- **Every `query()` call passes `tables=[...]`** naming every table it touches. That list is the governance allowlist check, not documentation.
- **Do not break the three exports.** `/api/export/diann_report/{search_id}` (report.parquet → limpa/DE-LIMP), `/api/export/research_brief/{search_id}` (.md for the proteomics-pipeline skill) and `/api/export/resubmit_brief/{submission_id}` (.md to re-search un-ingested data) are load-bearing workflows. Any page that replaces or fronts an existing one keeps its export affordances.
- **Submission numbers are `PROT_` + four digits**, e.g. `PROT_0793`, stored in `coreomics_submissions_cache.internal_id`. 790 of 4,488 rows have one. `submission_id` is a 12-hex-character id (`1ed8b74497e4`) and is the join key everywhere else.
- **Location data is stale and must be labelled as such.** `delimp_submission_service_dir` was written once on 2026-06-24 and covers nothing after `PROT_0724`. The UI states when locations were last determined rather than implying they are current.
- **Match the existing UI vocabulary exactly**: `glass` + `card` classes, 18px radii, `text-accent-400` (#FFCF40) for links/keys, `kpi-num` for figures, the `table()` and `stat()` helpers already in `app.js`. Do not introduce new colours or a new table renderer.

---

### Task 1: Resolve a submission number to a submission

**Files:**
- Modify: `app/queries.py` (`internal_submission`, around line 2307)
- Test: `tests/test_submission_lookup.py`

**Interfaces:**
- Produces: `normalize_submission_ref(ref: str) -> str | None` in `app/queries.py` — returns a canonical `PROT_####` for anything that names a submission number, else None. `internal_submission()` accepts either a number or a hex id.

- [ ] **Step 1: Write the failing test**

```python
"""A submission number must reach the submission page.

PROT_0793 is ProtiFi LLC. Before this change the only way to reach it was its hex id
(1ed8b74497e4), which nobody has in hand.

Run:  python tests/test_submission_lookup.py
"""
import os, sys
os.environ["DELIMP_INTERNAL_MODE"] = "1"          # internal tables; see app/db.py
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import queries                            # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)

n = queries.normalize_submission_ref
check("PROT_0793 normalises to itself", n("PROT_0793") == "PROT_0793", repr(n("PROT_0793")))
check("lowercase prot_0793 normalises", n("prot_0793") == "PROT_0793", repr(n("prot_0793")))
check("bare 0793 normalises", n("0793") == "PROT_0793", repr(n("0793")))
check("bare 793 zero-pads", n("793") == "PROT_0793", repr(n("793")))
check("whitespace is tolerated", n("  PROT_0793 ") == "PROT_0793", repr(n("  PROT_0793 ")))
check("a hex id is NOT a number", n("1ed8b74497e4") is None, repr(n("1ed8b74497e4")))
check("a name is not a number", n("ProtiFi") is None, repr(n("ProtiFi")))
check("empty is None", n("") is None)
# 5 digits is not this scheme; refusing beats silently truncating to 4
check("five digits is refused", n("12345") is None, repr(n("12345")))

d = queries.internal_submission("PROT_0793")
sub = d.get("submission") or {}
check("PROT_0793 resolves to the ProtiFi submission",
      (sub.get("institute") or "") == "ProtiFi LLC", repr(sub.get("institute")))
check("resolving by number returns the same submission as the hex id",
      d.get("submission_id") == queries.internal_submission("1ed8b74497e4").get("submission_id"),
      f'{d.get("submission_id")} vs hex lookup')
check("an unknown number returns no submission, without raising",
      (queries.internal_submission("PROT_9999") or {}).get("submission") is None)

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python tests/test_submission_lookup.py`
Expected: `AttributeError: module 'app.queries' has no attribute 'normalize_submission_ref'`.

- [ ] **Step 3: Implement**

Add above `internal_submission` in `app/queries.py`:

```python
_SUB_REF = re.compile(r"^\s*(?:prot[_-]?)?(\d{1,4})\s*$", re.I)


def normalize_submission_ref(ref: str) -> str | None:
    """'0793' / '793' / 'prot_0793' -> 'PROT_0793'. Anything else -> None.

    Submission numbers are what people actually have in hand — they appear on the folder
    (PROT_0793), in the CoreOmics UI and in conversation. The hex submission_id is the join key
    everywhere else and nobody quotes it. Note a hex id like '1ed8b74497e4' must NOT match: it can
    contain digits, and silently reading it as a number would resolve the wrong submission.
    """
    m = _SUB_REF.match(ref or "")
    return f"PROT_{int(m.group(1)):04d}" if m else None
```

Then, as the first statement inside `internal_submission`, translate a number to the hex id:

```python
    sid = (submission_id or "").strip()
    ref = normalize_submission_ref(sid)
    if ref:
        # internal_id is the human number; every other table joins on the hex submission_id.
        hit = query(
            "SELECT submission_id FROM coreomics_submissions_cache WHERE internal_id = %s",
            (ref,), tables=["coreomics_submissions_cache"])
        if hit:
            sid = hit[0]["submission_id"]
```

Leave the rest of the function unchanged — it already queries by `sid`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python tests/test_submission_lookup.py` → `ALL PASS`

- [ ] **Step 5: Prove the tests have teeth**

Temporarily change `_SUB_REF` to `r"^\s*prot_(\d{4})\s*$"` (dropping the bare-number and
case-insensitive forms). Run the suite: the "bare 0793", "bare 793" and "lowercase" checks must
FAIL. Restore, confirm they pass. Paste both outputs in your report.

- [ ] **Step 6: Commit**

```bash
git add app/queries.py tests/test_submission_lookup.py
git commit -m "site: reach a submission by its number, not just its hex id"
```

---

### Task 2: Make search find submission numbers, and submissions with no searches

**Files:**
- Modify: `app/queries.py` (`internal_people_search`, around line 2268)
- Test: `tests/test_submission_lookup.py` (append)

**Interfaces:**
- Consumes: `normalize_submission_ref` from Task 1.
- Produces: `internal_people_search(q, limit)` unchanged in signature; its result rows may now carry `internal_id`, `institute` and `kind` (`"search"` or `"submission"`).

- [ ] **Step 1: Write the failing test (append before the summary block)**

```python
# --- search must find a submission by number, and submissions with no searches -------------
r = queries.internal_people_search("PROT_0793", 50)
check("searching PROT_0793 returns something", (r.get("total") or 0) > 0, str(r.get("total")))
check("...and it names the right institute",
      any("protifi" in str(x.get("institute") or x.get("co_institute") or "").lower()
          for x in r.get("rows") or []), "no ProtiFi row")

r2 = queries.internal_people_search("0793", 50)
check("bare 0793 also finds it", (r2.get("total") or 0) > 0, str(r2.get("total")))

# A submission with NO linked FRAN search must still be findable. PROT_0804 (UC Davis, 2026-09-08)
# has none; the old query was rooted in delimp_search_provenance so it could not return one.
r3 = queries.internal_people_search("PROT_0804", 50)
check("a submission with no linked search is still findable",
      (r3.get("total") or 0) > 0, str(r3.get("total")))
```

- [ ] **Step 2: Run to verify it fails**

Run: `python tests/test_submission_lookup.py`
Expected: the three new checks FAIL — the current query matches only `p.coreomics_submission_id::text ILIKE …` (the hex id) and selects `FROM delimp_search_provenance`, so a number finds nothing and an unlinked submission cannot appear.

- [ ] **Step 3: Implement**

In `internal_people_search`, before building `like`, normalise a number so the caller can type either form:

```python
    term = (q or "").strip()
    if len(term) < 2:
        return {"q": term, "total": 0, "rows": []}
    ref = normalize_submission_ref(term)          # '0793' -> 'PROT_0793', else None
    like = f"%{term}%"
```

Add `co.internal_id` to the existing WHERE clause, and add `%(ref)s` as an exact alternative:

```sql
           OR co.internal_id ILIKE %(like)s OR co.internal_id = %(ref)s
```

passing `{"like": like, "ref": ref, "limit": int(limit)}`.

Then union in a submissions-rooted branch so a submission with no search is reachable. Keep it a
separate query rather than an OUTER JOIN — the provenance query is the common path and must not
slow down:

```python
    subs = query(
        """
        SELECT co.submission_id, co.internal_id, co.institute,
               co.pi_first_name, co.pi_last_name,
               co.submitter_first_name, co.submitter_last_name,
               co.submitted_at::date AS co_submitted, co.num_samples AS co_num_samples
        FROM coreomics_submissions_cache co
        WHERE co.internal_id IS NOT NULL
          AND (co.internal_id ILIKE %(like)s OR co.internal_id = %(ref)s
               OR co.institute ILIKE %(like)s
               OR co.pi_last_name ILIKE %(like)s OR co.submitter_last_name ILIKE %(like)s)
        ORDER BY co.submitted_at DESC NULLS LAST
        LIMIT %(limit)s
        """,
        {"like": like, "ref": ref, "limit": int(limit)},
        tables=["coreomics_submissions_cache"],
    )
    have = {r.get("coreomics_submission_id") for r in rows}
    for s in subs:
        if s["submission_id"] not in have:          # do not duplicate a submission already listed
            s["kind"] = "submission"
            rows.append(s)
    for r in rows:
        r.setdefault("kind", "search")
    return {"q": term, "total": len(rows), "rows": rows}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python tests/test_submission_lookup.py` → `ALL PASS`

- [ ] **Step 5: Prove the tests have teeth**

Temporarily delete the `subs` block (leaving only the provenance query). Run: the
"a submission with no linked search is still findable" check must FAIL. Restore it. Then
temporarily remove `co.internal_id` from the provenance WHERE clause: the "searching PROT_0793
returns something" check must still pass (it is found via the subs branch) — note that in your
report, because it means that one check alone does not prove the WHERE change. Paste all outputs.

- [ ] **Step 6: Commit**

```bash
git add app/queries.py tests/test_submission_lookup.py
git commit -m "site: search by submission number, and find submissions with no searches"
```

---

### Task 3: The submissions list query and endpoint

**Files:**
- Modify: `app/queries.py` (add `internal_submissions`), `app/main.py` (add the route)
- Test: `tests/test_submission_lookup.py` (append)

**Interfaces:**
- Produces: `internal_submissions(q=None, limit=100, offset=0) -> dict` with keys `submissions`, `total`, `n_with_searches`, `locations_as_of`. Each row: `internal_id, submission_id, institute, pi, submitter, num_samples, submitted_at, n_searches, in_fran, run_count, service_folder, service_folder_win`.
- Endpoint: `GET /api/internal/submissions?q=&limit=&offset=`.

- [ ] **Step 1: Write the failing test (append)**

```python
# --- the submissions list -------------------------------------------------------------------
L = queries.internal_submissions(limit=25)
check("list returns submissions", len(L.get("submissions") or []) > 0, str(len(L.get("submissions") or [])))
check("list reports a total", (L.get("total") or 0) >= 790, str(L.get("total")))
first = (L.get("submissions") or [{}])[0]
check("newest first", str(first.get("internal_id") or "") >= "PROT_0790", repr(first.get("internal_id")))
for k in ("internal_id", "institute", "n_searches", "num_samples", "submitted_at"):
    check(f"row carries {k}", k in first, sorted(first.keys())[:12])
check("locations_as_of is reported", L.get("locations_as_of") is not None)

F = queries.internal_submissions(q="ProtiFi", limit=25)
check("filtering by institute works",
      any((s.get("internal_id") == "PROT_0793") for s in F.get("submissions") or []),
      [s.get("internal_id") for s in (F.get("submissions") or [])][:5])
P = queries.internal_submissions(q="0793", limit=25)
check("filtering by bare number works",
      any((s.get("internal_id") == "PROT_0793") for s in P.get("submissions") or []),
      [s.get("internal_id") for s in (P.get("submissions") or [])][:5])
sub793 = next((s for s in (F.get("submissions") or []) if s.get("internal_id") == "PROT_0793"), {})
check("PROT_0793 shows its 2 ingested searches", (sub793.get("n_searches") or 0) >= 0)
```

- [ ] **Step 2: Run to verify it fails**

Expected: `AttributeError: module 'app.queries' has no attribute 'internal_submissions'`.

- [ ] **Step 3: Implement the query**

```python
def internal_submissions(q: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """PRIVATE: every numbered CoreOmics submission, newest first, with what FRAN knows about it.

    Three states per row, and none of them is a blank cell: it has searches in FRAN; or its data is
    located on the share and not ingested; or we have no location for it at all. The third is
    common and honest — delimp_submission_service_dir was written once on 2026-06-24 and knows
    nothing after PROT_0724 — so the caller is also told when locations were last determined.
    """
    ref = normalize_submission_ref(q or "") if q else None
    like = f"%{(q or '').strip()}%"
    where, params = "", {"limit": int(limit), "offset": int(offset)}
    if q:
        where = """AND (co.internal_id ILIKE %(like)s OR co.internal_id = %(ref)s
                        OR co.institute ILIKE %(like)s OR co.pi_last_name ILIKE %(like)s
                        OR co.submitter_last_name ILIKE %(like)s
                        OR co.submitter_email ILIKE %(like)s)"""
        params.update({"like": like, "ref": ref})
    rows = query(
        f"""
        SELECT co.internal_id, co.submission_id, co.institute,
               NULLIF(TRIM(CONCAT_WS(' ', co.pi_first_name, co.pi_last_name)), '')        AS pi,
               NULLIF(TRIM(CONCAT_WS(' ', co.submitter_first_name, co.submitter_last_name)), '') AS submitter,
               co.num_samples, co.submitted_at::date AS submitted_at,
               (SELECT COUNT(*) FROM delimp_search_provenance p
                 WHERE p.coreomics_submission_id = co.submission_id)                       AS n_searches,
               sd.in_fran, sd.run_count, sd.service_folder, sd.service_folder_win
          FROM coreomics_submissions_cache co
          LEFT JOIN delimp_submission_service_dir sd ON sd.submission_id = co.submission_id
         WHERE co.internal_id IS NOT NULL {where}
         ORDER BY co.submitted_at DESC NULLS LAST, co.internal_id DESC
         LIMIT %(limit)s OFFSET %(offset)s
        """,
        params,
        tables=["coreomics_submissions_cache", "delimp_search_provenance",
                "delimp_submission_service_dir"],
    )
    total = query(
        f"""SELECT COUNT(*) FROM coreomics_submissions_cache co
             WHERE co.internal_id IS NOT NULL {where}""",
        params, tables=["coreomics_submissions_cache"], fetch="val") or 0
    as_of = query("SELECT MAX(matched_at)::date AS d FROM delimp_submission_service_dir",
                  tables=["delimp_submission_service_dir"])
    return {"submissions": rows, "total": int(total),
            "n_with_searches": sum(1 for r in rows if (r.get("n_searches") or 0) > 0),
            "locations_as_of": (as_of[0]["d"] if as_of else None)}
```

- [ ] **Step 4: Add the endpoint** in `app/main.py`, beside the other `/api/internal` routes:

```python
@app.get("/api/internal/submissions")
def api_internal_submissions(q: str = "", limit: int = 100, offset: int = 0):
    """FULL only: the submission directory — every numbered CoreOmics submission, newest first."""
    if not db.is_full():
        raise HTTPException(404, "Not found.")
    return ok(_safe(lambda: queries.internal_submissions(q or None, limit, offset),
                    {"submissions": [], "total": 0}))
```

- [ ] **Step 5: Run the test to verify it passes** → `ALL PASS`

- [ ] **Step 6: Prove the tests have teeth**

Temporarily change `ORDER BY co.submitted_at DESC` to `ASC`: the "newest first" check must FAIL.
Restore. Temporarily drop the `OR co.internal_id = %(ref)s` term: the "filtering by bare number"
check must FAIL. Restore. Paste both outputs.

- [ ] **Step 7: Commit**

```bash
git add app/queries.py app/main.py tests/test_submission_lookup.py
git commit -m "site: a submissions directory query and endpoint"
```

---

### Task 4: The Submissions tab

**Files:**
- Modify: `app/static/app.js` (route, nav button, new `renderSubmissions`), `app/templates/index.html` (nav button)

**Interfaces:**
- Consumes: `GET /api/internal/submissions` from Task 3.
- Produces: route `#/submissions`, `renderSubmissions()`.

- [ ] **Step 1: Add the route and nav**

In `app.js`'s `route()` switch, beside `case 'collaborators'`:

```javascript
    case 'submissions': return renderSubmissions();
```

In `index.html`, before the Collaborators nav button:

```html
        <button data-view="submissions" id="nav_subs" class="navbtn hidden px-3 py-1.5 rounded-lg font-semibold text-accent-400 hover:text-white">📋 Submissions</button>
```

Find where `#nav_collab` has `hidden` removed for the FULL tier and do the same for `#nav_subs`, so the tab appears under exactly the same condition.

- [ ] **Step 2: Implement the view**

```javascript
/* ---------- INTERNAL: submission directory (private deployment only) ---------- */
async function renderSubmissions(){
  view.innerHTML=`<section class="mb-5 fade-in"><h1 class="text-2xl font-extrabold text-white tracking-tight">📋 Submissions <span class="text-[11px] font-bold text-rose-300 align-middle">CONFIDENTIAL</span></h1>
    <p class="text-slate-400 text-sm mt-1">Every CoreOmics submission, newest first — whether its data is in FRAN, still on the share, or not located yet.</p></section>
    <div class="relative mb-4 max-w-xl">
      <input id="subQ" placeholder="Submission number, institute, PI, submitter or email…" onkeydown="if(event.key==='Enter')renderSubmissions()"
        class="w-full bg-ink-800/70 border border-white/10 rounded-xl px-4 py-2.5 pl-10 text-sm placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-accent/50" />
      <svg class="absolute left-3 top-3 text-slate-500" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
    </div>
    <div class="glass card p-4 fade-in" id="subsBody"><div class="skeleton h-64 rounded-xl"></div></div>`;
  const q = (window.__SUBS_Q__||'');
  const el = $('#subQ'); if(el){ el.value=q; el.oninput=e=>{ window.__SUBS_Q__=e.target.value; }; }
  try{
    const d = await api(`/api/internal/submissions?limit=100&q=${encodeURIComponent(q)}`);
    const rows = d.submissions||[];
    if(!rows.length){ $('#subsBody').innerHTML=empty('No submissions match.'); return; }
    // Three states, never a blank cell. "not located" is the honest answer for anything the
    // 2026-06-24 disk-match never saw, which is everything after PROT_0724.
    const state = s => (s.n_searches>0)
      ? `<span class="text-emerald-300">✅ ${fmt(s.n_searches)} search${s.n_searches===1?'':'es'}</span>`
      : (s.run_count!=null || s.service_folder)
        ? `<span class="text-accent-400">📦 ${s.run_count!=null?fmt(s.run_count)+' runs':''} on the share</span>
           ${s.service_folder?`<div class="text-[10px] text-slate-500 font-mono break-all mt-0.5" title="${esc(s.service_folder_win||'')}">${esc(s.service_folder)}</div>`:''}
           <button onclick="event.stopPropagation();exportReport('${esc(s.submission_id)}',this,'resubmit')" class="mt-1 px-1.5 py-0.5 rounded text-[10px] font-semibold bg-plum/20 text-plum hover:bg-plum/30" title="Download a HIVE/Flinders re-search brief for this un-ingested data">🔄 Re-search this data</button>`
        : `<span class="text-slate-600">— not located</span>`;
    $('#subsBody').innerHTML =
      `<div class="text-xs text-slate-500 mb-3">${fmt(d.total)} numbered submission${d.total===1?'':'s'}${d.locations_as_of?` · share locations as of ${esc(String(d.locations_as_of))}`:''}</div>`
      + table(['Submission','Institute','PI · submitter','Samples','Submitted','In FRAN?'],
          rows.map(s=>[
            `<span class="font-mono font-semibold text-accent-400">${esc(s.internal_id)}</span>`,
            esc(s.institute||'—'),
            `<span class="text-xs text-slate-400">${esc([s.pi,s.submitter].filter(Boolean).join(' · ')||'—')}</span>`,
            s.num_samples!=null?fmt(s.num_samples):'—',
            `<span class="font-mono text-xs">${esc(String(s.submitted_at||'—'))}</span>`,
            state(s)]),
          rows.map(s=>`go('submission','${esc(s.internal_id)}')`));
  }catch(e){ dbError(e,'#subsBody'); }
}
```

- [ ] **Step 3: Verify in a browser**

`node --check app/static/app.js` must pass. Then confirm by reading the code that clicking a row
calls `go('submission', 'PROT_0793')` — which Task 1 made resolvable. State in your report that you
could not verify the rendered page without a running server, if that is the case; do not claim a
visual check you did not perform.

- [ ] **Step 4: Commit**

```bash
git add app/static/app.js app/templates/index.html
git commit -m "site: a Submissions tab, fronting the page that already existed"
```

---

### Task 5: Collaborators — search first, most recent first, labs grid relocated

**Files:**
- Modify: `app/queries.py` (`internal_collaborators`), `app/static/app.js` (`renderCollaborators`, add `case 'labs'`)
- Test: `tests/test_collaborator_recency.py`

**Interfaces:**
- Produces: each collaborator row gains `last_run` (an ISO date or None); `internal_collaborators()` returns them sorted by `last_run` descending.

- [ ] **Step 1: Write the failing test**

```python
"""Collaborators are ordered by when work was last ACQUIRED, not when it was ingested.

Ingest date is useless for this: 1,899 of 2,086 searches were ingested in a single June 2026
backfill, so ordering by it reproduces backfill sequence. raw_files.acquisition_date is the real
signal — 86% coverage, and all 184 collaborator groups have one.

Run:  python tests/test_collaborator_recency.py
"""
import os, sys
os.environ["DELIMP_INTERNAL_MODE"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import queries                            # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)

d = queries.internal_collaborators()
rows = d.get("collaborators") or []
check("collaborators returned", len(rows) > 100, str(len(rows)))
check("every row carries last_run", all("last_run" in r for r in rows))
check("searches count is still present", all("n_searches" in r for r in rows))
dates = [str(r["last_run"]) for r in rows if r.get("last_run")]
check("most rows have a last_run", len(dates) > 0.9 * len(rows), f"{len(dates)}/{len(rows)}")
check("sorted by last_run descending", dates == sorted(dates, reverse=True),
      f"first five {dates[:5]}")

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
```

- [ ] **Step 2: Run to verify it fails** — no `last_run` key exists yet.

- [ ] **Step 3: Implement**

In `internal_collaborators`, add the acquisition date to the grouped query. Measured at 0.14 s with
both joins, so no precomputation is needed:

```sql
        SELECT p.service_customer AS raw,
               -- DISTINCT is load-bearing, not decoration. The two LEFT JOINs below change the
               -- grain to (search x raw_file), so a bare COUNT(*) here counts PAIRS, not searches.
               -- Shipped once and caught by the final review: it read 7,476 corpus-wide against 949
               -- real provenance rows, NIST 1,046 against 33, and the directory contradicted its own
               -- drill-down one click apart. n_pis/n_projects survived only because they already had
               -- DISTINCT; MAX() is fan-out safe. Audit EVERY aggregate here against the join grain.
               COUNT(DISTINCT p.search_id) AS n_searches,
               COUNT(DISTINCT NULLIF(p.pi,'')) AS n_pis,
               COUNT(DISTINCT NULLIF(p.project,'')) AS n_projects,
               COUNT(DISTINCT p.search_id) FILTER (WHERE p.coreomics_submission_id IS NOT NULL
                                   OR p.sample_submission_id IS NOT NULL) AS n_lims_linked,
               MAX(p.service_campus) AS campus, MAX(p.service_source) AS source,
               MAX(rf.acquisition_date)::date AS last_run
        FROM delimp_search_provenance p
        LEFT JOIN search_raw_files srf ON srf.search_id = p.search_id
        LEFT JOIN raw_files rf ON rf.raw_path = srf.raw_path
        WHERE p.service_customer IS NOT NULL
        GROUP BY p.service_customer
```

with `tables=["delimp_search_provenance", "search_raw_files", "raw_files"]`. Carry `last_run` into
the merged dict (take the MAX across merged raw variants, since one canonical collaborator can span
several folders), and sort:

```python
        m["last_run"] = max([x for x in (m.get("last_run"), r["last_run"]) if x], default=None)
...
    keep = sorted((m for m in merged.values() if m["flag"] == "keep"),
                  key=lambda m: (m["last_run"] is None, m["last_run"] or "", m["n_searches"]),
                  reverse=True)
```

- [ ] **Step 4: Run the test to verify it passes** → `ALL PASS`

- [ ] **Step 5: Prove the test has teeth**

Temporarily sort by `-m["n_searches"]` (the old order). The "sorted by last_run descending" check
must FAIL. Restore. Paste both outputs.

- [ ] **Step 6: Re-layout the page**

In `renderCollaborators`: put the search box first (same markup as Task 4's, id `collabQ`, filtering
the table client-side on collaborator name / CoreOmics PI / institute); add a `Last run` column
immediately after `Collaborator`, rendered `font-mono text-xs`; drop the `PIs` column to make room;
change the caption to `${fmt(rows.length)} collaborators, most recent work first`. Replace the
`loadLabsByInstitution()` call and its `#labsByInst` container with a link block:

```javascript
    +`<div class="mt-4 flex items-center justify-between gap-4 p-3 rounded-xl bg-ink-900/40 border border-dashed border-white/10">
        <div class="text-xs text-slate-400">Labs by institution — every CoreOmics lab we hold data for, including data on the share that is not yet ingested.</div>
        <button onclick="go('labs')" class="shrink-0 px-3 py-1.5 rounded-lg text-xs font-semibold text-ink-900" style="background:linear-gradient(180deg,#FFCF40,#FFBF00)">Open labs view →</button>
      </div>`
```

Add `case 'labs': return renderLabs();` to the router and a `renderLabs()` that renders the heading
and calls the EXISTING `loadLabsByInstitution()` into a `#labsByInst` container. Do not modify
`loadLabsByInstitution`, `internal_labs_by_institution` or `internal_lab` — the labs view keeps
working exactly as it does today, it simply lives at its own route.

- [ ] **Step 7: Commit**

```bash
git add app/queries.py app/static/app.js tests/test_collaborator_recency.py
git commit -m "site: collaborators sorted by most recent work; labs grid gets its own route"
```
