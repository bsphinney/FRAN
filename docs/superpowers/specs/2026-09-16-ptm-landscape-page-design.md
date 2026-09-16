# The PTM landscape page — which modification experiments actually worked

**Date:** 2026-09-16
**Status:** design, approved in chat ("lets build the PTM landscape page").
**Supersedes:** Phase 2's landscape section in `2026-09-10-ptm-sites-and-modification-search-design.md`,
which assumed a `delimp_ptm_search` table that was never built.

## What changed since the original spec

Phase 2 proposed `delimp_ptm_search` (search × modification) as the thing a landscape page would
aggregate. The heatmap-filter work superseded it with **`delimp_search_protein_ptm`**
(search × protein_group, boolean flags + `n_mod_precursors`), which is strictly cheaper and already
built, backfilled and in `PUBLIC_TABLES`.

That substitution has one cost and one large benefit, and both shape this page.

**The cost — state it plainly rather than designing around it.** The rollup flags only three things:
`has_ptm`, `has_phospho`, `has_glygly`. It cannot distinguish oxidation from acetyl from
deamidation. So the original ambition — *"what modifications exist corpus-wide"* across all types —
is **not** answerable from this table. This page answers it for phospho, GlyGly, and
any-modification, and must say so rather than implying completeness.

**The benefit — this is the actual feature.** `n_mod_precursors` against
`delimp_searches.n_precursors_total` gives a **modified-precursor rate per search**, and that rate
separates a PTM enrichment that worked from one that did not. Measured on the two backfilled
searches:

| search | modified rate | reading |
|---|---|---|
| `20230803_160004_Bennett_Penn_Ubiq_July_2023` | **72.4%** | a working diGly enrichment — 5,393 GlyGly proteins |
| `20260528_093510_Toshi-uniprot-STY_phospho` | **5.1%** | named for STY phospho, 62 phospho proteins |

Twelve ubiquitin-named searches exist in the corpus; earlier measurement showed only the three
Bennett Penn ones exceed 60% GlyGly, while the rest sit at 0.1–4%. **A page that sorts by this rate
makes eight failed enrichments visible at a glance**, which no existing FRAN view does.

## The problem this page solves

A core facility runs PTM enrichments for clients. Today there is no way to ask "did that enrichment
work?" without opening the search and counting by hand — which is how eight under-performing
ubiquitin experiments sat unexamined. The question is operational, not exploratory, and it recurs
every time a client asks why their phospho data is thin.

## The page

Route `/#/ptm`, in the idiom of the existing Engines and Species pages.

### 1. Corpus summary

Counts over `delimp_search_protein_ptm`, stated for exactly what they cover:

- searches carrying any modification / phospho / GlyGly
- protein groups carrying each
- **explicitly captioned** "phospho and GlyGly only — this table does not distinguish oxidation,
  acetyl or deamidation"

### 2. Enrichment table — the substance of the page

One row per search with any modification, sortable, default sorted by modified rate descending:

| column | source |
|---|---|
| search name, date, species, instrument | `delimp_searches` join |
| protein groups with any modification | `count(*) FILTER (WHERE has_ptm)` |
| phospho proteins / GlyGly proteins | `count(*) FILTER (...)` |
| **modified rate** | `sum(n_mod_precursors) / n_precursors_total` |
| verdict | derived — see below |

**The verdict column is the point, and it must not overclaim.** A rate above ~50% on a search whose
name suggests enrichment is a working enrichment; a rate under ~5% on such a search is not. But the
threshold is a heuristic, not a measurement, so the column says **"enriched"** / **"low for an
enrichment"** / **"incidental"** and a tooltip states the rate and that the classification is by
rate alone. It must never say "failed" — FRAN cannot distinguish a failed enrichment from a sample
that was never enriched.

**Name-based detection of intent is deliberately excluded.** Matching `ubiq|phospho` on
`search_name` to decide whether enrichment was *intended* is a guess about a human's naming habits.
Show the rate; let the reader judge.

### 3. Distribution

Modified rate against species and instrument, and over time — enough to see whether a platform or a
period under-performs. Small, and it reads from the same aggregate.

## Constraints

- Every `query()` passes `tables=[...]`.
- **Reads only `delimp_search_protein_ptm` and `delimp_searches`.** Never `delimp_precursors` —
  the corpus-wide scan is >15 minutes and this is a public page.
- The page is **public-tier**. `delimp_searches.search_name` is already public elsewhere, but
  nothing filename-shaped or directory-shaped may reach the response, and `privacy.redact()`
  rewrites VALUES not KEYS.
- **Handle the un-backfilled state honestly.** The rollup covers 2 of 2,086 searches at the time of
  writing; a backfill is running. Until it completes the page must say how many searches are
  covered and that the rest are not yet computed — the same discipline as `ptm_rollup_ready` on the
  heatmap filter. A page silently showing two searches as "the corpus" is the
  absent-data-looks-like-clean-data failure this project keeps producing.
- Cache it. Public, and the aggregate touches every rollup row.
- Every test proven able to fail.

## Risks

| risk | mitigation |
|---|---|
| Reader takes "low for an enrichment" as "the experiment failed" | Wording never says failed; tooltip gives the rate and states the basis |
| Page reads as a complete modification census | Caption states phospho/GlyGly only, at the top, not in a footnote |
| Rollup partially backfilled → misleading corpus totals | Coverage stated on the page; not-yet-computed is distinct from zero |
| `n_precursors_total` is NULL or stale for some searches | Rate is omitted, not shown as 0 — absence is not a measurement |
| Aggregate cost grows with the corpus | Reads a rollup bounded by (searches × protein groups), not precursors; cached |

## Out of scope

- Per-modification breakdown beyond phospho/GlyGly. Needs a richer rollup and another backfill.
- Site-level views. Needs `delimp_ptm_site`, unbuilt.
- Inferring enrichment intent from search names.
- Random match probabilities, ancestry, anything not derivable from these two tables.
