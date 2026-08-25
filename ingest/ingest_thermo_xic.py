"""ingest_thermo_xic.py — extract Thermo XIC traces into the lane, and score every run.

STORING AND SCORING ARE ONE STEP ON PURPOSE. Validation across 9 Thermo acquisitions found seven
clean (MS2 r 0.93-0.97, area ~1.00) and two that disagree for reasons still unknown: one with
MS1 r 0.24 / area 2.79x, one with MS2 area 1.88x. Nothing in the traces themselves distinguishes a
good run from those two -- they look like ordinary chromatograms. So a run is scored against an
independent engine as it is written, and the score is stored beside the dataset. Consumers filter on
`agree_verdict`; nobody has to assume the extractor is uniformly good, because it is not.

PIPELINE, per run:
    .raw --(ThermoRawFileParser -f 3)--> .mzparquet --(build_thermo_xic_cache)--> .npy cache
         --(xic_extractor)--> [C, 32] tensors --> Lance + registry
         --(compare against DIA-NN's --xic chromatograms)--> agreement score

DIA-NN's reference XICs must already exist for the run (see val/diann). Scoring without a reference
is not silently skipped: the run is registered with agree_verdict='unscored', which is deliberately
distinct from 'pass'.

    python ingest/ingest_thermo_xic.py --cache-dir <dir> --xic-dir <diann report_xic> \\
        --report <diann report.parquet> --out-dir <lance dir> [--limit-precursors 2000] [--apply]
"""
import argparse
import functools
import glob
import os
import re
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
print = functools.partial(print, flush=True)   # noqa: A001

LABEL_RE = re.compile(r"^([yba]\d+)\^?(\d*)$")
_trapz = getattr(np, "trapezoid", None) or np.trapz


def _conn():
    import psycopg2
    from refresh_leaderboards import _token
    return psycopg2.connect(
        host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
        dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
        user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
        password=_token(), sslmode="require", connect_timeout=30,
        options="-c statement_timeout=600000")


def load_ref(xic_parquet, want):
    import pyarrow.parquet as pq
    t = pq.read_table(xic_parquet, columns=["pr", "feature", "rt", "value"])
    pr = t.column("pr").to_pylist(); ft = t.column("feature").to_pylist()
    rt = t.column("rt").to_numpy(); val = t.column("value").to_numpy()
    ref = defaultdict(lambda: ([], []))
    for i, (p, f) in enumerate(zip(pr, ft)):
        if f == "index" or p not in want:
            continue
        if f != "ms1" and not LABEL_RE.match(f):
            continue
        r, v = ref[(p, f)]
        r.append(rt[i]); v.append(val[i])
    out = {}
    for k, (r, v) in ref.items():
        if len(r) >= 8:
            o = np.argsort(r)
            out[k] = (np.asarray(r, float)[o], np.asarray(v, float)[o])
    return out


def process_run(run, cache_dir, xic_file, meta, out_dir, limit_prec):
    """Extract every identified precursor in this run, and score a subset against DIA-NN."""
    import pyarrow as pa
    import xic_extractor as X

    cache = X.Cache(cache_dir)
    prs = list(meta)[:limit_prec]
    ref = load_ref(xic_file, set(prs)) if xic_file and os.path.exists(xic_file) else {}
    by_pr = defaultdict(list)
    for (p, f) in ref:
        by_pr[p].append(f)

    rows = defaultdict(list)
    ms2_r, ms2_a, ms1_r, ms1_a = [], [], [], []
    n_scored = 0
    n_err = 0
    for p in prs:
        seq, ch, mz, rt_min = meta[p]
        feats = sorted(f for f in by_pr.get(p, []) if f != "ms1")
        # Extraction uses DIA-NN's ion set when we have it, else the extractor's own selection.
        # Which ions are traced is a SELECTION question; this file is about extraction quality.
        if feats:
            fmz, keep = [], []
            for f in feats[:X.N_FRAG_CHANNELS]:
                m = LABEL_RE.match(f)
                v = X.ion_mz(seq, m.group(1), int(m.group(2) or 1))
                if v:
                    fmz.append(float(v)); keep.append(f)
        else:
            fmz = [float(v) for v in X.top6_by_mz(seq)] if hasattr(X, "top6_by_mz") else []
            keep = []
        if not fmz:
            continue
        rt_native = cache.rt_to_native(rt_min)
        try:
            T = cache.extract(mz, ch, fmz, rt_native, im=0.0, normalize=False)
            ax2 = cache.rt_axis(rt_native, minutes=True, channel="ms2")
            ax1 = cache.rt_axis(rt_native, minutes=True, channel="ms1")
        except Exception as e:      # narrow: report rather than swallow. A bare except here hid an
            n_err += 1              # ambiguous-truth-value error on `array or array` and reported
            if n_err <= 3:          # "no traces extracted" for every run.
                print(f"    extract failed for {p}: {type(e).__name__}: {str(e)[:90]}")
            continue
        if ax1 is None:
            ax1 = ax2
        if T is None or ax2 is None:
            continue

        rows["run"].append(run); rows["stripped_seq"].append(seq)
        rows["charge"].append(int(ch)); rows["precursor_mz"].append(float(mz))
        rows["rt"].append(float(rt_min)); rows["im"].append(0.0)
        rows["q_value"].append(0.0); rows["n_frag"].append(len(fmz))
        rows["trace"].append(np.asarray(T, np.float32).ravel().tolist())

        for j, f in enumerate(keep):
            if (p, f) not in ref:
                continue
            ours = np.asarray(T[j], float)
            r_rt, r_v = ref[(p, f)]
            if ours.max() <= 0 or r_v.max() <= 0:
                continue
            on_ref = np.interp(r_rt, ax2, ours, left=0.0, right=0.0)
            if on_ref.max() <= 0:
                continue
            cc = np.corrcoef(on_ref, r_v)[0, 1]
            mr = (r_rt >= ax2.min()) & (r_rt <= ax2.max())
            if np.isfinite(cc):
                ms2_r.append(cc); n_scored += 1
                if mr.sum() > 1:
                    a = _trapz(r_v[mr], r_rt[mr])
                    if a > 0:
                        ms2_a.append(_trapz(ours, ax2) / a)
        if "ms1" in by_pr.get(p, []) and (p, "ms1") in ref:
            ours = np.asarray(T[X.N_FRAG_CHANNELS], float)
            r_rt, r_v = ref[(p, "ms1")]
            if ours.max() > 0 and r_v.max() > 0:
                on_ref = np.interp(r_rt, ax1, ours, left=0.0, right=0.0)
                if on_ref.max() > 0:
                    cc = np.corrcoef(on_ref, r_v)[0, 1]
                    mr = (r_rt >= ax1.min()) & (r_rt <= ax1.max())
                    if np.isfinite(cc):
                        ms1_r.append(cc)
                        if mr.sum() > 1:
                            a = _trapz(r_v[mr], r_rt[mr])
                            if a > 0:
                                ms1_a.append(_trapz(ours, ax1) / a)
    if not rows["run"]:
        return None
    import xic_trace_lance as XL
    tbl = pa.table({k: pa.array(v) for k, v in rows.items()}).cast(_schema())
    med = lambda a: float(np.median(a)) if a else None          # noqa: E731
    return {"table": tbl, "n": len(rows["run"]), "cache": cache,
            "ms2_r": med(ms2_r), "ms2_area": med(ms2_a),
            "ms1_r": med(ms1_r), "ms1_area": med(ms1_a), "n_scored": n_scored,
            "verdict": XL.agreement_verdict(med(ms2_r), med(ms2_a), med(ms1_r), med(ms1_a))}


