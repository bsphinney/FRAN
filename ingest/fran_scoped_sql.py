#!/usr/bin/env python3
"""fran_scoped_sql.py — the Phase 1 gate: does a SQL-SELECTED cohort beat the filename grep?

This is `fran_scoped.py` with EXACTLY ONE THING CHANGED: how the cohort is chosen.

The original selected datasets by `"60spd" in os.path.basename(p).lower()` — a filename substring
that found 16 datasets (horse, manatee, mouse, HeLa and one canine) and scored **10.57 s robust sd**,
against 15.52 s for the whole corpus pooled and 27.42 s for DIA-NN's own predicted iRT.

Here the cohort comes from the run dimension instead:

    delimp_spectrum_lane_runs  (lance_path -> run, built by build_lane_run_index.py)
      JOIN raw_files ON raw_files.raw_basename = run
      WHERE gradient_minutes BETWEEN :lo AND :hi

Everything downstream is byte-for-byte the original: q_value <= 0.01, non-decoy, the same 16 TEST run
ids excluded by substring, mean irt_empirical per (stripped_seq, charge), the same ground truth from
sn21cmp/every_precursor.parquet, IsotonicRegression(out_of_bounds="clip") under
KFold(5, shuffle=True, random_state=0), and robust sd = 1.4826 x MAD. If any of that drifts the
comparison is meaningless.

Two things the SQL cohort can do that the grep cannot:

  1. **Scope by RUN, not by dataset.** Datasets are one per SEARCH and only 218 of 1,552 hold a single
     run, so dataset-level scoping drags in every other run that search happened to contain. Rows are
     filtered to cohort runs here.
  2. **Be complete.** The grep found 16 datasets / 111 runs. The gradient 18-22 min band holds
     **3,480 runs across 559 datasets** — 31x more comparable data, selected on the actual LC
     parameter rather than on whether someone typed "60spd" in a filename.

Usage (compute node):
    python fran_scoped_sql.py --grad-lo 18 --grad-hi 22
    python fran_scoped_sql.py --legacy-grep      # reproduce the original cohort as a control
"""
import argparse
import functools
import glob
import os
import sys

import numpy as np
import pandas as pd
import lance
from sklearn.isotonic import IsotonicRegression
from sklearn.model_selection import KFold

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
print = functools.partial(print, flush=True)  # noqa: A001

G = "/quobyte/proteomics-grp/brett/glendon"
C = "/quobyte/proteomics-grp/brett/sn21cmp"
# The 16 held-out benchmark runs. Unchanged from fran_scoped.py — the leakage guard is a substring
# test against the run name, and `Dog_yeast_entrapment_SN21` IS in the corpus, so dropping this
# makes the result circular and flattering.
TEST = {"21552", "21561", "21524", "21567", "21585", "21533", "21579", "21664",
        "21620", "21673", "21663", "21637", "21779", "21782", "21766", "21783"}


def _conn():
    import psycopg2
    from refresh_leaderboards import _token
    return psycopg2.connect(
        host="pgfarm.library.ucdavis.edu", port=5432,
        dbname="uc-davis-genome-center-proteomics-core/delimp",
        user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
        password=_token(), sslmode="require", connect_timeout=30,
        options="-c statement_timeout=300000")


def sql_cohort(grad_lo, grad_hi, instrument=None):
    """(dataset -> set(cohort runs)) for every run whose gradient sits in the band.

    DISTINCT matters: raw_basename is 1:many against raw_files (a basename recurs across resubmits),
    so without it a run is counted once per duplicate raw_files row."""
    q = """
        SELECT DISTINCT lr.lance_path, lr.run
        FROM delimp_spectrum_lane_runs lr
        JOIN raw_files rf ON rf.raw_basename = lr.run
        WHERE rf.gradient_minutes BETWEEN %s AND %s
    """
    args = [grad_lo, grad_hi]
    if instrument:
        q += " AND rf.instrument_model ILIKE %s"
        args.append(f"%{instrument}%")
    con = _conn(); cur = con.cursor()
    cur.execute(q, args)
    out = {}
    for path, run in cur.fetchall():
        out.setdefault(path, set()).add(run)
    con.close()
    return out


def _rsd(r):
    r = r[np.isfinite(r)]
    return 1.4826 * np.median(np.abs(r - np.median(r)))


