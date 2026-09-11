# Heatmap filters, and the PTM rollup that makes one of them possible

**Date:** 2026-09-11
**Status:** design, approved in chat. Scope: build the rollup now, plus four filters.
**Spec it builds on:** `2026-09-10-ptm-sites-and-modification-search-design.md` (Phase 2's
`delimp_ptm_search` is the same idea at a coarser grain; this supersedes it — see "Relationship to
the PTM spec").

## The problem

Brett: *"It's not easy to find the proteins with PTMs on the heatmap. Can we have a filter button,
and under the filter button, say with PTMs and > 1 peptide / protein, and any other super awesome
filter you can think of that would be useful for a biologist."*

Today the search-page heatmap shows the top 50 genes by one of four ranking modes. There is no way
to ask "which of these carry a modification". In the Arabidopsis phospho search
(`2c4911a3-…`), **62 of 6,284 protein groups carry phospho — under 1%**. They are effectively
unfindable by scrolling, which is the whole complaint.

## What is affordable, measured before designing

Every number below is measured against live PG Farm, not estimated.

| candidate filter | source | cost |
|---|---|---|
| Hide contaminants | `is_contaminant` — already in the matrix payload | **free** |
| ≥N peptides per protein | `max(n_unique_peptides)` added to the aggregate the matrix already runs | **free** (same scan; 21-sample 10.2s → 4.2s with the extra columns, i.e. within noise, not additive) |
| Present in all samples / patchy | `n_samples` vs `n_samples_total` — already in the payload | **free** |
| Unique to this experiment | `reach` — already in the payload | **free** |
| **Has PTM / phospho / GlyGly** | `delimp_precursors`, per search | **16.4 s** (10-sample) to **90.9 s** (21-sample) |

**That last row is the entire architecture.** The matrix endpoint already costs 2.8–8.9 s and is
public and auto-firing; adding 16–91 s to it is not a filter, it is an outage. So the PTM flags are
precomputed, and everything else is computed in the scan already being paid for.

`n_unique_peptides` deserves a note: it is **per-run**, and summing it has previously overstated a
peptide count by up to 56×. The filter therefore uses `max()` across runs — "at least N peptides in
at least one run" — which is the honest reading of that column and is what the UI must say.

## The rollup

```
delimp_search_protein_ptm
    search_id       UUID
    protein_group   TEXT
    has_ptm         BOOLEAN NOT NULL
    has_phospho     BOOLEAN NOT NULL
    has_glygly      BOOLEAN NOT NULL
    n_mod_precursors INTEGER NOT NULL
    PRIMARY KEY (search_id, protein_group)
```

**Keyed by `protein_group`, not `gene`, deliberately.** Building it needs no join — the flags come
from `delimp_precursors` alone — and the matrix is already scanning `delimp_proteins`, which carries
both `protein_group` and `gene`, so the gene-level roll-up (`bool_or`) happens for free inside the
query that already runs. Keying by gene would force a join at build time across 437 M rows to save
nothing.

`has_glygly` matches the LITERAL `%GlyGly%`, not `UNIMOD:121`. Historical rows store the unmapped
literal `[GlyGly (K)]`; only re-ingested rows will carry `[UNIMOD:121]`. The predicate must match
both or it silently misses ~750,000 ubiquitin remnants — the exact defect this project spent
yesterday finding. Match `modified_seq_proforma ILIKE '%glygly%' OR ... LIKE '%UNIMOD:121%'`.

### Refresh

`ingest/refresh_search_ptm.py`, mirroring `ingest/refresh_corpus_reach.py`: incremental by
`search_id`, processing only searches absent from the table, with a `--rebuild` flag for schema
changes. It runs from the existing weekly SLURM job (`ingest/fran_mv_refresh.sbatch`), which already
has the node pinning, the lock and the schedule — the same conclusion the corpus-reach wrapper
reached the hard way yesterday. **Do not write a new cron.**

The first full build is a backfill measured separately (see "Backfill cost"); after that the
incremental pass only touches new searches.

## The filters

One "Filter" control above the heatmap, defaulting to everything off so the current view is
unchanged. Each filter states what it does in biologist's terms, not schema terms.

| filter | predicate | why a biologist wants it |
|---|---|---|
| **Has a modification** | `has_ptm` | the stated problem |
| ↳ **Phospho** / **Ubiquitin (GlyGly)** | `has_phospho` / `has_glygly` | the two that are biology rather than sample handling |
| **Hide contaminants** | `NOT is_contaminant` | removes keratin/trypsin/casein; the biggest readability win on a busy map |
| **≥2 peptides** | `max_peptides >= 2` | single-peptide identifications are the weakest evidence; the standard confidence cut |
| **In every sample** | `n_samples = n_samples_total` | where quantitative comparison across samples is valid |
| **Patchy (not in all)** | `n_samples < n_samples_total` | where on/off biology lives — the complement, and the more interesting half |
| **Unique to this experiment** | `reach = 1` | nothing else in the corpus has seen it: genuine novelty, or a misassignment worth catching |

Two design points that matter more than they look:

- **Filters apply BEFORE the top-N cut, not after.** Filtering the already-selected 50 rows would
  routinely yield two or three, because under 1% of proteins carry phospho. The filter belongs in
  the SQL `HAVING`/`WHERE`, so "top 50 phosphoproteins by CV" means the top 50 *of the
  phosphoproteins*. Filtering client-side is the obvious implementation and it is wrong.
- **The header must state the filtered population.** "Showing 50 of 62 with a modification" is the
  honest form. The current header already conflates protein groups with genes (6,388 vs 6,340 vs
  4,005 rankable); do not add a third number to that confusion.

## Constraints

- Every `query()` passes `tables=[...]`; the new table goes in `PUBLIC_TABLES` (it holds no
  filename, path, or person — only ids, booleans and counts).
- The matrix endpoint is **public and auto-fires on every search-page view**. Any filter must ride
  the existing cached query, not add a round trip. Cache keys must include the filter state, or two
  different filters will serve each other's results.
- Read `modified_seq_proforma`, never `mods` (1.43% populated).
- The `max(n_unique_peptides)` reading is "in at least one run" — the UI must not imply a total.
- Every test proven able to fail.

## Risks

| risk | mitigation |
|---|---|
| A filter silently returns nothing and reads as "no such proteins" | Empty results say which filter emptied them, and offer to clear it |
| The rollup goes stale, so a new search shows no PTMs | Same staleness surface as corpus reach — show the computed-at date; incremental refresh means new searches appear the following week at worst |
| `has_glygly` misses the literal form | Predicate matches both spellings; a test pins a known Bennett_Penn GG search |
| Cache key ignores filter state | A test asserting two different filter states return different row sets |
| Backfill is far more expensive than estimated | Measured before building (below); if it exceeds a SLURM window, chunk by search_id — the refresh is already incremental by design |

## Backfill cost

**Measured: 12 random searches in ONE pass = 13.7 s, i.e. 1.1 s per search → ~0.7 h for all 2,086.**
31,021 (search, protein_group) rows produced, 15,193 with a modification.

The batching is the whole story, and it inverts the earlier conclusion. Those same searches cost
**16–91 s each** when queried one at a time — a 20–80× difference — because one batched pass is a
single sequential scan over `delimp_precursors` instead of thousands of separate index lookups and
sorts. A naive per-search backfill loop would have run 9–53 hours; the batched build is a single
~40-minute job.

**So BATCH the backfill, do not loop.** The refresh script must accept a set of `search_id`s and
process them in one query, not iterate. That is also why the incremental path stays cheap: a week's
new searches are one small batch, not N queries.

If the full pass ever outgrows a SLURM window, the same batching is the chunking mechanism — run it
as an array job over search-id ranges, each range one batched query.

The sample also confirms what the filter is for: **phospho appeared in 0 of those 12 random
searches**, and GlyGly in 0. PTM-bearing searches are rare enough that finding them by browsing is
exactly the problem worth solving.

## Relationship to the PTM spec

`2026-09-10-ptm-sites-…` Phase 2 proposes `delimp_ptm_search` (per search × modification) and
`delimp_ptm_site` (per protein × site). This table sits between them at (search × protein) grain and
is strictly cheaper than either. It supersedes `delimp_ptm_search`, which it can generate by
aggregation. `delimp_ptm_site` remains separate and is still best sourced from Spectronaut's PTM
site report rather than derived.

## Deliberately out of scope

- Filtering by modification beyond PTM/phospho/GlyGly. The other three variable modifications are
  handling artifacts; a filter for "has oxidation" invites reading noise as signal.
- A free-text gene filter. Useful, but it is a search box, not a filter button, and belongs with the
  existing search UI.
- Per-site filtering ("phospho on S/T only"). That needs `delimp_ptm_site`, which is not built.
