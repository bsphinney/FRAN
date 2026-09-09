# Search results: a protein × sample heatmap, and coverage against the corpus

**Date:** 2026-09-09
**Status:** design, pending Brett's review

## The problem

A search detail page (`#/run/<search_id>`) currently reports *counts* — 222 raw files,
3,721,585 precursors, 6,398 proteins, 22,234 peptides — and then lists run names. It never shows the
result itself. You cannot see which proteins were identified, how they differ between samples, or
whether anything about the experiment is unusual.

Brett's ask, in his words: *"the search should display the data somehow, not just display the
names"* — a heatmap like DE-LIMP and Spectronaut show, proteins on Y, sample names on X, below what
is already there; clicking a protein opens its sequence coverage *"both in the current experiment and
also across the FRAN corpus"*; and the heatmap *"coloured both by the peptides in this experiment and
also across the entire FRAN corpus"*.

## What already exists (measured 2026-09-09, not assumed)

**The matrix is already in Postgres.** `delimp_proteins` is one row per (search, sample, protein):

| column | |
|---|---|
| `search_id`, `raw_path` | the cell coordinates — sample is `raw_path` |
| `protein_group`, `gene` | the row label |
| `normalized_intensity`, `intensity` | the value |
| `n_unique_peptides`, `n_precursors`, `pg_q_value`, `is_contaminant` | per-cell detail |

For `PROT_0793_search_mouse` (`8221f5fc-492e-5c9d-a08d-542cfdb48791`): **480,123 rows, 6,388
proteins × 222 samples, every intensity non-null**. Top proteins by sample-presence are Alb, Mup2,
Fabp1, Eef1a2, Cps1, Bhmt — a coherent mouse liver profile, so the numbers are real, not artefacts.

**Coverage is already fully built** and must be reused, not reinvented:
- `app/coverage.py` — `fetch_uniprot_sequence(acc)`, `map_coverage(seq, peptides)`
- `queries.protein_coverage_peptides(pg)` — cached; queries `delimp_precursors WHERE protein_group = %s`
  via `idx_prec_protein_group` in ~0.2 s
- `GET /api/protein/{pg}/coverage` — returns sequence, mapped coverage, `coverage_pct`
- `app/static/app.js:1696-1716` renders the bar, the percentage, and already handles the two hard
  cases: custom/recombinant constructs with no public sequence, and UniProt lookups that fail

`delimp_precursors` carries `search_id`. So **scoping coverage to one search is an added parameter,
not a new feature.**

**Corpus-wide peptide counts are already precomputed:** `delimp_protein_peptide_count`
(520,406 protein groups, `protein_group`, `n_peptides`, `n_precursor_rows`, `computed_at`).

## The one hard constraint

6,388 × 222 = **1,418,136 cells**. That cannot render, and would be unreadable if it did. The
heatmap is always top-N rows (N adjustable), and *how those rows are chosen* is the whole design.

Measured cost of each ranking rule on the mouse search:

| mode | time | verdict |
|---|---|---|
| most abundant (mean intensity) | **0.52 s** | live |
| most variable (CV across samples) | **3.86 s** | live |
| corpus rarity (searches per protein) | **53 s** | must be precomputed |

## Design

### 1. `delimp_protein_corpus_reach` — a precomputed table

```
protein_group   TEXT PRIMARY KEY
n_searches      INTEGER   -- how many FRAN searches have ever reported it
n_samples       INTEGER   -- how many distinct raw files
computed_at     TIMESTAMPTZ
```

Built by `SELECT protein_group, count(DISTINCT search_id), count(DISTINCT raw_path) FROM
delimp_proteins GROUP BY 1`, refreshed by the existing weekly Hive cron alongside the matviews —
the same pattern `delimp_protein_peptide_count` already follows. Reads become a keyed lookup.

**OPEN MEASUREMENT the plan must make before building:** how long the full rebuild takes across all
~520k protein groups. The 53 s figure is for one search's 6,388 proteins; the full scan is larger.
If it exceeds the cron's budget, the fallback is an incremental rebuild keyed on searches ingested
since `computed_at`. Do not write the cron until this is measured.

**Staleness must be visible.** This table is exactly the shape of artefact this codebase has been
bitten by repeatedly — a one-shot computation wearing an integration's clothes (the CoreOmics cache
sat 83 days stale; `delimp_submission_service_dir` 2.5 months). The UI states `computed_at`, and a
missing or stale row renders as "not computed" rather than as zero. **A protein with no reach row
must never colour as "seen in 0 searches"** — absence and zero are different, the same distinction
`count_runs() -> int | None` enforces in the scanner.

### 2. The heatmap panel

Rendered **below the existing Runs table**, not replacing anything.

- **Rows:** top N proteins (default 50, adjustable), with a **selector for all three rules**:
  most variable (default), most abundant, rarest in corpus. Brett asked for all three switchable.
  Default is CV because it shows what *differs* between samples — the biology — rather than what is
  merely abundant, which for this search is Alb/Mup2/Fabp1 every time.