def _cv(x, y):
    ok = np.isfinite(x) & np.isfinite(y)
    xi, yi = x[ok], y[ok]
    if len(xi) < 200:
        return np.array([np.nan])
    pr = np.full(len(xi), np.nan)
    for tr, te in KFold(5, shuffle=True, random_state=0).split(xi):
        pr[te] = IsotonicRegression(out_of_bounds="clip").fit(xi[tr], yi[tr]).predict(xi[te])
    return yi - pr


def scan_candidates(cohort, label="cand"):
    """One pass over the cohort keeping EVERY candidate search per (seq, charge, run).

    Scanning once and resolving all selectors from the same candidate set (rather than rescanning
    per selector) is both ~5x faster and strictly safer: every arm then sees byte-identical input, so
    a difference between arms cannot be an artefact of two different scans.

    Returns rk -> list of (q, sn, ics, nf, irt).
    """
    cands = {}
    n_ds = 0
    cols = ["stripped_seq", "charge", "run", "q_value", "irt_empirical", "is_decoy",
            "signal_to_noise", "int_corr_score", "fragment_count"]
    for p in sorted(cohort):
        keep_runs = cohort[p]
        if not os.path.exists(p):
            continue
        try:
            t = lance.dataset(p).scanner(columns=cols,
                                         filter="q_value <= 0.01").to_table().to_pydict()
        except Exception as e:  # noqa: BLE001
            print(f"  skip {os.path.basename(p)}: {str(e)[:60]}")
            continue
        for i in range(len(t["stripped_seq"])):
            if t["is_decoy"][i]:
                continue
            run = str(t["run"][i])
            if any(x in run for x in TEST):
                continue
            if keep_runs is not None and run not in keep_runs:
                continue
            ie = t["irt_empirical"][i]; ch = t["charge"][i]
            if ie is None or ch is None:
                continue
            def _f(col, dflt):
                v = t[col][i]
                return float(v) if v is not None else dflt
            rk = (str(t["stripped_seq"][i]), int(ch), run)
            cands.setdefault(rk, []).append(
                (_f("q_value", 1.0), _f("signal_to_noise", -1.0),
                 _f("int_corr_score", -1.0), _f("fragment_count", -1.0), float(ie)))
        n_ds += 1
        if n_ds % 50 == 0:
            print(f"  [{label}] {n_ds}/{len(cohort)} datasets, {len(cands):,} observations")
    print(f"  [{label}] done: {n_ds} datasets, {len(cands):,} (seq,charge,run) observations")
    return cands


_IDX = {"q": 0, "sn": 1, "ics": 2, "nf": 3}


def resolve(cands, selector, floor_on=None, floor_pct=25.0):
    """Collapse candidates to one observation per (seq,charge,run), then to a per-precursor mean.

    floor_on: apply a MINIMUM percentile on a physical measure before taking best-q. This is the
    fifth arm — it exists because the discriminator showed best-q is typically fine (median 72.7th
    percentile on signal_to_noise) but has a left tail (p25 = 7.7), i.e. ~a quarter of the time it
    keeps a physically weak measurement. The floor is a WITHIN-CANDIDATE PERCENTILE, not an absolute
    threshold, so it never compares a raw score across searches — the same incomparability that made
    raw q suspect in the first place.
    """
    acc = {}
    i = _IDX[selector]
    hib = selector != "q"          # higher-is-better for the physical measures
    fi = _IDX[floor_on] if floor_on else None
    for (seq, ch, _run), cs in cands.items():
        pool = cs
        if fi is not None and len(cs) > 1:
            vals = sorted(c[fi] for c in cs)
            cut = vals[min(len(vals) - 1, int(len(vals) * floor_pct / 100.0))]
            kept = [c for c in cs if c[fi] >= cut]
            pool = kept or cs      # never empty out an observation
        best = max(pool, key=lambda c: c[i]) if hib else min(pool, key=lambda c: c[i])
        k = (seq, ch)
        s, n = acc.get(k, (0.0, 0))
        acc[k] = (s + best[4], n + 1)
    return acc


