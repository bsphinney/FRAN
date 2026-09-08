# De novo → homology lane for FRAN (Casanovo + DIAMOND)

**Status:** design approved 2026-09-08. Not yet implemented.
**Branch:** `denovo-lane`.
**Repos:** DE-LIMP owns the write path; FRAN owns schema DDL and the read side.

## 1. What this is for

Wildlife forensics. Whooping crane (*Grus americana*) and ocelot (*Leopardus pardalis*)
feather/hair, plus bald eagle and mute swan queued behind them. These samples have **no
reference proteome**, so a database search cannot work and the species call comes from
Casanovo de novo sequencing followed by DIAMOND homology and an LCA over the hits.

The crane report ends with the sentence that motivates this whole document: *"Processing of
these samples will expand the forensic reference library."* FRAN is already a cross-case
reference library for peptides. This lane makes it one for de novo peptides too.

## 2. The measurements this design rests on

All measured 2026-09-08 against the live corpus (485.6M precursors, 3.34M distinct peptides,
2.93M in `delimp_peptide_consensus`, 262 GB) using the crane bundle
(`denovo_results_casanovo_20260407-3.zip`, 28,281 PSMs, 15,512 unique peptides ≥7 aa).

| Measurement | Value |
|---|---|
| Crane de novo peptides already in FRAN, **exact** match | 2,533 / 15,512 (16.3%) |
| Same, **I/L-normalized** | **5,082 / 15,512 (32.8%)** |
| FRAN-known among **Sage-confirmed** crane peptides | 1,394 / 3,283 (**42.5%**) |
| FRAN-known among **de novo-only ("novel")** crane peptides | 1,139 / 12,229 (**9.3%**) |
| Enrichment | **4.6×** |
| Join cost, 15,512 peptides vs `delimp_peptide_consensus` | 6.6 s exact / 14.9 s I/L unindexed |
| `delimp_peptide_consensus` size | 1,291 MB |

Two conclusions follow, and the design is built on them:

1. **FRAN corpus membership independently reproduces the Sage ground-truth signal** (4.6×
   enrichment) without any database search of the crane data. It is a genuine orthogonal
   correctness feature, derived from 485M PSMs rather than one entrapment run.
2. **I/L normalization is half the signal** (16.3% → 32.8%). De novo cannot distinguish
   isoleucine from leucine, so every cross-modality join must be on an I/L-normalized key.
   This is not a refinement; without it the integration loses half its reach.

The *complement* is the forensic payload: the ~90% of de novo-only peptides FRAN has never
seen is where species-discriminating substitutions live. A peptide observed in 1,376
independent FRAN searches is human keratin or a housekeeping protein — it is not evidence of
crane. The crane report hand-curated 134 forensic markers; this turns that step into a corpus
lookup that improves every time FRAN ingests anything.

## 3. Repo split

| Repo | Owns |
|---|---|
| **DE-LIMP** | The entire write path: producing the bundle (Casanovo → decoy gen → DIAMOND → mokapot FDR → LCA) and `scripts/fran_denovo_ingest.py`, which reads a bundle and writes the `delimp_denovo_*` tables directly. |
| **FRAN** | Read side only: `schema/denovo.sql`, `app/denovo.py`, queries, page, `PUBLIC_TABLES` entries. FRAN never writes a de novo row. |

This works because the PG Farm service account **owns all 48 public tables** in the `delimp`
database, so DE-LIMP can run its own additive DDL with no human in the loop. (Not true of the
`stan` database, where the service account cannot `ALTER` — relevant if this ever moves.)

The contract between the repos is the bundle format plus the schema. Both are versioned.

## 4. Bundle format

v1 already exists and is declared in `denovo_info.json` as `"format": "delimp_denovo_v1"`
(DE-LIMP v3.7.0). The crane and ocelot bundles in hand are v1.

