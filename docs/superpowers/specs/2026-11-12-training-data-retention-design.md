# FRAN as a training corpus — what we must store, and how

**Status:** design + first measurements, 2026-11-12. Supersedes the 4-line plan in
`.omc/handoff-2026-11-12.md` §4c. Owner decision points are marked **DECIDE**.

**The ask (Brett, 2026-11-12):** "FRAN was made for this — make sure we are storing everything we
need for future AI training", like the Cascadia work, and "look how other groups are training on
proteomics data and what they need."

**Short answer.** FRAN stores *labels* well (485.6M precursor rows) but almost none of the *signal*
or *negatives* that modern models train on, and its per-run acquisition facts are half empty. Lance
is still the right container for derived training tensors, and the two lane gaps are worth closing
(they are cheap and mostly ingest-only). But closing them is not "everything": the irreducible
store is the **raw files**, made reliably addressable, plus three things FRAN does not hold at all
yet — **decoys / pre-FDR candidates**, the **isolation-window scheme and NCE per run**, and a way to
pull **unfiltered peaks around any precursor** without re-running a search.

---

## 1. What other groups train on (survey, 2026-11-12)

Condensed from a literature pass; [M] = from recall, not re-verified this session.

| Model family | Training unit | Needs beyond target IDs |
|---|---|---|
| DIA de novo — Cascadia (10.1101/2024.06.03.597251), DIANovo (arXiv 2411.15684), DeepNovo-DIA | "augmented spectrum": all MS2 peaks in the precursor's isolation window across ±N cycles + MS1 | unfiltered peaks, window scheme, RT; DIANovo only beats DDA with narrow windows |
| DDA de novo — Casanovo v4/v5, InstaNovo, ContraNovo, PrimeNovo | one PSM spectrum (~30M MassIVE-KB PSMs) | full peak list, enzyme (separate checkpoints) |
| Intensity / RT / IM predictors — Prosit(-timsTOF), AlphaPeptDeep, MS2PIP, DeepLC, Chronologer, IM2Deep, **Carafe** (PMC12592563, trains from DIA data directly) | peptide → fragments / RT / CCS | **NCE, fragmentation, instrument** as inputs; Carafe needs interference masks |
| Rescoring / FDR — Percolator, MS2Rescore, Oktoberfest, Spectralis, DIA-BERT, AlphaDIA nets | candidate feature vectors / XIC tensors | **decoys + the full pre-FDR candidate list** |
| Detectability — pFly, AP3 | peptide sequence | **expected-but-not-observed** peptides (FASTA + IDs) |
| Sample-level — tissue/species/phenotype | quant matrices | **SDRF metadata**, leakage-safe split keys |
| Formats | mzPeak (HUPO-PSI Parquet successor to mzML, 10.1021/acs.jproteome.5c00435, spec in progress) | — |

What corpora most often fail to keep, and later regret: (1) full spectra — matched fragments cannot
rebuild an augmented spectrum; (2) decoys and pre-FDR candidates — exports are post-FDR; (3) NCE /
per-scan acquisition metadata — lost on conversion; (4) interference information for fragment
labels; (5) consistent sample metadata and modification notation.

## 2. FRAN against that checklist (measured 2026-11-12, PG Farm + Hive)

37,102 runs in 3,036 searches (2,204 Spectronaut, 830 DIA-NN).

