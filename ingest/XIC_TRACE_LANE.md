# The XIC trace lane — can Lance replace the raw for scoring and training?

**Answered on one run, 2026-07-28.** Short version: yes for re-scoring and model training on
precursors already extracted; **no** for re-analysis. And it is not a reason to delete raw data.

## The question

Reading a `.d` costs an I/O-bound trip through ~300M memory-mapped events to recover a few hundred
numbers per precursor. If the numbers a model consumes were stored once, in a columnar format built
for random access, the raw would not be needed for routine work.

## What was tested

Run `11May2026_DIA_60spd_VER_185_S2-B5_1_21766` (diaPASEF, dog + yeast entrapment; the Spectronaut
search ingested as `Dog_yeast_entrapment_SN21`).

**Phase 1** opened the `.d` cache once, extracted all 18,016 precursors with `xic_extractor`, and
wrote a Lance dataset carrying the tensor **plus everything needed to interpret it later**: the RT
axis bounds, the identity of every channel (m/z, ion label, charge, neutral loss), the extraction
parameters (`ppm`, `im_tol`, `rt_half`, `n_points`), and provenance back to the source.

**Phase 2** dropped the cache handle, reopened only the Lance dataset, rebuilt every chromatogram
from it, and compared against Spectronaut's own All-XIC export.

## Result

| | from Lance only | direct from the raw cache |
|---|---|---|
| median correlation | **0.847** | 0.833 |
| median \|Δapex\| | **0.39 s** | 0.59 s |
| within one 1.1 s cycle | **69.3%** | 66.2% |
| median area ratio | **1.210** | 1.238 |

Measured on **103,915 matched ion pairs**, with a round-trip md5 **MATCH** confirming the data
survived write/read intact. The Lance-only numbers match the raw-derived ones — marginally better,
on 45× more pairs. (Both columns predate the v1.3.0 mobility fix; see the changelog for current
figures. The point here is Lance-vs-raw, and that comparison is unaffected.)

## Economics

**1,235 bytes per precursor** — 22.2 MB for the run — at 82 ms per precursor to build.

Extrapolated across the corpus's 354M precursors that is roughly **440 GB**, or **under a terabyte**
including decoys, against **8 TB** of raw. Storing float32 rather than float16 is deliberate: raw
counts exceed float16's 65,504 ceiling and would silently become `inf`.

## The limits — read these before treating it as a raw substitute

A stored XIC is a **derived product**, cut at a fixed mass tolerance, RT window, mobility window and
fragment set. From tensors alone you can never:

- re-search with a new library — new peptides have unknown RT and mobility, so there is nothing to
  extract *at*;
- widen the mass or mobility tolerance — the gate is baked in;
- recover ions that were not stored.

So the achievable claim is **"never read the raw for routine scoring and training"**, not
"re-analysis without raw". **This is not a basis for deleting raw data**, which remains the primary
record and the only thing every derived lane can be rebuilt from.

## What a lane must store to be self-sufficient

Learned by getting them wrong first:

1. **Ion mobility per precursor.** The first XIC lane omitted it, so a verification run passed a
   placeholder `im = 1.0` and **65% of precursors returned empty traces**. A diaPASEF chromatogram
   without the mobility it belongs to cannot be reproduced or compared.
2. **The extraction parameters.** A tensor cut at 15 ppm and ±0.030 1/K0 is not comparable to one
   cut at 20 ppm and ±0.05. Without them stored, two datasets cannot be pooled.
3. **The RT axis**, or the bounds to rebuild it — otherwise tensors cannot be mapped back to time.
4. **Full channel identity** — label, charge **and neutral loss**. Label alone is ambiguous: 34.2% of
   precursors contain a repeated ion label differing only by loss.
5. **Provenance and a content checksum**, so a tensor's origin is provable after the raw moves to
   cold storage and corruption is detectable. `delimp_spectrum_lane` is the pattern to copy.

## Decoys

For a target/decoy training lane, decoys must be extracted in the **same pass** and must reuse the
target's own ion identities recomputed on the mutated sequence (`xic_extractor.mirror_ions`). Giving
targets observed ions while decoys get sequence-predicted ones makes the two arms separable on ion
choice alone — a label leak unrelated to the data.

## Not yet done

- A corpus-wide build. This is one run; the pattern and the per-precursor cost are established.
- Decoys are not in the tested lane — the ~440 GB figure doubles with them.
- The known **−0.3 s MS2 timestamp shift** (see the changelog) is present in any lane built before it
  is fixed. It does not affect shape, but it does affect absolute fragment RT.
