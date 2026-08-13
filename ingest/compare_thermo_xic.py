"""compare_thermo_xic.py — validate the Thermo XIC extractor against DIA-NN's own chromatograms.

xic_extractor.py v1.4.0 was written for diaPASEF and validated only there (median r 0.879 on MS2
fragments vs Spectronaut 21). build_thermo_xic_cache.py lets it READ Thermo data; whether it
EXTRACTS Thermo correctly is a separate question and this answers it.

DIA-NN is an independent reference -- different engine, different extraction code, same raw file --
so disagreement is informative rather than circular. Nothing here is fitted.

PAIRING IS EXACT, NOT FUZZY:
  * DIA-NN `pr` is modified-sequence + charge, matching FRAN's precursor_id convention.
  * DIA-NN `feature` is 'y7^1' / 'b3^1'; ion identity and charge are parsed from it and the fragment
    m/z recomputed on the sequence with ion_mz(), so both sides trace the same ion.
  * Only the ion labels DIA-NN actually traced are compared; the extractor is given exactly those.

THE TIMESTAMP CORRECTION IS MEASURED, NOT ASSUMED. The whole comparison runs TWICE: once with the
cache's per-cycle measured MS2 time (cycle_rt_ms2) and once with the Bruker convention of stamping
every event with the cycle's MS1 frame time. On this K562 run those differ by a median 1.176 s. If
the correction does not move the numbers it is not worth the complexity.

    python ingest/compare_thermo_xic.py --cache <dir> --xic <run.xic.parquet> \\
        --report <report.parquet> --run <run basename> [--limit 300]
"""
import argparse
import functools
import os
import re
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
print = functools.partial(print, flush=True)   # noqa: A001

LABEL_RE = re.compile(r"^([yba]\d+)\^?(\d*)$")


def load_reference(xic_parquet, want_prs):
    """DIA-NN chromatograms -> {(pr, feature): (rt_min[], value[])}, fragments only."""
    import pyarrow.parquet as pq
    t = pq.read_table(xic_parquet, columns=["pr", "feature", "rt", "value"])
    pr = t.column("pr").to_pylist(); ft = t.column("feature").to_pylist()
    rt = t.column("rt").to_numpy(); val = t.column("value").to_numpy()
    ref = defaultdict(lambda: ([], []))
    for i, (p, f) in enumerate(zip(pr, ft)):
        if f in ("ms1", "index") or p not in want_prs:
            continue
        if not LABEL_RE.match(f):
            continue
        r, v = ref[(p, f)]
        r.append(rt[i]); v.append(val[i])
    out = {}
    for k, (r, v) in ref.items():
        if len(r) >= 8:
            o = np.argsort(r)
            out[k] = (np.asarray(r, float)[o], np.asarray(v, float)[o])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", required=True)
    ap.add_argument("--xic", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--run", required=True, help="substring identifying this run in report.Run")
    ap.add_argument("--limit", type=int, default=300)
    a = ap.parse_args()

    import pyarrow.parquet as pq
    import xic_extractor as X

    rep = pq.read_table(a.report, columns=["Run", "Precursor.Id", "Stripped.Sequence",
                                           "Precursor.Charge", "Precursor.Mz", "RT"]).to_pandas()
    rep = rep[rep["Run"].astype(str).str.contains(a.run, regex=False)]
    rep = rep.drop_duplicates(subset=["Precursor.Id"]).head(a.limit)
    meta = {r[1]: (str(r[2]), int(r[3]), float(r[4]), float(r[5]))
            for r in rep.itertuples(index=False)}
    print(f"{len(meta):,} precursors from report for run ~{a.run}")

    ref = load_reference(a.xic, set(meta))
    print(f"{len(ref):,} reference fragment traces")
    if not ref:
        sys.exit("no reference traces matched — check --run and the xic file")

    by_pr = defaultdict(list)
    for (p, f) in ref:
        by_pr[p].append(f)

    for corrected in (True, False):
        cache = X.Cache(a.cache)
        if not corrected:
            ms1 = os.path.join(a.cache, "cycle_rt_ms1.npy")
            if os.path.exists(ms1):
                cr = np.load(ms1)
                cache.cycle_rt = cr[:len(cache.cycle_rt)]
        label = ("cycle_rt_ms2  (measured MS2 time)" if corrected
                 else "MS1-frame time (Bruker convention)")
        rs, apex_d, areas, n_empty = [], [], [], 0
        for p, feats in by_pr.items():
            seq, ch, mz, rt_min = meta[p]
            feats = feats[:X.N_FRAG_CHANNELS]        # extract() returns this many fragment channels
            fmz, keep = [], []
            for f in feats:
                m = LABEL_RE.match(f)
                ion, fz = m.group(1), int(m.group(2) or 1)
                v = X.ion_mz(seq, ion, fz)
                if v:
                    fmz.append(v); keep.append(f)
            if not fmz:
                continue
            rt_native = cache.rt_to_native(rt_min)
            try:
                T = cache.extract(mz, ch, fmz, rt_native, im=0.0, normalize=False)
                ax = cache.rt_axis(rt_native, minutes=True)
            except Exception:
                continue
            if T is None or ax is None:
                continue
            for j, f in enumerate(keep):
                ours = np.asarray(T[j], float)
                if ours.max() <= 0:
                    n_empty += 1
                    continue
                r_rt, r_v = ref[(p, f)]
                if r_v.max() <= 0:
                    continue
                v_on_ref = np.interp(r_rt, ax, ours, left=0.0, right=0.0)
                if v_on_ref.max() <= 0:
                    n_empty += 1
                    continue
                cc = np.corrcoef(v_on_ref, r_v)[0, 1]
                if np.isfinite(cc):
                    rs.append(cc)
                    apex_d.append(abs(r_rt[v_on_ref.argmax()] - r_rt[r_v.argmax()]) * 60.0)
                    areas.append(v_on_ref.sum() / max(r_v.sum(), 1e-9))
        if not rs:
            print(f"\n{label}: 0 comparable traces ({n_empty} empty) — extraction returned nothing")
            continue
        rs = np.asarray(rs)
        print(f"\n{label}")
        print(f"   traces compared {len(rs):,}   (empty from our side: {n_empty:,})")
        print(f"   median r        {np.median(rs):.4f}")
        print(f"   r > 0.8         {100*(rs > 0.8).mean():.1f}%")
        print(f"   apex |d| median {np.median(apex_d):.2f} s")
        print(f"   area ours/DIANN {np.median(areas):.3f}x")


if __name__ == "__main__":
    main()
