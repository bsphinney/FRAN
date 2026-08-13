"""build_thermo_xic_cache.py — make Thermo .raw readable by xic_extractor.py.

The extractor was written for diaPASEF and reads a directory of memory-mapped .npy arrays built from
a Bruker .d. This produces the SAME arrays from a Thermo .raw, so the extraction core does not have
to be forked. Two vendors, one extractor.

    ThermoRawFileParser -i run.raw -f 3 -o dir/     ->  run.mzparquet   (44 s for 0.76 GB)
    python ingest/build_thermo_xic_cache.py run.mzparquet <cache_dir>

VALIDATED 2026-08-13 against DIA-NN 2.6.0 across NINE acquisitions -- 3 instruments (Exploris 120,
Exploris 480, Fusion Lumos), gradients 26-174 min, all 12 fragments DIA-NN traces plus the
monoisotopic MS1, ~17k traces:

    MEDIAN across runs:  MS2 r 0.955   r>0.8 74.4%   MS2 area 1.003x
                         MS1 r 0.972                 MS1 area 1.039x

Seven of the nine are clean (MS2 r 0.93-0.97, area 0.996-1.05; MS1 r 0.91-0.995, area 1.01-1.12).
The earlier single-run figure of 0.887 was pessimistic: that K562 method had a 2.15 s cycle against
a 2.14 s median peak FWHM, i.e. about one sample per peak, which is the worst case for a shape
comparison rather than a typical one.

TWO RUNS DISAGREE AND THE CAUSE IS NOT YET KNOWN -- do not quote these as extractor quality:
  * Ex010222_DiaBoxcar_60m_1 -- MS1 r 0.241, MS1 area 2.79x, MS2 area 0.328x. Ruled out: it is not
    BoxCar-style boxed MS1 (its MS1 scans carry no isolation windows) and not segmented MS1 (one
    m/z range). Its MS1 is unusually sparse, 137 peaks/scan against ~570 elsewhere.
  * FL010424_He50ng-DiaW22_90m -- MS2 area 1.883x while its MS1 is normal (r 0.979, area 1.019).

Because such runs are detectable as outliers on their own agreement metrics, the safe way to use
this is to SCORE EACH RUN and store the score, not to assume the extractor is uniformly good.

WHY NOT alpharaw. It is installed and its pythonnet bridge works, but it bundles Thermo's
.NET-Framework RawFileReader, which calls System.Security.AccessControl.MutexSecurity during
file-open. That type does not exist on .NET Core on Linux, so every file fails with
MissingMethodException regardless of environment. ThermoRawFileParser ships a Linux-compatible
reader and is already used by FRAN for instrument metadata on 4,197 raws.

TWO THINGS THIS GETS RIGHT THAT THE BRUKER PATH DOES NOT
--------------------------------------------------------
1. THE MOBILITY FILTER NEEDS NO CODE CHANGE. The extractor filters on
   `abs(mobility - im) <= IM_TOL` in both the MS2 and MS1 paths. Orbitrap data has no mobility axis,
   so this cache writes mobility_values as ZEROS; calling the extractor with im=0 makes the
   comparison `abs(0-0) <= IM_TOL` trivially true and the filter becomes a no-op. No fork, no flag.
   Be clear-eyed about what that means: mobility is a real third separating dimension on diaPASEF,
   and Thermo extraction is genuinely less selective without it. Expect worse agreement than the
   diaPASEF validation, and do not read that as a bug.

2. THE -0.3 s MS2 TIMESTAMP DEFECT DOES NOT HAVE TO BE INHERITED. On Bruker the extractor derives
   `cycle_rt[c] = rt[c * frames_per_cycle]`, i.e. every event in a cycle is stamped with the time of
   that cycle's FIRST frame -- which is the MS1 frame -- so MS2 traces are labelled about a quarter
   cycle early (measured: -0.295 s MS2 vs -0.032 s MS1). That is an artifact of the frame/cycle
   model, not of the instrument. In mzparquet every scan carries its own rt, so this writes an
   explicit `cycle_rt_ms2.npy` holding the MEAN RT of the MS2 scans actually in each cycle. The
   extractor uses it when present (see the `cycle_rt` override); without the override the Thermo arm
   would reproduce the same bias for no reason.

   THE LAG IS NOT A CONSTANT AND MUST NEVER BE HARDCODED. It is set by how long a cycle spends in
   MS2, so it scales with the number of isolation windows: a 50-window method and a 20-window method
   have very different lags, and it varies WITHIN a run too (this K562 run has cycles of 21 to 50
   scans). So `cycle_rt_ms2` is stored PER CYCLE, measured from the scan timestamps, never derived
   from a method setting or a global offset.

   The same applies to the Bruker side: -0.295 s is a property of THAT acquisition's cycle structure,
   not a universal correction. Anyone "fixing" the diaPASEF defect by subtracting 0.295 s would be
   wrong on every other method. The per-frame times exist in the Bruker cache too -- they are simply
   discarded by `rt[c * frames_per_cycle]` -- so the correct fix there is the same one: record the
   real per-cycle MS2 time and stop inferring it from the cycle's first frame.
"""
import argparse
import functools
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
print = functools.partial(print, flush=True)   # noqa: A001


