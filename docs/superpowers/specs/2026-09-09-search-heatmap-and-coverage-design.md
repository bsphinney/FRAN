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

**MEASURED 2026-09-09: the full rebuild is 637 s (10.6 min) for 618,520 protein groups.** That fits
a weekly cron comfortably; no incremental rebuild is needed, and the fallback design is dropped.

**But the same measurement invalidated the rarity key, and this is the important part.** The
distribution over `protein_group` is:

    median searches per protein group:  2
    maximum:                            1,176
    seen in exactly ONE search:         290,944 of 618,520  (47%)

Nearly half of all protein groups are seen exactly once, so "rare" is the *normal* state and a
colour scale keyed on `protein_group` would push ~47% of rows to the extreme end and discriminate
nothing. The cause is already documented in this codebase (`app/static/app.js:1556`): protein-*group*
strings are raw DIA-NN group strings that vary between FASTAs, so one real protein becomes many group
strings. Keyed this way, "corpus rarity" would largely be measuring **FASTA string variation and
calling it biology** — a plausible-looking feature that is quietly wrong, which is the failure mode
this spec exists to avoid.

**Therefore rarity is keyed on `gene`, not `protein_group`**, since gene symbols are stable across
FASTAs. `delimp_proteins.gene` is populated. The reach table becomes:

    gene            TEXT PRIMARY KEY
    n_searches      INTEGER
    n_samples       INTEGER
    computed_at     TIMESTAMPTZ

**MEASURED, and the rarity colouring is VIABLE.** Gene-level rebuild is 121 s for 298,391 distinct
genes (5× cheaper than the protein_group form), and only 4% of `delimp_proteins` rows carry no gene.

The gene-level *global* distribution is still singleton-heavy — median 2, 40% seen once, deciles
`[1,1,1,2,2,3,3,5,16]` — which by the criterion originally written here would have CUT the feature.
That criterion was measuring the wrong denominator. The heatmap never colours the global corpus; it
colours ~50 real proteins from one search. Measured on the rows that would actually be shown (top-50
by CV, `PROT_0793_search_mouse`):

    reach:      min 2   median 114.5   max 591
    seen-in-1:  0 of 50
    deciles:    [34, 63, 89, 99, 114, 152, 183, 221, 277]

Zero singletons and a spread across two orders of magnitude — the scale discriminates. And it
discriminates *biologically*: the common end is Aldoa (278), Ywhah (283), Hsp90ab1 (294), Jup (295),
TPM2 (591) — housekeeping proteins this lab sees in everything. The rare end is Mup2 (33) and Mup21
(21), major urinary proteins that are mouse-specific so only mouse work sees them, and Ighg3 (2) and
Igkv1-110 (12), immunoglobulins that vary per animal. That is real signal, not FASTA noise.

The global 40% is the corpus-wide long tail of one-off FASTA entries, which never reaches a top-CV
row set.

**Consequences for the build:**
- Scale must be **log or percentile**, never linear — the within-view range is 2 to 591 and the
  corpus range runs to 1,849.
- Percentile the scale **against the displayed rows**, not the whole corpus, or the global singleton
  mass flattens everything again. This is the finding above, encoded.
- Rows with no gene (4%) render "not computed", never as rare. Absence is not zero.
- Because reach is keyed on `gene`, a protein group with no gene symbol has no reach value; the
  heatmap still shows it, uncoloured in rarity mode, labelled as such.

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
| Full corpus-reach rebuild too slow for the cron | RESOLVED by measurement: 637 s for 618,520 groups, fits the weekly cron |
| Rarity keyed on `protein_group` measures FASTA string variation, not biology | RESOLVED: 47% of groups seen once. Keyed on `gene` instead (121 s rebuild, 4% of rows have no gene) |
| Gene-level rarity is globally singleton-heavy (40% seen once) | RESOLVED by measuring the right denominator: within the ~50 rows actually displayed, 0 singletons, reach spans 2-591. Scale percentiled against DISPLAYED rows, not the corpus |
| Reach table silently goes stale | `computed_at` shown in the UI; absent row renders "not computed", never 0 |
| Rarity mode surfaces TrEMBL junk | Measured and accepted; rarity is a colour, CV is the default sort |
| 1.4M cells | Top-N with the rule exposed; N and mode both user-controlled |
| Heatmap slows an already-heavy page | Panel loads lazily after the Runs table; its query is separately timed |
