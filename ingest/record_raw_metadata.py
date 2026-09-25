"""Extract instrument metadata from located raws (Thermo .raw via ThermoRawFileParser, Bruker .d via
analysis.tdf) and (with --apply) record it in raw_files. Default: dry-run on a --sample of raws.

The readers live in `raw_metadata.py` — the same module `corpus_ingest.py` uses on the go-forward
path, so a field fixed here is fixed for new ingests too."""
import argparse, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/quobyte/proteomics-grp/brett/glendon/fran_ingest")
import plan_spectrum_backfill as P
from raw_metadata import read_bruker, read_thermo, read_raw_metadata  # noqa: F401
from instrument_labels import normalize as _norm_instrument


def _canon(m):
    """Canonicalise (instrument_model, instrument_serial) IN PLACE before writing.

    THIS IS NOT OPTIONAL. corpus_ingest calls instrument_labels.normalize on the go-forward path;
    this script did not, and writing the raw vendor strings re-split one physical instrument across
    several labels -- exactly the damage fix_instrument_labels.py was written to undo. Measured
    2026-09-24 after a --bruker pass over 8,856 raws: 2,390 rows gained a leading space, leaving
    ' timsTOF Pro' (2,428) beside 'timsTOF Pro' (58), and serial '1854399.153' (250) beside
    '1854399.00153' (2,428) -- reproducing the 2026-08-25 split almost row for row.

    Every per-instrument aggregate silently breaks when this is skipped, and nothing errors."""
    try:
        m["instrument_model"], m["instrument_serial"] = _norm_instrument(
            m.get("instrument_model"), m.get("instrument_serial"))
    except Exception:  # noqa: BLE001 - metadata tidying must never fail a backfill
        pass
    return m


