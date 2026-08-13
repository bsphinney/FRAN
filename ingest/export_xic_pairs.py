"""export_xic_pairs.py — dump paired chromatograms (ours vs DIA-NN) for visual diagnosis.

The summary numbers say the Thermo extractor agrees on SHAPE (median r 0.8872) but has two
unexplained problems: 34% of traces come back empty from our side, and areas run 27% low. Neither
moves with the RT correction, so neither is a timing artefact. Aggregate statistics cannot say why;
the traces can.

Samples deliberately across the failure modes rather than uniformly, because a random sample would
be mostly good traces and show nothing:

    good      r >= 0.9      -- what working looks like
    poor      r <  0.5      -- disagreement in shape
    empty     ours all zero -- the 34%
    low_area  r >= 0.8 but area ratio < 0.5  -- the 27% deficit, with shape still right

Also records, per trace, the things that would explain an empty or truncated result: our RT window
against DIA-NN's, how many of our points are non-zero, the fragment m/z, and the isolation window
the precursor fell in.

    python ingest/export_xic_pairs.py --cache <dir> --xic <run.xic.parquet> \\
        --report <report.parquet> --run <basename> --out pairs.json
"""
import argparse
import functools
import json
import os
import re
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
print = functools.partial(print, flush=True)   # noqa: A001

LABEL_RE = re.compile(r"^([yba]\d+)\^?(\d*)$")
PER_CLASS = 12


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", required=True)
    ap.add_argument("--xic", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    import pyarrow.parquet as pq
    import xic_extractor as X

    rep = pq.read_table(a.report, columns=["Run", "Precursor.Id", "Stripped.Sequence",
                                           "Precursor.Charge", "Precursor.Mz", "RT"]).to_pandas()
    rep = rep[rep["Run"].astype(str).str.contains(a.run, regex=False)]
    rep = rep.drop_duplicates(subset=["Precursor.Id"]).head(a.limit)
    meta = {r[1]: (str(r[2]), int(r[3]), float(r[4]), float(r[5]))
            for r in rep.itertuples(index=False)}

    t = pq.read_table(a.xic, columns=["pr", "feature", "rt", "value"])
    pr = t.column("pr").to_pylist(); ft = t.column("feature").to_pylist()
    rt = t.column("rt").to_numpy(); val = t.column("value").to_numpy()
    ref = defaultdict(lambda: ([], []))
    for i, (p, f) in enumerate(zip(pr, ft)):
        if p in meta and LABEL_RE.match(f):
            r, v = ref[(p, f)]
            r.append(rt[i]); v.append(val[i])
    ref = {k: (np.asarray(r, float)[np.argsort(r)], np.asarray(v, float)[np.argsort(r)])
           for k, (r, v) in ref.items() if len(r) >= 8}
    print(f"{len(meta):,} precursors, {len(ref):,} reference traces")

    by_pr = defaultdict(list)
    for (p, f) in ref:
        by_pr[p].append(f)

    cache = X.Cache(a.cache)
    buckets = {"good": [], "poor": [], "empty": [], "low_area": []}
    n_seen = 0
    for p, feats in by_pr.items():
        seq, ch, mz, rt_min = meta[p]
        feats = feats[:X.N_FRAG_CHANNELS]
        fmz, keep = [], []
        for f in feats:
            m = LABEL_RE.match(f)
            v = X.ion_mz(seq, m.group(1), int(m.group(2) or 1))
            if v:
                fmz.append(float(v)); keep.append(f)
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
            r_rt, r_v = ref[(p, f)]
            n_seen += 1
            on_ref = np.interp(r_rt, ax, ours, left=0.0, right=0.0)
            nz = int((ours > 0).sum())
            if ours.max() <= 0 or on_ref.max() <= 0:
                cls, cc, ar = "empty", None, None
            else:
                cc = float(np.corrcoef(on_ref, r_v)[0, 1])
                ar = float(on_ref.sum() / max(r_v.sum(), 1e-9))
                if not np.isfinite(cc):
                    continue
                cls = ("good" if cc >= 0.9 else
                       "low_area" if (cc >= 0.8 and ar < 0.5) else
                       "poor" if cc < 0.5 else None)
                if cls == "good" and ar < 0.5:
                    cls = "low_area"
            if cls and len(buckets[cls]) < PER_CLASS:
                buckets[cls].append({
                    "precursor": p, "seq": seq, "charge": ch, "feature": f,
                    "precursor_mz": round(mz, 4), "fragment_mz": round(fmz[j], 4),
                    "report_rt_min": round(rt_min, 4),
                    "r": None if cc is None else round(cc, 4),
                    "area_ratio": None if ar is None else round(ar, 4),
                    "our_nonzero_points": nz, "our_points": int(len(ours)),
                    "our_rt": [round(float(x), 5) for x in ax],
                    "our_value": [round(float(x), 2) for x in ours],
                    "ref_rt": [round(float(x), 5) for x in r_rt],
                    "ref_value": [round(float(x), 2) for x in r_v],
                })
        if all(len(v) >= PER_CLASS for v in buckets.values()):
            break

    out = {"run": a.run, "n_traces_seen": n_seen,
           "our_window_s": X.RT_HALF, "our_points": X.N_POINTS,
           "ppm": X.PPM, "buckets": buckets,
           "counts": {k: len(v) for k, v in buckets.items()}}
    with open(a.out, "w") as fh:
        json.dump(out, fh)
    print(f"wrote {a.out}: " + ", ".join(f"{k}={len(v)}" for k, v in buckets.items()))
    print(f"our extraction window +/-{X.RT_HALF}s at {X.N_POINTS} points; PPM={X.PPM}")


if __name__ == "__main__":
    main()
