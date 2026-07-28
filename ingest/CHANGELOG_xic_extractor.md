# xic_extractor — changelog

Versions are recorded in `xic_extractor.VERSION`. Each release states what it was measured against,
because "the extractor works" is only meaningful next to a number.

## v1.4.0 — 2026-07-28

**Neutral loss is now carried through, and channel pairing uses Spectronaut's own trace naming.**

`ObservedFragments.get()` returns `(label, charge, loss)` per fragment instead of `(label, charge)`,
and `spectronaut_trace_label()` builds the key Spectronaut actually uses — `y5+`, `y5+ -H2O`,
`y6+ -NH3`, `y19++`. Pairing on the bare ion label was wrong: **5,756 of 108,852** observed
fragments carry a neutral loss, and **34.2% of precursors** contain the same ion label more than
once (y5, y5−NH3, y5−H2O differ only by loss and m/z). That mispaired **4,187 channels**, comparing
a loss-ion chromatogram against its parent's.

**Final verification, loss-aware, full run:**

| channel | n | *r* median | *r* > 0.8 | apex \|Δ\| | area |
|---|---|---|---|---|---|
| MS2 fragments | 99,500 | **0.8791** | 72.1% | 0.39 s | 1.028× |
| MS2 loss ions | 5,576 | **0.8608** | 65.0% | 0.39 s | 1.047× |
| mono-isotopic | 18,015 | **0.9861** | 94.3% | 0.37 s | 0.963× |
| M+1 | 18,015 | **0.9835** | 92.7% | 0.37 s | 0.976× |
| M+2 | 18,015 | **0.9663** | 87.2% | 0.39 s | 1.035× |
| M+3 | 18,010 | **0.9259** | 76.8% | 0.39 s | 1.111× |
| M+4 | 13,086 | **0.8931** | 70.2% | 0.39 s | 1.192× |

Channels Spectronaut never traced are now **excluded rather than mispaired** — 4,926 M+4 (it traced
M+4 for only 13,290 of 18,287 precursors), 614 fragments, 115 loss ions.

**Honest note on impact:** fixing this moved the MS2 median only **0.8751 → 0.8791**, and the
worst-channel flag 31 → 30 of 100. A loss ion and its parent are the same peptide at the same
retention time, so their *shapes* barely differ and a correlation metric was nearly blind to the
mispairing. An area metric would not have been. The fix is still correct — loss ions are now
evaluated on their own terms and hold up at r 0.861 — but the predicted impact was overstated.

### Known defect, open: MS2 timestamps run ~0.3 s early

Over 42,199 clean fragment channels (r > 0.9, sub-bin apex), our MS2 apex sits a median **−0.295 s**
from Spectronaut's while MS1 sits at **−0.032 s** — a systematic **−0.263 s** between the two levels,
about a quarter of the 1.098 s acquisition cycle.

Cause: `cycle_rt[c] = rt[c × frames_per_cycle]` stamps every event in a cycle with the time of that
cycle's **first** frame, which is the MS1 frame. The 11 MS2 frames are acquired later in the same
cycle, so fragment traces are labelled early while MS1 lands correctly. Peak *shape* is unaffected
(correlations don't change), but absolute fragment RT is wrong and this is the likely source of the
residual MS1-vs-MS2 co-elution scatter. Fix: offset the MS2 axis by the mean MS1→MS2 frame gap.

## v1.3.0 — 2026-07-27

**The ion-mobility window was too wide, and that — not integration philosophy — was the 1.2× area
inflation v1.0.0 documented as inherent.** `IM_TOL` 0.05 → **0.030**.

Spectronaut measures the ion-mobility peak width at a median of **0.0216** 1/K0. The old ±0.05 was a
full width of 0.10, about **4.6× the peak**, so every extraction admitted co-eluting ions at other
mobilities. A sweep over 1,200 precursors × 7 tolerances against Spectronaut's own chromatograms
shows correlation tracing a clean inverted-U peaking at ±0.025 on *both* levels, with the area ratio
crossing 1.0 at ~±0.030. Spectronaut's own window for the same file is **±0.03** (its IM extraction
plot reports median *width* 0.06) — two independent routes landing on the same number.

| IM tol | full width | MS2 *r* | MS2 area | MS1 *r* | MS1 area |
|---|---|---|---|---|---|
| 0.0100 | 0.020 | 0.832 | 0.665× | 0.970 | 0.632× |
| 0.0200 | 0.040 | 0.872 | 0.880× | 0.985 | 0.847× |
| 0.0250 | 0.050 | **0.878** | 0.954× | **0.988** | 0.897× |
| **0.0300** | 0.060 | *chosen* | ~1.00× | | |
| 0.0500 *(old)* | 0.100 | 0.847 | 1.214× | 0.976 | 1.117× |
| 0.0800 | 0.160 | 0.818 | 1.363× | 0.969 | 1.234× |

