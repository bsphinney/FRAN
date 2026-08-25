"""selfref_calibrate.py — decide whether the scalar self-reference gate is usable at all.

THE QUESTION. Trace-level scoring (ours vs DIA-NN chromatograms) exists for 9 Orbitrap runs and gives
6 pass / 3 fail. Self-reference scoring (ours vs the search engine's own reported areas and RT) is
available for the whole corpus and costs no re-search. It is only worth having if it AGREES with the
trace-level verdict on the runs where both exist. This script asks that, and is written to be able
to answer "no".

WHY THE SHIPPED VERDICT IS ALREADY KNOWN TO BE INSUFFICIENT. selfref_verdict gates on a robust CV --
scatter of (our area / engine area). Calibration run FL010424_He50ng-DiaW22_90m has a trace verdict of
fail:ms2_area=1.91 (a systematic 1.91x magnitude error) and scores ms2_cv 0.078: it passes cleanly. A
constant scale factor leaves scatter untouched, so CV structurally cannot see the defect that every
observed trace-level failure actually has. The median RATIO can, but its absolute scale is arbitrary
(our extraction window vs the engine's integration bounds), so no threshold is defensible a priori --
it must come from the corpus distribution. That is what this measures.

WHAT WOULD MAKE THE GATE USABLE, stated before looking so the bar is not moved afterwards:
  1. The 3 known failures must sit outside the bulk of the population on some metric.
  2. A threshold separating them from the 6 known passes must flag a SMALL fraction of the 491
     unlabelled runs. A rule that flags 40% of the corpus is not a quality gate, it is noise.
  3. n=9 is tiny and 3 failures cannot support a precise threshold. Any rule derived here is
     provisional and must be reported as such.

    python ingest/selfref_calibrate.py [--flag-budget 0.10]
"""
import argparse
import functools
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
print = functools.partial(print, flush=True)   # noqa: A001

METRICS = [("selfref_ms2_ratio", "MS2 area ratio"), ("selfref_ms1_ratio", "MS1 area ratio"),
           ("selfref_ms2_cv", "MS2 scatter (CV)"), ("selfref_ms1_cv", "MS1 scatter (CV)"),
           ("selfref_rt_delta_s", "RT delta (s)")]


def _conn():
    import psycopg2
    from refresh_leaderboards import _token
    return psycopg2.connect(
        host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
        dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
        user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
        password=_token(), sslmode="require", connect_timeout=30,
        options="-c statement_timeout=600000")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--flag-budget", type=float, default=0.10,
                    help="max fraction of unlabelled runs a usable rule may flag")
    a = ap.parse_args()

    conn = _conn()
    q = conn.cursor()
    cols = ", ".join(m for m, _ in METRICS)
    q.execute(f"""SELECT run, agree_verdict, selfref_verdict, selfref_n, {cols}
                  FROM delimp_xic_trace_lane WHERE selfref_scored_at IS NOT NULL""")
    rows = q.fetchall()
    conn.close()
    if not rows:
        sys.exit("no self-reference scores recorded yet")

    labelled = [r for r in rows if r[1] and r[1] not in ("unscored",)
                and not str(r[1]).startswith("selfref")]
    unlabelled = [r for r in rows if r not in labelled]
    print(f"{len(rows)} scored runs: {len(labelled)} with a trace-level verdict, "
          f"{len(unlabelled)} without\n")
    if not labelled:
        sys.exit("no run carries BOTH a trace verdict and a selfref score — cannot calibrate")

    print("CALIBRATION SET (trace-level truth vs the scalar metrics)")
    hdr = "  %-38s %-22s" % ("run", "trace verdict")
    for _, lab in METRICS:
        hdr += "%15s" % lab.split(" (")[0][:14]
    print(hdr)
    for r in sorted(labelled, key=lambda x: str(x[1])):
        line = "  %-38s %-22s" % (str(r[0])[:38], str(r[1])[:22])
        for k in range(len(METRICS)):
            v = r[4 + k]
            line += "%15s" % ("  --  " if v is None else f"{v:.4g}")
        print(line)

    passes = [r for r in labelled if r[1] == "pass"]
    fails = [r for r in labelled if str(r[1]).startswith("fail")]
    print(f"\n  {len(passes)} pass / {len(fails)} fail in the calibration set")
    if not fails or not passes:
        print("  cannot separate: the calibration set has only one class")
        return

    print("\nSEPARATION — does any single metric put the known failures outside the passes?")
    print("  %-20s %-26s %-26s %s" % ("metric", "pass range", "fail values", "separable?"))
    usable = []
    for k, (col, lab) in enumerate(METRICS):
        pv = [r[4 + k] for r in passes if r[4 + k] is not None]
        fv = [r[4 + k] for r in fails if r[4 + k] is not None]
        if len(pv) < 2 or not fv:
            print("  %-20s %-26s %-26s insufficient data" % (lab, "-", "-"))
            continue
        lo, hi = min(pv), max(pv)
        outside = [v for v in fv if v < lo or v > hi]
        ok = len(outside) == len(fv)
        print("  %-20s %-26s %-26s %s"
              % (lab, f"{lo:.4g} .. {hi:.4g}",
                 ", ".join(f"{v:.4g}" for v in fv),
                 "YES all outside" if ok else f"no ({len(outside)}/{len(fv)} outside)"))
        if outside:
            usable.append((col, lab, lo, hi, fv, len(outside), len(fv)))

    if not usable:
        print("\nVERDICT: no metric separates the known failures from the known passes.")
        print("  Self-reference cannot stand in for trace-level scoring. Do not promote.")
        return

    print("\nCOST OF A RULE — what fraction of the unlabelled corpus would each rule flag?")
    print(f"  (a usable rule must stay under the {a.flag_budget:.0%} budget)")
    for col, lab, lo, hi, fv, n_out, n_f in usable:
        k = [i for i, (c, _) in enumerate(METRICS) if c == col][0]
        uv = np.asarray([r[4 + k] for r in unlabelled if r[4 + k] is not None], float)
        if uv.size == 0:
            continue
        flagged = int(((uv < lo) | (uv > hi)).sum())
        frac = flagged / uv.size
        ok = frac <= a.flag_budget and n_out == n_f
        print("  %-20s outside [%.4g, %.4g] flags %d/%d unlabelled (%.1f%%)  %s"
              % (lab, lo, hi, flagged, uv.size, 100 * frac,
                 "USABLE" if ok else "too broad" if frac > a.flag_budget else "partial"))
        p = np.percentile(uv, [1, 5, 25, 50, 75, 95, 99])
        print("       population: p1 %.4g  p5 %.4g  p25 %.4g  med %.4g  p75 %.4g  p95 %.4g  p99 %.4g"
              % tuple(p))

    print(f"\nn={len(labelled)} labelled runs ({len(fails)} failures). Any threshold from this is "
          f"PROVISIONAL:\n  3 failures cannot pin a boundary, and none of them is a shape defect, "
          f"which this tier\n  cannot see at all. Treat a passing selfref score as 'magnitude and "
          f"timing look normal',\n  never as 'the chromatogram is correct'.")


if __name__ == "__main__":
    main()
