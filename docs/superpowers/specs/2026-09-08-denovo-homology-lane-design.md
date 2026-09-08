# De novo → homology lane for FRAN (Casanovo + DIAMOND)

**Status:** design approved 2026-09-08. Not yet implemented.
**Branch:** `denovo-corpus` (worktree `.claude/worktrees/denovo`).
**Repos:** DE-LIMP owns the write path; FRAN owns schema DDL and the read side.

## 1. What this is for

### The general capability, stated plainly

> **Here are the peptides de novo sequencing found. Each one links into the FRAN corpus if the
> corpus has it — and if it *nearly* has it, that near-miss is a variant candidate.**

That holds for any de novo run, any organism, any submitter. The lane is **engine-agnostic**
(`denovo_engine`: `casanovo` | `instanovo` | `cascadia`); Casanovo merely produced the bundles in
hand. Homology, LCA, species calls and FDR are all secondary to that one linkage, and a bundle
carrying none of them still produces a useful page.

The queries must run **across all de novo runs at once**, not per-case. A single case answers
"what is this sample"; the corpus of cases answers "which markers are species-specific, which are
merely avian, and which are artifacts" — and that is the question the reference library exists to
answer. See §8 (cross-run querying and the MCP backend).

### The first customer

Wildlife forensics. Whooping crane (*Grus americana*) and ocelot (*Leopardus pardalis*)
feather/hair, with bald eagle and mute swan queued behind them. These samples have **no reference
proteome**, so a database search cannot work and the species call comes from Casanovo → DIAMOND →
LCA. The crane report ends with the sentence that motivates this document: *"Processing of these
samples will expand the forensic reference library."* FRAN is already a cross-case reference
library for peptides; this makes it one for de novo peptides too.

## 2. The measurements this design rests on

All measured 2026-09-08 against the live corpus (485.6M precursors, 3.34M distinct peptides,
2,925,160 in `delimp_peptide_consensus` → 2,906,206 I/L-distinct, 262 GB) using the crane bundle
(`denovo_results_casanovo_20260407-3.zip`, 28,281 PSMs, 15,512 unique peptides ≥7 aa).

| Measurement | Value |
|---|---|
| Crane de novo peptides in FRAN, **exact** match | 2,533 / 15,512 (16.3%) |
| Same, **I/L-normalized** | **5,082 / 15,512 (32.8%)** |
| FRAN-known among **Sage-confirmed** peptides | 1,394 / 3,283 (**42.5%**) |
| FRAN-known among **de novo-only** peptides | 1,139 / 12,229 (**9.3%**) |
| Enrichment | **4.6×** |
| Join cost, 15,512 peptides | 6.6 s exact / 14.9 s I/L unindexed |
| `delimp_peptide_consensus` size | 1,291 MB |

Two conclusions, and the design is built on them:

1. **Corpus membership independently reproduces the Sage ground-truth signal** (4.6×) with no
   database search of the crane data. It is a real orthogonal correctness feature, drawn from
   485M PSMs rather than one entrapment run.
2. **I/L normalization is half the signal** (16.3% → 32.8%). De novo cannot distinguish
   isoleucine from leucine, so every cross-modality join must use an I/L-normalized key.

The *complement* is the forensic payload: the peptides FRAN has never seen are where
species-discriminating substitutions live. A peptide observed in 1,376 independent searches is
human keratin or a housekeeping protein — not evidence of crane.

## 3. Repo split

| Repo | Owns |
|---|---|
| **DE-LIMP** | The entire write path: producing the bundle (Casanovo → decoy gen → DIAMOND → mokapot FDR → LCA) and `scripts/fran_denovo_ingest.py`, which reads a bundle, computes the corpus join and the GVP classification, and writes the `delimp_denovo_*` tables. |
| **FRAN** | Read side only: `schema/denovo.sql`, `app/denovo.py`, queries, page, MCP tools, `PUBLIC_TABLES` entries. FRAN never writes a de novo row. |

