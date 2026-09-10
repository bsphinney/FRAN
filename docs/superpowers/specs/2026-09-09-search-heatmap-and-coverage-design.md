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
| `intensity` | the value — **use this one**, see (g); `normalized_intensity` exists for only 4% of searches |
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
| corpus abundance (mean within-search percentile) | **97 s** | must be precomputed |
| most variable (CV across samples) | **3.86 s** | live |
| corpus rarity (searches per protein) | **53 s** | must be precomputed |

## Design

### 1. `delimp_protein_corpus_reach` — a precomputed table

```
gene            TEXT PRIMARY KEY   -- keyed on gene, NOT protein_group; see the measurement below
n_searches      INTEGER            -- how many FRAN searches have ever reported it
n_samples       INTEGER            -- how many distinct raw files
computed_at     TIMESTAMPTZ
```

Built by `SELECT gene, count(DISTINCT search_id), count(DISTINCT raw_path) FROM delimp_proteins
WHERE NULLIF(gene,'') IS NOT NULL GROUP BY 1`, refreshed by the existing weekly Hive cron alongside
the matviews — the same pattern `delimp_protein_peptide_count` already follows. Reads become a keyed
lookup. Measured rebuild: **121 s for 298,391 genes**.

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
FASTAs, and `delimp_proteins.gene` is populated on 96% of rows.

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

### 1b. What the interactive mockup changed (2026-09-09, real data on screen)

A mockup was built against the live `PROT_0793_search_mouse` data and iterated with Brett. EIGHT
things were wrong in the design above and are corrected here. Every one was invisible on paper, and
none was found by reasoning — each came from putting real data on screen and looking at it.

**(a) Rarity must be matched CASE-INSENSITIVELY.** Gene symbols are capitalised per species — mouse
`Aldoa`, human `ALDOA` — and the corpus is mostly human. Measured: `Aldoa` 278 searches vs `ALDOA`
1,278; `Actb` 93 vs `ACTB` 1,007; `Calm1` **1** vs `CALM1` 30. Calmodulin reading "seen in 1 search"
was the tell. Keyed on the exact string, rarity in a mouse search would largely have encoded *"this
is a mouse experiment"*. Reach is therefore computed on `upper(gene)`.
The correction separates artefact from biology rather than flattening everything: Alb 273 -> 1,789,
Aldoa 278 -> 1,529, Ighg3 2 -> 287, but **Mup2 stays at 33 and Try4 at 2** — those are genuinely
mouse-specific with no human ortholog to merge with.

**(b) Every ranking needs a PRESENCE FLOOR.** Without one, `avg(intensity)` puts a protein seen in
one sample above albumin seen in 219. Measured, unfiltered: `Or6c75` (an olfactory receptor, 1/222
samples, 85.4 **billion** mean) ranked first; albumin was 11th. With a floor of 20% of samples the
list becomes Try4, Plbd1, Stom, Alb, Aldoa, Mup2, Cps1, Bhmt — a coherent mouse liver profile.
4,005 of 6,388 genes clear the floor, so it is not over-aggressive. The plan's
`HAVING count(*) >= 3` is far too weak; use `>= 0.2 * n_samples`.
(The CV ranking was checked for the same defect and is clean on its own — its top 50 sit in a median
147/222 samples — but it gets the floor too, for consistency.)

**(c) RARITY IS A ROW ANNOTATION, NOT A CELL COLOUR.** Brett's catch, and it is decisive: rarity is a
property of the protein, so colouring cells by it paints one number across all 222 columns and throws
the x-axis away. Cells always show intensity, which genuinely varies per sample. Per-protein facts go
in thin annotation strips beside the gene name — the standard heatmap idiom. This removes the
colour-mode toggle entirely and shows both axes at once instead of making the user choose.

**(d) A second strip: the contaminant flag.** `delimp_proteins.is_contaminant` already exists and
flags 112 genes / 5,931 rows in this search. It explains an artefact visible in the mockup — `ACTB`,
`KRT8`, `Krt18`, `HBA`, `CYCS`, `RGN`, `PRSS1` all appear alongside their mouse spellings and all
resolve to `Cont_*` accessions. Human keratins and trypsin in a mouse sample are the classic
contaminant panel, not a mixed FASTA. Worth surfacing, not hiding.

**(e) Coverage is a THREE-STATE RESIDUE VIEW, matching the existing renderer.** Not two bars. Reuse
the idiom already at `app/static/app.js:1696-1716`: a peptide-tile overview track plus the full
monospace sequence — but colour each residue three ways: **found in this experiment**, **found by the
corpus but not here**, **never observed**. Verified live: Fabp1 (P12710, 127 aa) is 96.1% here and
98.4% including the corpus, with **18 corpus peptides this experiment did not find**; Mup2 (P11589,
180 aa) is 82.2% with only 4 corpus-only peptides.
That contrast is itself a result: **the rarity strip predicts how much the corpus can add.** Fabp1 is
corpus-common so the corpus knows much more; Mup2 is mouse-specific and rare, so it barely knows more
than you do.

