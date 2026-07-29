#!/usr/bin/env python3
"""Does scoping FRAN to LC-comparable runs recover Spectronaut's precision?

FRAN is >90% Spectronaut, and SN achieves 7.4 s WITHIN a run. Pooled across all 1,552 corpus runs
(bat, manatee, yeast IP, 2021 Orbitrap ... 2026 timsTOF) the consensus gives 17.8 s. Hypothesis:
the loss is cross-condition pooling, not the source.

Test: build the consensus from the 16 '60spd' runs only -- same gradient family as the benchmark,
and none of them is in the 16-run TEST set, so no leakage. Compare against the all-runs number.

Small on purpose: 16 datasets, not 1,552.
"""
import numpy as np, pandas as pd, lance, glob, re, os, functools
print = functools.partial(print, flush=True)
from sklearn.isotonic import IsotonicRegression
from sklearn.model_selection import KFold

G = "/quobyte/proteomics-grp/brett/glendon"
C = "/quobyte/proteomics-grp/brett/sn21cmp"
TEST = {"21552", "21561", "21524", "21567", "21585", "21533", "21579", "21664",
        "21620", "21673", "21663", "21637", "21779", "21782", "21766", "21783"}

sets = [p for p in sorted(glob.glob(f"{G}/spectra_lance/*.lance"))
        if "60spd" in os.path.basename(p).lower()]
print("LC-comparable datasets (60spd): %d" % len(sets))

acc = {}
for p in sets:
    try:
        t = lance.dataset(p).scanner(
            columns=["stripped_seq", "charge", "run", "q_value", "irt_empirical", "is_decoy"],
            filter="q_value <= 0.01").to_table().to_pydict()
    except Exception as e:
        print("  skip %s: %s" % (os.path.basename(p), e)); continue
    n = 0
    for i in range(len(t["stripped_seq"])):
        if t["is_decoy"][i]: continue
        run = str(t["run"][i])
        if any(x in run for x in TEST): continue          # leakage guard
        ie = t["irt_empirical"][i]; ch = t["charge"][i]
        if ie is None or ch is None: continue
        k = (str(t["stripped_seq"][i]), int(ch))
        s, c = acc.get(k, (0.0, 0)); acc[k] = (s + float(ie), c + 1)
        n += 1
    print("  %-52s +%d" % (os.path.basename(p)[:52], n))
print("consensus precursors: %d" % len(acc))

sn = pd.read_parquet(f"{C}/every_precursor.parquet")
sn = sn[(sn.sn_is_decoy == False) & (sn.sn_qvalue <= 0.01) & sn.our_pid.notna()].copy()
sn["our_rt"] = sn["sn_apex_rt_s"] + sn["our_delta_rt_to_sn"]
sn = sn[np.isfinite(sn.our_rt)]
sn["k"] = list(zip(sn.sn_stripped_seq.astype(str), sn.sn_charge.astype(int)))
sn["scoped"] = [acc[k][0] / acc[k][1] if k in acc else np.nan for k in sn.k]
sn["nobs"] = [acc[k][1] if k in acc else 0 for k in sn.k]

cov = np.isfinite(sn.scoped)
print("\ncoverage of the 15,273 SN-confident precursors: %d (%.1f%%)" % (cov.sum(), 100 * cov.mean()))
print("observations per covered precursor: median %d, max %d"
      % (np.median(sn.nobs[cov]) if cov.sum() else 0, sn.nobs.max()))


def rsd(r):
    r = r[np.isfinite(r)]
    return 1.4826 * np.median(np.abs(r - np.median(r)))


def cv(x, y):
    ok = np.isfinite(x) & np.isfinite(y)
    xi, yi = x[ok], y[ok]
    if len(xi) < 200: return np.array([np.nan])
    p = np.full(len(xi), np.nan)
    for tr, te in KFold(5, shuffle=True, random_state=0).split(xi):
        p[te] = IsotonicRegression(out_of_bounds="clip").fit(xi[tr], yi[tr]).predict(xi[te])
    return yi - p


print("\n%-44s %7s %10s %10s" % ("predictor (5-fold held out)", "n", "robust_sd", "med|r|"))
sub = sn[cov]
for nm, x, s in (("FRAN scoped to 60spd runs", sub.scoped.to_numpy(float), sub),
                 ("  same, n_obs >= 2", sub[sub.nobs >= 2].scoped.to_numpy(float), sub[sub.nobs >= 2]),
                 ("  same, right-peak only", sub[sub.our_delta_rt_to_sn.abs() <= 10].scoped.to_numpy(float),
                  sub[sub.our_delta_rt_to_sn.abs() <= 10])):
    r = cv(x, s["our_rt"].to_numpy(float))
    if np.all(~np.isfinite(r)):
        print("%-44s %7d   (too few)" % (nm, len(s))); continue
    print("%-44s %7d %9.2fs %9.2fs" % (nm, len(s), rsd(r), np.median(np.abs(r[np.isfinite(r)]))))

print("\nreference, same cohort and fit:")
print("   FRAN pooled over all 1,552 runs   17.8 s")
print("   DIA-NN 2.6 predicted iRT          27.9 s")
print("   perfect predictor (SN apex)       16.0 s all / 0.65 s right-peak only")
print("   Spectronaut within-run             7.4 s")