The PG Farm service account **owns all 48 public tables** in the `delimp` database, so DE-LIMP can
run its own additive DDL with no human in the loop. (Not true of the `stan` database, where it
cannot `ALTER` — relevant if this ever moves.)

## 4. Bundle format

v1 exists and is declared in `denovo_info.json` as `"format": "delimp_denovo_v1"` (DE-LIMP
v3.7.0). The crane and ocelot bundles in hand are v1.

| File | v1 | v2 adds |
|---|---|---|
| `casanovo_psms.csv` | ✓ | `is_decoy` |
| `blast_results.csv` | ✓ (species as a *string*) | → `homology_hits.csv`; `staxids`, decoy rows |
| `classification_{confirmed,novel}.csv` | ✓ | unchanged |
| `species_summary.csv` | ✓ | unchanged |
| `denovo_fdr.csv` | — | mokapot 5-feature q-value + decoy flag, per peptide |
| `lca_calls.csv` | — | taxon_id / taxon_name / rank / lineage |
| `denovo_info.json` | format, engine, counts | + decoy FRAC, DIAMOND mode, `max_target_seqs`, DB, model version |

**The ingestor reads both.** A v1 bundle lands with FDR and LCA columns **NULL** and
`bundle_format='delimp_denovo_v1'` recorded — never faked. The corpus join and GVP classification
(§6, §7) do **not** depend on the bundle version: they are computed from the peptide list alone,
so a v1 bundle gets the full integration. This is what lets the crane and ocelot data land today.

## 5. Data model

A de novo run gets a real `delimp_searches` row (`search_engine='casanovo'`,
`pipeline_id='delimp_denovo'`) so it inherits provenance, lab attribution, `sharing_status` and
run linkage. Observations live in their own tables, because a de novo PSM has no protein group and
no q-value and would be mostly NULL in `delimp_precursors`.

- **`delimp_denovo_psm`** — one row per spectrum→peptide call: `search_id`, `denovo_engine`,
  `raw_basename`, `psm_id`, `sequence`, `stripped_seq`, `seq_il`, `charge`, `exp_mz`, `calc_mz`,
  `score`, `mean_aa_score`, `aa_scores real[]`, `is_decoy`.
- **`delimp_denovo_peptide`** — per distinct (`search_id`, `stripped_seq`): `seq_il`, `n_psms`,
  `n_runs`, `best_score`, `mean_conf`, `length`, `mokapot_score`, `q_value`, `corpus_class`
  (`conserved` | `gvp` | `novel`), `is_decoy`.
- **`delimp_denovo_corpus_match`** — the corpus linkage join table (§6).
- **`delimp_denovo_gvp`** — the variant candidates (§7).
- **`delimp_denovo_homology`** — DIAMOND hits: `subject`, `staxids int[]`, `pident`, `aln_length`,
  `evalue`, `bitscore`, `qstart/qend/sstart/send`.
- **`delimp_denovo_lca`** — taxon call per peptide, **mirroring the existing `delimp_peptide_lca`
  (Unipept) column shape** so the peptide page can show both side by side.
- **`delimp_denovo_species`** — per-search species roll-up.

### Decisions taken

**DDA stays out of the DIA corpus.** De novo PSMs never enter `delimp_precursors`, so the 485.6M
headline and every matview on it are untouched. The `delimp_searches` count ticks up by one per de
novo run; the row is self-identifying via `search_engine='casanovo'`.

**Homology-hit storage.** ~312k ocelot peptides × 25 hits ≈ 7.8M rows per cohort on a 262 GB
database. Target hits go to Postgres; decoy hits are kept as summary statistics only, since decoys
are needed at FDR-compute time and not at browse time. A Lance lane is the documented escape hatch
— the same call FRAN already makes for XIC.

**Safety.** New tables are invisible to the FRAN app until added to `app/db.py PUBLIC_TABLES`, an
allowlist `scripts/predeploy_check.py` enforces against every `tables=[...]` literal. Creating them
in the live corpus is therefore additive and safe.

## 6. The corpus linkage — both directions

The user requirement: *DDA de novo data leverages the corpus, and the DIA corpus can search the de
novo data.* One shared key serves both.