| Element | FRAN today | Gap |
|---|---|---|
| Target IDs ≤1% FDR, RT, IM, charge, mods | yes (485.6M precursor rows) | mod notation has 3 spellings (memory `fran-mod-notation-variants`) |
| Matched fragments (Spectronaut) | spectrum lane: 1,545 datasets, 351.6M precursors, 2.08B fragments | **775 SN searches / 314M precursors have no lane** |
| Fragment + MS1 XICs | 98 XIC-lane datasets; trace lane 491 runs (pilot) | **810 of 830 DIA-NN searches have no XIC lane** — the traces are already on disk |
| Unfiltered peaks around a precursor | **none** (lanes hold matched/extracted ions only) | must come from raw |
| Decoys / pre-FDR candidates | **none** (decoy leak guard keeps them out of the corpus) | SN export capped at 1% (Biognosys question open); **DIA-NN can do it from .quant — §4.3** |
| Raw file located on Hive | `raw_files.hive_path` 25,255 / 37,102 (68%) | 32% unlocated; Radiant session joins by basename, 1:many, fails silently on renames |
| Raw checksum / size | `md5` 0%, `file_size_bytes` 0% | no way to prove a raw is the one searched |
| Raw integrity | 61,058-.d tdf sweep exists, not in FRAN | not queryable per run |
| NCE / activation | `nce` **0%**, `activation_method` 19% | Prosit/AlphaPeptDeep input |
| Resolution | 26% (CORPUS_RESOLUTION.tsv has 5,127 Orbitrap runs not loaded) | load it |
| Isolation-window scheme | **no column anywhere** | Cascadia/DIANovo/rescoring all need it |
| AGC / max IT / sample amount | 0% | lower priority |
| Mobility range, cycle time, mass range | 44–67% | fill from raw probe |
| Sample metadata (SDRF) | organism 98%; **tissue, disease, cell line, replicate: 0%** | predictors exist (`predict_tissue.py`) but don't write here |
| Held-out / test-set flag | none | training needs a stable per-run split key |
| Shareable vs client-data flag | none (only `delimp_raw_files_internal` linkage) | decides whether model **weights** may be published |
| Engine + version per label | in `delimp_searches`, not on lane rows | join works; fine |
| Data-integrity label: `platform='orbitrap'` with `acquisition_method='diaPASEF'` | **2,420 runs** | instrument mislabel to investigate |

## 3. Architecture: four tiers, one rule

**Rule: store each byte of signal once; index everything else.** A per-precursor augmented spectrum
duplicates every MS2 scan across every precursor in its window. Measured on Brett's own Cascadia
set (`de-limp/cascadia/training/val_combined.asf`): **4,125 peaks and 178 KB (text) per training
spectrum**. Even packed to ~10 bytes/peak that is ~40 KB per precursor; across FRAN's 485.6M
precursor-run rows that is **~19 PB**. Materialising augmented spectra corpus-wide is not an option —
only for curated training subsets.

| Tier | What | Where | State |
|---|---|---|---|
| **T0 raw of record** | the `.raw` / `.d`, never deleted, read only via immutable opens | Flinders NFS / Quobyte | exists; needs census, checksum, integrity, verified path in FRAN |
| **T1 labels + metadata** | targets, **decoys, pre-FDR candidates**, engine features, run acquisition facts incl. **window scheme + NCE**, SDRF, split + shareable flags | PG Farm (`delimp_*`, `raw_files`) | targets yes; rest missing |
| **T2 derived lanes** | matched fragments (SN), XICs (SN + DIA-NN), our own trace lane | Lance on Quobyte, registry in PG | built for most SN; DIA-NN XIC missing |
| **T3 signal index (new)** | per precursor: run, apex scan/frame, window id, RT/IM bounds → lets a loader cut an augmented spectrum (or XIC, or mobilogram) straight from the raw or from a scan lane | PG or Lance | new |
| **T3b scan lane (option, DECIDE after pilot)** | every scan's centroided peaks once per run (+ IM for diaPASEF), vendor-neutral, mzPeak-compatible columns | Lance on Quobyte | new; size unmeasured |

Why Lance stays: columnar Arrow, random access by row, versioned, zero-copy into PyTorch, already
holding 351.6M precursors with a checksum registry; Quobyte has **122 TB free** (Flinders NFS is 82%
full, 37 TB free — new lanes go to Quobyte). Keep column names close to mzPeak so a T3b store can be
exported to the PSI format when it is final.

What we deliberately do **not** store:
- DIA-NN `*_mobilogram.parquet` — still an unlabelled 64×40,001 int block in 2.7.0 (checked
  2026-11-12); precursor identity is not recoverable (memory `fran-diann-mobilograms`). Our own
  extractor can make mobilograms with known identity instead.
- Corpus-wide augmented spectra (petabytes, see above).

## 4. Work plan