| File | v1 | v2 adds |
|---|---|---|
| `casanovo_psms.csv` | ✓ | `is_decoy` |
| `blast_results.csv` | ✓ (species as a *string*) | → `homology_hits.csv`; `staxids`, decoy rows |
| `classification_{confirmed,novel}.csv` | ✓ (Sage-confirmed) | unchanged |
| `species_summary.csv` | ✓ | unchanged |
| `denovo_fdr.csv` | — | mokapot 5-feature q-value + decoy flag, per peptide |
| `lca_calls.csv` | — | taxon_id / taxon_name / rank / lineage |
| `denovo_info.json` | format, engine, counts | + decoy FRAC, DIAMOND mode, `max_target_seqs`, DB, Casanovo model version |

**The ingestor reads both.** A v1 bundle lands with FDR and LCA columns **NULL** and
`bundle_format='delimp_denovo_v1'` recorded — never faked. This matters because it lets the
crane and ocelot bundles land today rather than waiting on a re-run.

## 5. Data model

A de novo run gets a real `delimp_searches` row (`search_engine='casanovo'`,
`pipeline_id='delimp_denovo'`) so it inherits provenance, lab attribution, `sharing_status`
and run linkage for free. Its observations then live in their own tables, because a de novo
PSM has no protein group and no q-value and would be mostly NULL in `delimp_precursors`.

- **`delimp_denovo_psm`** — one row per spectrum→peptide call: `search_id`, `raw_basename`,
  `psm_id`, `sequence`, `stripped_seq`, `seq_il`, `charge`, `exp_mz`, `calc_mz`, `score`,
  `mean_aa_score`, `aa_scores real[]`, `is_decoy`.
- **`delimp_denovo_peptide`** — per distinct (`search_id`, `stripped_seq`): `seq_il`, `n_psms`,
  `n_runs`, `best_score`, `mean_conf`, `length`, `mokapot_score`, `q_value`, `is_decoy`.
- **`delimp_denovo_homology`** — DIAMOND hits: `subject`, `staxids int[]`, `pident`,
  `aln_length`, `evalue`, `bitscore`, `qstart/qend/sstart/send`.
- **`delimp_denovo_lca`** — taxon call per peptide. **Deliberately mirrors the existing
  `delimp_peptide_lca` (Unipept) column shape** so the peptide page can show a de novo NCBI
  call beside a Unipept call.
- **`delimp_denovo_species`** — per-search species roll-up (`species_summary.csv`).
- **`delimp_denovo_corpus_match`** — the integration table (§6).

### Decisions taken

**DDA stays out of the DIA corpus.** De novo PSMs never enter `delimp_precursors`, so the
485.6M headline count and every matview built on it are untouched. The `delimp_searches` count
does tick up by one per de novo run; that is accepted as correct — the corpus is gaining a
second modality, and the row is self-identifying via `search_engine='casanovo'`.

**Homology-hit storage.** ~312k ocelot peptides × 25 hits ≈ 7.8M rows per cohort on a 262 GB
database. Target hits go to Postgres; decoy hits are kept as summary statistics only, since
decoys are needed at FDR-compute time and not at browse time. A Lance lane is the documented
escape hatch if this grows — the same call FRAN already makes for XIC.

**Safety.** New tables are invisible to the FRAN app until added to `app/db.py PUBLIC_TABLES`,
an allowlist that `scripts/predeploy_check.py` enforces against every `tables=[...]` literal.
Creating the tables in the live corpus is therefore additive and safe.

## 6. The corpus-join layer — both directions

The user requirement: *DDA de novo data leverages the corpus, and the DIA corpus can search
the de novo data.* One shared key serves both.

**The key is I/L-normalized `seq_il`.** On the de novo side it is a stored, indexed column. On
the corpus side it is an **expression index**:

```sql
CREATE INDEX idx_consensus_seq_il
    ON delimp_peptide_consensus (replace(stripped_seq, 'I', 'L'));
```