def _schema():
    import pyarrow as pa
    return pa.schema([("run", pa.string()), ("stripped_seq", pa.string()), ("charge", pa.int16()),
                      ("precursor_mz", pa.float32()), ("rt", pa.float32()), ("im", pa.float32()),
                      ("q_value", pa.float32()), ("n_frag", pa.int16()),
                      ("trace", pa.list_(pa.float32()))])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", required=True)
    ap.add_argument("--xic-dir", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--limit-precursors", type=int, default=2000)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    import pyarrow.parquet as pq
    import xic_extractor as X
    import xic_trace_lance as XL

    rep = pq.read_table(a.report, columns=["Run", "Precursor.Id", "Stripped.Sequence",
                                           "Precursor.Charge", "Precursor.Mz", "RT"]).to_pandas()
    caches = sorted(d for d in glob.glob(os.path.join(a.cache_dir, "*")) if os.path.isdir(d))
    print(f"{len(caches)} caches; extractor v{X.__dict__.get('VERSION','?')} "
          f"{X.N_FRAG_CHANNELS}+{X.N_MS1_CHANNELS} channels x {X.N_POINTS} pts")

    conn = None
    if a.apply:
        conn = _conn(); XL.ensure_registry(conn)
        os.makedirs(a.out_dir, exist_ok=True)

    results = []
    for c in caches:
        run = os.path.basename(c)
        sub = rep[rep["Run"].astype(str).str.contains(run, regex=False)]
        sub = sub.drop_duplicates(subset=["Precursor.Id"])
        meta = {r[1]: (str(r[2]), int(r[3]), float(r[4]), float(r[5]))
                for r in sub.itertuples(index=False)}
        if not meta:
            print(f"  {run[:38]:40s} no precursors in report — skipped"); continue
        xf = os.path.join(a.xic_dir, f"{run}.xic.parquet")
        out = process_run(run, c, xf, meta, a.out_dir, a.limit_precursors)
        if not out:
            print(f"  {run[:38]:40s} no traces extracted"); continue
        f = lambda v: "  --  " if v is None else f"{v:.3f}"     # noqa: E731
        print(f"  {run[:38]:40s} n={out['n']:>5}  MS2 r {f(out['ms2_r'])} area {f(out['ms2_area'])}"
              f"  MS1 r {f(out['ms1_r'])} area {f(out['ms1_area'])}  -> {out['verdict']}")
        results.append((run, c, out))
        if a.apply:
            import lance
            lp = os.path.join(a.out_dir, f"{run}.xictrace.lance")
            ds = lance.write_dataset(out["table"], lp, mode="overwrite")
            md5 = XL.content_md5(out["table"])
            XL.register(conn, run, lp, out["n"], md5, ds.version,
                        n_channels=X.N_FRAG_CHANNELS + X.N_MS1_CHANNELS,
                        n_cycles=len(out["cache"].cycle_rt))
            XL.record_agreement(conn, lp, out["ms2_r"], out["ms2_area"],
                                out["ms1_r"], out["ms1_area"], out["n_scored"],
                                reference="DIA-NN 2.6.0 --xic 30")

    ok = [r for r in results if r[2]["verdict"] == "pass"]
    print(f"\n{len(results)} runs processed, {len(ok)} pass, {len(results)-len(ok)} flagged")
    for run, _, o in results:
        if o["verdict"] != "pass":
            print(f"   FLAGGED {run[:44]:46s} {o['verdict']}")
    if not a.apply:
        print("\nDRY RUN — re-run with --apply to write the lane and record scores.")
        return
    import versions as V
    cur = conn.cursor()
    V.record_run(cur, "thermo_xic_trace_lane", V.XIC_TRACE_LANE_WRITER_VERSION,
                 notes=f"{len(results)} runs, {len(ok)} pass")
    conn.commit(); conn.close()
    print("DONE")


if __name__ == "__main__":
    main()