- **Columns:** samples (`raw_path`, basename shown), in acquisition order where
  `raw_files.acquisition_date` is available, else name order. Acquisition order makes batch drift and
  column-wise QC problems visible as vertical bands.
- **Cell colour, mode A — intensity:** log2 `normalized_intensity`, per-row z-scored so a row is
  readable regardless of the protein's absolute abundance. A missing (protein, sample) pair is a
  distinct "not identified" colour, never zero — a protein absent from a sample is information.
- **Cell colour, mode B — corpus rarity:** each *row* tinted by `n_searches` from
  `delimp_protein_corpus_reach`. Common housekeeping proteins sit cool; something the lab has seen
  in three searches glows.

  **Rarity is a colour, not the default sort.** Measured: the rarest proteins in this search are
  `F6RH57`, `F6SH14`, `F6QI43` — unreviewed TrEMBL accessions with no gene symbol, each seen in
  exactly one search. Sorting by rarity surfaces junk; colouring by it while sorting by CV is where
  the value is, because a biologically interesting protein *glows* on its own.

  This is the part no other tool can draw. Spectronaut and DE-LIMP see one experiment. FRAN sees
  ~2,000 searches, so it can say *"this protein is unusual for this lab"* — that is the reason to
  build this in FRAN rather than export to something else.

### 3. Protein click → two-track coverage

Extends the existing widget; does not replace it.

- `protein_coverage_peptides(pg, search_id=None)` gains an optional scope. When set, the
  `delimp_precursors` query adds `AND search_id = %s` — the index already covers `protein_group`,
  so this narrows an already-fast lookup.
- `GET /api/protein/{pg}/coverage?search_id=…` returns **both** peptide sets and both mapped tracks.
  Without `search_id`, behaviour is byte-identical to today — existing callers are untouched.
- The UI draws two tracks on one sequence: **this experiment** above, **the whole corpus** below,
  with both percentages. The gap is the payload: *"you covered 34% here; the corpus has covered 71%
  across 74 searches — here are the regions you missed."* That is a re-search recommendation
  nothing else in the lab's stack can produce.
- The existing custom-construct and missing-sequence paths keep working unchanged.

## Interfaces

```
internal: delimp_protein_corpus_reach (protein_group PK, n_searches, n_samples, computed_at)

queries.search_protein_matrix(search_id, mode='cv', limit=50) -> {
    proteins: [{protein_group, gene, n_searches, n_samples_corpus, cells:[…]}],
    samples:  [{raw_path, basename, acquisition_date}],
    mode, limit, corpus_reach_computed_at, n_proteins_total, n_samples_total }

GET /api/search/{search_id}/matrix?mode=cv|abundance|rarity&limit=50
GET /api/protein/{pg}/coverage?search_id=…      (search_id optional; omitted == today)
```

## Constraints this must honour

- **Do not break the three exports.** `/api/export/diann_report/{search_id}` (report.parquet →
  DE-LIMP/limpa), `/api/export/research_brief/{search_id}`, `/api/export/resubmit_brief/{submission_id}`.
  The search page keeps every affordance it has today; the heatmap is added below.
- **Governance.** Every `query()` passes `tables=[...]` naming every table it touches. Decide
  explicitly whether `delimp_protein_corpus_reach` is public or internal and record why —
  `delimp_proteins` is public, so a reach table derived only from it carries no new confidentiality,
  but the decision must be deliberate, not incidental.
- **UI vocabulary:** `glass` + `card`, 18px radii, `text-accent-400` (#FFCF40), `kpi-num`, the
  existing `table()`/`stat()` helpers. No new colour system beyond the heatmap's own scale, which
  must be colour-blind safe (viridis-like, not red/green).
- **Every test must be proven able to fail.** These two plans produced *ten* defects of one shape:
  the assertion was correct, the witness or input could not discriminate. Break the behaviour, run,
  paste the FAIL, restore, and end each teeth-proof with `git diff` showing a clean restore —
  a restore that dropped half a WHERE clause shipped green on this very branch.
- **Performance is a requirement, not a footnote.** The panel loads on a page that already renders
  200 runs. The matrix query must be measured on `PROT_0793_search_mouse` (the largest search in the
  corpus: 222 samples, 6,388 proteins) and its time reported, not assumed.

## Deliberately out of scope

- Statistics — no differential expression, no p-values. LIMPA/DE-LIMP does that, and the
  `report.parquet` export already feeds it. This panel is for *seeing*, not testing.
- Clustering/dendrograms on either axis. Row order is a stated rule; adding hierarchical clustering
  is a separate question once the panel is in use.
- Editing, annotation, or saved views.

## Risks

| risk | mitigation |
|---|---|
| Full corpus-reach rebuild too slow for the cron | Measure before building; incremental fallback keyed on `computed_at` |
| Reach table silently goes stale | `computed_at` shown in the UI; absent row renders "not computed", never 0 |
| Rarity mode surfaces TrEMBL junk | Measured and accepted; rarity is a colour, CV is the default sort |
| 1.4M cells | Top-N with the rule exposed; N and mode both user-controlled |
| Heatmap slows an already-heavy page | Panel loads lazily after the Runs table; its query is separately timed |
