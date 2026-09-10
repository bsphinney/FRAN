# PTM sites on the coverage map, and modification search across the corpus

**Date:** 2026-09-10
**Status:** design, pending Brett's review.
**Origin:** Brett, looking at search `2c4911a3-79fd-5367-bdd0-ee85a16cd25b`: *"were there any
proteins with phos's in this search. I want to check the protein coverage that has phos sites"*
and *"is there a way to search for modifications in FRAN? This seems like a gap we should fill."*

## The gap, stated precisely

FRAN's entire peptide layer is keyed on `stripped_seq` — the *unmodified* sequence. Every peptide
route in `app/main.py` takes `{stripped_seq}`: `/api/peptide/{stripped_seq}/lca`, `/proteins`,
`/fragments`, `/observed`, `/charges`, `/predicted`, `/xic`, `/interference`, `/summary`,
`/flyability`, `/funfacts`. `search_peptides()` upper-cases the query and matches `stripped_seq`.
`protein_coverage_peptides()` — the coverage map — does `GROUP BY stripped_seq`.

So a phosphopeptide and its unmodified twin are the same row everywhere in the UI. The data is
present and complete; it is simply not reachable.

This is a genuine gap, not an oversight to paper over: FRAN can currently tell you a peptide was
seen 460 times and *cannot* tell you that those observations were phosphorylated.

### The one existing PTM index does not work

`delimp_precursors` carries `idx_prec_mods_gin` — a GIN index on the `mods` jsonb column. Measured:
`mods` is populated on **1.43%** of rows. The index is real, the column behind it is not. Anyone
reaching for "we already have a PTM index" will find it indexes almost nothing.

## Measured facts this design rests on

All measured against the live corpus on 2026-09-10, not assumed.

| fact | value | how |
|---|---|---|
| `delimp_precursors` size | 437,470,720 rows / 238 GB | `pg_class` / `pg_total_relation_size` |
| `modified_seq_proforma` populated | **100.00%** (249,978 of 249,978 sampled) | `TABLESAMPLE SYSTEM (0.05)` |
| `mods` (jsonb) populated | **1.43%** | same sample |
| `n_mods > 0` | **19.69%** of all precursors | same sample |
| `site_localization_probability` populated | **0** rows, corpus-wide | same sample |
| distinct modification types corpus-wide | **6** | token scan of 18,856 modified precursors |

Modification vocabulary — the entire corpus, as a share of *modified* precursors:

| UNIMOD | name | share |
|---|---|---|
| 4 | Carbamidomethyl | 60.9% |
| 35 | Oxidation | 34.0% |
| 1 | Acetyl | 5.9% |
| 21 | **Phospho** | 0.6% |
| 7 | Deamidated | 0.5% |
| 27 | Glu->pyro-Glu | 0.1% |

Phospho is 0.13% of *all* precursors, which extrapolates to roughly 570,000 phospho precursors
corpus-wide.

> **CORRECTION, 2026-09-10 — the table above is WRONG and the "six types" claim was a measurement
> artifact.** See "The unmapped-modification gap" immediately below. The scan that produced it
> matched `UNIMOD:(\d+)`, so any modification the ingest failed to normalize was invisible to the
> instrument by construction. Brett asked "what about ubiq gg" and the answer is that GlyGly — the
> ubiquitin remnant — is present in enormous quantity and absent from that table entirely. The
> percentages are also sample-unstable (see the Acetyl note under Task 1 in the plan's ledger:
> 5.9% in one TABLESAMPLE, 0.05% in another, because TABLESAMPLE reads whole blocks and the table
> is clustered by search). Treat nothing in that table as a corpus fact.

### The unmapped-modification gap — the real finding

`ingest/spectronaut_to_corpus.py:78-79` maps modification names to UNIMOD ids:

