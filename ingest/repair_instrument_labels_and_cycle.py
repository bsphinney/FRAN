"""repair_instrument_labels_and_cycle.py — undo two specific defects, both measured.

1. INSTRUMENT LABEL SPLIT, re-introduced 2026-09-24. record_raw_metadata.py wrote the raw vendor
   strings instead of the canonical pair, re-creating the split fix_instrument_labels.py had
   undone: ' timsTOF Pro' (2,428 rows) beside 'timsTOF Pro' (58), and serial '1854399.153' (250)
   beside '1854399.00153'. Two labels for one physical instrument silently breaks every
   per-instrument aggregate. The write path is fixed; this repairs the rows already written.
   Re-normalising is IDEMPOTENT -- normalize() of a canonical pair returns it unchanged -- so this
   is safe to run repeatedly and safe to run over rows that were never wrong.

2. CORRUPT cycle_time_sec ON ORBITRAP ROWS, 17 left. These come from Spectronaut's RunSummaries
   "Cycle Time (MS1)" with a five-decimal comma decimal separator stripped, e.g. 0.74888 s stored
   as 74888. The Bruker pass repairs them by re-deriving from the frames; Thermo cannot -- TRFP's
   -m 0 output carries no scan times -- so COALESCE had nothing to substitute and the bad value
   survived. Repaired here ARITHMETICALLY, and only where the arithmetic lands in a plausible band:
   v/1e5 in [0.1, 20]. Anything else is set NULL rather than guessed, because a wrong cycle time
   silently becomes a wrong points-per-peak.

    python ingest/repair_instrument_labels_and_cycle.py            # dry run
    python ingest/repair_instrument_labels_and_cycle.py --apply
"""
import argparse
import functools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/quobyte/proteomics-grp/brett/glendon/fran_ingest")
import plan_spectrum_backfill as P                      # noqa: E402
from instrument_labels import normalize                 # noqa: E402

print = functools.partial(print, flush=True)            # noqa: A001


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    c = P._conn(); c.autocommit = False; cur = c.cursor()

    # ---- 1. labels -------------------------------------------------------------------------
    cur.execute("""SELECT DISTINCT instrument_model, instrument_serial FROM raw_files
                   WHERE instrument_model IS NOT NULL OR instrument_serial IS NOT NULL""")
    pairs = cur.fetchall()
    fixes = []
    for m, s in pairs:
        nm, ns = normalize(m, s)
        if (nm, ns) != (m, s):
            fixes.append((m, s, nm, ns))
    print(f"{len(pairs)} distinct (model, serial) pairs; {len(fixes)} need canonicalising")
    for m, s, nm, ns in fixes:
        cur.execute("""SELECT count(*) FROM raw_files
                       WHERE instrument_model IS NOT DISTINCT FROM %s
                         AND instrument_serial IS NOT DISTINCT FROM %s""", (m, s))
        n = cur.fetchone()[0]
        print(f"   {str(m)!r:26s} {str(s)!r:18s} -> {nm!r:22s} {ns!r:18s}  ({n:,} rows)")
        if a.apply:
            cur.execute("""UPDATE raw_files SET instrument_model=%s, instrument_serial=%s
                           WHERE instrument_model IS NOT DISTINCT FROM %s
                             AND instrument_serial IS NOT DISTINCT FROM %s""", (nm, ns, m, s))
    if a.apply:
        c.commit()

    # ---- 2. cycle_time_sec ------------------------------------------------------------------
    cur.execute("""SELECT raw_path, platform, cycle_time_sec FROM raw_files
                   WHERE cycle_time_sec > 20 ORDER BY cycle_time_sec DESC""")
    bad = cur.fetchall()
    rescale = nulled = 0
    print(f"\n{len(bad)} rows with an implausible cycle_time_sec (> 20 s)")
    for rp, plat, v in bad:
        cand = v / 1e5
        if 0.1 <= cand <= 20:
            rescale += 1
            print(f"   {v:>12.1f} -> {cand:7.5f} s   [{plat}]")
            if a.apply:
                cur.execute("UPDATE raw_files SET cycle_time_sec=%s WHERE raw_path=%s", (cand, rp))
        else:
            nulled += 1
            print(f"   {v:>12.1f} -> NULL (no rescale lands in range)   [{plat}]")
            if a.apply:
                cur.execute("UPDATE raw_files SET cycle_time_sec=NULL WHERE raw_path=%s", (rp,))
    if a.apply:
        c.commit()
    print(f"\n{rescale} rescaled by 1e5, {nulled} set NULL. apply={a.apply}")
    if not a.apply:
        print("(dry run -- nothing written)")
    c.close()


if __name__ == "__main__":
    main()
