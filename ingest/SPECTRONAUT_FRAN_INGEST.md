# How FRAN ingests Spectronaut searches — the `.sne` → report → corpus pipeline

**Written 2026-07-17 from the live DB + the canonical ingest scripts (DE-LIMP repo), so we don't forget it.**
Everything below is verified against the actual `delimp_*` tables and the scripts that wrote them — not memory.

> **TL;DR for DIA-CLIP:** the observed MS2 fragments we need *were* exported by Spectronaut into every
> `_Report_FRAN (Normal).parquet`, but `corpus_ingest.py` **collapses fragment rows down to one row per
> precursor and drops the fragment payload** before writing `delimp_precursors` (the fragment "home" —
> the XIC/`delimp_precursor_xic` lane — was a deferred schema-v2 lane that never ran at scale). The
> fragments are therefore **not in the DB, but they are on Flinders** in the archived report parquets and
> are recoverable by re-parsing them — no prediction, no Koina, no Spectronaut re-run. See §6.

---

## 1. The pipeline at a glance

```
 Spectronaut .sne experiment            (proprietary Biognosys project archive; the search result)
        │
        │  spectronaut manageSNE -sne <file.sne> -n <name> -o <out> -rs FRAN.rs
        │  (+ --setXICExportDirectory <out>/xics  → per-raw-file All-XIC SQLite dbs)
        ▼
 <name>_Report_FRAN (Normal).parquet    ← fragment-level report (one row PER FRAGMENT)
        │                                  contains F_Frg* + FG_XICDBID + RT/IM/precMz  (see §4)
        │  zipped per experiment, copied to Flinders (§5), then:
        ▼
 spectronaut_to_corpus.py               ← fuzzy-maps Spectronaut columns → FRAN fields, streams chunks
        │
        ▼
 corpus_ingest.py --engine spectronaut  ← COLLAPSES fragment rows → precursor rows,  ⚠ drops fragments
        │                                  bulk-COPY into the DB
        ▼
 delimp_searches / raw_files / search_raw_files / delimp_proteins / delimp_precursors   (precursor-level)
        │
        └── provenance.py → delimp_search_provenance   ← THE COORDINATION TABLE (§3)
        └── sne_xic_ingest.py (optional) → delimp_precursor_xic  ← only for the GUI-exported XIC SQLite dbs
```

The bulk of FRAN's corpus (~96% of runs) came through this Spectronaut path. DIA-NN searches take a parallel
path (`report.parquet` + `report-lib.parquet` from each `out/` dir) through the same `corpus_ingest.py`.

## 2. The exact report-generation command

The report generator is **Spectronaut's `manageSNE` CLI action** run against the **`FRAN.rs` report schema**.
This is the whole command — identical across every run (Spectronaut 21 manual §3.11, Table 12):

```bash
spectronaut manageSNE -sne <file.sne> -n <name> -o <out_dir> -rs FRAN.rs
```

- `-sne <file.sne>` — the Spectronaut experiment (the search result).
- `-n <name>` — experiment/report name.
- `-o <out_dir>` — output dir; the report lands as `<out_dir>/<name>_Report_FRAN (Normal).parquet`
  (`(Normal)` = the flat/normal report layout, not a pivot).
- `-rs FRAN.rs` — **the custom report schema** (§4). This is what makes it a "FRAN report" instead of a
  stock BGS report.
- On Windows the CLI is invoked as `dotnet C:\...\Spectronaut.dll manageSNE ...` (set `SPECTRONAUT_DLL` or
  pass `--spectronaut "dotnet C:\Path\To\Spectronaut.dll"`).
- For the GUI-only chromatogram viewer, `sne_export.py` also runs `--setXICExportDirectory <out>/xics`
  first, so Spectronaut dumps its per-raw **All-XIC SQLite** dbs. **These `.xic.db`/`.sqlite` files exist
  only for a minority of exports** (they require the `.sne` to have been saved WITH ion traces / FULL) —
  they are *not* the source of the bulk corpus. The bulk corpus is the report parquet above.

The `sne_*.py` scripts (below) are just batch wrappers that drive this one command over many `.sne` files.

## 3. The coordination table — `delimp_search_provenance`