### 4.1 Lane gap: DIA-NN XIC lane — ingest only, start now
The corpus chain writes `xic/t<N>_xic/<run>.xic.parquet` (+ mobilograms) for every search; only 115
of 649 searches also have the older `report_xic/` layout, which is why the gap looked like "no data".
`diann_xic_to_lance.py` already walks nested dirs: dry run on `08Feb2024_Mouse1ug_tipctrls`
(search `e1a0e756…`) = 3 runs, 144,185 precursors, 2,014,210 traces from 147 MB, OK.
Next: array job over the 613 corpus searches (+ the 217 non-corpus DIA-NN searches where XICs exist),
`--apply` registers in `delimp_xic_lane`. Then add it to `corpus_ingest_drip.py` so it never lags.

**Training label rule for the DIA-NN XIC lane (verified 2026-11-13).** A DIA-NN XIC lane is a
SUPERSET of the corpus: it also keeps traces DIA-NN wrote in run X for precursors it reported only
in ANOTHER run of the search, with `q_value` NULL ("signal extracted, not identified here"). On 12
lanes checked by opening them, rows with `q_value <= 0.01` equalled the corpus rows exactly, there
were 0 decoys, and the NULL-q rows were 12-40% of each lane (206,769 of 562,687 in one). So:
- **positive labels = `q_value IS NOT NULL AND q_value <= 0.01` only.** A NULL-q trace is never a
  positive; using one teaches the model a label nothing confirmed in that run.
- NULL-q traces are usable only as explicitly UNLABELLED / candidate examples, marked as such.
- Labelled designs (SILAC / plexDIA): the corpus has one row per channel, the lane one trace per
  precursor (channels share the trace), so lane < corpus there is expected; join on
  (run, precursor) and carry the channel from the corpus.
The daily health scan opens the 3 newest DIA-NN lanes and checks q<=0.01 rows == corpus, 0 decoys.

### 4.2 Lane gap: Spectronaut spectrum lane — 165 now, 610 via Windows
Of the 775 lane-less SN searches (all arrived via the Windows `spectronaut-uploader`, which never
writes the lane):

| Bucket | Searches | Precursors | Action |
|---|---|---|---|
| FRAN report already on Hive | 165 | 149.7M | `backfill_fragments.py --worklist` now (1.44 TB of reports) |
| only a non-FRAN report (BGS Factory etc., no fragments) | 220 | 53.8M | re-export with FRAN.rs |
| no report next to the .sne | 229 | 51.8M | re-export |
| .sne on an unmapped drive (B:, S:, E:, D:, C:) | 153 | 56.0M | re-export on the Windows node that has the drive |
| directory gone | 8 | 2.6M | find or accept loss |

