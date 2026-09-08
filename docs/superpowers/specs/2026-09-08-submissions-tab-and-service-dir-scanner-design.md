# Submissions tab, collaborator simplification, and a regenerable service-directory scanner

- **Date:** 2026-09-08
- **Status:** approved design, not yet implemented
- **Touches:** `app/queries.py`, `app/main.py`, `app/static/app.js`, new `ingest/scan_service_dir.py`

## Problem

Three related complaints, one root cause.

### 1. There is no way to look up a submission number

`https://fran.stan-proteomics.org/#/run/8221f5fc-…` is submission PROT_0793 — ProtiFi LLC — but
nothing in the UI says so, and there is no route that accepts `PROT_0793`. The data is present:
`internal_submission()` already returns the CoreOmics record, its samples, every linked FRAN search
AND the share location. It simply has no front door, and it is keyed by the hex `submission_id`
rather than the human number.

### 2. The collaborators page does too much

It stacks three tools: a 184-row client table, a per-client drill-down, and a "Labs by institution"
grid that renders every PI lab across every institution as a chip. Asked what the page is for, the
answer was **look up one known client** and **find recent/active work** — not browsing labs, not
auditing attribution. The largest element on the page serves the use nobody picked.

### 3. The un-ingested inventory is stale and cannot be regenerated

`delimp_submission_service_dir` maps each CoreOmics submission to where its raw data lives on the
share, with `run_count` and `in_fran`. 1,862 rows, of which only 238 are `in_fran` — so **1,624
submissions have data located but not ingested**. That inventory is the answer to "what still needs
ingesting", and today it is reachable only through the labs grid.

It is also decaying:

- `matched_at` is **2026-06-24** for every row, and `matched_by` is `ai-disk-match`. Nothing after
  that date has a location — which is exactly the recent work that matters.
- `write_submission_service_dir.py` reads `disk_match_*.tsv` from
  `/private/tmp/claude-501/-Users-brettphinney-Documents-claude/f2dfbf3c…/scratchpad/`, an ephemeral
  Claude scratchpad **that no longer exists**. The table cannot be rebuilt from anything in the repo.
- Only 551 of its 1,862 rows join to a submission that has a `PROT_` number at all.

This is the third instance of the same pattern found today, after the CoreOmics cache (one import,
2026-06-17, 83 days stale) and `delimp_spectrum_regen_queue`: a one-shot artifact wearing an
integration's clothes. Simplifying the collaborators page without relocating this inventory would
hide a dataset that is already quietly going out of date.

## Goals

- A submission number is a first-class way to navigate FRAN: `PROT_0793` resolves to a page.
- The un-ingested inventory becomes MORE visible than it is today, not less, and is expressed
  per-submission ("10 runs on the share at R:\…") rather than as a colour on a chip.
- The collaborators page becomes a lookup, because that is what it is used for.
- The inventory is regenerable by a script in version control, on a schedule.

## Non-goals

- Raising submission↔search linkage above its current 26%. Only 209 of 790 numbered submissions have
  a FRAN search attached, and path derivation is a dead end: exactly 2 searches carry a `PROT_####`
  in their path, both registered on 2026-09-08. A real linker (sample-name / service-folder / email
  matching) is worthwhile and is its own project.
- Deleting the labs-by-institution view. It moves; it does not disappear.
- Re-doing the AI disk-match's fuzzy matching at full fidelity (see "Matching", below).

## Part 1 — Submissions tab

### Routes

| Route | Purpose |
|---|---|
| `#/submissions` | List, newest first, with a search box |
| `#/submission/PROT_0793` | Detail. Also accepts the hex id, so existing links keep working |

### List

New query `internal_submissions(q=None, limit, offset)` over `coreomics_submissions_cache` joined to
`delimp_submission_service_dir` and a count of linked searches. Columns:

```
Submission · Institute · PI · Submitter · Samples · Submitted · FRAN status
```

`FRAN status` is never blank. It is one of:

- `✅ N searches` — linked, click through
- `📦 N runs on the share` — located by the scanner, not ingested; the folder is shown
- `— no data located` — neither, which is itself informative

Default sort is `submitted_at DESC`, which makes this the recent-work view.

### Detail

`internal_submission()` already returns everything needed and is unchanged apart from accepting an
`internal_id`. Resolution order: if the argument matches `^PROT_\d{4}$` (case-insensitive, and
bare `793`/`0793` normalised to `PROT_0793`), look up `internal_id`; otherwise treat it as the hex
`submission_id`. Both forms must keep working — `internal_lab()` already links by hex.

The page shows CoreOmics metadata, the sample table, linked FRAN searches, and — prominently when
`in_fran` is false — the share location and run count, because that is the actionable state.

### Search

One box, present on both this tab and collaborators, accepting: a submission number (`793`,
`0793`, `PROT_0793`), an institute, a PI or submitter name, or an email. A submission number routes
straight to the detail page. Everything else lists matches.

`internal_people_search()` already exists and its docstring claims "submission number", but it has
two specific defects that make it unable to answer this question today:

1. It matches `p.coreomics_submission_id::text ILIKE …` — the **hex** id. It never looks at
   `co.internal_id`, so typing `PROT_0793` or `0793` returns nothing. Fix: add `co.internal_id`
   to the WHERE clause, and normalise a bare `793`/`0793` to `PROT_0793` before matching.
2. It selects `FROM delimp_search_provenance`, so a submission can only be found if it already has
   a FRAN search. That structurally excludes the 581 numbered submissions with no linked search and
   every one of the 1,624 located-but-un-ingested folders — precisely the rows the new tab exists to
   surface. Fix: a second branch rooted in `coreomics_submissions_cache`, unioned in, so a
   submission is findable before any data is ingested for it.