Tightening *below* ±0.025 clips the peptide's own mobility peak — area collapses to 0.665× and
empty traces climb — so this is a genuine optimum, not "narrower is better".

**Full-run verification at ±0.030** (18,016 precursors, every channel, against Spectronaut 21):

| channel | n | *r* median | *r* > 0.8 | apex | area | empty |
|---|---|---|---|---|---|---|
| MS2 fragments | 103,797 | **0.8751** | 70.1% | 0.39 s | **1.022×** | 1,039 |
| mono-isotopic | 18,015 | **0.9861** | 94.3% | 0.37 s | 0.963× | 0 |
| M+1 | 18,015 | **0.9835** | 92.7% | 0.37 s | 0.976× | 0 |
| M+2 | 18,015 | **0.9663** | 87.2% | 0.39 s | 1.035× | 0 |
| M+3 | 18,010 | **0.9259** | 76.8% | 0.39 s | 1.111× | 0 |
| M+4 | 13,086 | **0.8931** | 70.2% | 0.39 s | 1.192× | 0 |

Every channel improved on both correlation and area versus ±0.05, and **every area ratio now sits
near 1.0**. The weakest channels gained most — M+4 went 0.812 → 0.893 with area 1.680× → 1.192× —
because the faintest signal was the most contaminated by out-of-mobility interference. Cost: 1,039
empty MS2 fragment traces (1.0%), zero empty MS1.

**Method note worth keeping.** v1.0.0 documented the area excess as a property of the extractor. It
was a parameter. The mechanism was plausible and every measurement agreed with it, which is exactly
why it survived three rounds of verification — the data could not separate "integrates differently"
from "window too wide". Only a sweep could. Check whether a parameter explains a systematic before
writing it down as inherent.

**Still approximate:** a fixed tolerance can only approach what Spectronaut does — its window is
*dynamic* and DNN-predicted per precursor, and the measured peak width varies 1.7× across precursors
(p10 0.0165, p90 0.0277). Scaling the window per precursor from `FG_IonMobilityPeakWidth`, a column
Spectronaut records and this extractor does not yet read, is the better long-term fix.

## v1.2.0 — 2026-07-27

**Five isotope channels instead of three**, matching what Spectronaut traces (`M`, `M+1` … `M+4`);
the tensor becomes `[11, 32]`. Measured on a real export, Spectronaut has M+3 for 18,286 of 18,287
precursors and M+4 for 13,290, and both agree well enough to be worth keeping (see v1.3.0 table).
The isotope envelope's *shape* discriminates a real precursor from interference, so truncating at
M+2 discarded part of that comparison. Costs ~256 bytes per precursor.

**Explicit normalisation modes.** MS1 and MS2 are not the same magnitude: measured over 18,015
precursors, MS1 is **5.5× louder** at the median and louder in **95.1%** of cases (p90 18.8×). The
single global max — one scale for all channels — therefore hands MS1 the full range and squashes the
FRAGMENT rows to a median peak of **0.182**, below 0.1 for **24.5%** of precursors. The fragments are
the identification evidence. `normalize=False` (raw counts) is correct for storage; `"per_block"`
scales MS1 and MS2 to their own maxima; the legacy global mode remains the default only for backward
compatibility. Per-block discards the cross-modality ratio, which is real information — carry it
alongside as `log10(ms1_max/ms2_max)`.

## v1.1.0 — 2026-07-27

**10–37× faster, bit-identical output.** No API change, no flag, no per-file setup.

Profiling showed the extractor was I/O bound and that ~2/3 of its runtime was spent merely
*locating* data: `searchsorted` over a ~300M-row memory-mapped cycle index costs ~28 random page
faults per call on network storage. The actual science — matching six fragment m/z — was **0.5 ms**.

v1.1.0 builds a per-cycle row-offset index (one int64 per cycle, ~9 KB for a 21-minute gradient) so
slicing is a lookup instead of a search. It is built automatically the first time a cache is opened
and persisted next to the arrays, so it applies to **every** run with no intervention and costs
nothing on later opens. If the cache directory is read-only it stays in memory and is still fast.

Measured, with output verified bit-identical to the engine implementation throughout:

| cache | before | after | speedup | identical |
|---|---|---|---|---|
| `..._VER_185_S2-B5_1_21766` (index warm) | 3,468 ms/precursor | **94 ms** | **36.8×** | 40/40 |
| `..._VER_10_S2-B2_1_21552` (never seen, index built on open) | 1,647 ms/precursor | **102 ms** | **16.2×** | 12/12 |

First open of a new cache spends a few seconds building the index (0.5–6.7 s depending on I/O
contention); re-opening an indexed cache takes 0.17 s. The spread in "before" numbers is shared-
filesystem contention, which is exactly what the index removes sensitivity to.

**Still open:** only ~1.8% of the events read survive the isolation-window and mobility gate,
because events are indexed by cycle but not by isolation window — every precursor still reads all
~12 diaPASEF windows to use one. Partitioning events by window is the next significant win and
needs a cache-format change.

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