An expression index avoids a new column, a new writer, and any new maintenance job on a table
FRAN already owns. It goes on `delimp_peptide_consensus` (2.93M peptides, 1,291 MB) and
**not** on `delimp_precursors` (485M rows), where it would cost tens of GB for no extra reach.

**Direction A — de novo leverages the corpus.** `delimp_denovo_corpus_match`, computed at
ingest: `corpus_n_obs`, `corpus_n_searches`, `corpus_irt_mean`, `corpus_im_mean`,
`corpus_min_q`, `match_kind ('exact'|'il'|'none')`. Buys three things:

1. *A correctness feature.* `log1p(corpus_n_obs)` and `corpus_n_searches` become features 6–7
   in the existing 5-feature linear mokapot. **Caveat, and it is load-bearing:** this signal is
   partly "the peptide is common", so it helps conserved peptides and does nothing for
   species-specific ones. It is a reported feature, never a gate — the same conclusion
   `denovo_homology_fdr.py` §5 already reached about its own model.
2. *A conservation filter* — §2's forensic payload, automated.
3. *An iRT sanity check where it applies.* `irt_mean`/`irt_sd` span up to 1,376 searches.
   Limits stated rather than papered over: comparison needs gradient alignment, and `im_mean`
   is timsTOF-only — the crane is Exploris DDA, so ion mobility is unavailable for these
   bundles. Populate only when alignable.

**Direction B — the corpus searches the de novo data.** A view `delimp_v_peptide_sources`
unions both modalities on `seq_il`, labelled by source, so a single lookup answers from both:

- `/peptide/{seq}` (a DIA peptide) gains a **"seen de novo"** panel: which forensic cases
  called this peptide, at what confidence.
- FRAN's peptide **search** resolves against the view, so a sequence is found in either
  modality rather than only the DIA corpus.
- `delimp_denovo_homology` + `delimp_denovo_lca` become an **annotation resource for DIA
  peptides in poorly-annotated organisms** — a DIA peptide with no protein group in a
  non-model species can be given a homology and a taxon by the de novo lane.

### Withdrawn from the design

**"Casanovo as a 5th confirming engine."** `delimp_consensus_ids` joins on
`raw_path + stripped_seq`, so per-run engine agreement requires the same raw file on both
sides. Verified 2026-09-08: neither `Ex03212025*` (crane) nor `Ex032802024*` (ocelot) appears
in `delimp_raw_catalog` — 0 rows each. This becomes possible only if the DDA raws are ingested
or Casanovo is run over raws FRAN already holds. The tables are shaped so it stays possible;
it is not claimed now.

## 7. Site surface (FRAN read side)

- `/denovo` — case index, one card per de novo search: species call, peptide count, confidence tier.
- `/denovo/{search_id}` — case page: LCA species tree, species table, confidence distribution,
  and the conserved-vs-species-candidate split from the corpus join.
- `/peptide/{seq}` — the "seen de novo" panel (Direction B).
- Species page — de novo-derived taxa alongside database-derived ones.

## 8. Correctness, errors, testing

- **Idempotent** per `search_id` (uuid5 of `output_dir`, matching `corpus_ingest`); re-ingest is
  delete+copy scoped to one search.
- `bundle_format` records v1 vs v2; v1 lands with NULL FDR/LCA rather than invented values.
- A `delimp_component_version` row for the writer, matching the existing convention.
- **Tests:** a trimmed crane bundle as fixture; round-trip ingest; and an assertion that the
  **42.5% / 9.3%** enrichment reproduces — so the central claim of this design is
  regression-tested rather than asserted once.

## 9. Deployment notes

No dev/staging site exists: `plan-fran` is **B1 Basic, which supports zero deployment slots**.
Deploy fires only on push to `main` and only for `app/**`, `requirements.txt` and the workflow
files — `ingest/**`, `schema/**` and `docs/**` never trigger a production restart. Branch work
is therefore inert. Local development is `uvicorn app.main:app --port 7860` against the live
read-only corpus.
