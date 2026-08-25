"""selfref_score.py — score one run's XIC extraction against the SEARCH ENGINE's own reported values.

WHY THIS TIER EXISTS. The trace-level gate (ingest_thermo_xic.py) compares our chromatograms to
DIA-NN's and is the stronger test, but it needs DIA-NN `--xic` output, which exists for 9 runs.
Generating it for the whole corpus means re-searching the corpus. The spectrum lane, however, already
carries what the original search engine reported for every precursor it identified: an RT, a
per-fragment integrated area, and per-isotope MS1 intensities. That is a genuinely independent
reference -- a different program, run years earlier, on the same raw file -- and it costs nothing but
the extraction.

WHAT IT CAN AND CANNOT SHOW. It compares integrated scalars, not shapes. A run that passes here has
been shown to produce the right MAGNITUDE and TIMING, not the right chromatogram. That is why the
verdict is `selfref_ok` and never `pass`, and why record_selfref refuses to overwrite a trace-level
verdict with a scalar one. Both trace-level failures observed so far were magnitude errors (MS1
2.79x, MS2 1.88x), so the weaker test happens to cover the failure mode actually seen -- but that is
an empirical accident, not a guarantee, and it does not generalise to shape defects.

THE RATIO IS NOT THE SIGNAL; ITS SCATTER IS. Our extraction window and the engine's integration
bounds differ, and the engine's areas are in arbitrary units, so the absolute ratio (ours/theirs) can
legitimately sit anywhere. What is diagnostic is CONSISTENCY: within a healthy run the ratio is
roughly constant across precursors. A run whose ratio scatters wildly is not measuring the same
thing. Scatter is therefore measured as a ROBUST CV -- 1.4826*MAD/median -- because a handful of
badly-integrated precursors would otherwise dominate a plain std/mean and flag healthy runs.

MS1 AND MS2 ARE SCORED SEPARATELY. Two of the three known trace-level failures were MS1-only with
MS2 clean beside them. Pooling the axes would average a broken one against a healthy one and hide
exactly what this is for.

    python ingest/selfref_score.py --run <run> --lance <spectrum lane .lance> \\
        --raw <hive .raw path> --work-dir <scratch> [--limit-precursors 1500] [--apply]
"""
import argparse
import functools
import os
import shutil
import subprocess
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
print = functools.partial(print, flush=True)   # noqa: A001

_trapz = getattr(np, "trapezoid", None) or np.trapz

TRFP = os.environ.get("FRAN_TRFP", "/quobyte/proteomics-grp/tools/ThermoRawFileParser/ThermoRawFileParser")
MIN_FRAG_AREA = 1.0        # engine areas at or below this are "not quantified", not "tiny"
MIN_PREC_FOR_RUN = 50      # below this the CV is noise; selfref_verdict returns 'unscored' anyway


def robust_cv(x):
    """1.4826*MAD/median. Returns None when undefined."""
    x = np.asarray([v for v in x if np.isfinite(v) and v > 0], float)
    if x.size < 10:
        return None
    med = float(np.median(x))
    if med <= 0:
        return None
    mad = float(np.median(np.abs(x - med)))
    return 1.4826 * mad / med


def convert(raw_path, pq_dir):
    """.raw -> .mzparquet. Returns the path, or None. Only -f 3 is needed: the -f 2 mzML in the
    validation pipeline existed solely to feed DIA-NN, which is not involved here."""
    bn = os.path.basename(raw_path)
    for ext in (".raw", ".RAW"):
        if bn.endswith(ext):
            bn = bn[: -len(ext)]
            break
    out = os.path.join(pq_dir, bn + ".mzparquet")
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return out
    os.makedirs(pq_dir, exist_ok=True)
    r = subprocess.run([TRFP, "-i", raw_path, "-f", "3", "-o", pq_dir],
                       capture_output=True, text=True, timeout=7200)
    if not (os.path.exists(out) and os.path.getsize(out) > 0):
        print(f"    convert failed rc={r.returncode}: {(r.stderr or r.stdout)[-200:]}")
        return None
    return out