def selector_compare(a):
    """Score all four collapse selectors ON THE INTERSECTION OF WHAT ALL FOUR COVER.

    WHY NOT "lowest robust sd wins". Because the selectors CHANGE THE ROW SET: a selector that keeps
    fewer, easier precursors wins on robust sd for free. That is the same composition effect that made
    the cohort work look like a 29% methodology win when it was 6% — and judging selectors on a
    benchmark whose rows they themselves determine would repeat it a fourth time.

    So this is a TWO-NUMBER decision, deliberately not collapsed into one:
      * accuracy  — robust sd on the rows ALL FOUR selectors cover (composition held fixed)
      * reach     — how many precursors each covers, reported separately

    Neither number alone decides it; a selector that is 0.1 s better on common rows while covering
    20% fewer precursors is not obviously better, and that trade should be visible rather than
    silently resolved by whichever metric was printed.
    """
    cohort = sql_cohort(a.grad_lo, a.grad_hi, a.instrument)
    print(f"cohort: {len(cohort)} datasets / {sum(len(v) for v in cohort.values())} runs")
    cands = scan_candidates(cohort)
    ARMS = [
        ("q",        dict(selector="q")),
        ("sn",       dict(selector="sn")),
        ("ics",      dict(selector="ics")),
        ("nf",       dict(selector="nf")),
        # fifth arm: best-q, but only among candidates clearing a 25th-percentile floor on
        # signal_to_noise. Targets the left tail the discriminator exposed (q-winner S/N p25 = 7.7).
        ("q|sn>=p25",  dict(selector="q", floor_on="sn", floor_pct=25.0)),
        ("q|ics>=p25", dict(selector="q", floor_on="ics", floor_pct=25.0)),
    ]
    accs = {name: resolve(cands, **kw) for name, kw in ARMS}
    for name in accs:
        print(f"  arm {name:12s} covers {len(accs[name]):,} precursors")

    sn = pd.read_parquet(f"{C}/every_precursor.parquet")
    sn = sn[(sn.sn_is_decoy == False) & (sn.sn_qvalue <= 0.01) & sn.our_pid.notna()].copy()  # noqa: E712
    sn["our_rt"] = sn["sn_apex_rt_s"] + sn["our_delta_rt_to_sn"]
    sn = sn[np.isfinite(sn.our_rt)]
    sn["k"] = list(zip(sn.sn_stripped_seq.astype(str), sn.sn_charge.astype(int)))
    for sel, acc in accs.items():
        sn[sel] = [acc[k][0] / acc[k][1] if k in acc else np.nan for k in sn.k]

    right = sn.our_delta_rt_to_sn.abs() <= 10
    common = right.copy()
    for sel in accs:
        common &= np.isfinite(sn[sel])

    print("\n" + "=" * 72)
    print("SELECTOR COMPARISON — accuracy on COMMON rows, reach reported separately")
    print("=" * 72)
    print(f"{'arm':12s} {'common n':>9s} {'robust_sd':>10s} | {'own n':>8s} {'own sd':>9s}")
    for sel in accs:
        sub = sn[common]
        r = _cv(sub[sel].to_numpy(float), sub["our_rt"].to_numpy(float))
        own = sn[right & np.isfinite(sn[sel])]
        ro = _cv(own[sel].to_numpy(float), own["our_rt"].to_numpy(float))
        sd_c = _rsd(r) if np.any(np.isfinite(r)) else float("nan")
        sd_o = _rsd(ro) if np.any(np.isfinite(ro)) else float("nan")
        print(f"{sel:12s} {int(common.sum()):>9,} {sd_c:9.2f}s | {len(own):>8,} {sd_o:8.2f}s")
    print("\nLeft block is the comparable one (identical rows). The right block is each selector's")
    print("own reach and is NOT comparable across rows — it is there so a coverage/accuracy trade is")
    print("visible rather than hidden inside a single ranking.")