```python
_MOD_UNIMOD = {"Carbamidomethyl": 4, "Oxidation": 35, "Acetyl": 1,
               "Phospho": 21, "Deamidation": 7, "Gln->pyro-Glu": 28, "Glu->pyro-Glu": 27}
```

Seven names. `_to_proforma()`'s `repl()` returns `m.group(0)` — the ORIGINAL text — for any name not
in that dict. So an unmapped modification is stored verbatim, e.g. `K[GlyGly (K)]`, and is
invisible to every query that looks for `UNIMOD:`.

**GlyGly is not in the dict.** Measured consequence, in the three searches Brett named:

| search | precursors | GG precursors | GG peptides | GG proteins |
|---|---|---|---|---|
| 20230803_160004_Bennett_Penn_Ubiq_July_2023 | 579,048 | **356,557 (61.6%)** | 37,866 | 5,393 |
| 20230810_161326_Bennett_Penn_Ubiq_July_2023 | 60,802 | **40,208 (66.1%)** | 28,117 | 4,707 |
| 20230810_163055_Bennett_Penn_Ubiq_July_2023 | 572,034 | **352,727 (61.7%)** | 35,302 | 5,312 |

Roughly 750,000 GG precursors in three searches — more than all the phospho found anywhere. Twelve
ubiquitin-named searches exist (`ubiq|digly|glygly` on `search_name`), and spot checks confirm
`[GlyGly (K)]` stored as literal text in several.

Two consequences that change this design:

1. **The parser must resolve literal modification names, not only UNIMOD tags.** Fixing
   `_MOD_UNIMOD` helps future ingests only; ~750,000 existing rows are already stored in literal
   form, and re-ingesting them to correct a naming issue would be absurd. So `parse_proforma()`
   needs a name→UNIMOD fallback: `[GlyGly (K)]` → UNIMOD 121, applying the same
   `.split(" ")[0].split("(")[0]` reduction the ingest uses. The current parser's behaviour on
   these is *safe but incomplete* — it skips unknown bracket tokens without counting them as
   residues, so positions stay correct and nothing is corrupted; the sites simply never appear.
2. **`_MOD_UNIMOD` gains `"GlyGly": 121`** so new ingests normalize properly, and the real fix is
   to stop silently passing unmapped names through at all — an ingest that cannot name a
   modification should say so, not store it as prose.

**GG belongs in the variable, biological set.** It is the ubiquitin remnant: the direct readout of
protein ubiquitination, and more biologically load-bearing than oxidation, which dominates the
counts above and is mostly sample handling.

### What the enrichment rates tell us about the phospho anomaly

These ubiquitin searches run **61-66% modified**. That is what a real PTM enrichment looks like,
and it is the comparison the phospho anomaly was missing. Against it, the `STY phospho` search's
**0.11%** is not merely low — it is three orders of magnitude away from what an enriched experiment
produces in this same corpus, through the same pipeline, from the same instrument platform. That
strongly favours "something is wrong with that search or its ingest" over "the enrichment
underperformed", and it raises the priority of the re-export diagnostic accordingly.

**The methodological lesson, recorded because it is the same one this project keeps paying for.**
The "six modification types" claim was produced by an instrument that could not have detected a
seventh: a regex for `UNIMOD:\d+` over data where unnormalized modifications are stored as prose.
It is the identical defect shape as the thirteen-plus assertion failures in the sibling heatmap
plan — an observation incapable of discriminating the thing it was trusted to decide. It was caught
by a domain expert asking an obvious question, not by the measurement. Any future claim about
"which modifications exist in the corpus" must scan for **bracket tokens generally**, then classify,
never for the normalized form alone.

### Position is recoverable; confidence is not

Sampling 400 phospho precursors:

- Every one has a residue immediately preceding the `[UNIMOD:21]` tag. **0 unparseable.**
- Residue distribution: **S 81.7%, T 14.1%, Y 4.0%**. That is textbook phosphoproteomics
  (canonical is ~85/13/2). The parse is correct and the underlying data is real biology, not
  search noise.
