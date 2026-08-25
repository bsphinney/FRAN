# Re-running the Siegel diaPASEF set with `--mobilograms`

**Status:** ready to run. Two attempts of mine failed on a wrong `--lib` path; the corrected command
is below and has been checked against the paths that exist today (2026-08-13).

## What's wrong with the existing output

Every `ms1_mobilogram.parquet` / `ms2_mobilogram.parquet` under
`par/timstof_Cer/xic/` and `par/timstof_STr/xic/` is **2,560,064 int64 rows, all zero** — verified
from parquet column statistics (`min=0`, `max=0`) across all three row groups in all 24 files.
`2,560,064 = 64 × 40,001`.

`--xic` preallocates these files; only `--mobilograms` fills them. The flag was never passed.

Two traps worth stating outright, because both fooled me:

- **File size proves nothing.** These parquets are written *uncompressed*, so they occupy 8 bytes per
  row whether the data is real or zeros. A 20 MB mobilogram file is exactly what an empty one looks
  like. Check `max` from the column statistics, not `ls -la`.
- **A zero-filled file is also what a silent failure looks like.** The binary carries
  `ERROR: not enough RAM to save the mobilogram`. If that fires, the output is indistinguishable
  from the flag never having been passed. Grep the log; don't infer success from the files existing.

## Version: 2.3.0 is fine, 2.6.0 is not required

I earlier claimed 2.3.0 lacked mobilogram support. **That was wrong.** The check was unsound —
`strings` isn't installed inside the 2.3.0 container, so `apptainer exec … strings … | grep -c`
returned 0 because the command failed, not because the strings were absent. Copying the binary out
and scanning it from the host shows **both 2.3.0 and 2.6.0 contain the same 10 mobilogram strings**.

Confirmed empirically: both of my runs logged `Mobilograms will be saved along with XICs` before
dying on the library path. The flag is recognised by 2.3.0.

Use 2.3.0 to match the original run. (2.6.0 also works on these — its .NET 8.0.17 constraint applies
to Thermo `.raw`, not Bruker `.d`.)

## The failure to avoid

The original job script passes `--lib …/timstof_Cer/libpriv/t0/lib.parquet`. **That path no longer
exists** — `libpriv/` is an empty directory; it was scratch space from an earlier pipeline step.
DIA-NN does not abort on a missing library, it logs `ERROR: cannot open …` and proceeds to produce
an empty report, which is why both my jobs "completed" in 14 seconds.

`par/timstof_Cer/report.log.txt` records what the run that produced the current XICs actually used:

```
--lib /quobyte/proteomics-grp/brett/siegel_glp1_2026-08-12/par/timstof_Cer/empirical.parquet
[0:00] Spectral library loaded: 2730 protein isoforms, 2609 protein groups and
       24919 precursors in 20213 elution groups.
```

Take `--lib` from the log, not from the job script.

## Command

```bash
A=/quobyte/proteomics-grp/apptainers/diann2.3.0.sif
B=/quobyte/proteomics-grp/brett/siegel_glp1_2026-08-12/par/timstof_Cer
RAWD=/nfs/lssc0/flinders/proteomics/Data/lab/service/on_campus/Siegel-bloodOzempic_iul26/d-SiegelBlood-Str-Cer
OUT=<new directory — do not overwrite par/timstof_Cer/xic/>

apptainer exec -B /quobyte -B /nfs "$A" /diann-2.3.0/diann-linux \
  --f "$RAWD/<run>.d" \
  --fasta /quobyte/proteomics-grp/MRS/UP000005640_9606_plus_universal_contam.fasta \
  --lib "$B/empirical.parquet" \
  --temp "$OUT/tmp" --quant-ori-names \
  --xic 30 --mobilograms \
  --out "$OUT/<run>.report.parquet" \
  --threads 16 --qvalue 0.01 --mass-acc 15 --mass-acc-ms1 15 --window 8 \
  --cont-quant-exclude Cont_
```

Differs from the original in exactly two places: `--mobilograms` added, and `--lib` pointed at the
library that exists. `--no-ifs-removal` was dropped — the original log shows it produced
`WARNING: unrecognised option`, so it never did anything.

Six `.quant` files survive in `quant_step4/`. Pointing `--temp` there with `--use-quant` should make
this a re-extraction rather than a full re-search; worth trying first, but note that `--use-quant`
also needs the same library, so it will not rescue a wrong `--lib`.

Allocate memory generously (I used `--mem=180G`) because of the RAM error above.

## Verifying it worked

Two independent checks — the log line and the data — because either alone can mislead:

```bash
grep -iE "mobilogram|not enough RAM" "$OUT"/*.log     # expect the "will be saved" line, no RAM error
```

```python
import pyarrow.parquet as pq, glob, os
for f in sorted(glob.glob(f"{OUT}/**/*mobilogram.parquet", recursive=True)):
    p = pq.ParquetFile(f); mx = None
    for i in range(p.metadata.num_row_groups):
        st = p.metadata.row_group(i).column(0).statistics
        if st and st.has_min_max:
            mx = st.max if mx is None else max(mx, st.max)
    print(f"{os.path.basename(f):60s} rows {p.metadata.num_rows:,}  max {mx}")
```

`max` must be non-zero. If it is 0, the run failed regardless of what the log says.

## Then what

The point of the re-run is to decide whether mobility dimension adds anything over the RT-only XICs
the extractor already builds. Once real mobilograms exist, the comparison is: for the same precursor,
does the mobilogram separate signal from co-eluting interference that the XIC alone cannot? If yes,
it becomes a channel in the timsTOF path of `ingest/xic_extractor.py`; if not, it costs storage for
nothing. That question is not answerable from the current all-zero files.