def build(mzparquet: str, out_dir: str) -> dict:
    import pyarrow.parquet as pq

    cols = ["scan", "level", "rt", "mz", "intensity",
            "isolation_lower", "isolation_upper"]
    t = pq.read_table(mzparquet, columns=cols)
    scan = t.column("scan").to_numpy(zero_copy_only=False)
    level = t.column("level").to_numpy(zero_copy_only=False)
    rt = t.column("rt").to_numpy(zero_copy_only=False)
    mz = t.column("mz").to_numpy(zero_copy_only=False)
    inten = t.column("intensity").to_numpy(zero_copy_only=False)
    iso_lo = t.column("isolation_lower").to_numpy(zero_copy_only=False)
    iso_hi = t.column("isolation_upper").to_numpy(zero_copy_only=False)
    print(f"read {len(mz):,} peak rows from {os.path.basename(mzparquet)}")

    # ---- per-scan view (peaks are long-form; scans repeat) ---------------------------------
    order = np.argsort(scan, kind="stable")
    scan, level, rt, mz, inten = (a[order] for a in (scan, level, rt, mz, inten))
    iso_lo, iso_hi = iso_lo[order], iso_hi[order]
    first = np.flatnonzero(np.r_[True, scan[1:] != scan[:-1]])
    s_id, s_lvl, s_rt = scan[first], level[first], rt[first]
    n_scans = len(s_id)
    n_ms1 = int((s_lvl == 1).sum())
    print(f"{n_scans:,} scans: {n_ms1:,} MS1, {n_scans - n_ms1:,} MS2")

    # ---- cycles: one MS1 followed by its MS2 windows ---------------------------------------
    # A DIA cycle starts at each MS1. Anything before the first MS1 has no cycle and is dropped.
    cyc_of_scan = np.cumsum(s_lvl == 1) - 1
    if (cyc_of_scan < 0).any():
        keep = cyc_of_scan >= 0
        s_id, s_lvl, s_rt, cyc_of_scan = s_id[keep], s_lvl[keep], s_rt[keep], cyc_of_scan[keep]
    n_cycles = int(cyc_of_scan.max()) + 1
    per_cycle = np.bincount(cyc_of_scan)
    frames_per_cycle = int(np.round(np.median(per_cycle)))
    print(f"{n_cycles:,} cycles, median {frames_per_cycle} scans/cycle "
          f"(range {per_cycle.min()}-{per_cycle.max()})")

    scan_to_cycle = dict(zip(s_id.tolist(), cyc_of_scan.tolist()))
    pk_cycle = np.fromiter((scan_to_cycle.get(int(s), -1) for s in scan), dtype=np.int32,
                           count=len(scan))
    valid = pk_cycle >= 0
    is_ms1 = (level == 1) & valid
    is_ms2 = (level == 2) & valid

    # ---- honest per-cycle timestamps --------------------------------------------------------
    # MS1: the cycle's MS1 scan time. MS2: the MEAN of that cycle's MS2 scan times, which is what
    # the fragment traces are actually sampled at. On Bruker both collapse to the MS1 frame time,
    # which is the -0.295 s defect.
    cycle_rt_ms1 = np.zeros(n_cycles, dtype=np.float64)
    ms1_scan_mask = s_lvl == 1
    cycle_rt_ms1[cyc_of_scan[ms1_scan_mask]] = s_rt[ms1_scan_mask]
    sums = np.bincount(cyc_of_scan[s_lvl == 2], weights=s_rt[s_lvl == 2], minlength=n_cycles)
    cnts = np.bincount(cyc_of_scan[s_lvl == 2], minlength=n_cycles)
    cycle_rt_ms2 = np.where(cnts > 0, sums / np.maximum(cnts, 1), cycle_rt_ms1)
    off = float(np.median((cycle_rt_ms2 - cycle_rt_ms1)[cnts > 0]))
    unit = "min" if float(s_rt.max()) < 200 else "s"
    print(f"MS2 lags MS1 by a median {off:.4f} {unit} within a cycle "
          f"({off*60:.3f} s)" if unit == "min" else f"MS2 lags MS1 by {off:.3f} s")
    print("  -> recorded in cycle_rt_ms2.npy; stamping MS2 with the MS1 time is the Bruker defect")

    # UNITS: store RT in SECONDS. The extractor decides units by `cycle_rt.max() > 200` and then
    # applies RT_HALF (default 12) in whatever unit it inferred. mzparquet reports minutes, so a
    # minutes cache makes a 30-minute run look like "not seconds" and turns the +/-12 extraction
    # window into +/-12 MINUTES -- the whole gradient. Seconds keeps the Bruker semantics exactly.
    if unit == "min":
        cycle_rt_ms1 = cycle_rt_ms1 * 60.0
        cycle_rt_ms2 = cycle_rt_ms2 * 60.0
        s_rt = s_rt * 60.0

    os.makedirs(out_dir, exist_ok=True)
    save = lambda n, a: np.save(f"{out_dir}/{n}.npy", a)          # noqa: E731

    # rt_values is indexed by FRAME. Reconstruct a frame-ordered RT array so that
    # rt[cycle * frames_per_cycle] lands on that cycle's MS1, matching the Bruker layout.
    rt_frames = np.zeros(n_cycles * frames_per_cycle, dtype=np.float64)
    for c in range(n_cycles):
        rt_frames[c * frames_per_cycle] = cycle_rt_ms1[c]
        rt_frames[c * frames_per_cycle + 1:(c + 1) * frames_per_cycle] = cycle_rt_ms2[c]
    save("rt_values", rt_frames)
    save("cycle_rt_ms1", cycle_rt_ms1)
    save("cycle_rt_ms2", cycle_rt_ms2)
    # mobility: zeros. With im=0 at call time the extractor's |mob - im| <= IM_TOL is always true.
    save("mobility_values", np.zeros(max(frames_per_cycle, 2), dtype=np.float64))
    save("meta", np.array([frames_per_cycle], dtype=np.int64))

    save("ms1_mz", mz[is_ms1].astype(np.float64))
    save("ms1_int", inten[is_ms1].astype(np.float32))
    save("ms1_scan", np.zeros(int(is_ms1.sum()), dtype=np.int32))
    save("ms1_cycle_idx", pk_cycle[is_ms1].astype(np.int32))

    save("ev_mz", mz[is_ms2].astype(np.float64))
    save("ev_int", inten[is_ms2].astype(np.float32))
    save("ev_scan", np.zeros(int(is_ms2.sum()), dtype=np.int32))
    save("cycle_idx", pk_cycle[is_ms2].astype(np.int32))
    save("iso_lo", np.nan_to_num(iso_lo[is_ms2], nan=0.0).astype(np.float64))
    save("iso_hi", np.nan_to_num(iso_hi[is_ms2], nan=1e6).astype(np.float64))

    print(f"cycle_rt stored in SECONDS (max {cycle_rt_ms1.max():.0f} s) so RT_HALF is seconds")
    stats = {"n_scans": n_scans, "n_cycles": n_cycles, "frames_per_cycle": frames_per_cycle,
             "n_ms1_peaks": int(is_ms1.sum()), "n_ms2_peaks": int(is_ms2.sum()),
             "ms2_lag": off, "rt_unit": unit}
    print(f"wrote cache -> {out_dir}")
    print(f"   MS1 peaks {stats['n_ms1_peaks']:,}   MS2 peaks {stats['n_ms2_peaks']:,}")
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mzparquet")
    ap.add_argument("out_dir")
    a = ap.parse_args()
    if not os.path.exists(a.mzparquet):
        sys.exit(f"no such file: {a.mzparquet}")
    build(a.mzparquet, a.out_dir)
    print("DONE")


if __name__ == "__main__":
    main()
