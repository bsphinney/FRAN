# FRAN SNE Export — find Spectronaut `.sne` experiments and export FRAN-ready reports

This kit finds every Spectronaut experiment (`.sne`) under the folders you point it at,
exports a report from each using the Spectronaut command line, zips each experiment into a
single archive (so you don't get a sea of files), copies the zips to Flinders, and
(optionally) loads them straight into the FRAN corpus.

## Files in this folder
- `sne_export.py` — the program you run.
- `corpus_ingest.py` — FRAN report ingester (used by `--ingest`).
- `spectronaut_to_corpus.py` — Spectronaut→FRAN column adapter.
- `sne_xic_ingest.py` — ingests the XIC SQLite dbs so the peptide-page chromatogram viewer works.
- `xic_ingest.py`, `refresh_leaderboards.py` — helpers imported by `sne_xic_ingest.py`.

Keep all of them together.

## Prerequisites
1. **Spectronaut installed & licensed** on this workstation. The program calls the
   Spectronaut CLI; it does not bundle Spectronaut.
   - Linux: the `spectronaut` command should be on your PATH.
   - Windows: set `SPECTRONAUT_DLL` to the full path of `Spectronaut.dll`, or pass
     `--spectronaut "dotnet C:\Path\To\Spectronaut.dll"`.
2. **A custom report schema named `FRAN_ingest`** (or a path to a `.rs` file). Make it in
   the Spectronaut GUI with the columns listed below. Run `python sne_export.py --columns`
   to reprint them anytime.
3. **Python 3** with `pandas` (only needed for `--ingest`).

## The report-schema columns FRAN needs
Add these to your custom Spectronaut report schema (export as **TSV**):

**Required:** `R.FileName`, `PEP.StrippedSequence`, `FG.Charge`

**Core precursor record (strongly recommended):**
`PG.ProteinGroups`, `PG.Genes`, `PG.Qvalue`, `EG.ModifiedSequence`, `EG.Qvalue`, `EG.PEP`,
`FG.PrecMz`, `EG.ApexRT`, `EG.iRT`, `EG.IonMobility`, `EG.CCS`, `FG.Quantity`,
`FG.NormalizedMS2PeakArea`

**MS2 fragment spectrum (include these to get the spectra / AI-training payload — makes a
larger, fragment-level report):**
`F.FrgMz`, `F.FrgType`, `F.FrgNum`, `F.FrgZ`, `F.FrgLossType`, `F.PeakArea`

**Optional metadata:** `R.Instrument`, `FG.CollisionEnergy`

> Tip: include the `F.*` columns. They capture the observed MS2 spectrum FRAN displays and
> trains on. A precursor-only report (no `F.*`) still works if you only want IDs/quant.
> Don't pre-filter q-value in the schema — ingest filters at q≤0.01.

### Keep your schema lean (the report is huge otherwise)
FRAN reads only **these 24 columns** — delete everything else from the schema. A full
Spectronaut report has ~150 columns; on a fragment-level report that bloat repeats on
*every fragment row*, so trimming to these makes the file far smaller:

```
R.FileName  PG.ProteinGroups  PG.Genes  PG.Qvalue  PEP.StrippedSequence
EG.ModifiedSequence  FG.Charge  EG.Qvalue  EG.PEP  FG.PrecMz  EG.ApexRT
EG.iRTEmpirical  EG.IonMobility  FG.Quantity
FG.XICDBID                                  # links each precursor to its XIC
F.FrgType  F.FrgNum  F.Charge  F.FrgMz  F.FrgLossType  F.PeakArea
F.NormalizedPeakArea  F.Rank  F.ExcludedFromQuantification
```
The biggest savings come from dropping the per-fragment diagnostics you don't need —
`F.InterferenceScore`, `F.PossibleInterference`, `F.Log10SignalToNoise`, `F.Noise`,
`F.*MassAccuracy*`, `F.*Tolerance*`, `F.MeasuredMz`, `F.TheoreticalMz`,
`F.IsotopicPatternTheoretical`, `F.PeakHeight`, `F.NormalizedPeakHeight`, `F.CalibratedMz`,
plus run-wise/global score columns (`*.Cscore`, `*.PEP (Run-Wise)`, `EG.GlobalCScore`, …).
If you want to shrink even further, `F.NormalizedPeakArea` is optional.

## Ion-mobility / XIC chromatograms
The peptide-page **dual-pane XIC viewer** needs Spectronaut's separate **All-XICs SQLite**
export (the report carries the spectrum + the `FG.XICDBID` link, but not the chromatogram
traces). `sne_export.py` turns this on automatically: it runs
`--setXICExportDirectory <out>/xics` before `manageSNE`, so the per-raw-file `.sqlite` dbs
land in the experiment folder and get zipped with the report, and `--ingest` then loads them
via `sne_xic_ingest.py`.

> **Requirement:** the `.sne` must have been saved **WITH ion traces (FULL)** for XICs to
> exist to dump. If the `xics/` folder comes out empty, the SNE was saved without traces —
> re-extract XICs in Spectronaut (manual §3.4.1.12) or re-save the SNE with ion traces.
> Pass `--no-xics` to skip XIC export entirely.

## How to run

Sanity check first — lists every `.sne` and the exact command, runs nothing:
```
python sne_export.py /path/to/projects --schema FRAN_ingest --dry-run
```

Full run — export, zip each experiment, copy zips to Flinders, load into FRAN:
```
python sne_export.py /path/to/projects \
    --schema FRAN_ingest \
    --out ./sne_reports \
    --flinders /path/to/Flinders/sne_archive \
    --ingest
```

You end up with one `<experiment>.zip` per experiment in `./sne_reports` **and** on Flinders,
and (with `--ingest`) the data in the FRAN corpus.

## Options
| flag | meaning |
|------|---------|
| `roots...` | one or more folders (or `.sne` files) to search |
| `--schema` | report schema: a `.rs` file path, or a schema name already in Spectronaut |
| `--out DIR` | where to put the per-experiment zips (default `./sne_reports`) |
| `--flinders DIR` | also copy each zip here, right after it's made |
| `--ingest` | load each report into FRAN (`corpus_ingest --engine spectronaut`) before zipping |
| `--keep-loose` | keep the unzipped report folders too (default: zip only) |
| `--spectronaut` | how to invoke Spectronaut, e.g. `"dotnet C:\...\Spectronaut.dll"` |
| `--parquet` | write parquet instead of tsv (needs adapter parquet support — leave off) |
| `--dry-run` | list SNEs + commands, run nothing |
| `--columns` | print the schema columns above and exit |

## What it runs under the hood
For each `.sne`, the verified Spectronaut 21 command (manual §3.11, Table 12 — manageSNE):
```
spectronaut manageSNE -sne <file.sne> -n <name> -o <out_dir> -rs <schema>
```
Then it zips `<out_dir>` to `<name>.zip`, copies it to `--flinders`, and removes the loose
folder. The Flinders copy is incremental — if a run is interrupted, finished experiments are
already archived. If the mount drops it warns and keeps going.

## Ingesting later (without `--ingest`)
```
unzip -p <name>.zip '*.tsv' > r.tsv
python corpus_ingest.py r.tsv --engine spectronaut --name <name>
```
