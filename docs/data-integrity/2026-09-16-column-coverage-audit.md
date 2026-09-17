# Column coverage audit — 2026-09-16

First run of a standing check: **which FRAN columns are empty, and which of those should not be.**
Kept in git so the answer can be compared over time rather than rediscovered.

Re-run it with:

```bash
python3 ingest/audit_column_coverage.py                                        # report
python3 ingest/audit_column_coverage.py --check docs/data-integrity/column_coverage_baseline.json
python3 tests/test_precursor_column_coverage.py                                # no DB needed
```

## Why this was needed

Three defects, none of which ever raised an error:

- `delimp_precursors.pep` was parsed from `EG.PEP` on **every** Spectronaut row since the adapter's
  first day and written on **none** of them. `_PREC_COLS` in `ingest/corpus_ingest.py` — the only
  insert path — had no `pep` column, and an adapter key with no matching column is silently
  discarded. No exception, no changed row count, nothing in a log.
- Four DIA-NN fields (`empirical_quality`, `precursor_id_diann`, `peak_fwhm`, and `pep` again) were
  written by the 2026-06 loader and quietly stopped when the path was consolidated. They are 100%
  populated in three June searches and 0% in everything since.
- `delimp_precursors.intensity_log2` had **no writer at all**, yet three surfaces read it: the
  peptide page's `Avg log₂ int` column (an em-dash on every peptide in the corpus), the Ion
  Mobility scatter's intensity dimension, and `delimp_training_gold` — the ML training view, which
  served an all-NULL feature to the training lane. A missing value renders as a dash rather than
  throwing, so the omission survived into production on all three. (The third was found by review,
  *after* this document first claimed there were two. See "Follow-ups".)

The shared shape: **absent data is indistinguishable from clean data.** Reading either half of the
code looks correct; only comparing what an adapter *produces* against what the INSERT *consumes*
finds it.

## Fixed in this pass

| What | Where | Effect |
|---|---|---|
| `intensity_log2` derived from `intensity` | `app/queries.py` (peptide table + IM scatter), `ingest/refresh_leaderboards.py` (matview) | `Avg log₂ int` renders real numbers; scatter regains its intensity dimension. Verified against live rows: 8.494 / 4.976 / 10.988 where the stored column returns NULL |
| `pep` + 3 DIA-NN fields wired into the insert | `ingest/corpus_ingest.py` `_PREC_COLS`, both tuple branches, `_diann_rows` mapping | Populated on every future ingest. Names taken from surviving legacy VALUES, not guessed |
| **Unmapped-key guard** | `ingest/corpus_ingest.py` `_warn_unmapped_record_keys()` | An adapter key with no column now prints a loud warning instead of vanishing. Never raises — an unexpected key must not abort an otherwise-correct ingest |
| `--rebuild` for matviews | `ingest/refresh_leaderboards.py` | `--create` uses `CREATE ... IF NOT EXISTS`, a no-op on an existing view, and `REFRESH` re-runs the *catalog's* definition, not this file's. Editing matview SQL was previously inert and looked like it worked |
| Unguarded `FASTA proteins` KPI tile | `app/static/app.js` | 2,089 of 2,093 search pages showed a dead tile; now hidden when absent, matching its neighbours |
| Stale schema comments | `ingest/annotate_schema.py` | The block said experimental groups "have never been ingested" — they are, in `delimp_run_design` (replicate 100%). A stale entry in the read-me-first block sends readers away from a question the corpus can now answer |
| New column comments | `ingest/annotate_schema.py` | `intensity_log2` (do not read), `pep`, `raw_basename` (100% NULL, no reader, name invites a join returning nothing), `library_match` |

### Found by the new test, not by the audit

`tests/test_precursor_column_coverage.py` runs both adapters over synthetic reports and compares
the keys they emit against `_PREC_COLS`. Within minutes it surfaced five more discarded keys the
audit had not: `engine`, `instrument`, `organism` (all consumed at search level — legitimate), and
**`ccs` and `ce`** — collision cross section and collision energy, parsed from every Spectronaut
row with no column on `delimp_precursors` to receive them. Declared in `_PREC_DROPPED_OK` with
that reasoning rather than waved through: the values are already in the report, and only a column
plus two tuple slots stand between them and the corpus.

## Not fixed, and why

- **`library_match`** — its only values are the constant string `'empirical'` on three legacy
  searches. That is a property of the library, not a per-precursor measurement from any report
  column. Left NULL rather than given an invented mapping.