def compare(a):
    """Score the grep cohort and the SQL cohort ON THE SAME ROWS.

    WHY THIS MODE EXISTS. The headline "10.57 s -> 7.52 s" compares n=4,115 against n=10,039 — two
    different row sets, because the wider cohort covers more precursors. A robust sd computed over
    more (and different) precursors is not directly comparable to one over fewer: the extra
    precursors are the ones the grep cohort could not predict at all, and they are not guaranteed to
    be equally easy. So the improvement was real in direction but unquantified in size.

    Here both cohorts are evaluated on the INTERSECTION of what they cover, under one identical fit,
    so the difference is attributable to cohort selection and nothing else. The union/each-alone
    numbers are printed too, because coverage is itself a result — a prior that covers 2.4x more
    precursors is more useful even at equal per-precursor accuracy.

    NOT CLOSED HERE: the DIA-NN 2.6 baseline (27.42 s) was measured on the grep cohort's 4,115
    right-peak rows, and the truth parquet carries no DIA-NN predicted-iRT column, so it cannot be
    re-scored on this row set from FRAN's side. Any "% better than DIA-NN" figure therefore mixes row
    sets. Direction is safe (DIA-NN predicts every precursor, so its coverage does not shrink), but
    the margin is not established. Closing it needs DIA-NN's predicted iRT joined to these keys.
    """
    print("=== cohort A: legacy filename grep (60spd) ===")
    setsA = [p for p in sorted(glob.glob(f"{G}/spectra_lance/*.lance"))
             if "60spd" in os.path.basename(p).lower()]
    cohortA = {p: None for p in setsA}
    print(f"  {len(cohortA)} datasets")
    accA = scan_cohort(cohortA, "grep")

    print(f"\n=== cohort B: SQL, gradient {a.grad_lo}-{a.grad_hi} min ===")
    cohortB = sql_cohort(a.grad_lo, a.grad_hi, a.instrument)
    print(f"  {len(cohortB)} datasets / {sum(len(v) for v in cohortB.values())} runs")
    accB = scan_cohort(cohortB, "sql")

    print("\n=== cohort B, SEARCH-DEDUPLICATED (one physical acquisition = n 1) ===")
    accBd = scan_cohort(cohortB, "sql-dedup", dedup_runs=True)

    sn = pd.read_parquet(f"{C}/every_precursor.parquet")
    sn = sn[(sn.sn_is_decoy == False) & (sn.sn_qvalue <= 0.01) & sn.our_pid.notna()].copy()  # noqa: E712
    sn["our_rt"] = sn["sn_apex_rt_s"] + sn["our_delta_rt_to_sn"]
    sn = sn[np.isfinite(sn.our_rt)]
    sn["k"] = list(zip(sn.sn_stripped_seq.astype(str), sn.sn_charge.astype(int)))
    sn["A"] = [accA[k][0] / accA[k][1] if k in accA else np.nan for k in sn.k]
    sn["B"] = [accB[k][0] / accB[k][1] if k in accB else np.nan for k in sn.k]
    sn["Bd"] = [accBd[k][0] / accBd[k][1] if k in accBd else np.nan for k in sn.k]

    right = sn.our_delta_rt_to_sn.abs() <= 10          # the "right-peak only" subset, as in the original
    covA, covB = np.isfinite(sn.A), np.isfinite(sn.B)
    both = covA & covB

    print("\n" + "=" * 74)
    print("SAME-ROWS COMPARISON (right-peak only, one identical isotonic 5-fold fit)")
    print("=" * 74)
    rows = [
        ("A grep,  rows A covers",       sn[right & covA], "A"),
        ("B SQL,   rows A covers",       sn[right & covA], "B"),
        ("A grep,  rows BOTH cover",     sn[right & both], "A"),
        ("B SQL,   rows BOTH cover",     sn[right & both], "B"),
        ("B SQL,   rows B covers",       sn[right & covB], "B"),
        ("Bd SQL dedup, rows A covers",  sn[right & covA], "Bd"),
        ("Bd SQL dedup, rows B covers",  sn[right & covB], "Bd"),
    ]
    print(f"{'variant':34s} {'n':>7s} {'robust_sd':>10s} {'med|r|':>9s}")
    for nm, sub, col in rows:
        r = _cv(sub[col].to_numpy(float), sub["our_rt"].to_numpy(float))
        if np.all(~np.isfinite(r)):
            print(f"{nm:34s} {len(sub):>7d}   (too few)"); continue
        print(f"{nm:34s} {len(sub):>7d} {_rsd(r):9.2f}s {np.median(np.abs(r[np.isfinite(r)])):8.2f}s")

    print(f"\ncoverage (right-peak rows): grep {int((right & covA).sum()):,}  "
          f"SQL {int((right & covB).sum()):,}  both {int((right & both).sum()):,}")
    print("\nRead the two 'rows BOTH cover' lines against each other -- that is the only pair that\n"
          "differs solely by cohort selection. The DIA-NN 27.42 s baseline is NOT on this row set\n"
          "and is deliberately not printed here; see this function's docstring.")