**The key is I/L-normalized `seq_il`** — a stored, indexed column on the de novo side, and an
**expression index** on the corpus side:

```sql
CREATE INDEX idx_consensus_seq_il
    ON delimp_peptide_consensus (replace(stripped_seq, 'I', 'L'));
```

An expression index avoids a new column, a new writer, and any new maintenance job on a
FRAN-owned table. It goes on `delimp_peptide_consensus` (2.93M peptides, 1,291 MB) and **not** on
`delimp_precursors` (485M rows), where it would cost tens of GB for no extra reach.

### `delimp_denovo_corpus_match` is a join table

One row per (de novo peptide × matched corpus spelling) — not columns on the peptide — because the
I/L collapse is one-to-many in reverse. Measured on the crane bundle:

| Corpus spellings matched by one de novo peptide | de novo peptides |
|---|---|
| 1 (unambiguous) | 4,664 |
| 2 | 375 |
| 3 | 34 |
| 4 / 5 / 7 | 6 / 1 / 2 |

**418 of 5,082 matches (8.2%) are ambiguous.** `ALLEVLGR` matches three real corpus peptides
(`AIIEVLGR`, `ALIEVLGR`, `ALLEVLGR`); `APGLLPR` matches `APGIIPR`, `APGILPR`, `APGLLPR`. The UI
shows every candidate rather than silently choosing one — that ambiguity is a true property of de
novo sequencing, and hiding it would misrepresent the evidence.

Columns: `search_id`, `stripped_seq`, `corpus_stripped_seq` (the link target), `match_kind`
(`exact` | `il`), `n_candidates`, `corpus_n_obs`, `corpus_n_searches`, `corpus_irt_mean`,
`corpus_im_mean`, `corpus_min_q`.

**Direction A — de novo leverages the corpus.** Three uses:

1. *A correctness feature.* `log1p(corpus_n_obs)` and `corpus_n_searches` become features 6–7 in
   the existing 5-feature linear mokapot. **Caveat, load-bearing:** the signal is partly "the
   peptide is common", so it helps conserved peptides and does nothing for species-specific ones.
   It is a reported feature, never a gate — the conclusion `denovo_homology_fdr.py` §5 already
   reached about its own model.
2. *A conservation filter* — §2's forensic payload, automated.
3. *An iRT sanity check where it applies.* `irt_mean`/`irt_sd` span up to 1,376 searches. Limits:
   comparison needs gradient alignment, and `im_mean` is timsTOF-only — the crane is Exploris DDA,
   so ion mobility is unavailable for these bundles. Populate only when alignable.

**Direction B — the corpus searches the de novo data.** A view `delimp_v_peptide_sources` unions
both modalities on `seq_il`, labelled by source, so one lookup answers from both:

- `#/peptide/{seq}` gains a **"seen de novo"** panel: which runs called this peptide, at what
  confidence, and whether any run called a *variant* of it (§7).
- FRAN's peptide **search** resolves against the view, finding a sequence in either modality.
- `delimp_denovo_homology` + `delimp_denovo_lca` become an **annotation resource for DIA peptides
  in poorly-annotated organisms** — a DIA peptide with no protein group in a non-model species can
  be given a homology and a taxon by the de novo lane.

### Withdrawn from the design

**"Casanovo as a 5th confirming engine."** `delimp_consensus_ids` joins on
`raw_path + stripped_seq`, so per-run engine agreement needs the same raw file on both sides.
Verified 2026-09-08: neither `Ex03212025*` (crane) nor `Ex032802024*` (ocelot) appears in
`delimp_raw_catalog` — 0 rows each. Possible only if the DDA raws are ingested or Casanovo is run
over raws FRAN already holds. The tables are shaped so it stays possible; it is not claimed now.

## 7. GVP detection — the near-miss is the point

A genetically variant peptide carries an amino-acid substitution from a SNP. The crane report's
"134 feather keratin peptides bearing species-discriminating amino acid substitutions" are exactly
that, curated by hand. The corpus join already computes distance to the corpus, so **one pass
classifies everything**:

| Distance to corpus | `corpus_class` | Meaning |
|---|---|---|
| 0 (exact or I/L) | `conserved` | contaminant / housekeeping — not evidence of species |
| **1 substitution** | **`gvp`** | **variant candidate — the forensic signal** |
| no near match | `novel` | genuinely new, or de novo error |

### The algorithm, and why it is affordable

Naive comparison is 15k × 2.9M. Instead use **pigeonhole blocking**: two equal-length strings
differing in ≤1 position must agree exactly on at least one half, so index the corpus by
`(len, left_half)` and `(len, right_half)`, probe both, and verify with a real Hamming distance.
Exact — no false negatives.

This runs **in the DE-LIMP ingestor, in Python**, against a one-time `COPY` of the corpus peptide
list (2.93M peptides, 44 MB, dumped in 5.2 s). No corpus-side schema change, no Postgres
extension, no new maintenance job. Measured end to end:

- corpus load + I/L fold: 2.5 s · blocking index (4.2M buckets): 4.1 s
- **classify 15,512 peptides: 0.1 s**

### The two controls, and what they remove

A 1-residue difference from de novo data is as likely to be a *sequencing error* as a variant.
Both controls are applied before a candidate is called a GVP:

1. **SNP reachability.** A real GVP comes from a single nucleotide change, so the substitution must
   be reachable by one nucleotide substitution in the genetic code. `S→E` is not, and is discarded.
2. **Non-isobaric.** Near-isobaric swaps (K↔Q, 0.036 Da) are the classic de novo artifact.

Measured on the crane bundle:

| | Candidates | Share |
|---|---|---|
| 1 substitution from a corpus peptide | 1,662 | — |
| reachable by one nucleotide change | 1,149 | 69.1% |
| near-isobaric (discarded) | 7 | 0.4% |
| **passes both controls** | **1,139** | **68.5%** |
| …substitution in the peptide **interior** | **897** | 78.8% of credible |

Near-isobaric confusion is *not* the dominant error mode here — I/L normalization already removed
the big one. The controls mainly remove substitutions needing more than one nucleotide change.

**Terminal substitutions are reported but flagged.** 188 candidates (16.5%) place the substitution
at position 1 and 54 (4.7%) at the C-terminal residue, where de novo confidence is lowest —
Casanovo's own feature set weights `nterm` and `cterm` confidence separately for this reason. The
position-1 calls are spread across residues (G 25, T 22, N 20, E 18, L 17, A 15) rather than piled
on one, so this is not a single systematic artifact; but the **897 interior** substitutions are the
defensible core and the UI ranks them first.

The top interior substitutions are textbook conservative SNP changes, which is the sanity check
that the detector works: `Q→E` 98, `D→N` 63, `V→L` 52, `N→D` 52, `C→G` 50, `L→V` 35, `S→T` 24,
`D→E` 23.

`delimp_denovo_gvp` columns: `search_id`, `stripped_seq`, `corpus_stripped_seq`, `position`,
`aa_from`, `aa_to`, `mass_delta`, `one_nt_reachable`, `is_isobaric`, `is_terminal`, `n_neighbours`.

**Open caveat.** `C→G` (50) is worth watching: if the bundle spells cysteine as `C+57.021` while
the corpus stores it plain, a carbamidomethyl-handling mismatch could manufacture spurious cysteine
substitutions. The ingestor must strip modifications to a bare residue string before comparison,
and the test suite should assert that a carbamidomethylated peptide matches its plain corpus form.

## 8. Cross-run querying and the Claude backend

Queries run across **all** de novo runs, not within one. That means:

- `delimp_denovo_peptide` and `delimp_denovo_gvp` are indexed on `seq_il` and on
  `(aa_from, aa_to, position)` **without** `search_id` leading, so cross-run scans are cheap.
- A materialized view `delimp_mv_denovo_peptide_index` aggregates per `seq_il` across runs:
  `n_runs`, `n_psms`, `best_score`, `array_agg(search_id)`, `corpus_class`. This is what answers
  "which markers appear in crane but not eagle or swan" — the cross-case question.