**(f) Show modified forms, split the same way.** **SUPERSEDED 2026-09-10 — not built on the
search-heatmap branch, and do not build it from this paragraph.** It moved to the `ptm-sites` branch
and `docs/superpowers/specs/2026-09-10-ptm-sites-and-modification-search-design.md`, which measured
the columns this paragraph names and found one of them unusable: **`mods` is 1.43% populated** and is
ruled out by name there in favour of `modified_seq_proforma` (100.00% of 249,978 sampled). The
sentence below is kept for the worked examples only; take the data source from the PTM spec.
`delimp_precursors` carries `modified_seq_diann`,
`mods` and `n_mods`. Real examples pulled for Fabp1: `_YQLQSQENFEPFM[Oxidation (M)]K_`,
`_[Acetyl (Protein N-term)]MNFSGKYQLQSQENFEPFMK_`,
`_NEFTLGEEC[Carbamidomethyl (C)]ELETM[Oxidation (M)]TGEK_`. Colour each by whether this experiment saw
that modified form or only the corpus did.

**(g) USE `intensity`, NOT `normalized_intensity`. This one decides whether the feature works at
all.** Everything above was prototyped on `normalized_intensity` because
`PROT_0793_search_mouse` has it fully populated. Corpus-wide it is nearly absent:

    searches with protein rows           2,086
      with normalized_intensity             75   (4%)
      with raw intensity                 2,084   (100%)
    rows: normalized 3,887,530 / raw 45,815,369 / total 46,567,602

A panel keyed on `normalized_intensity` would render for 4% of searches and be **blank for the other
96%**, while looking perfect on the one search it was built against. Raw `intensity` is present for
every search and 98% of rows.

Nothing is lost by switching: the heatmap z-scores each row independently, and the corpus ranking is
a percentile *within* each search — both normalise the scale away themselves, which is exactly why
the raw column is sufficient. Any future ranking added here must state which column it reads and be
checked against this table before it is believed.

**(h) A fourth row mode: typical abundance across the corpus.** Brett asked to rank by corpus
abundance. Raw mean intensity across searches is meaningless — measured, the same gene spans 906x
(Alb), 3,646x (Aldoa) and 4,389x (Hsp90ab1) between searches, so it would rank instruments and
loading. What IS comparable is each gene's `percent_rank()` **within** its own search, averaged
across searches: "where does this protein typically sit in a run?" Genes need a minimum number of
contributing searches or a single search yields a spurious 1.000 (measured: `OR6C75` scored 1.000
from one search, `GM6133` 0.033 from one).

Layout: **the legend goes ABOVE the grid, not below it.**

### 2. The heatmap panel

Rendered **below the existing Runs table**, not replacing anything. Settled against real data in the
mockup; the section above records what changed and why.

- **Rows:** top N proteins (default 50, adjustable), with FOUR ranking modes. Each control states its
  own scope, and there is no group-level scope label — two modes are within-search and two are
  corpus-wide, so a shared "in this search" heading is false. Labels as shipped:

  | control | ranks by | scope |
  |---|---|---|
  | Varies most — your samples *(default)* | CV of `intensity` across samples | this search |
  | Most abundant — your samples | mean `intensity` | this search |
  | Rarest — across the corpus | fewest corpus searches containing the gene | corpus |
  | Most abundant — across the corpus | mean `percent_rank()` within a search | corpus |

  Every mode applies the presence floor (>= 20% of samples). CV is the default because it shows what
  *differs* between samples rather than what is merely abundant, which here is Alb/Mup2/Fabp1 every
  time.

  **The corpus-abundance mode needs a minimum contributing-search count** (20 used in the mockup) or a
  gene seen once scores a spurious 1.000. Validated: with the floor, the top of that ranking is H4c1
  (0.909, from 616 searches), Hsp90ab1 (0.906, 1,346), Gapdh (0.897, 1,317), Atp5f1a (0.885, 1,331),
  EEF1A1 (0.880, 997), ACTB (0.878, 1,099), Hspa5 (0.874, 1,393), Alb (0.873, 1,786) — the textbook
  list of the most abundant proteins in shotgun proteomics, which is the sanity check that says the
  metric is sound.

- **Columns:** samples, in acquisition order where `raw_files.acquisition_date` is available, else
  name order. Acquisition order makes batch drift and column-wise QC problems visible as vertical
  bands — the mockup showed exactly such a band on this search, a block of samples where much of the
  panel drops out.

  **CORRECTED 2026-09-10, twice.** (1) No filename is shown or shipped: the columns are unlabeled
  and a sample row carries only an opaque id — see the Interfaces note. Do not implement
  "basename shown". (2) **The rationale does not hold on this search.** 0 of its 222 samples carry
  an `acquisition_date` (86% corpus-wide), so the sort falls through to filename order and the
  "vertical band" the mockup showed is not evidence of batch drift. The panel now states which
  ordering is actually in force rather than asserting one the data cannot support.

- **Cells always show amount:** log2 `intensity`, z-scored per row so a faint protein is as readable
  as albumin. A missing (protein, sample) pair renders as a distinct "not identified" colour, never
  as zero — a protein absent from a sample is information. Scale must be colour-blind safe
  (viridis-like, not red/green).