# Collapse selectors. q is MINIMISED, the physical measures are MAXIMISED.
#
# Measured 2026-07-30, five multi-search runs: best-q's top winner takes a mean 54.0% of a run's
# peptides where an even split would be 10.2% — 5.3x concentrated, so it IS substantially selecting a
# search rather than an observation. BUT the proposed mechanism, bigger library => more permissive =>
# lower q, is NOT supported: in 5 of 5 runs the best-q winner was not the largest library (winners sat
# mid-range, 61k-72k, against a 24k-110k spread).
#
# The selectors also disagree materially — a physical measure picked a different winning search in 13
# of 15 comparisons, while signal_to_noise and int_corr_score usually agreed with EACH OTHER. So the
# choice changes which observation is kept and is not cosmetic. Rather than argue it, make it a flag
# and let the benchmark decide.
SELECTORS = {
    "q":   ("q_value",         False),   # statistical: relative to each search's own FDR model
    "sn":  ("signal_to_noise", True),    # physical: property of the measurement
    "ics": ("int_corr_score",  True),    # physical: Spectronaut's own correlation score
    "nf":  ("fragment_count",  True),    # physical: how much evidence was present
}
_BASE_COLS = ["stripped_seq", "charge", "run", "q_value", "irt_empirical", "is_decoy"]


def _cols_for(selector):
    col = SELECTORS[selector][0]
    return _BASE_COLS if col in _BASE_COLS else _BASE_COLS + [col]


