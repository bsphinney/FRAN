"""backfill_raw_metadata_from_siblings.py — give metadata-less raw_files rows the instrument
metadata their siblings already measured.

STEP 4, and deliberately last. Steps 1-3 (resolve_raw_hive_paths --fix-meta, then
record_raw_metadata --only-missing) read the values out of the physical file; this only covers
what is left when no file can be located. A value read from the raw beats a value copied from a
cousin row, so this must never run first -- it would satisfy the NULL and stop the better pass
from ever filling it.

WHY ROWS ARE EMPTY AT ALL. raw_files.raw_path is often SYNTHETIC: when corpus_ingest cannot find
the raw beside the search output it records "<output_dir>/<run>.<ext>", a path that does not
exist. The re-ingest log says so in as many words -- "no .d/.raw found beside .../output --
instrument fields will fall back to COALESCE". Every search that does this mints another row for
the same physical run, which is why one run reached 19 rows and 42% of the table is duplicates.

WHY COPYING IS SAFE HERE, AND WHERE IT IS NOT. Measured 2026-09-23 over 25,233 rows:
  * 0 basenames carry two different instrument_model values. Where two rows both know, they agree.
  * instrument_model and instrument_serial are 100% co-populated (20,159 both, 0 with only one),
    because both come from one header read and _norm_instrument() canonicalises the pair.
So copying is safe. MERGING the rows is NOT: of 51,341 multi-copy basenames, 611 resolve to files
differing by more than 10% in size -- e.g. ExApr_wa.raw exists in apr25/ and apr26/, a QC wash
whose name was reused a year apart. Collapsing on name would attribute one search's run to a
different acquisition. Hence: inherit metadata, never merge rows. Same rule as the .sne duplicates
-- dedupe by name AND corroboration, never by name alone.

    python ingest/backfill_raw_metadata_from_siblings.py              # dry run
    python ingest/backfill_raw_metadata_from_siblings.py --apply
"""
import argparse
import functools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/quobyte/proteomics-grp/brett/glendon/fran_ingest")
import plan_spectrum_backfill as P                              # noqa: E402

print = functools.partial(print, flush=True)                    # noqa: A001

# Copied as a unit. Every one is a property of the PHYSICAL FILE, so it is the same for every row
# naming that file. Deliberately excluded: raw_path/hive_path (row identity), gradient_minutes and
# samples_per_day (derived per-search from the report, not from the raw), xic_*/labeled_mgf_*
# (lane artefacts belonging to one row), and file_size_bytes/md5 (a copy may legitimately differ --
# 4,325 multi-copy basenames differ by <=64KB of sidecar).
FIELDS = ["instrument_model", "instrument_serial", "acquisition_date", "acquisition_method",
          "mass_range_min", "mass_range_max", "mobility_min", "mobility_max",
          "n_ms1_frames", "n_ms2_frames", "cycle_time_sec", "ms1_resolution", "ms2_resolution",
          "activation_method", "lc_method", "instrument_metadata_json"]

# The guard. A basename is only a donor if its non-null values AGREE -- on the model AND on the
# serial. Two serials under one basename means two physical instruments, i.e. two different runs
# sharing a name, and nothing may be copied. Measured as 0 today; it is checked every run because
# "it was zero once" is not a guarantee, and a silent wrong instrument is worse than a NULL.
AMBIGUOUS = """
SELECT raw_basename, array_agg(DISTINCT instrument_model) AS models,
       array_agg(DISTINCT instrument_serial) AS serials
FROM raw_files
WHERE instrument_model IS NOT NULL OR instrument_serial IS NOT NULL
GROUP BY raw_basename
HAVING count(DISTINCT instrument_model) > 1 OR count(DISTINCT instrument_serial) > 1
"""

DONORS = """
SELECT raw_basename, {cols}
FROM (
  SELECT raw_basename, {picks},
         row_number() OVER (PARTITION BY raw_basename
                            ORDER BY (instrument_model IS NULL), (hive_path IS NULL), raw_path) AS rn
  FROM raw_files WHERE instrument_model IS NOT NULL
) d WHERE rn = 1
"""

RECIPIENTS = """
SELECT raw_path, raw_basename, platform FROM raw_files WHERE instrument_model IS NULL
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--fix-platform", action="store_true",
                    help="also correct platform to match the donor, ONLY where the recipient has "
                         "no mobility data and no metadata of its own (the proven mislabel shape)")
    a = ap.parse_args()

    c = P._conn(); c.autocommit = False; cur = c.cursor()

    cur.execute(AMBIGUOUS)
    amb = cur.fetchall()
    if amb:
        print(f"REFUSING {len(amb):,} ambiguous basename(s) -- donors disagree on model or serial:")
        for b, models, serials in amb[:10]:
            print(f"  {b}: models={models} serials={serials}")
        print("  (these are skipped, not guessed; everything else proceeds)")
    skip = {r[0] for r in amb}

    cols = ", ".join(FIELDS)
    picks = ", ".join(FIELDS)
    cur.execute(DONORS.format(cols=cols, picks=picks))
    donors = {r[0]: r[1:] for r in cur.fetchall()}
    print(f"\n{len(donors):,} basenames can donate metadata")

    cur.execute(RECIPIENTS)
    recips = cur.fetchall()
    print(f"{len(recips):,} rows are missing instrument_model")

    todo = [(rp, b) for rp, b, _plat in recips if b in donors and b not in skip]
    print(f"{len(todo):,} of them have an unambiguous donor  ->  {len(recips) - len(todo):,} stay NULL\n")

    if not a.apply:
        for rp, b in todo[:10]:
            d = donors[b]
            print(f"  would set {b[:44]:44s} model={d[0]} serial={d[1]}")
        print(f"\n(dry run -- nothing written. {len(todo):,} rows would change.)")
        c.close(); return

    import psycopg2.extras
    sets = ", ".join(f"{f} = %s" for f in FIELDS)
    payload = [tuple(donors[b]) + (rp,) for rp, b in todo]
    psycopg2.extras.execute_batch(
        cur, f"UPDATE raw_files SET {sets} WHERE raw_path = %s", payload, page_size=500)
    c.commit()
    print(f"inherited metadata onto {len(todo):,} rows")

    if a.fix_platform:
        # Only where the recipient demonstrably never had its own reading: no mobility, no
        # metadata json. That is the shape measured on all 632 mislabelled rows.
        cur.execute("""
            UPDATE raw_files r SET platform = d.platform
            FROM raw_files d
            WHERE d.raw_basename = r.raw_basename AND d.instrument_model IS NOT NULL
              AND r.platform <> d.platform
              AND r.mobility_min IS NULL AND r.instrument_metadata_json IS NULL
        """)
        print(f"corrected platform on {cur.rowcount:,} rows")
        c.commit()
    c.close()


if __name__ == "__main__":
    main()
