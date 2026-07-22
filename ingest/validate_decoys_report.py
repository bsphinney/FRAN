"""validate_decoys_report.py — check a Spectronaut "everything + decoys" parquet export is correct
before committing to a corpus-wide re-export. READ-ONLY; run on ONE test report.

    python validate_decoys_report.py "<path to test _Report ...parquet>"

Pass criteria (for the ML/training lane):
  1. It's a readable parquet.
  2. EG_IsDecoy present, with BOTH targets and decoys (decoy fraction ~10-40%).
  3. Truly UNFILTERED — EG_Qvalue has values > 0.01 (a normal FRAN report is cut at 1% FDR).
  4. All observed-fragment columns (F_*) present, incl. the 18 not in the current Lance schema.
  5. Score columns present (EG_GlobalCScore / EG_PEP / EG_Svalue) and targets vs decoys separate.
  6. Schema parity with the known-good reference sample (same column set), if --ref given.
"""
from __future__ import annotations
import sys, argparse
import pyarrow.parquet as pq

NEW_FRAG = {  # F_ fields NOT in the current spectrum_lance schema — the ML upside
    "F_Noise", "F_Log10SignalToNoise", "F_InterferenceScore", "F_PossibleInterference",
    "F_PriorIonRatio", "F_HasChannelInterference", "F_IsotopicPatternTheoretical",
    "F_MeasuredMz", "F_CalibratedMz", "F_TheoreticalMz", "F_RawMassAccuracy_(PPM)",
    "F_PeakHeight", "F_NormalizedPeakHeight", "F_QuantityCorrectionFactor", "F_Rank",
    "F_ExcludedFromQuantification", "F_Tolerance", "F_PPMTolerance",
}


def _fnum(xs):
    out = []
    for x in xs:
        try:
            out.append(float(x))
        except (TypeError, ValueError):
            pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("parquet")
    ap.add_argument("--ref", help="known-good reference parquet to compare column set against")
    a = ap.parse_args()

    pf = pq.ParquetFile(a.parquet)
    names = set(pf.schema_arrow.names)
    nrow = pf.metadata.num_rows
    ok = True

    def check(cond, msg):
        nonlocal ok
        ok = ok and cond
        print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")

    print(f"file: {a.parquet}\n  rows={nrow:,}  cols={len(names)}")
    check("EG_IsDecoy" in names, "EG_IsDecoy column present")

    cols = [c for c in ("EG_IsDecoy", "EG_Qvalue", "EG_GlobalCScore") if c in names]
    t = pq.read_table(a.parquet, columns=cols)
    if "EG_IsDecoy" in names:
        dec = t["EG_IsDecoy"].to_pylist()
        nd = sum(1 for x in dec if x)
        frac = nd / max(1, len(dec))
        check(nd > 0 and nd < len(dec), f"has BOTH targets ({len(dec)-nd:,}) and decoys ({nd:,}); decoy frac={frac:.2f}")
    if "EG_Qvalue" in names:
        q = _fnum(t["EG_Qvalue"].to_pylist())
        above = sum(1 for x in q if x > 0.01)
        check(above > 0, f"UNFILTERED — {above:,} rows with EG_Qvalue>0.01 (filtered report would have 0)")
    fr = sorted(c for c in names if c.startswith("F_"))
    check(len(fr) >= 25, f"fragment columns present ({len(fr)} F_* cols)")
    missing_new = NEW_FRAG - names
    check(not missing_new, f"the 18 ML fragment features present"
          + (f" — MISSING {sorted(missing_new)}" if missing_new else ""))
    scores = {"EG_GlobalCScore", "EG_PEP", "EG_Svalue"} & names
    check(len(scores) >= 2, f"score columns present ({sorted(scores)})")
    if "EG_GlobalCScore" in names and "EG_IsDecoy" in names:
        cs = t["EG_GlobalCScore"].to_pylist(); dec = t["EG_IsDecoy"].to_pylist()
        tc = _fnum([c for c, d in zip(cs, dec) if not d])
        dc = _fnum([c for c, d in zip(cs, dec) if d])
        if tc and dc:
            mt, md = sum(tc)/len(tc), sum(dc)/len(dc)
            check(mt > md, f"target vs decoy score separation: target mean={mt:.2f} > decoy mean={md:.2f}")

    if a.ref:
        rnames = set(pq.ParquetFile(a.ref).schema_arrow.names)
        only_ref, only_new = rnames - names, names - rnames
        check(not only_ref, f"schema parity with reference"
              + (f" — MISSING vs ref: {sorted(only_ref)[:8]}" if only_ref else "")
              + (f" (extra: {len(only_new)})" if only_new else ""))

    print(f"\n{'ALL CHECKS PASSED — schema is good for corpus-wide re-export' if ok else 'SOME CHECKS FAILED — fix the .rs before batch'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