**The Claude backend already exists.** `app/mcp_server.py` (221 lines, FastMCP, read-only, public
tools only, served at `/mcp`) is FRAN's MCP endpoint. De novo tools are added there alongside the
existing `search_peptides` / `peptide_detail` / `corpus_overview`:

| Tool | Answers |
|---|---|
| `search_denovo_peptides(q, engine, corpus_class, limit)` | across every de novo run |
| `denovo_peptide_detail(seq)` | corpus linkage, homology, LCA, which runs called it |
| `find_gvp_candidates(aa_from, aa_to, interior_only, min_runs, search_id)` | the variant hunt |
| `denovo_run_detail(search_id)` | one case: species call, class split, top markers |
| `compare_denovo_runs(search_ids)` | markers unique to one run vs shared — the forensic question |

These inherit the existing security posture verbatim: `_assert_allowlisted()` means a tool cannot
reach a non-public table even if a query names one, and no internal/collaborator tools are
registered. De novo tables must be added to `PUBLIC_TABLES` **only** for runs whose
`sharing_status` permits it — the same default-deny the corpus already applies.

## 9. Site surface (FRAN read side)

**The primary surface is the peptide list.** A de novo run's page is a table of the peptides it
found; each peptide FRAN knows is a link into the existing peptide page. This is the cheapest
useful thing to build and the first thing to ship.

The link plumbing exists: the SPA routes `#/peptide/<seq>` (`app/static/app.js` `route()`), and
`pepChip(seq)` at `app/static/app.js:1153` already renders a clickable peptide chip. The de novo
table reuses that component, so a matched peptide behaves exactly like a peptide anywhere else.

| Column | Content |
|---|---|
| Peptide | de novo spelling, monospace |
| Class | `conserved` / `gvp` / `novel` chip |
| In FRAN | linked corpus spelling(s) via `pepChip`, or "not seen" |
| Variant | `Q→E @ 9` for a GVP, with an interior/terminal flag |
| Observations | `corpus_n_obs` / `corpus_n_searches` |
| Confidence | de novo score |

Then, in decreasing priority:

- `/denovo` — run index: engine, organism (if called), peptide count, class split.
- `/denovo/{search_id}` — the run page carrying the table above; LCA species tree and confidence
  distribution where the bundle supplies them.
- `/denovo/gvp` — the cross-run variant hunt, filterable by substitution and run.
- `#/peptide/{seq}` — the "seen de novo" panel (Direction B).
- Species page — de novo-derived taxa alongside database-derived ones.

## 10. Correctness, errors, testing

- **Idempotent** per `search_id` (uuid5 of `output_dir`, matching `corpus_ingest`); re-ingest is
  delete+copy scoped to one search.
- `bundle_format` records v1 vs v2; v1 lands with NULL FDR/LCA rather than invented values.
- A `delimp_component_version` row for the writer, matching the existing convention.
- **Tests**, each asserting a number measured above so the design's claims are regression-tested
  rather than asserted once:
  - round-trip ingest of a trimmed crane bundle
  - the **42.5% / 9.3%** corpus-confirmation enrichment reproduces
  - I/L matching finds **5,082** where exact finds 2,533
  - GVP classification yields **1,139** credible candidates, **897** interior
  - `S→E` is rejected as not one-nucleotide-reachable; `K→Q` is rejected as isobaric
  - a carbamidomethylated `C+57.021` peptide matches its plain corpus spelling

## 11. Deployment notes

No dev/staging site exists: `plan-fran` is **B1 Basic, which supports zero deployment slots**.
Deploy fires only on push to `main` and only for `app/**`, `requirements.txt` and the workflow
files — `ingest/**`, `schema/**` and `docs/**` never trigger a production restart. Branch work is
inert. Local development is `uvicorn app.main:app --port 7860` against the live read-only corpus.

For collaborator testing (Glendon), the path is **production plus the existing access tiers**, not
a dev site: the de novo run ships with `sharing_status` private and is visible only to the `lab`
tier via Entra group membership. Nothing appears on the public site.
