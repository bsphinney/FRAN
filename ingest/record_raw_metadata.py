"""Extract instrument metadata from located raws (Thermo .raw via ThermoRawFileParser, Bruker .d via
analysis.tdf) and (with --apply) record it in raw_files. Default: dry-run on a --sample of raws.

The readers live in `raw_metadata.py` — the same module `corpus_ingest.py` uses on the go-forward
path, so a field fixed here is fixed for new ingests too."""
import argparse, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/quobyte/proteomics-grp/brett/glendon/fran_ingest")
import plan_spectrum_backfill as P
from raw_metadata import read_bruker, read_thermo, read_raw_metadata  # noqa: F401


def _int_or_none(v):
    """raw_files.ms2_resolution is INTEGER but TRFP reports 'mass resolution' as a float (0.5)."""
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
    paths = [r[0] for r in cur.fetchall()]
    print(f"{len(paths):,} distinct {label} to process. apply={a.apply}", flush=True)
    ok = err = written = 0
    for i, hp in enumerate(paths, 1):
        try:
            # with_size=False: a .d size walk over thousands of raws is the expensive part and
            # file_size_bytes is not what this pass is for.
            m = read_raw_metadata(hp, with_size=False)
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
                    lc_method = COALESCE(%s, lc_method),
                    activation_method = COALESCE(%s, activation_method),
                    ms2_resolution = COALESCE(%s, ms2_resolution),
                    instrument_metadata_json = COALESCE(%s::jsonb, instrument_metadata_json)
                    WHERE hive_path=%s""",
                    (m["instrument_model"], m["instrument_serial"], m["acquisition_method"],
                     m.get("acquisition_date"), m["n_ms1_frames"], m["n_ms2_frames"],
                     m["mass_range_min"], m["mass_range_max"],
                     m.get("mobility_min"), m.get("mobility_max"),
                     m.get("gradient_minutes"),
                     m.get("lc_method"), m.get("activation_method"),
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