- **Two row-annotation strips** beside the gene name, NOT cell colours:
  1. **corpus rarity** — tint by `n_searches` from `delimp_protein_corpus_reach`, percentiled against
     the DISPLAYED rows.
  2. **contaminant flag** — `delimp_proteins.is_contaminant`.

  Rarity cannot be a cell colour: it is a per-protein value, so it would paint one number across all
  222 columns and waste the x-axis. This was Brett's finding and it removed the colour-mode toggle
  entirely — both axes are now visible at once instead of the user choosing between them.

  This is the part no other tool can draw. Spectronaut and DE-LIMP see one experiment; FRAN sees
  ~2,000 searches, so it can say *"this protein is unusual for this lab"*. That is the reason to build
  it here rather than export elsewhere.

### 3. Protein click → the peptide map

Extends the existing widget; does not replace it. Settled in the mockup against real data.

- `protein_coverage_peptides(pg, search_id=None)` gains an optional scope. When set, the
  `delimp_precursors` query adds `AND search_id = %s` — `idx_prec_protein_group` already covers the
  lookup, so this narrows an already-fast query.
- `GET /api/protein/{pg}/coverage?search_id=…` returns **both** peptide sets. Without `search_id`,
  behaviour is byte-identical to today, so existing callers are untouched.
- **The display is an HDX-style peptide map**, not two summary bars: the sequence in wrapped rows
  with residue numbering, and every peptide drawn as its own bar, lane-packed beneath the residues it
  covers. Gold = found in this experiment; teal = the corpus found it and this experiment did not;
  grey residues = never observed by anyone. Reuse the tile/monospace idiom already at
  `app/static/app.js:1696-1716`.
  Verified live: Fabp1 (P12710, 127 aa) 96.1% here / 98.4% including corpus, **18 corpus peptides
  this experiment missed**; Mup2 (P11589, 180 aa) 82.2% with only 4. Mup2's uncovered N-terminus is
  its signal peptide — correctly never observed, and a good smoke test that the mapping is right.
- **Clicking a peptide opens an inline detail card**, not a navigation: residue range, and a
  side-by-side of *this experiment* vs *the corpus* — precursor rows, runs, searches, charge states,
  best q-value — plus that peptide's modified forms, each marked gold or teal by whether this
  experiment saw that form. Real examples: `_YQLQSQENFEPFM[Oxidation (M)]K_`,
  `_[Acetyl (Protein N-term)]MNFSGKYQLQSQENFEPFMK_`.
- The existing custom-construct and missing-sequence paths keep working unchanged.

**Navigation — where each link lives, and why.** Settled by trying it the wrong way round first.

| element | action |
|---|---|
| gene name on the y-axis | opens the coverage panel — **in-page, not a link** |
| gene name in the coverage header | `go('gene', <symbol>)` — the corpus view of that protein |
| peptide bar in the map | opens the inline detail card |
| peptide sequence in that card | `go('peptide', <stripped_seq>)` |

The y-axis is for *scanning* 50 proteins, so its single click must be the cheap in-page action;
making it navigate meant every attempt to see coverage left the page and lost the sort. Navigation
belongs one level deeper, once the user has committed to a protein. Both destinations already exist
(`case 'gene'` and `case 'peptide'` in the router) and both were verified against live data —
`/api/gene/Aldoa` returns 7 groups / 278 searches / 3,191 runs, `/api/gene/Mup2` 1 / 33 / 563.

This completes a drill path entirely out of pages that already exist: **search → heatmap → gene →
coverage → peptide → the whole corpus.** The heatmap is only the entry point that was missing.

## Interfaces

```
delimp_protein_corpus_reach (gene PK, n_searches, n_samples, computed_at)

queries.search_protein_matrix(search_id, mode='cv', limit=50) -> {
    proteins: [{gene, protein_group, n_samples, is_contaminant, cv, mean_int,
                reach, mean_pct_rank, reach_pct_rank, cells:{sample_id: float}}],
    samples:  [{id}],
    mode, limit, reach_computed_at, n_rankable, n_samples_total, n_samples_dated, floor_pct }

# CORRECTED 2026-09-10. This block used to read `samples: [{raw_path, basename, acquisition_date}]`
# and `cells:[…]`. Both were wrong in ways that SHIPPED as real leaks before being caught:
#   - `basename` is precisely the key NOT in privacy._FILE_KEYS, so it passes through redact()
#     untouched and reaches the public tier as a real acquisition filename.
#   - cells keyed by filename are invisible to redact() at all — it rewrites string VALUES under
#     known keys and never renames KEYS.
# The shipped shape keys cells by an OPAQUE positional sample id ("s0", "s1", ...) and a sample row
# carries that id and nothing else, so the response cannot carry a filename by construction. Do not
# re-derive the old shape from this spec.
# `n_proteins_total` (DISTINCT protein_group, floor ignored) was likewise replaced by `n_rankable`
# (genes clearing the presence floor) — the panel's rows are genes, and 6,388 vs 4,005 named a
# population the panel does not show.

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
