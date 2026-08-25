"""fix_instrument_labels.py — collapse the instrument model/serial variants in raw_files.

One physical instrument was reaching raw_files under several labels (leading whitespace, a serial
zero-padding variant, a serial case variant, and one serial carrying three different model names).
Every per-instrument aggregate was silently split as a result. `instrument_labels.normalize()` is
the single definition of correct; corpus_ingest applies it on write, and this applies it to the
history.

Idempotent: normalize() is a pure function and a second run changes nothing.

    python fix_instrument_labels.py            # dry run: show exactly what would change
    python fix_instrument_labels.py --apply
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from instrument_labels import normalize, platform_for_model  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    import corpus_ingest as ci
    conn = ci._conn()
    cur = conn.cursor()
    cur.execute("SET statement_timeout='300s'")

    cur.execute("""SELECT instrument_model, instrument_serial, COUNT(*)
                   FROM raw_files
                   WHERE instrument_model IS NOT NULL OR instrument_serial IS NOT NULL
                   GROUP BY 1, 2 ORDER BY 3 DESC""")
    pairs = cur.fetchall()

    changes, unchanged, n_rows = [], 0, 0
    for model, serial, n in pairs:
        new_m, new_s = normalize(model, serial)
        if (new_m, new_s) != (model, serial):
            changes.append((model, serial, new_m, new_s, n)); n_rows += n
        else:
            unchanged += 1

    print(f"{len(pairs)} distinct (model, serial) pairs; {len(changes)} need normalising, "
          f"{unchanged} already canonical")
    if changes:
        print(f"\n{'rows':>7}  {'from':<62} {'to'}")
        for m, s, nm, ns, n in changes:
            print(f"{n:>7,}  {str(m)!r} / {str(s)!r}")
            print(f"{'':>7}  {'':<62} -> {str(nm)!r} / {str(ns)!r}")
        print(f"\n{n_rows:,} rows affected")
    else:
        print("labels: already canonical")

    if not a.apply:
        print("\nDRY RUN — re-run with --apply.")
        conn.close(); return

    total = 0
    for m, s, nm, ns, _ in changes:
        # NULL-safe match on the exact pair this change was computed from, so a row that has since
        # changed underneath us is left alone rather than force-written.
        cur.execute("""UPDATE raw_files SET instrument_model=%s, instrument_serial=%s
                       WHERE instrument_model IS NOT DISTINCT FROM %s
                         AND instrument_serial IS NOT DISTINCT FROM %s""", (nm, ns, m, s))
        total += cur.rowcount
    if changes:
        conn.commit()
    print(f"\nlabels: updated {total:,} rows")

    # Verify by re-deriving from the DB, not by trusting the counters above.
    cur.execute("""SELECT instrument_model, instrument_serial, COUNT(*)
                   FROM raw_files WHERE instrument_model IS NOT NULL
                   GROUP BY 1, 2 ORDER BY 3 DESC""")
    after = cur.fetchall()
    print(f"\n=== after: {len(after)} (model, serial) pairs ===")
    for m, s, n in after:
        print(f"  {n:>7,}  {str(m):<32} {s}")
    residual = [(m, s) for m, s, _ in after if normalize(m, s) != (m, s)]
    print(f"\ncheck: pairs still non-canonical after apply: {len(residual)}"
          + (f"  {residual}" if residual else "  (none — clean)"))
    cur.execute("SELECT count(*) FROM raw_files WHERE instrument_model <> btrim(instrument_model)")
    print(f"check: rows with untrimmed whitespace: {cur.fetchone()[0]}")

    # --- platform contradicting an unambiguous model -------------------------------------------
    # Only ever applied where the run has NO ion-mobility range: that is what makes it safe. A
    # genuine timsTOF acquisition always has one, so its absence alongside an Orbitrap model is not
    # a judgement call. A mislabelled model on a real IM-bearing run is left strictly alone.
    cur.execute("""SELECT platform, instrument_model, COUNT(*)
                   FROM raw_files
                   WHERE instrument_model IS NOT NULL AND platform IS NOT NULL
                     AND mobility_min IS NULL
                   GROUP BY 1, 2""")
    plat_fix = [(pl, m, n) for pl, m, n in cur.fetchall()
                if platform_for_model(m) and platform_for_model(m) != pl]
    if plat_fix:
        print("\n=== platform contradicts the model (and the run has no mobility data) ===")
        nfix = 0
        for pl, m, n in plat_fix:
            want = platform_for_model(m)
            print(f"  {n:>5,}  {m:<26} platform {pl!r} -> {want!r}")
            cur.execute("""UPDATE raw_files SET platform=%s
                           WHERE instrument_model=%s AND platform=%s AND mobility_min IS NULL""",
                        (want, m, pl))
            nfix += cur.rowcount
        conn.commit()
        print(f"  updated {nfix:,} rows")
        cur.execute("""SELECT count(*) FROM raw_files
                       WHERE platform='timstof' AND instrument_model LIKE 'Orbitrap%'""")
        print(f"  check: Orbitrap-model rows still on platform=timstof: {cur.fetchone()[0]}")
    else:
        print("\ncheck: no platform/model contradictions")

    try:
        import versions as V
        V.record_run(cur, "fix_instrument_labels", "1.0.0", notes=f"{total} rows, {len(changes)} pairs")
        conn.commit()
    except Exception as e:  # noqa: BLE001
        print(f"[warn] could not record run: {type(e).__name__}: {e}")
    conn.close()


if __name__ == "__main__":
    main()