def scan_cohort(cohort, label, dedup_runs=False, selector="q"):
    """Build the (seq, charge) -> (irt_sum, n) consensus for one cohort.

    dedup_runs=False reproduces fran_scoped.py exactly: every Lance row is one observation.

    dedup_runs=True collapses the SEARCH dimension first, so one physical acquisition contributes
    n=1 regardless of how many searches covered it. This matters more than it sounds:

      * The same run appears in multiple Lance datasets because the same raw was searched several
        times -- 1.34x corpus-wide, 1.40x in the gate cohort, up to 14 datasets for one run
        (datasets == search_ids exactly, so they are distinct searches, not duplicate storage).
      * Those searches DISAGREE. Measured on two 14-dataset runs: only 4.9-5.6% of precursors have
        an identical irt_empirical across all of them; median spread 0.50 iRT, p90 3.5-4.5, max 84.

    So without dedup, n is overstated AND the consensus is pulled toward whichever acquisitions
    happened to be searched most often, using genuinely different values. That is a biased estimator,
    not just an inflated count.

    The collapse rule is BEST-Q, not mean-across-searches. Averaging would blend different libraries'
    iRT conventions inside a single acquisition -- re-introducing, at run level, exactly the
    cross-run comparability problem irt_calibration_source exists to expose.
    """
    acc = {}
    # (seq, charge, run) -> {"q": best q, "irt": irt of the best-q search, "src": winning dataset,
    #                        "vals": [irt from every search], }
    # `vals` is kept so the CROSS-SEARCH SPREAD survives the collapse. It is a per-observation
    # confidence signal nothing else can supply — a peptide whose iRT is stable across 14 searches is
    # more trustworthy than one varying by 84 iRT units — and it is destroyed the moment you pick a
    # winner. Capturing it costs one pass; recovering it later costs a 137 GB rescan.
    best = {}
    n_ds = 0
    sel_col, sel_max = SELECTORS[selector]
    for p in sorted(cohort):
        keep_runs = cohort[p]
        if not os.path.exists(p):
            continue
        try:
            t = lance.dataset(p).scanner(
                columns=_cols_for(selector),
                filter="q_value <= 0.01").to_table().to_pydict()
        except Exception as e:  # noqa: BLE001
            print(f"  skip {os.path.basename(p)}: {str(e)[:60]}")
            continue
        for i in range(len(t["stripped_seq"])):
            if t["is_decoy"][i]:
                continue
            run = str(t["run"][i])
            if any(x in run for x in TEST):
                continue
            if keep_runs is not None and run not in keep_runs:
                continue
            ie = t["irt_empirical"][i]; ch = t["charge"][i]
            if ie is None or ch is None:
                continue
            k = (str(t["stripped_seq"][i]), int(ch))
            if dedup_runs:
                sc = t[sel_col][i]
                sc = float(sc) if sc is not None else (1.0 if not sel_max else -1.0)
                score = -sc if sel_max else sc     # normalise to "lower is better"
                rk = (k[0], k[1], run)
                e = best.get(rk)
                if e is None:
                    best[rk] = {"score": score, "irt": float(ie), "src": os.path.basename(p),
                                "vals": [float(ie)]}
                else:
                    e["vals"].append(float(ie))
                    if score < e["score"]:
                        e["score"] = score; e["irt"] = float(ie); e["src"] = os.path.basename(p)
            else:
                s, c = acc.get(k, (0.0, 0)); acc[k] = (s + float(ie), c + 1)
        n_ds += 1
        if n_ds % 50 == 0:
            print(f"  [{label}] {n_ds}/{len(cohort)} datasets, "
                  f"{len(best) if dedup_runs else len(acc):,} seen")
    if dedup_runs:
        spreads = []
        multi = 0
        for (seq, ch, _run), e in best.items():
            k = (seq, ch)
            s, c = acc.get(k, (0.0, 0)); acc[k] = (s + e["irt"], c + 1)
            if len(e["vals"]) > 1:
                multi += 1
                spreads.append(max(e["vals"]) - min(e["vals"]))
        print(f"  [{label}] dedup: {len(best):,} (seq,charge,run) observations "
              f"-> {len(acc):,} precursors")
        if spreads:
            spreads.sort(); n = len(spreads)
            stable = sum(1 for s in spreads if s <= 1.0)
            print(f"  [{label}] cross-search spread on the {multi:,} multi-search observations: "
                  f"median {spreads[n // 2]:.3f}  p90 {spreads[int(n * 0.9)]:.3f}  "
                  f"max {spreads[-1]:.3f}")
            print(f"  [{label}]   within 1.0 iRT across searches: {stable:,}/{n:,} "
                  f"({100 * stable / n:.1f}%)  <- the free confidence signal")
    print(f"  [{label}] done: {n_ds} datasets, {len(acc):,} covered precursors")
    return acc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grad-lo", type=float, default=18.0)
    ap.add_argument("--grad-hi", type=float, default=22.0)
    ap.add_argument("--instrument", default=None, help='e.g. "timsTOF HT"')
    ap.add_argument("--legacy-grep", action="store_true", help="reproduce the original 60spd cohort")
    ap.add_argument("--max-datasets", type=int, default=0, help="cap datasets (smoke test)")
    ap.add_argument("--selector-compare", action="store_true",
                    help="score all four collapse selectors on the rows ALL of them cover")
    ap.add_argument("--compare", action="store_true",
                    help="score BOTH cohorts on the SAME rows (see the note in --compare's output)")
    a = ap.parse_args()

    if a.selector_compare:
        return selector_compare(a)
    if a.compare:
        return compare(a)

    if a.legacy_grep:
        sets = [p for p in sorted(glob.glob(f"{G}/spectra_lance/*.lance"))
                if "60spd" in os.path.basename(p).lower()]
        cohort = {p: None for p in sets}   # None => accept every run in the dataset (original behaviour)
        print(f"COHORT = legacy filename grep: {len(cohort)} datasets")
    else:
        cohort = sql_cohort(a.grad_lo, a.grad_hi, a.instrument)
        nruns = sum(len(v) for v in cohort.values())
        print(f"COHORT = SQL, gradient {a.grad_lo}-{a.grad_hi} min"
              + (f", instrument ~ {a.instrument}" if a.instrument else "")
              + f": {len(cohort)} datasets / {nruns} runs")
    if a.max_datasets:
        cohort = dict(list(cohort.items())[:a.max_datasets])
        print(f"  capped to {len(cohort)} datasets")

    acc = {}
    n_ds = 0
    for p in sorted(cohort):
        keep_runs = cohort[p]
        if not os.path.exists(p):
            continue
        try:
            t = lance.dataset(p).scanner(
                columns=["stripped_seq", "charge", "run", "q_value", "irt_empirical", "is_decoy"],
                filter="q_value <= 0.01").to_table().to_pydict()
        except Exception as e:  # noqa: BLE001
            print(f"  skip {os.path.basename(p)}: {str(e)[:60]}")
            continue
        n = 0
        for i in range(len(t["stripped_seq"])):
            if t["is_decoy"][i]:
                continue
            run = str(t["run"][i])
            if any(x in run for x in TEST):        # leakage guard, unchanged
                continue
            if keep_runs is not None and run not in keep_runs:
                continue                            # run-level scoping (the new part)
            ie = t["irt_empirical"][i]; ch = t["charge"][i]
            if ie is None or ch is None:
                continue
            k = (str(t["stripped_seq"][i]), int(ch))
            s, c = acc.get(k, (0.0, 0)); acc[k] = (s + float(ie), c + 1)
            n += 1
        n_ds += 1
        if n_ds % 25 == 0:
            print(f"  [{n_ds}/{len(cohort)}] {len(acc):,} covered precursors")
    print(f"datasets scanned: {n_ds}   consensus precursors: {len(acc):,}")

    sn = pd.read_parquet(f"{C}/every_precursor.parquet")
    sn = sn[(sn.sn_is_decoy == False) & (sn.sn_qvalue <= 0.01) & sn.our_pid.notna()].copy()  # noqa: E712
    sn["our_rt"] = sn["sn_apex_rt_s"] + sn["our_delta_rt_to_sn"]
    sn = sn[np.isfinite(sn.our_rt)]
    sn["k"] = list(zip(sn.sn_stripped_seq.astype(str), sn.sn_charge.astype(int)))
    sn["scoped"] = [acc[k][0] / acc[k][1] if k in acc else np.nan for k in sn.k]
    sn["nobs"] = [acc[k][1] if k in acc else 0 for k in sn.k]

    cov = np.isfinite(sn.scoped)
    print(f"\ncoverage of the SN-confident precursors: {cov.sum()} ({100 * cov.mean():.1f}%)")
    print(f"observations per covered precursor: median {int(np.median(sn.nobs[cov])) if cov.sum() else 0}, "
          f"max {int(sn.nobs.max())}")

    def rsd(r):
        r = r[np.isfinite(r)]
        return 1.4826 * np.median(np.abs(r - np.median(r)))

    def cv(x, y):
        ok = np.isfinite(x) & np.isfinite(y)
        xi, yi = x[ok], y[ok]
        if len(xi) < 200:
            return np.array([np.nan])
        pr = np.full(len(xi), np.nan)
        for tr, te in KFold(5, shuffle=True, random_state=0).split(xi):
            pr[te] = IsotonicRegression(out_of_bounds="clip").fit(xi[tr], yi[tr]).predict(xi[te])
        return yi - pr

    print("\n%-44s %7s %10s %10s" % ("predictor (5-fold held out)", "n", "robust_sd", "med|r|"))
    sub = sn[cov]
    for nm, x, s in (("FRAN scoped (this cohort)", sub.scoped.to_numpy(float), sub),
                     ("  same, n_obs >= 2", sub[sub.nobs >= 2].scoped.to_numpy(float), sub[sub.nobs >= 2]),
                     ("  same, right-peak only",
                      sub[sub.our_delta_rt_to_sn.abs() <= 10].scoped.to_numpy(float),
                      sub[sub.our_delta_rt_to_sn.abs() <= 10])):
        r = cv(x, s["our_rt"].to_numpy(float))
        if np.all(~np.isfinite(r)):
            print("%-44s %7d   (too few)" % (nm, len(s))); continue
        print("%-44s %7d %9.2fs %9.2fs" % (nm, len(s), rsd(r), np.median(np.abs(r[np.isfinite(r)]))))

    print("\nreference (same fit, same held-out split):")
    print("   FRAN scoped to the 60spd filename grep   10.57 s   <- the number to beat")
    print("   FRAN pooled over all 1,552 runs          15.52 s")
    print("   DIA-NN 2.6 predicted iRT                 27.42 s")
    print("   DIA-NN 2.6.1 within-run                  16.7  s")
    print("   Spectronaut 21 within-run                 7.4  s")


if __name__ == "__main__":
    main()