**This is the "whole table we set up to coordinate the ingesting."** Written by `scripts/provenance.py`.
It is a **private** table (not in the FRAN browser app's allowlist — internal LIMS/customer tracking only),
one row per search, and it records where each `.sne` came from and where its exported report went:

| column | meaning |
|---|---|
| `search_id` | FK to `delimp_searches` |
| `real_search_name` | the human name (e.g. `SpN_Montell-RauthN-4retinalOrganoidHoSa_mai26`) |
| `output_dir` | the source `.sne` path (idempotency key) — e.g. `B:\Automatic_SNE_storage\...\<name>.sne` |
| `report_path` | the exported report — e.g. `C:\fran_sne_export\<name>\<ts>_<name>\<name>_Report_FRAN (Normal).parquet` |
| `raw_files_json` | every raw `.d` in the search: `[{name, path}, …]` |
| `n_raw_files`, `scope`, `campus`, `client`, `pi`, `project` | provenance / LIMS fields |
| `coreomics_submission_id`, `linkage_status` | LIMS linkage (`unlinked`/`matched`/…) |

Current state: **1,933 rows.** `linkage_status` is mostly `unlinked` (931) or `service-dir`-matched.
Two sibling tables complete the ingest bookkeeping:

- **`delimp_submission_service_dir`** (1,862 rows) — maps a LIMS submission → its service folder on the
  `R:\Data\lab\service\...` share (`in_fran` flag, `match_confidence`, `clue`). This is the "what still
  needs ingesting / where does this run live" ledger.
- **`delimp_search_sources`** (703 rows) — one row per *source file* per search, with `file_role`
  (`report`, `report_lib`, `speclib`, `xic`, `xic_sqlite`, …), `host`, `path`, `bytes`, `extracted`.
  This is where the DIA-NN `report.parquet`/`report-lib.parquet` and the Spectronaut XIC SQLite dbs are
  registered.

## 4. The `FRAN.rs` report schema (what columns get exported)

`FRAN.rs` lives at **`R:\Data\FRAN_SNE_export\FRAN.rs`** (a binary Spectronaut report-scheme file; build/edit
it in the Spectronaut GUI: Report perspective → Schemes). FRAN's ingester reads only these **24 columns** —
the schema should be trimmed to them (a full Spectronaut report is ~130+ columns and, because it's
fragment-level, that bloat repeats on *every fragment row*):

```
R.FileName  PG.ProteinGroups  PG.Genes  PG.Qvalue  PEP.StrippedSequence
EG.ModifiedSequence  FG.Charge  EG.Qvalue  EG.PEP  FG.PrecMz  EG.ApexRT
EG.iRTEmpirical  EG.IonMobility  FG.Quantity
FG.XICDBID                                        # links each precursor to its XIC db
F.FrgType  F.FrgNum  F.Charge  F.FrgMz  F.FrgLossType  F.PeakArea
F.NormalizedPeakArea  F.Rank  F.ExcludedFromQuantification
```

Column groups and where they map (via `spectronaut_to_corpus.py`, fuzzy-matched so names can drift across
Spectronaut versions; the parquet export renames the dotted prefix to an underscore, e.g. `F.FrgMz` →
`F_FrgMz`):

- **Precursor identity + FDR (required):** `R.FileName`→run, `PEP.StrippedSequence`→`stripped_seq`,
  `EG.ModifiedSequence`→modified seq, `FG.Charge`→`charge`, `EG.Qvalue`→`q_value` (1% filter at ingest),
  `PG.*`→protein group/gene/`pg_q_value`, `FG.PrecMz`→`precursor_mz`, `FG.Quantity`→`intensity`.
- **AI-training RT/IM:** `EG.ApexRT`→`rt`, `EG.iRTEmpirical`→`irt`, `EG.IonMobility`→`im` (1/K0),
  `EG.CCS`→`ccs`.
- **AI-training MS2 fragment spectrum (this is the part we care about):** `F.FrgMz`, `F.FrgType`,
  `F.FrgNum`, `F.FrgZ`/`F.Charge`, `F.FrgLossType`, `F.PeakArea`/`F.NormalizedPeakArea`. Including the
  `F.*` columns is what makes the report *per-fragment* (one row per observed fragment) — the only way to
  capture the real acquired spectrum.

> **CE / instrument are NOT in the report** — Spectronaut doesn't emit per-precursor collision energy or
> instrument model. Pull those from the `.d` metadata (`analysis.tdf` / HyStar) at ingest, same as DIA-NN.

## 5. Where the archived reports physically live (verified on Hive)

`sne_export.py` copies each zipped experiment to Flinders. The exported reports are reachable **now**:

- **`/nfs/lssc0/flinders/proteomics/Data/FRAN_reports/<name>/<ts>_<name>/<name>_Report_FRAN (Normal).parquet`**
  (+ a `.params` sidecar) — the archived fragment-level reports.
- **`/nfs/lssc0/flinders/proteomics/Data/FRAN_SNE_export/`** — the SNE-export archive root.
- On the export workstation, reports also sit under `C:\fran_sne_export\` (the `report_path` in
  `delimp_search_provenance`).

Spot-checked `…/FruehProtmx-Dia_Report_FRAN (Normal).parquet`: **131 columns, 39,391 fragment rows**, and it
contains `FG_XICDBID, F_FrgMz, F_FrgType, F_FrgNum, F_FrgIon, F_Charge, F_FrgLossType, F_PeakArea,
F_NormalizedPeakArea` plus `EG_ApexRT, EG_IonMobility, FG_PrecMz`. (This one wasn't trimmed to the lean 24 —
schemas varied over time — but the fragment payload is all present.)

## 6. Why fragments aren't in `delimp_precursors` — and how to get them back

The fragments were **exported** but never **stored**:

1. `spectronaut_to_corpus.py` *does* parse each fragment (it builds a `rec["fragment"] = {mz, type, num,
   charge, loss, area}` from the `F.*` columns).
2. `corpus_ingest.py` then **collapses the fragment rows to one row per precursor** (log line:
   `collapsed N fragment-rows -> M precursors (fragment-level report)`) and writes only precursor-level
   fields to `delimp_precursors`. Its own comment says *"delimp_precursors is precursor-level (fragments
   live in the XIC lane)"* and *"iRT/iIM + observed fragment spectra need the schema-v2 columns (not in
   v1)."*
3. That schema-v2 fragment lane (`delimp_precursor_xic.fragments`) was the deferred `--xic` pipeline and
   **was never run at corpus scale** — so the observed fragments landed nowhere in the DB. (The
   `delimp_precursors.peak_mz`/`peak_intensity`/… columns are an even older dead draft — no writer — don't
   use them.)

**Recovery path (recommended, no re-search / no prediction):** re-parse the archived
`…_Report_FRAN (Normal).parquet` files under `/nfs/lssc0/flinders/proteomics/Data/FRAN_reports/` (and
`FRAN_SNE_export/`). Group by `(R_FileName, PEP_StrippedSequence, EG_ModifiedSequence, FG_Charge)` and read
`F_FrgMz` / `F_FrgType` / `F_FrgNum` / `F_Charge` / `F_PeakArea` per precursor. That gives the **real
observed fragments the search engine used** — exactly the DIA-CLIP requirement — keyed by the same RT/IM the
DB already stores. Only `.sne` files whose report wasn't archived need a re-export via the §2 command.

This directly replaces the sequence-guessed `top6(seq)` fragments in `build_xic_shard.py`.

## 7. Scripts (all in the DE-LIMP repo, `~/Documents/claude/scripts/` unless noted)

| script | role |
|---|---|
| `sne_export.py` | **the runner.** Finds every `.sne` under given roots, runs the §2 `manageSNE` export, zips each experiment, copies zips to Flinders, and (with `--ingest`) calls `corpus_ingest`. `--dry-run` lists SNEs + exact commands; `--columns` prints the schema columns. |
| `sne_one.py` | single-`.sne` runner (was making the dog report when the DB filled). |
| `sne_backlog.py`, `sne_macaque.py`, `sne_phase2.py` | batch/backlog wave runners (temp/scratch on the workstation). |
| `spectronaut_to_corpus.py` | Spectronaut→FRAN **column adapter** — fuzzy-resolves `R./PG./PEP./EG./FG./F.` columns (parquet or TSV), streams 200k-row chunks, emits per-precursor records (with parsed fragments). `--dry-run` prints resolved fields + an AI-training availability summary. |
| `corpus_ingest.py` | **the ingester** — `--engine spectronaut|diann`, collapses fragment rows → precursors, delete-then-insert by `output_dir`, bulk-`COPY` into `delimp_*`. |
| `sne_xic_ingest.py` | ingests the GUI-exported **All-XIC SQLite** dbs (`FGID == FG.XICDBID`, base64 float32 traces) → `delimp_precursor_xic` for the peptide-page chromatogram viewer. Needs BOTH the report (for peptide identity + `FG.XICDBID`) and the `.xic.db` dir. Minority of runs only. |
| `provenance.py` | writes/back-fills **`delimp_search_provenance`** (§3). |
| `write_submission_service_dir.py` | writes `delimp_submission_service_dir` (§3). |
| `db_to_spectronaut_report.py` | reverse: reconstruct a Spectronaut-style report FROM the DB for a `search_id`. |
| docs: `scripts/SNE_EXPORT_README.md`, `docs/SPECTRONAUT_EXPORT_COLUMNS.md`, `docs/COWORK_SNE_EXPORT_RUNBOOK.md` | the original runbooks this file consolidates. |

## 8. Reproduce / re-ingest a search

```bash
# Dry run — list every .sne it finds and the exact manageSNE command, run nothing:
python sne_export.py /path/to/projects --schema FRAN.rs --dry-run

# Full run — export each .sne, zip, copy to Flinders, ingest into FRAN:
python sne_export.py /path/to/projects \
    --schema FRAN.rs --out ./sne_reports \
    --flinders /nfs/lssc0/flinders/proteomics/Data/FRAN_SNE_export --ingest

# Ingest an already-exported report by hand:
python corpus_ingest.py "<name>_Report_FRAN (Normal).parquet" --engine spectronaut --name <name> --bulk-copy
# validate column resolution first:
python spectronaut_to_corpus.py "<name>_Report_FRAN (Normal).parquet" --dry-run
```

---

### Provenance of this doc
Verified 2026-07-17 against: `delimp_search_provenance` (1,933 rows), `delimp_submission_service_dir`
(1,862), `delimp_search_sources` (703) on PG-Farm `delimp`; the archived report
`…/FRAN_reports/…/FruehProtmx-Dia_Report_FRAN (Normal).parquet` (131 cols / 39,391 frag rows, fragment
columns confirmed present); and the scripts `sne_export.py`, `spectronaut_to_corpus.py`, `corpus_ingest.py`,
`sne_xic_ingest.py`, `provenance.py` in the DE-LIMP repo. **The ingest/ETL code lives in the DE-LIMP repo
(`~/Documents/claude`), not the FRAN browser-app repo.**