def load_engine_rows(lance_path, run, limit, max_q=0.01):
    """The engine's reported values for this run: RT, fragment areas, MS1 isotope intensities."""
    import lance
    ds = lance.dataset(lance_path)
    have = {f.name for f in ds.schema}
    cols = [c for c in ("run", "stripped_seq", "modified_seq", "charge", "precursor_mz", "rt",
                        "q_value", "frg_mz", "frg_peak_area", "frg_excluded", "frg_ion",
                        "frg_charge", "ms1_iso_measured", "ms2_quantity", "is_decoy")
            if c in have]
    # Filter in the scanner, not in Python: these lanes carry millions of rows per search and the
    # run of interest is one of many inside a single .lance directory.
    esc = run.replace("'", "''")
    flt = f"run = '{esc}'"
    if "is_decoy" in have:
        flt += " AND is_decoy = false"
    if "q_value" in have:
        flt += f" AND q_value <= {max_q}"
    t = ds.to_table(columns=cols, filter=flt)
    rows = t.to_pylist()
    if limit and len(rows) > limit:
        # Deterministic stride, not head(): the head of a lane is one RT region, and RT-correlated
        # sampling would confound the score with elution position.
        step = max(1, len(rows) // limit)
        rows = rows[::step][:limit]
    return rows


def score(run, lance_path, raw_path, work_dir, limit):
    import xic_extractor as X

    pq_dir = os.path.join(work_dir, "pq")
    cache_dir = os.path.join(work_dir, "cache", run)
    rows = load_engine_rows(lance_path, run, limit)
    if len(rows) < MIN_PREC_FOR_RUN:
        return {"skip": f"only {len(rows)} identified precursors in the lane"}
    mzpq = convert(raw_path, pq_dir)
    if not mzpq:
        return {"skip": "raw conversion failed"}
    if not os.path.isdir(cache_dir):
        import build_thermo_xic_cache as B
        sys.argv = ["build_thermo_xic_cache.py", mzpq, cache_dir]
        try:
            B.main()
        except SystemExit:
            pass
        except Exception as e:
            return {"skip": f"cache build failed: {type(e).__name__}: {str(e)[:80]}"}
    if not os.path.isdir(cache_dir):
        return {"skip": "cache build produced nothing"}

    cache = X.Cache(cache_dir)
    ms2_ratios, ms1_ratios, rt_deltas = [], [], []
    n_prec = n_err = 0
    for r in rows:
        seq = r.get("stripped_seq")
        fmz = r.get("frg_mz") or []
        far = r.get("frg_peak_area") or []
        fex = r.get("frg_excluded") or [False] * len(fmz)
        if not seq or not fmz or len(far) != len(fmz):
            continue
        # Drop fragments the engine itself excluded from quant: its reported area for those is not
        # a quantity it stands behind, so disagreement there says nothing about our extraction.
        keep = [i for i in range(min(len(fmz), X.N_FRAG_CHANNELS))
                if not fex[i] and far[i] is not None and far[i] > MIN_FRAG_AREA]
        if not keep:
            continue
        use_mz = [float(fmz[i]) for i in keep]
        try:
            rt_native = cache.rt_to_native(float(r["rt"]))
            T = cache.extract(float(r["precursor_mz"]), int(r["charge"]), use_mz,
                              rt_native, im=0.0, normalize=False)
            ax2 = cache.rt_axis(rt_native, minutes=True, channel="ms2")
            ax1 = cache.rt_axis(rt_native, minutes=True, channel="ms1")
        except Exception as e:
            n_err += 1
            if n_err <= 3:
                print(f"    extract failed: {type(e).__name__}: {str(e)[:80]}")
            continue
        if T is None or ax2 is None:
            continue
        if ax1 is None:
            ax1 = ax2
        n_prec += 1
        best = None
        for j, i in enumerate(keep):
            ours = np.asarray(T[j], float)
            if ours.max() <= 0:
                continue
            a = float(_trapz(ours, ax2))
            if a > 0:
                ms2_ratios.append(a / float(far[i]))
                if best is None or ours.max() > best[0]:
                    best = (ours.max(), float(ax2[int(ours.argmax())]))
        # RT is taken from the most intense fragment rather than an average: a weak co-eluting
        # channel's apex is noise-dominated and would blur the timing signal we are testing.
        if best is not None:
            rt_deltas.append(abs(best[1] - float(r["rt"])) * 60.0)
        iso = r.get("ms1_iso_measured")
        if iso is not None and len(iso) and T.shape[0] > X.N_FRAG_CHANNELS:
            eng_ms1 = max((float(v) for v in iso if v is not None), default=0.0)
            ours1 = np.asarray(T[X.N_FRAG_CHANNELS], float)
            if eng_ms1 > 0 and ours1.max() > 0:
                a1 = float(_trapz(ours1, ax1))
                if a1 > 0:
                    ms1_ratios.append(a1 / eng_ms1)
    med = lambda x: float(np.median(x)) if x else None      # noqa: E731
    return {"n": n_prec, "ms2_cv": robust_cv(ms2_ratios), "ms1_cv": robust_cv(ms1_ratios),
            "ms2_n": len(ms2_ratios), "ms1_n": len(ms1_ratios),
            # Both median ratios are kept: the CV cannot see a systematic offset, and a systematic
            # offset is what every trace-level failure so far has actually been.
            "ratio": med(ms2_ratios), "ms2_ratio": med(ms2_ratios), "ms1_ratio": med(ms1_ratios),
            "rt_delta": med(rt_deltas),
            "cache_dir": cache_dir, "mzpq": mzpq}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--lance", required=True, help="spectrum lane .lance holding this run")
    ap.add_argument("--raw", required=True, help="Hive-reachable .raw path")
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--trace-lance", default=None,
                    help="xictrace lance_path to attach the score to; defaults to a derived path")
    ap.add_argument("--limit-precursors", type=int, default=1500)
    ap.add_argument("--keep-cache", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    os.makedirs(a.work_dir, exist_ok=True)
    out = score(a.run, a.lance, a.raw, a.work_dir, a.limit_precursors)
    if "skip" in out:
        print(f"  {a.run[:46]:48s} SKIP {out['skip']}")
        return
    f = lambda v: "  --  " if v is None else f"{v:.3f}"     # noqa: E731
    g = lambda v: "  --  " if v is None else f"{v:.4g}"     # noqa: E731
    import xic_trace_lance as XL
    v = XL.selfref_verdict(out["ms2_cv"], out["ms1_cv"], out["rt_delta"], out["n"])
    print(f"  {a.run[:46]:48s} n={out['n']:>5} MS2 cv {f(out['ms2_cv'])} ratio {g(out['ms2_ratio'])} "
          f"| MS1 cv {f(out['ms1_cv'])} ratio {g(out['ms1_ratio'])} "
          f"| rt {f(out['rt_delta'])}s -> {v}")

    if a.apply:
        import psycopg2
        from refresh_leaderboards import _token
        conn = psycopg2.connect(
            host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
            dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
            user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
            password=_token(), sslmode="require", connect_timeout=30,
            options="-c statement_timeout=600000")
        XL.ensure_registry(conn)
        lp = a.trace_lance or os.path.join(a.work_dir, "xictrace", f"{a.run}.xictrace.lance")
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM delimp_xic_trace_lane WHERE lance_path=%s", (lp,))
        if not cur.fetchone():
            # A selfref score describes a dataset. If no dataset row exists we register a
            # scored-but-unstored placeholder rather than silently dropping the measurement.
            cur.execute("""INSERT INTO delimp_xic_trace_lane
                             (lance_path, run, n_precursors, writer_version, extractor_version)
                           VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                        (lp, a.run, out["n"], "selfref-only", "1.4.0"))
            conn.commit()
        # promote=False: the scatter-only verdict must not be written into agree_verdict until a
        # ratio threshold from the corpus distribution is shown to separate the known failures.
        XL.record_selfref(conn, lp, area_ratio=out["ratio"], ms2_cv=out["ms2_cv"],
                          ms1_cv=out["ms1_cv"], rt_delta_s=out["rt_delta"], n=out["n"],
                          ms2_n=out["ms2_n"], ms1_n=out["ms1_n"],
                          ms2_ratio=out["ms2_ratio"], ms1_ratio=out["ms1_ratio"], promote=False)
        conn.close()

    # Intermediates are ~1.5 GB per run and the score does not need them again. Removing them keeps
    # peak scratch at (concurrency x 1.5 GB) instead of (total runs x 1.5 GB).
    if not a.keep_cache:
        shutil.rmtree(out["cache_dir"], ignore_errors=True)
        try:
            os.remove(out["mzpq"])
        except OSError:
            pass


if __name__ == "__main__":
    main()