- `site_localization_probability` is NULL on every row in the corpus.

### Localization is missing because nobody asked for it — and it is recoverable

Brett: *"I do want site localization to be displayed by FRAN."* Chasing why it is empty produced a
much better answer than "we don't have it". The data was never lost; it was never requested.

The chain, each link verified:

1. **Nothing in `ingest/` writes `site_localization_probability`.** `grep` across every ingest
   script returns zero hits. The column was created and never populated.
2. **The Spectronaut report schema FRAN asks for has no localization column.** `COLMAP` in
   `ingest/spectronaut_to_corpus.py:32-74` maps 30 columns — run, protein group, q-values,
   fragments, ion mobility — and not one PTM localization field. Spectronaut exports
   `EG.PTMLocalizationProbabilities` / `EG.PTMAssayProbability` when the schema includes them.
   FRAN's schema does not include them, so they are absent from every report ever ingested.
3. **FRAN drives that export itself.** `ingest/sne_export.py` runs
   `spectronaut manageSNE -sne <file.sne> -o <out> -rs <report_schema>`. The schema is ours. The
   `.sne` project files are the Spectronaut results, and they are on disk.
4. **Therefore historical searches are recoverable by re-export, without re-searching.** Add the
   localization columns to the report schema, re-export the affected `.sne` files, re-ingest.

This matters for the engine mix: **spectronaut 2,008 searches, diann 76, radiant 1, fragpipe 1.**
Spectronaut is 96% of the corpus, so the Spectronaut path is the one that decides whether FRAN can
show localization at all.

On the DIA-NN side (76 searches) the columns already exist in `report.parquet`:
`PTM.Site.Confidence`, `Site.Occupancy.Probabilities`, `Protein.Sites`, `Lib.PTM.Site.Confidence`.
Measured on one report: `PTM.Site.Confidence` is 100% populated but reads `1.0` on peptides with
no modification at all, so it defaults rather than meaning anything on unmodified rows;
`Site.Occupancy.Probabilities` and `Protein.Sites` were 0% populated on that report, which had no
variable modifications whatsoever. **Populated-but-defaulted is exactly the `mods` trap again:
presence is not evidence of meaning, and any ingest of these columns must be validated on a report
that actually contains phospho.**

Until a re-export lands, a site FRAN displays is *the position the search engine reported*, with no
independent evidence the engine put it on the right residue. Site localization is the most
contested number in phosphoproteomics. The UI must say so in the interface, not in a doc — and it
must be built so that adding real localization scores later changes the badge, not the layout.

### An anomaly the re-export will settle

Search `2c4911a3-…` is named `20260528_093510_Toshi-uniprot-STY phospho.sne` — a dedicated STY
phospho experiment. FRAN holds 454,865 precursors for it and finds **519 phospho** (0.11%), 79
peptides across 62 proteins. For a phospho-enriched sample that is implausibly low; enrichment
should make phosphopeptides a large fraction, not a rounding error.

Two possible explanations, and this spec does not guess between them:

- the enrichment underperformed, or the sample was not enriched despite the name, or
- something between Spectronaut and `delimp_precursors` is dropping phospho identifications.

The `_to_proforma()` conversion is **not** the culprit — `_MOD_UNIMOD` maps `Phospho -> 21` and
`name.split(" ")[0].split("(")[0]` correctly reduces `[Phospho (STY)]` to `Phospho`, verified by
reading the code. Beyond that, the re-export is the cheapest discriminator: exporting this one
`.sne` with a PTM-inclusive schema and comparing its phospho count against FRAN's 519 answers it
outright. **Do that before building Phase 2 on top of these numbers.**

### One parsing subtlety that will silently corrupt positions if missed

N-terminal modifications carry no preceding residue:

```
[UNIMOD:1]SETAPAETATPAPVEKS[UNIMOD:21]PAK
^^^^^^^^^^ N-terminal acetyl -- tag precedes residue 1
```

A parser that assumes "the tag follows the residue it modifies" will assign the N-terminal acetyl
to a nonexistent residue 0 and then shift **every subsequent position in that peptide by one**.
Acetyl is 5.9% of modified precursors, so this is common, not exotic. The parser must return
position 0 for an N-terminal tag and must be tested against a peptide carrying both an N-terminal
mod and an internal one — the case where the off-by-one actually manifests.

## Why this splits cleanly into two phases

The two halves have completely different cost profiles, and this is measured, not estimated:

- **Protein-scoped** modform query (`WHERE protein_group = %s`): **0.13 s**, 7 modforms returned.
  `idx_prec_protein_group` already covers it. No new infrastructure required.
- **Corpus-wide** modification query (`WHERE modified_seq_proforma LIKE '%UNIMOD:21%'`, no protein
  filter): unindexed scan of 238 GB. Started at 12:47, still running past 13:00 — **over 13
  minutes and not finished.** This cannot be served from a web request, ever, at any timeout.

That gap — 0.13 s against >13 minutes — is the entire architecture. Phase 1 needs nothing new.
Phase 2 needs precomputation, exactly as `delimp_protein_corpus_reach` did for the heatmap's
rarity ranking.

---

## Phase 1 — PTM sites on the protein coverage map

**Deliverable:** open a protein in a search, see which residues carry which modification.

### Query change

`_protein_coverage_peptides()` currently aggregates `GROUP BY stripped_seq`. It gains a second,
parallel lookup at the same protein-group scope — *not* a change of the existing grain:

```sql
SELECT modified_seq_proforma,
       stripped_seq,
       COUNT(*)                 AS n_precursors,
       COUNT(DISTINCT raw_path) AS n_runs
  FROM delimp_precursors
 WHERE protein_group = %s AND n_mods > 0
 GROUP BY modified_seq_proforma, stripped_seq
```

Keeping the existing `stripped_seq` aggregation untouched matters: the coverage map's bars, the
`here`/corpus comparison, and the peptide table all read it, and every one of those is working
today. Modforms arrive as an additional key on each peptide, so a peptide with no modifications is
byte-identical to what it returns now.

Scoped to a search, the same `search_id` filter the `here` lookup already uses applies.

### Position mapping

The coverage map already carries `start`/`end` per peptide, 1-based inclusive — confirmed by
`app/static/app.js:634`, which indexes with `p.start-1`.

```
site_position_in_protein = peptide.start + position_in_peptide - 1
```

where `position_in_peptide` is the 1-based index of the residue the tag follows, and 0 means
N-terminal. An N-terminal tag maps to the peptide's own start.

### Display

Mark the exact residue, per Brett's decision. Against the amino-acid sequence already rendered:

- A marker on the modified residue, one glyph/colour per UNIMOD type, phospho most prominent.
- The residue letter stays legible — the marker annotates, it does not replace.
- Hover gives: modification name, residue and protein position (`S 45`), how many precursors and
  runs carry it here, and the same for the corpus.
- Where the same residue is modified in some precursors and unmodified in others — the normal case
  — show the **occupancy fraction** (`18 of 46 precursors`). A site marked without that fraction
  reads as "this residue is phosphorylated", which is not what partial occupancy means.
- **A visible statement that the site is as-reported-by-the-search-engine and not independently
  localized.** Not a tooltip only. Given `site_localization_probability` is empty corpus-wide, this
  is the honest framing and it is not optional.

### Testing

- The N-term + internal mod peptide, asserting both positions. Must be shown to fail against a
  parser that treats every tag as following a residue.
- A protein with zero modifications returns exactly today's shape — the regression that matters,
  since the coverage map is live.
- `RES[UNIMOD:21]RS[UNIMOD:21]PPPYEK` on P92966 (RS41): a real two-site peptide from the corpus,
  asserting two distinct positions from one peptide.

