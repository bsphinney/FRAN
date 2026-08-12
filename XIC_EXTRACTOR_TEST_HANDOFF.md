# Handoff: validating the XIC extractor against DIA-NN on the Siegel timsTOF set

**From:** FRAN-side session, 2026-08-12
**For:** whoever is working on the extractor / Retriever side
**Goal:** compare `ingest/xic_extractor.py` v1.4.0's chromatograms against a reference for the same
acquisitions. Everything below was verified on Hive today; nothing here needs rediscovering.

---

## The data is ready and it is the right kind

`/quobyte/proteomics-grp/brett/siegel_glp1_2026-08-12/par/timstof/`

12 diaPASEF runs, human blood (Siegel GLP-1 / Ozempic study), searched with **DIA-NN 2.3.0**.

| | |
|---|---|
| `.d` directories | **exist and are readable** at the paths in `file_list.txt` (under `/nfs/lssc0/.../Siegel-bloodOzempic_iul26/d-SiegelBlood-Str-Cer/`) |
| `.quant` files | present in **three** dirs: `quant_step2/`, `quant_step2_orig/`, `quant_step4/` |
| `--xic` in the original run | **not used** — no chromatograms were written |
| library | `empirical.parquet` (in the same dir) |

This matters because the extractor is **diaPASEF-only** (see below), so timsTOF is the only data it
can be tested on at all.

## Getting the reference XICs is cheap

The original command is in `report.log.txt`. It already used `--use-quant --quant-ori-names` with
`--temp .../quant_step4`, so re-running with `--xic` added should reuse the existing `.quant` files
rather than re-extracting from the `.d`:

```
/quobyte/proteomics-grp/apptainers/diann2.3.0.sif   # apptainer image, DIA-NN 2.3.0
  --f <each of the 12 .d>                            # verbatim from report.log.txt
  --fasta /quobyte/proteomics-grp/MRS/UP000005640_9606_plus_universal_contam.fasta
  --lib   .../par/timstof/empirical.parquet
  --use-quant --quant-ori-names
  --temp  .../par/timstof/quant_step4
  --out   <NEW output dir — do not overwrite report.parquet>
  --threads 32 --qvalue 0.01 --mass-acc 15 --mass-acc-ms1 15 --cont-quant-exclude Cont_
  --xic <window>                                     # the addition
```

Write to a **new** `--out`; the existing `report.parquet` is the current result for this study.

## What the extractor needs on our side

`xic_extractor.py` does **not** read a `.d` directly. It reads a directory of memory-mapped `.npy`
arrays:

```
rt_values, mobility_values, meta, ms1_{mz,int,scan,cycle_idx}, ev_{mz,int,scan}, cycle_idx, iso_{lo,hi}
```

built by **`build_xic_shard.py`** — imported as `B` in `build_xic_trace_lance.py` and described there
as a Hive-only module. `build_xic_trace_lance.py` takes `VRUN` / `VLANCE` / `VCACHE` env vars, so the
cache path is `VCACHE`. That cache does not yet exist for any Siegel run; building one is the first
step on our side.

## Three things to know before trusting a comparison

1. **KNOWN OPEN DEFECT — MS2 timestamps run ~0.3 s early.** Documented at `xic_extractor.py:60`.
   Any apex-RT comparison will show a systematic offset of about a quarter of a cycle that is ours,
   not DIA-NN's. Either fix it first or subtract it knowingly; do not tune anything else to absorb it.

2. **Two extraction paths disagree about what a trace is.** `xic_extractor.py` is v1.4.0 producing an
   `[11,32]` layout at `IM_TOL=0.030`; `XIC_TRACE_LANE_WRITER_VERSION` is still `0.2.0`, described in
   `ingest/versions.py` as *"pilot; still the [9,32] layout, pre-corpus-scale"*. Decide which one the
   comparison is testing before running it.

3. **The extractor prefers observed fragments over predicted ones**, reading the traced ion set from
   FRAN's spectrum Lance lane (98.7% match to Spectronaut's own set). For a run **not** in the lane it
   falls back to `top6_by_mz()`, which reproduces only 26.3% of Spectronaut's traced ions. The Siegel
   runs are new, so check whether they are in `delimp_spectrum_lane` first — otherwise the comparison
   measures the fallback, not the extractor.

## Existing validation, for reference

v1.4.0 against Spectronaut 21's own "All XIC" export, same acquisition
(`11May2026_DIA_60spd_VER_185_S2-B5_1_21766`, diaPASEF, dog + yeast entrapment):