- **`predicted_organism_*` (1 of 23,451)** — `ingest/backfill_organism_from_lance.py` is complete
  and wired but has never been run with `--apply`. This is a data backfill, not a code defect;
  deferred while PG Farm was IO-saturated. Impact is limited: curated `organism_name` covers 97.4%
  and the app COALESCEs.
- **`site_localization_probability`** — the parser shipped 2026-09-10; no search ingested since
  was a PTM-localization analysis. Being fixed separately by re-ingest (2 of 7 candidate searches
  done as of this date). Only searches whose *original* Spectronaut analysis enabled localization
  can ever gain it — re-export cannot create what was never computed.
- **`delimp_raw_catalog`** (8 columns, 50 rows flagged ingested with no `fran_search_id`) — no
  reader or writer exists in this repo; it is written by a Windows node. Flagged, not classified.
- **The dead-draft precursor columns** (`peak_mz`, `peak_intensity`, `peak_annotation`,
  `n_peaks_total`, `usi`, `predicted_mz`, `precursor_mass`, `ms2_spectrum_md5`, `ms1_apex_scan`,
  `ms2_apex_scan`) — already documented as having no writer. Correctly empty.

## Follow-ups required — this pass is not complete without them

Found by adversarial review of the fix itself, not by the audit. Listed because each is a way the
fix is currently *partial*, which is worse than untouched if nobody knows.

1. **`delimp_training_gold` still selects the dead `intensity_log2`.** It is a DB-only view with
   no source in this repo (`schema/fran_schema.sql:771` is a dump of it, not its definition), so
   it needs a `CREATE OR REPLACE VIEW` against PG Farm. Until then the ML training lane keeps
   receiving an all-NULL feature.
2. **That same view's localization filter is about to change behaviour.** Its WHERE clause reads
   `COALESCE(p.site_localization_probability, 1::real) > 0.75`. While the column was 100% NULL the
   COALESCE defaulted **every modified precursor to "perfectly localized"** and admitted it. As the
   localization re-ingests land, that filter starts genuinely excluding poorly-localized
   precursors — the intended behaviour, but a real change in what the training set contains, and
   it will arrive search-by-search rather than all at once. Anyone comparing training runs across
   this boundary needs to know.
3. **`schema/fran_schema.sql` still builds the broken matview** (lines 661-677 create
   `delimp_mv_im_scatter` reading the dead column). It is generated by `scripts/dump_schema.py`
   from the live database, so the order is: run `refresh_leaderboards.py --rebuild` against PG
   Farm **first**, then regenerate the dump. Until both happen, a fresh install from `schema/`
   reproduces exactly the bug this pass fixes.
4. **`predicted_organism_*`** needs a `backfill_organism_from_lance.py --apply` run.
5. **Localization re-ingests**: 2 of 7 candidate searches done.
6. **The `denovo` worktree holds a stale `corpus_ingest.py`** with its own copy of the precursor
   tuple. It needs the same `_PREC_COLS` change when that branch merges, or merging silently
   reverts `pep` and the three DIA-NN fields to being dropped again. `tests/
   test_precursor_column_coverage.py` will catch it, which is the point of having it.

## Legitimately empty — do not "fix" these

- `delimp_run_design.fraction` — the parser works; most experiments simply are not fractionated.
- `delimp_xic_trace_lane.agree_*` — deliberately withheld (`selfref_score.py:245`, `promote=False`).
  Writing them would mark a run "checked" on the strength of a test that does not support it.
- Spectronaut `iim` / `mods` / `normalized_intensity` — explicitly guarded in the adapter and
  already documented.
- `best_q_value` — ~99.9% NULL; the app computes `MIN(q_value)` and does not read the column.

## Method note

`pg_stat_user_tables` is **unreliable on this database**: `n_live_tup` reported 130 rows for
`delimp_searches` (really 2,093) and 0 for four tables holding millions, with `last_analyze` and
`stats_reset` NULL. `pg_stats` itself is accurate — its `null_frac` reconciles exactly against real
counts — but `delimp_precursors`' sample dates to 2026-08-28, so every precursor finding was
re-verified with scoped `WHERE search_id = ...` queries across 12 searches, 4 engines and all
ingest eras. No bare aggregates were run against a 416M-row table.

This is why `audit_column_coverage.py` reports **both** a corpus-wide `pg_stats` view and a scoped
per-engine sample of the newest search: a writer that broke yesterday moves the scoped number
immediately and barely touches the corpus-wide one.