---

## Phase 2 — corpus-wide modification search and a PTM landscape page

**Deliverable:** find proteins and sites carrying a given modification, corpus-wide; browse what
modifications exist in the corpus.

### The rollup tables

Following the `delimp_protein_corpus_reach` pattern exactly: an ingest-side script computes them,
a Hive cron refreshes them, the app reads only the rollup and never scans `delimp_precursors`.

```
delimp_ptm_site
    protein_group   TEXT
    gene            TEXT          -- denormalized, as corpus_reach does
    unimod_id       INTEGER
    residue         CHAR(1)       -- NULL for N-terminal
    position        INTEGER       -- 1-based in protein; 0 = N-terminal
    n_searches      INTEGER
    n_precursors    BIGINT
    n_peptides      INTEGER
    n_runs          INTEGER
    first_seen      DATE
    PRIMARY KEY (protein_group, unimod_id, position)

delimp_ptm_search
    search_id       UUID
    unimod_id       INTEGER
    n_precursors    BIGINT
    n_peptides      INTEGER
    n_proteins      INTEGER
    PRIMARY KEY (search_id, unimod_id)
```

`delimp_ptm_search` is what makes "which experiments have phospho" instant, and it is what the
landscape page aggregates.

Both go in `PUBLIC_TABLES`. Neither carries a filename or a directory path, so neither creates a
privacy surface — but every `query()` still passes `tables=[...]`, and that is enforced by
`_assert_allowlisted()` as the first line of `query()` regardless.

**Sizing, so nobody is surprised:** the site table's row count is bounded by (distinct protein
groups × modified positions), not by the 437 M precursors. Carbamidomethyl on every cysteine will
dominate it. The refresh script must report its own row count on each run, and the first run is a
measurement, not a deployment — if `delimp_ptm_site` turns out to be enormous, restricting it to
the biologically interesting subset (phospho, and anything not in the fixed-modification set) is
the fallback, decided on the measured number rather than now.

### Refresh

`ingest/refresh_ptm_rollups.py`, mirroring `ingest/refresh_corpus_reach.py`, with a cron wrapper
mirroring `ingest/cron_corpus_reach.sh`. Runs on Hive, never on the login node.

Incremental by `search_id`: a full rebuild is a 238 GB scan and the corpus grows continuously, so
the script must process only searches absent from the rollup, exactly as the reach refresh does.
The full rebuild path stays available behind an explicit flag, for schema changes.

### Landscape page

A page in the existing idiom of Engines/Species: what modifications exist, how common, how they
distribute across searches, species, and instruments. It reads `delimp_ptm_search` joined to
`delimp_searches`, so it is cheap.

With six modification types, this page is small and honest — and it will immediately show that
five of the six are sample-handling artifacts. That is genuinely useful information for a core
facility, and it is the kind of thing that is invisible until someone plots it.

### Site search

Given a modification (and optionally a gene or protein), return sites ranked by corpus support.
Reads `delimp_ptm_site` only. The existing search box gains modification as a filter dimension
rather than becoming a second, parallel search UI.

---

## Constraints

- **Never scan `delimp_precursors` unindexed from a web request.** Measured at >13 minutes. Any
  query without a `protein_group`, `search_id`, or `stripped_seq` predicate belongs in the refresh
  script, not the app.
- **`mods` is not a data source.** 1.43% populated. Read `modified_seq_proforma`. This mirrors the
  `normalized_intensity` rule already in `search_protein_matrix()` (4% populated → use `intensity`),
  and it is the same failure mode: a column that looks canonical and is nearly empty.
- **Positions are engine-reported, never independently localized.** Surface this in the UI.
- **The ProForma parser is the correctness core of both phases.** It gets its own tests, and every
  one must be proven able to fail. This plan family has produced defects whose common shape was an
  assertion whose witness could not discriminate; a position parser is exactly where an
  under-discriminating test hides an off-by-one that silently misplaces every site in the corpus.