| channel | n | median r | r>0.8 | apex \|d\| | area ours/SN |
|---|---|---|---|---|---|
| MS2 fragments | 99,500 | 0.8791 | 72.1% | 0.39 s | 1.028x |
| MS2 loss ions | 5,576 | 0.8608 | 65.0% | 0.39 s | 1.047x |
| mono-isotopic | 18,015 | 0.9861 | 94.3% | 0.37 s | 0.963x |

A DIA-NN comparison is a genuinely independent second opinion — different engine, different
extraction code — so disagreement is informative rather than circular.

## Not applicable: Thermo

The extractor cannot read Thermo `.raw` at all. Its docstring says "from a diaPASEF run", and the
ion-mobility filter is load-bearing in **both** the MS2 and MS1 selection paths
(`abs(mob - im) <= IM_TOL`). Orbitrap data has no mobility axis, so porting means replacing the
selectivity criterion, not swapping a file reader. Corpus-wide that is 12,750 timsTOF vs 7,124
Orbitrap acquisitions — the extractor covers roughly 64% of runs and none of the Thermo half.

(The Hagerman brain set was considered for this test and ruled out: Orbitrap Fusion Lumos, no XICs in
the folder, no `.quant` files, `--xic` never in its DIA-NN log.)

---

# ADDENDUM — how to read Thermo on Hive (solved 2026-08-12)

**alpharaw cannot do it, and no amount of environment fixing will help.** alpharaw 0.6.0 is installed
in `envs/alphadia2` and its Python<->.NET bridge works once you `module load dotnet-core-sdk/8.0.4`
and set `PYTHONNET_RUNTIME=coreclr` (do NOT set `PYTHONNET_CORECLR_RUNTIME_CONFIG` -- it wants a
`.runtimeconfig.json` FILE, and giving it a directory produces a misleading
"self-contained components" error). With the bridge working, every Thermo file still fails
identically:

```
System.MissingMethodException
Method not found: 'Void System.Threading.Mutex..ctor(Boolean, System.String, Boolean ByRef,
                   System.Security.AccessControl.MutexSecurity)'
  at ThermoFisher.CommonCore.RawFileReader.Utilities.CreateNamedMutexAndWait
```

alpharaw bundles Thermo's **.NET Framework (Windows)** RawFileReader; `MutexSecurity` is a Windows
ACL type absent from .NET Core on Linux, and it is called during file-open. Different assemblies
would be needed, not different settings.

**ThermoRawFileParser works, and its Parquet mode is the right input.** Already on disk at
`/quobyte/proteomics-grp/tools/ThermoRawFileParser/`, already used by FRAN for instrument metadata on
4,197 raws. It is a self-contained .NET app with a Linux-compatible reader, so it avoids the bridge
entirely. Needs the same dotnet module + `DOTNET_ROOT`.

```
ThermoRawFileParser -i <file.raw> -f 3 -o <dir>      # -f 3 = Parquet (writes *.mzparquet)
```

Measured on `k562_200ng_30min_DIA_mcp2020_20210405174752.raw` (0.76 GB, 48,771 scans):
**44 s, 0 errors, 100 MB output, 11,299,926 rows.**

The schema is peak-level long-form and carries every field the cache builder needs:

| column | use |
|---|---|
| `scan`, `level`, `rt` | scan index, MS level, retention time |
| `mz`, `intensity` | the peaks — one row each |
| `isolation_lower`, `isolation_upper` | the `e_lo <= precursor_mz <= e_hi` window filter |
| `precursor_scan`, `precursor_mz`, `precursor_charge` | MS2 -> MS1 linkage |
| `ion_mobility` | present as a column, NULL on Orbitrap |

**That last row is the useful one.** `ion_mobility` existing but NULL means one cache builder can
serve both vendors: keep the mobility filter conditional on the column being non-null, rather than
forking the extractor into a Bruker path and a Thermo path. The Thermo arm will be less selective
because the axis genuinely is not there — that is a real loss of discrimination, not a bug, and it is
why the diaPASEF validation numbers cannot be carried over.

**Cost at scale:** ~44 s and ~100 MB per raw. For the 7,124 Orbitrap acquisitions in the corpus that
is roughly 87 CPU-hours (embarrassingly parallel) and ~700 GB if the intermediate is kept. Fine for a
2-run validation; worth designing around before anything corpus-wide -- the mzparquet is derived and
can be transient.
