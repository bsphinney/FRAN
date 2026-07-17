# Cowork runbook — find Spectronaut `.sne` files and produce FRAN-ready reports

**Audience:** a fresh agent/operator on a NEW workstation with NO prior context. Follow
top to bottom. Goal: locate Spectronaut analyses (`.sne`) and produce the tab-separated
report FRAN needs, then hand those reports to the FRAN uploader.

---

## 0. Key facts you must know first
- **A `.sne` file CANNOT be parsed directly.** It's a proprietary .NET binary blob — the
  numbers are inside but with no field names. (Verified: `docs/SNE_FORMAT_INVESTIGATION.md`.)
  So you **cannot** read a `.sne` with Python/pandas. You MUST use Spectronaut itself to
  produce a report.
- A `.sne` is a *saved Spectronaut experiment* (a completed analysis). The report you need
  is a **TSV export** of that experiment's precursor+fragment table.
- Spectronaut has a **command-line mode** (`Spectronaut.exe` on Windows, `spectronaut` on
  Linux). Verified flags below. Caveat: the documented CLI **runs an analysis from raw
  files** and writes the report; a "load .sne → export report only" command is
  version-specific — **check `Spectronaut.exe --help` and the installed version's manual**
  before assuming it exists.

## 1. Prereqs (do once on the new workstation)
1. Install Spectronaut (same major version that produced the `.sne` files, ideally).
2. **Create the FRAN report scheme** (the columns FRAN ingests) in the GUI: Report
   perspective → Schemes → New → add the columns in §4 below → save as scheme
   **`FRAN_export`**. (Schemes live in Spectronaut's settings dir; reused by name in CLI.)
3. Have the PG Farm token at `~/.pgfarm_token` and the FRAN tools
   (`corpus_uploader.py`, `corpus_ingest.py`, `spectronaut_to_corpus.py`) on this machine.
4. `pip install psycopg2-binary pandas pyarrow duckdb`.

## 2. Find the `.sne` files
```bash
# scan the data drives for Spectronaut experiments
find /path/to/data -type f -iname '*.sne' 2>/dev/null
```
Record each `.sne` path + its folder (the folder usually also holds the raw files /
`.htrms` and the spectral library used).

## 3. Produce a FRAN report for each `.sne`
Pick the first option that works on this install:

**Option A — CLI report export from the experiment (preferred IF supported).**
Run `Spectronaut.exe --help` (or check the version's manual, "Command line mode" /
"Reporting"). Newer versions (18+) expose reporting via CLI. If there is a report/export
command that takes an existing experiment + a report scheme, use it with scheme
`FRAN_export` and output a `.tsv` (Parquet is also offered in pipeline mode on recent
versions). This is the cleanest automation.

**Option B — re-run the analysis via CLI (works when the raw files + library are present).**
The documented CLI re-runs the search and writes the report. Build an `arguments.txt`:
```
-s "FRAN_export_or_settings_schema"        # settings schema (.prop) or its name
-n "ExperimentName"                          # from the .sne / folder name
-o "/path/to/output_dir"                     # reports land in a dated subfolder here
-a "/path/to/spectral_library.kit_or_bgms"   # the library that was used
-d "/path/to/folder_with_raw_or_.d_files"    # all runs in a dir (Bruker .d supported)
```
then:
```bash
"C:\Program Files\Biognosys\Spectronaut\bin\Spectronaut.exe" -command "C:\path\arguments.txt"
#   Linux:  spectronaut -command /path/arguments.txt
```
Output includes the report (e.g. `Report_FRAN_export.tsv` / `.xls`, plus `Candidates.tsv`).
NOTE: this re-does the search — only do it if Option A is unavailable.

**Option C — GUI export (always works, manual).** Open the `.sne` in Spectronaut → Report →
choose scheme `FRAN_export` → Export → tab-separated `.tsv`.

Validate any produced report before ingest:
```bash
python spectronaut_to_corpus.py "Report_FRAN_export.tsv" --dry-run
# prints which fields resolved + whether the fragment spectra / iRT / IM are present
```

## 4. The FRAN report scheme columns (build this scheme in §1.2)
Required: `R.FileName` · `PG.ProteinGroups` · `PG.Genes` · `PG.Qvalue` ·
`PEP.StrippedSequence` · `EG.ModifiedSequence` · `FG.Charge` · `EG.Qvalue` ·
`FG.PrecMz` · `FG.Quantity`
AI-training (RT/IM): `EG.ApexRT` · `EG.iRT` · `EG.IonMobility` · `EG.CCS`
AI-training (MS2 SPECTRUM — include these or there are no spectra):
`F.FrgMz` · `F.FrgType` · `F.FrgNum` · `F.FrgZ` · `F.FrgLossType` · `F.PeakArea`
(Full rationale: `docs/SPECTRONAUT_EXPORT_COLUMNS.md`.)

## 5. Ingest into FRAN (PG Farm — no HIVE needed)
```bash
python corpus_ingest.py "Report_FRAN_export.tsv" --engine spectronaut \
       --organism-name "Canis lupus familiaris" --taxon 9615
# or batch a whole tree of produced reports:
python corpus_uploader.py --scan /path/with/reports --upload
```
Run `--dry-run` first; then one report; then the rest.

## Sources
- Spectronaut command-line tutorial: https://files.biognosys.ch/Tutorials/Command_Line_Use_in_Spectronaut.pdf
- Spectronaut 20 manual (command-line / reporting sections): https://biognosys.com/content/uploads/2025/06/Spectronaut-20-Manual.pdf
- `.sne` is not directly parseable: `docs/SNE_FORMAT_INVESTIGATION.md`