- Every `query()` passes `tables=[...]`.
- Phase 1 must not change what the coverage map returns for an unmodified protein.

## Risks

| risk | mitigation |
|---|---|
| A displayed phospho site is on the wrong residue (engine mislocalization) | State engine-reported in the UI; show occupancy; never present a site as validated |
| `delimp_ptm_site` is far larger than expected | First refresh run is a measurement; restrict to non-fixed modifications if needed |
| The N-terminal off-by-one shifts every position in affected peptides | A specific test with N-term + internal mod on one peptide, proven to fail against the naive parser |
| The dead `idx_prec_mods_gin` misleads a future implementer into using `mods` | Named in this spec and in a code comment beside the proforma read |
| Refresh cost grows with the corpus | Incremental by `search_id`, like the reach refresh |
| Phase 1 regresses the live coverage map | Modforms are an additive key; unmodified proteins return today's exact shape, with a test pinning it |

## Deliberately out of scope

- Re-localizing sites from spectra. FRAN has the fragment data to attempt it and that is a research
  project, not a feature.
- Backfilling or dropping `idx_prec_mods_gin` / the `mods` column. DDL on a 238 GB table is its own
  change with its own risk; this spec only stops depending on it.
- Modification-aware XIC or fragment views.
- Open/unrestricted modification search. The vocabulary is six known types.

## Decisions taken (previously open questions)

Brett: *"I don't know how to answer your questions"* — correctly, because they were implementation
calls dressed up as product ones. Decided here, with the reasoning, so they can be overturned on
the merits later.

1. **`idx_prec_mods_gin` and the `mods` column — leave both alone, read `modified_seq_proforma`.**
   The column is empty *by construction*, not by accident: `ingest/spectronaut_to_corpus.py:199`
   writes `"mods": None` with the comment `# full mod JSON TODO; proforma carries detail`. Since
   Spectronaut is 96% of the corpus, that one line is the whole 1.43% figure. Dropping the index is
   DDL on a 238 GB table and backfilling is a 437 M-row rewrite; neither buys anything this feature
   needs, because the proforma column is 100% populated and already indexed *by protein group*,
   which is the access path Phase 1 uses. Recorded as a comment beside the proforma read so the
   next person does not re-derive this.

2. **Scope to VARIABLE modifications; exclude Carbamidomethyl.** Brett: *"I think we should focus
   on variable PTMs as those are not reagent based and more biological."* Agreed, and it also
   solves the sizing risk — Carbamidomethyl is 60.9% of all modifications and would have dominated
   both `delimp_ptm_site` and every chart on the landscape page while carrying no biological
   information at all. The five in scope are Oxidation (35), Acetyl (1), Phospho (21), Deamidated
   (7), Glu->pyro-Glu (27).

   One honesty caveat to carry into the UI rather than bury: *variable* is not the same as
   *biological*. Oxidation is 34% of modifications and is largely a sample-handling artifact;
   Glu->pyro-Glu likewise. Phospho and N-terminal Acetyl are the genuinely biological ones. The
   landscape page should group them by that distinction rather than implying all five are biology.

3. **`delimp_ptm_site` scope decided on the first refresh run.** With Carbamidomethyl excluded the
   size risk largely evaporates, but the first run still reports its row count before anything is
   built on top of it.

## Open questions for Brett

1. **Re-export scope.** Recovering localization means re-exporting `.sne` files with a
   PTM-inclusive report schema and re-ingesting. Doing that for all 2,008 Spectronaut searches is a
   large batch job. Doing it only for searches that already show variable PTMs is far cheaper and
   covers the actual need. Recommend the latter, starting with the one STY phospho search as the
   proof — but the report schema change is Brett's to make in Spectronaut, so this needs him
   either way.