def _int_or_none(v):
    """raw_files.ms1/ms2_resolution are INTEGER; the trailer reader yields ints already, but keep
    the coercion -- it is what turned TRFP's bogus 0.5 into the 0 now stored on 6,992 rows, and a
    future reader returning a float should not reintroduce that silently."""
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bruker", action="store_true", help="all located Bruker .d raws (full run)")
    ap.add_argument("--thermo", action="store_true", help="all located Thermo .raw files (full run)")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--apply", action="store_true")
    # Same reasoning as the Thermo resolution pass: reading thousands of raws off the share is
    # I/O-bound, so split it. Sharded on a stable sort so the shards are disjoint and complete.
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--only-missing", action="store_true",
                    help="restrict to rows still missing instrument_model or acquisition_date")
    a = ap.parse_args()
    ext, label = (".raw", "Thermo .raw") if a.thermo else (".d", "Bruker .d")
    c = P._conn(); c.autocommit = False; cur = c.cursor()
    where = "hive_path ILIKE %s AND hive_path<>''"
    args = [f"%{ext}"]
    if a.only_missing:
        where += " AND (instrument_model IS NULL OR acquisition_date IS NULL)"
    sql = f"SELECT DISTINCT hive_path FROM raw_files WHERE {where}"
    if not (a.bruker or a.thermo):
        sql += " LIMIT %s"; args.append(a.sample)
    cur.execute(sql, args)
    paths = sorted(r[0] for r in cur.fetchall())
    total = len(paths)
    if a.shards > 1:
        paths = paths[a.shard::a.shards]
    print(f"{total:,} distinct {label}; shard {a.shard}/{a.shards} takes {len(paths):,}. "
          f"apply={a.apply}", flush=True)
    ok = err = written = 0
    for i, hp in enumerate(paths, 1):
        try:
            # with_size=False: a .d size walk over thousands of raws is the expensive part and
            # file_size_bytes is not what this pass is for.
            m = read_raw_metadata(hp, with_size=False)
            if m:
                _canon(m)
            if not m or not m.get("instrument_model"):
                err += 1; continue
            ok += 1
            if a.apply:
                # COALESCE on the incoming value, not the stored one: a reader that returns NULL for
                # a field must not blank a value some other pass already established.
                cur.execute("""UPDATE raw_files SET
                    instrument_model = COALESCE(%s, instrument_model),
                    instrument_serial = COALESCE(%s, instrument_serial),
                    acquisition_method = COALESCE(%s, acquisition_method),
                    acquisition_date = COALESCE(%s::timestamptz, acquisition_date),
                    n_ms1_frames = COALESCE(%s, n_ms1_frames),
                    n_ms2_frames = COALESCE(%s, n_ms2_frames),
                    mass_range_min = COALESCE(%s, mass_range_min),
                    mass_range_max = COALESCE(%s, mass_range_max),
                    mobility_min = COALESCE(%s, mobility_min),
                    mobility_max = COALESCE(%s, mobility_max),
                    gradient_minutes = COALESCE(gradient_minutes, %s),
                    -- Fill a gap, and REPAIR an implausible stored value, but never overwrite a
                    -- good one. The stored source is Spectronaut's RunSummaries "Cycle Time (MS1)",
                    -- and 23 of its 8,089 values are ~1e5 too large -- a five-decimal comma decimal
                    -- separator stripped, e.g. 0.74888 s recorded as 74888. Measured against the
                    -- frames-derived value on those exact runs: 74888.000 vs 0.7493. No real DIA
                    -- cycle approaches 20 s (corpus max is 7.88 s on a Fusion Lumos), so >20 is a
                    -- safe repair trigger that cannot touch a legitimate reading.
                    cycle_time_sec = CASE
                        WHEN cycle_time_sec IS NULL OR cycle_time_sec > 20
                        THEN COALESCE(%s, cycle_time_sec) ELSE cycle_time_sec END,
                    lc_method = COALESCE(%s, lc_method),
                    activation_method = COALESCE(%s, activation_method),
                    ms1_resolution = COALESCE(%s, ms1_resolution),
                    -- OVERWRITE, not COALESCE, when the stored value is bogus. Every Thermo row
                    -- FRAN has ever written carries ms2_resolution = 0, because read_thermo used
                    -- to map TRFP's `mass resolution` (a constant 0.5 placeholder, not resolving
                    -- power) through int(round(0.5)). Measured: 0 on 6,992 rows, NULL on 18,581,
                    -- not one plausible value corpus-wide. A plain COALESCE would PRESERVE that 0
                    -- forever, since a stored 0 is not NULL. No real Orbitrap setting is under
                    -- 1000 -- the corpus runs 15,000-120,000 -- so this cannot overwrite a
                    -- legitimate reading.
                    ms2_resolution = CASE
                        WHEN ms2_resolution IS NULL OR ms2_resolution < 1000
                        THEN COALESCE(%s, ms2_resolution) ELSE ms2_resolution END,
                    instrument_metadata_json = COALESCE(%s::jsonb, instrument_metadata_json)
                    WHERE hive_path=%s""",
                    (m["instrument_model"], m["instrument_serial"], m["acquisition_method"],
                     m.get("acquisition_date"), m["n_ms1_frames"], m["n_ms2_frames"],
                     m["mass_range_min"], m["mass_range_max"],
                     m.get("mobility_min"), m.get("mobility_max"),
                     m.get("gradient_minutes"), m.get("cycle_time_sec"),
                     m.get("lc_method"), m.get("activation_method"),
                     _int_or_none(m.get("ms1_resolution")),
                     _int_or_none(m.get("ms2_resolution")),
                     m.get("instrument_metadata_json"), hp))
                written += cur.rowcount
        except Exception as e:
            err += 1
            if err <= 5:
                print(f"  ERR {hp[-50:]}: {str(e)[:60]}", flush=True)
        if i % 500 == 0:
            if a.apply:
                c.commit()
            print(f"  [{i:,}/{len(paths):,}] ok={ok} err={err} rows_written={written}", flush=True)
    if a.apply:
        c.commit()
    print(f"\nDONE: {ok} extracted, {err} failed of {len(paths):,} distinct; {written} raw_files rows updated.", flush=True)
    c.close()


if __name__ == "__main__":
    main()
