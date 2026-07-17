# Spectronaut export for FRAN corpus ingest

To ingest a Spectronaut search into the FRAN corpus (via `scripts/spectronaut_to_corpus.py`
→ `scripts/corpus_ingest.py`), export a **tab-separated report** with the columns below.
Build them once as a saved **Report Scheme** (Report perspective → Schemes → new/edit →
add columns → save) and reuse it for every project. Validate any export with:

    python scripts/spectronaut_to_corpus.py "YourReport.tsv" --dry-run

which prints exactly which fields resolved + an AI-training-data availability summary.

## Columns

### Required — precursor identity + FDR
| Spectronaut column | maps to |
|---|---|
| `R.FileName` (or `R.Raw File Name`) | run / raw file |
| `PG.ProteinGroups` | protein_group |
| `PG.Genes` | gene |
| `PG.Qvalue` | pg_q_value |
| `PEP.StrippedSequence` | stripped_seq |
| `EG.ModifiedSequence` (or `EG.ModifiedPeptide`) | modified sequence |
| `FG.Charge` | charge |
| `EG.Qvalue` | q_value (1% filter) |
| `FG.PrecMz` (or `EG.PrecursorMz`) | precursor_mz |
| `FG.Quantity` (or `FG.MS2Quantity`) | intensity |

### AI-training — retention time & ion mobility
| `EG.ApexRT` (or `EG.MeanApexRT`) | rt |
| `EG.iRT` (or `EG.RTEmpirical`) | irt (cross-run RT) |
| `EG.IonMobility` (or `EG.ApexIonMobility`) | im (1/K0) |
| `EG.CCS` | ccs (timsTOF) |

### AI-training — MS2 fragment spectrum (REQUIRED for spectrum-prediction training)
| `F.FrgMz` | fragment m/z |
| `F.FrgType` | b/y |
| `F.FrgNum` | ion number |
| `F.FrgZ` | fragment charge |
| `F.FrgLossType` | neutral loss |
| `F.PeakArea` (or `F.NormalizedPeakArea`, `F.MeasuredRelativeIntensity`) | fragment intensity |

> Including `F.*` makes the report **per-fragment** (one row per fragment) — much larger,
> but it's the only way to capture observed spectra. A precursor-only report has no spectra.

### Optional
`EG.PEP` (posterior error prob) · `FG.NormalizedMS2PeakArea` (normalized intensity)

## Not supported: reading `.sne` directly
The `.sne` is Biognosys' proprietary, undocumented project archive — not a stable parse
target. Use the report export above. For automation without the GUI, Spectronaut's
pipeline / command-line mode can run a saved export scheme per project.

## Collision energy / instrument
Spectronaut reports usually do NOT carry per-precursor collision energy or instrument
model. For CE/instrument-conditioned training, pull those from the run metadata
(the .d analysis.tdf / HyStar / .raw header) at ingest — same as the DIA-NN path.
