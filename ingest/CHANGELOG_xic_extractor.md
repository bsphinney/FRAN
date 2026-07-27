# xic_extractor — changelog

Versions are recorded in `xic_extractor.VERSION`. Each release states what it was measured against,
because "the extractor works" is only meaningful next to a number.

## v1.0.0 — 2026-07-27

First published version. Extraction core lifted out of the Retriever-DIA pipeline driver
(`build_xic_shard.py`) so it can be imported, tested and run off-cluster; proven **bit-identical**
to that implementation (60/60 tensors, max absolute difference 0.0).

**Fragment selection now comes from measured data.** The previous behaviour, `top6_by_mz()`, derives
six ions from the bare peptide sequence and takes the highest-m/z ones. Measured against
Spectronaut 21's own exported XICs, only **26.3%** of those were ions Spectronaut ever traced: they
sit at a median **0.86** of peptide length (the longest y/b ions) while the ions carrying real
intensity sit at **0.47**, and that route can never emit the ~15% of traced ions that are multiply
charged. `ObservedFragments` reads the ions actually measured from FRAN's spectrum Lance lane, where
they reproduce Spectronaut's traced ion set **98.7%** exactly.

**Verified against Spectronaut 21** on run `11May2026_DIA_60spd_VER_185_S2-B5_1_21766` (diaPASEF),
400 precursors per test, nothing fitted:

| test | with observed fragments | with `top6_by_mz()` |
|---|---|---|
| summed-profile Pearson *r* (median) | **0.915** | 0.679 |
| *r* > 0.8 | **78.0%** | 38.0% |
| apex RT \|Δ\| (median) | **0.55 s** | 1.33 s |
| apex within one 1.1 s cycle | **73.8%** | 47.3% |
| empty traces (of 400) | **0** | 108 |

Ion-for-ion, each fragment against Spectronaut's trace for that same ion (2,325 pairs): *r* median
**0.833**, apex median **0.59 s**, 66% within one cycle. Intensity on raw counts (2,312 pairs): area
ratio median **1.238**, peak ratio **0.886**, 81% within 2×.

**Known systematic:** more area with a lower apex means our peaks are slightly broader than
Spectronaut's — it integrates within fitted peak boundaries, we sum everything passing the m/z and
mobility gate across the window. Shape and position agree closely; absolute areas are comparable to
about 1.2×, not interchangeable.

**Decoy safety.** `mirror_ions()` recomputes a target's own ion identities on a mutated sequence.
Giving targets observed ions while decoys get sequence-predicted ones would make the two arms
separable on ion choice alone — a label leak unrelated to the data. Use it for any target/decoy
extraction.

**Ion mobility is required.** These are diaPASEF runs; the mobility gate is what separates
co-isolated precursors. Passing a wrong or placeholder `im` silently returns near-empty traces —
that mistake produced a 65%-empty run during development before the XIC lane stored `im` at all.

Configuration is via `FRAN_XIC_PPM`, `FRAN_XIC_IM_TOL`, `FRAN_XIC_RT_HALF`, `FRAN_XIC_POINTS`. The
module opens no database and reads no credentials.