**Correction (2026-11-13): the "bug" was deliberate de-duplication.** Datasets are named by the
timestamp-stripped search name *on purpose* (`_strip_ts` docstring: "re-exports of the same
experiment … map to ONE dataset path -> no duplicate datasets"). The `--worklist` mode
(`<name>__<search_id[:8]>.lance`, explicit search id) bypassed that and wrote the 14 colliding
searches again. Checked run-by-run against the older namesakes (`clash_check2.out` in the scratch
dir):

| Clash | Old dataset | New vs old |
|---|---|---|
| Ab_geli, SpN_Park_JeongEun, diaPASEF_run_2, RM-ucsf, SpN_Polymeris, AW_28may-2025, 09Dec204_BNguyen, Ngan_P11 (8) | unlinked (search_id NULL) | **identical**: same runs, same row count per run |
| SpN_LifengJ (338,283 vs 338,248) | unlinked | same 7 runs, re-export |
| FLiuBetterDB | unlinked, 16 runs | 16 shared + 2 new runs |
| Mohibli, Curtis | linked to another search (8e3f89b9, 6e3d9e0f) | same runs, slightly different counts (a re-search) |
| KG-human-2 | linked to 76f7a3ad, 39 runs | 39 shared + 55 new runs (94) |
| JanniferSet2 ×2 (b47f4e73, c5fd8af8) | none | 17 of 18 runs shared between the two |

So the real gap for 10 of them was a **missing search_id link** on an existing dataset, not
missing spectra. Nothing is deleted; the redundant copies need a decision (§4.6 run key).
Worklist: `/quobyte/proteomics-grp/brett/scratch/training_pilot_20261112/sn_backfill_worklist.tsv`.

The 610 re-exports go to the Windows nodes over the claude bus (`manageSNE -rs FRAN.rs`). **DECIDE:**
export with the decoy schema in the same pass? It is still capped at 1% FDR (memory
`fran-decoys-ml-lane`), so it adds decoys but not hard negatives.

### 4.3 Negatives: a DIA-NN "training report" from saved .quant files
The corpus report step already re-reads `.quant` files (`--use-quant`), no raw access. DIA-NN 2.7.0
has `--report-decoys`, `--export-decoys`, `--export-quant` and `--export-windows` (found in the
binary 2026-11-12). If a report at a loose q-value with decoys can be regenerated from the `.quant`
alone, every finished DIA-NN search yields targets + decoys + pre-FDR candidates + the window scheme
for minutes of CPU, with no re-search — which sidesteps the Biognosys blocker on the Spectronaut
side. **Pilot job 25684183** (`scratch/training_pilot_20261112/diann_trainrep/`) works on copies of
the 3 `.quant` files and compares a baseline report against the training report.

**Result (2026-11-12, 26 s per report, no raw access, source `.quant` md5-unchanged):**
- baseline 132,373 rows; training report 150,481 = **144,185 targets + 6,296 decoys**.
- 11,812 targets with 0.01 < q ≤ 0.05; 144,185 − 11,812 = 132,373 exactly → strict superset of the
  production report.
- **Ceiling: q ≤ 0.05.** `--qvalue 0.5` is accepted, but the saved `.quant` files only carry candidates
  to the library's 5% cut, so this is "decoys + the 1–5% zone", not every candidate ever scored.
  Deeper negatives need the search itself re-run with a looser cut — not worth it corpus-wide.
- `--export-quant` adds per-fragment `Fr.0..11.{Id,Index,Quantity,Score}`, MS1 isotopes
  `Ms1.0..3(.Score)`, and **`Ms2.Scan`** (apex scan index) — the per-fragment scores serve
  interference masking (Carafe-style), and `Ms2.Scan` is the T3 pointer for free.
- `--export-windows` writes nothing in report-from-quant mode → the window scheme comes from §4.4.
- Size: 44.6 MB vs 18.6 MB for 3 runs (2.4×).

Next: add a step 6 to the chain (`--report-decoys --export-quant --qvalue 0.05`, out
`report.training.parquet`), backfill the 613 finished searches (~26 s each + queue), ingest into a
decoy-separated store (Lance lane or its own table — never `delimp_precursors`; keep the
decoy-leak guard).

### 4.4 Raw census + run acquisition facts (T0/T1)
One array job over every `raw_files` row (37,102) plus Radiant's `raw_catalog_combined.tsv`:
verified Hive path (resolving Windows `raw_path` the way `map_sn.py` does), size, a cheap content
fingerprint (sha256 of `analysis.tdf` + `tdf_bin` size for `.d`; sha256 of `.raw`), integrity verdict
(reuse `bruker_tdf.tdf_integrity`, the 2026-09-21 sweep, and the Thermo `InAcquisition` /
instrument-count test), and acquisition facts — NCE, resolution (load CORPUS_RESOLUTION.tsv), window
scheme (Bruker `DiaFrameMsMsWindows`, Thermo scan filters, or DIA-NN `--export-windows`).
All tdf opens `?mode=ro&immutable=1` (memory `bruker-tdf-wal-truncation`). Writes need new columns /
a `delimp_run_windows` table — **DDL, needs Brett's OK.**

### 4.5 Scan-lane pilot (T3/T3b)
Convert 1 Orbitrap + 1 timsTOF run to a scan-level Lance dataset; build the precursor → scan index;
write a loader that reproduces Radiant's ASF (`prepare_training_data_mobfilter.py`) for the same
precursors (parity test). Measure bytes vs raw. **DECIDE** from the numbers: full scan lane for all
runs, scan lane for a curated subset, or index-only against the raws.

### 4.6 Label hygiene (T1)
- `held_out` and `shareable` flags per run (Radiant session asked for both; client data decides
  whether weights can be published).
- **Split by raw run, never by dataset or search.** The same raw run appears in many lane rows by
  design: SN re-exports and re-searches (the 14 clashes in §4.2), SN vs the DIA-NN corpus re-search,
  and superset searches (KG-human-2). A dataset-level train/test split leaks. The split key is a
  run identity (raw md5 once §4.4 fills it; until then normalised run name + acquisition time),
  and every lane row must carry it.
  **Measured 2026-11-13** (`delimp_spectrum_lane_runs` after job 25706846, run key = lower-cased
  run name without extension): 14,540 distinct runs; **4,353 (30%) are in 2+ datasets**, up to 14
  copies; **130.3M of 489.3M lane rows (27%) repeat a run already stored**. Of the 157 datasets the
  2026-11-12 backfill added, 27 hold only runs already stored, 18 some, 112 none. These are
  distinct searches (other FASTA / library / settings), so their labels are kept; only the split
  must group by run.
  **Grouping by run is necessary but not enough (2026-11-13, split rule v3).** v1 hashed each run
  alone, which scattered the replicates of one sample across train and test, so test scores would
  include peptides and near-identical spectra the model trained on. `delimp_run_flags` now assigns
  the split per leakage GROUP (`group_key`, `build_run_flags.py`): runs are linked by CoreOmics
  submission, PROT_ number in the name, PXD accession, and by folder only when the folder holds a
  single submission (linking through the monthly `raw_data/<instrument>/<month>` dumps chained 23.5%
  of runs into one group). Standards (HeLa, NIST, blanks, K562, PRTC/iRT) are `std:*` groups and
  always train, because the same standards recur every month in different folders. Result: 1,079
  groups, the largest 2.6%; 90.5% train / 3.1% val / 6.4% test runs; test holds no named standards.
  Residual leakage a model must still handle: unnamed standards, and the same organism's peptides
  across projects. A peptide-disjoint evaluation is a training-time filter, not a run split.
- Canonical ProForma per precursor alongside the engine string.
- Multi-engine agreement per precursor (the `n_engines_confirming` column is a stub — memory
  `fran-engines-confirming-stub`).
- Write tissue/sample-type predictions into `delimp_sample_metadata` (0% filled) with provenance.
- Investigate the 2,420 `orbitrap` + `diaPASEF` runs.

### 4.7 Stop the gaps growing
Ingest drip: build the DIA-NN XIC lane and the training report per search. SN uploader: enqueue a
lane job (or export FRAN.rs alongside every upload).

## 5. Coordination
The Radiant-TIMS session runs the Cascadia retraining inventory
(`/quobyte/proteomics-grp/de-limp/cascadia/fran_inventory/`, read-only label scan + raw catalog
join). FRAN does not touch that tree; its `fran_run_inventory.tsv` is an input to §4.4, and §4.6's
flags answer its requests (raw path, acquisition mode/window scheme, engine per dataset, held-out
marker, shareable flag).

## 6. Results log
- 2026-11-13: split rule v1 -> v2 (group by submission / PROT / PXD / single-submission folder) ->
  v3 (standards always train), all before any training; v1 rows backed up in
  `scratch/fran_health/run_flags_v1_backup_20261113_132746.tsv`.
- 2026-11-12: survey; gap table; XIC dry run OK; SN 775 split; worklist bug fixed; DIA-NN flags found.
- 2026-11-12: DIA-NN training-report pilot 25684183 — works, see §4.3.
- 2026-11-12: `--worklist` test, 3 reports incl. the colliding `Ab_geli` (1,553,402 precursors /
  9,281,698 fragments → `Ab_geli__fbb6ba7f.lance`), scratch only.
- 2026-11-12: launched spectrum-lane backfill of the 165 — jobs 25684651 (31 reports >15 GB, 1
  worker, 300G) and 25684652 (134 reports, 4 workers, 160G), log
  `scratch/training_pilot_20261112/sn_lane_165_<jobid>.log`. Runs from a scratch copy of the
  branch's `backfill_fragments.py`; Hive's `fran_ingest/` is untouched. 25684652 (4-worker pool)
  died on start (`SemLock` FileNotFoundError in spawned workers, 0 written); resubmitted as array
  25684886 (8 tasks × 1 worker, `wl_small_<n>.tsv`).
- 2026-11-12: DIA-NN XIC lane planned: 747 searches (skipped 42 no output dir, 18 no report/xic,
  4 coverage mismatch — more xic files than runs), array 25684900 (16 tasks, %4), logs
  `scratch/training_pilot_20261112/xic_backfill/run_*.log`.