Both are small changes to an existing function rather than a new search subsystem.

## Part 2 — Collaborators simplification

- The page leads with the search box. The 184-row table stays but below it, and is filtered by the
  same box.
- The **"Labs by institution" grid moves off this page.** Its unique content — which labs have data
  that is not in FRAN — is better served by the Submissions tab's `📦` status, per submission, with
  the folder and run count. What remains genuinely browse-shaped (labs grouped by institution) moves
  behind an explicit link, e.g. `#/labs`, reachable from both pages.
- `/api/internal/labs`, `internal_labs_by_institution()` and `internal_lab()` are **unchanged**. Only
  where the UI renders them changes. Nothing is deleted.

## Part 3 — `ingest/scan_service_dir.py`

Replaces the dead AI disk-match with something repeatable.

### Inventory (deterministic, the valuable half)

Walk `/nfs/lssc0/flinders/proteomics/Data/lab/service` at `campus/client/project` depth — 396
on-campus and 354 off-campus client folders today. For each project folder record:

- `service_folder` (`on_campus/SadeghC/Chechneva_beadsMusM_iun26`) and its `R:\` spelling, matching
  the existing column format exactly so old and new rows are interchangeable
- `run_count`: `.d` directories plus `.raw` files, counted without descending into `.d`
- `in_fran`: whether any `delimp_search_provenance.service_customer` / `output_dir` resolves here

Skip `Thumbs.db`, `htrms_quarantine_*` and the other non-client entries at campus level.

This alone answers "what is on the share and not ingested", for every folder, with no matching
required — and unlike the current table it is true as of the last run.

### Matching submissions to folders (heuristic, kept honest)

The AI disk-match used clues like `submitter+date+organism` and stored a `match_confidence`. A
script cannot fully reproduce that judgement, and should not pretend to. It attempts only
defensible matches:

- client folder name vs PI or submitter surname (normalised)
- submission date within a window of the folder's newest raw file
- organism agreement where both are known

Each row records `matched_by = 'scan_service_dir/v1'` and its `clue`, alongside the existing
`ai-disk-match` rows. **A scanner match never overwrites an `ai-disk-match` row of higher
confidence** — the June matches encode human-reviewed judgement that this script cannot re-derive.
Where the scanner cannot match, the folder is still inventoried with a NULL `submission_id`, so
un-ingested data is visible even when we cannot attribute it. That requires relaxing the current
one-row-per-submission shape; see "Schema change".

### Schema change

`delimp_submission_service_dir` is currently one row per submission and keyed on `submission_id`. The
inventory needs rows for folders with no matched submission. Add a surrogate key and a
`UNIQUE (service_folder)`, keep `submission_id` nullable, and add `scanned_at`. Existing rows are
preserved; `matched_at`/`matched_by` continue to distinguish their origin.

### Schedule

Weekly, from the Hive cron, with the `LOGNAME`/`set -u` guard every cron script here needs. It walks
a large share, so it is an sbatch job rather than login-node work. It writes `scanned_at` on every
run, so staleness is visible in the data — the specific failure that made the June table rot
unnoticed.

## Functionality that MUST survive this change

Three exports exist today and are load-bearing workflows, not decoration. Any new page that
replaces or fronts an existing one carries them forward.

| Endpoint | Builder | Produces | Feeds |
|---|---|---|---|
| `/api/export/diann_report/{search_id}` | `build_report_parquet` | DIA-NN-style `report.parquet` | `limpa::readDIANN()` / DE-LIMP (HF or local) |
| `/api/export/research_brief/{search_id}` | `build_research_brief` | markdown brief | a HIVE Claude running the proteomics-pipeline skill |
| `/api/export/resubmit_brief/{submission_id}` | `build_resubmit_brief` | markdown brief | a HIVE/Flinders Claude, to re-search UN-INGESTED data |

Where they appear now, and must still appear:

- Run/search page: `⬇ report.parquet → DE-LIMP`, with the footnote explaining parquet → DE-LIMP and
  HIVE brief → markdown packet (`app.js:475`, `:478`).
- Collaborator search rows: a per-search `⬇ Export` (`app.js:1887`).
- Lab page, per un-ingested submission: `📦 on share · N runs`, the `service_folder` in mono, and
  `🔄 Re-search this data` (`app.js:2012`, `:2057`). **The Submissions tab reproduces exactly this
  trio** — it is the pattern being relocated, not replaced.

### The resubmit brief depends on the stale table

`build_resubmit_brief` reads `service_folder` and `service_folder_win` from
`delimp_submission_service_dir` to tell the receiving Claude where the raw data is. When there is no
row it still builds a brief, but with no paths — so for every submission after PROT_0724 the
"re-search this data" workflow silently produces a packet that cannot locate the data.

That makes Part 3's scanner a prerequisite for keeping an EXISTING feature working, not merely a
nicety for a new status column. It is the reason the scanner is sequenced first in the rollout.

## Testing

- `PROT_0793`, `0793` and `793` all resolve to the same submission; the hex id still resolves.
- A submission with searches shows `✅`; one with only a service dir shows `📦` with folder and count;
  one with neither shows the explicit no-data state.
- The scanner is idempotent: a second run changes only `scanned_at`.
- A scanner match does not overwrite a higher-confidence `ai-disk-match` row.
- A folder with no matchable submission still appears in the inventory.
- `/api/internal/labs` returns identical output before and after the UI move.

## Rollout

1. `scan_service_dir.py` first, so the tab is built against a current inventory rather than one
   frozen in June.
2. Submission-number resolution + list endpoint.
3. The tab, then the collaborators re-layout.
4. Link `PROT_0793`'s two searches (`coreomics_submission_id = 1ed8b74497e4`), which are currently
   NULL, as the first fully-wired example.
