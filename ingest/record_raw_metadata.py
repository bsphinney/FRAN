"""Extract instrument metadata from located raws (Thermo .raw via ThermoRawFileParser, Bruker .d via
analysis.tdf) and (with --apply) record it in raw_files. Default: dry-run on a --sample of raws."""
import argparse, glob, json, os, sqlite3, subprocess, sys, tempfile
sys.path.insert(0, "/quobyte/proteomics-grp/brett/glendon/fran_ingest")
import plan_spectrum_backfill as P

TRFP = "/quobyte/proteomics-grp/tools/ThermoRawFileParser/ThermoRawFileParser"


def read_thermo(path):
    out = tempfile.mkdtemp()
    try:
        subprocess.run([TRFP, "-i", path, "-m", "0", "-o", out], capture_output=True, text=True, timeout=240)
        js = glob.glob(out + "/*etadata*") + glob.glob(out + "/*.json")
        if not js:
            return None
        d = json.load(open(js[0]))
        flat = {it.get("name"): it.get("value") for sec in d.values() if isinstance(sec, list) for it in sec if isinstance(it, dict)}
    finally:
        for f in glob.glob(out + "/*"):
            try: os.remove(f)
            except OSError: pass
        os.rmdir(out)
    return {
        "instrument_model": flat.get("Thermo Scientific instrument model"),
        "instrument_serial": flat.get("instrument serial number"),
        "acquisition_method": "DIA",
        "n_ms1_frames": _int(flat.get("Number of MS1 spectra")),
        "n_ms2_frames": _int(flat.get("Number of MS2 spectra")),
        "mass_range_min": _flt(flat.get("MS min MZ")), "mass_range_max": _flt(flat.get("MS max MZ")),
        "gradient_minutes": _flt(flat.get("MS max RT")),
    }


def read_bruker(path):
    con = sqlite3.connect(f"file:{path}/analysis.tdf?mode=ro", uri=True)
    g = dict(con.execute("SELECT Key, Value FROM GlobalMetadata").fetchall())
    try:
        n_ms1 = con.execute("SELECT count(*) FROM Frames WHERE MsMsType=0").fetchone()[0]
        n_ms2 = con.execute("SELECT count(*) FROM Frames WHERE MsMsType<>0").fetchone()[0]
    except sqlite3.Error:
        n_ms1 = n_ms2 = None
    con.close()
    return {
        "instrument_model": g.get("InstrumentName"),
        "instrument_serial": g.get("InstrumentSerialNumber"),
        "acquisition_method": g.get("MethodName"),          # exact: DIA_11x3-k07t13Ra85.m
        "n_ms1_frames": n_ms1, "n_ms2_frames": n_ms2,
        "mass_range_min": _flt(g.get("MzAcqRangeLower")), "mass_range_max": _flt(g.get("MzAcqRangeUpper")),
        "gradient_minutes": None,
    }


def _int(v):
    try: return int(float(v))
    except (TypeError, ValueError): return None
def _flt(v):
    try: return round(float(v), 3)
    except (TypeError, ValueError): return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bruker", action="store_true", help="all located Bruker .d raws (full run)")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    c = P._conn(); c.autocommit = False; cur = c.cursor()
    if a.bruker:
        cur.execute("SELECT DISTINCT hive_path FROM raw_files WHERE hive_path ILIKE '%.d' AND hive_path<>''")
        paths = [r[0] for r in cur.fetchall()]
    else:
        cur.execute("SELECT DISTINCT hive_path FROM raw_files WHERE hive_path ILIKE '%.d' AND hive_path<>'' LIMIT %s", (a.sample,))
        paths = [r[0] for r in cur.fetchall()]
    print(f"{len(paths):,} distinct Bruker .d to process. apply={a.apply}", flush=True)
    ok = err = written = 0
    for i, hp in enumerate(paths, 1):
        try:
            m = read_bruker(hp)
            if not m or not m.get("instrument_model"):
                err += 1; continue
            ok += 1
            if a.apply:
                cur.execute("""UPDATE raw_files SET instrument_model=%s, instrument_serial=%s,
                    acquisition_method=%s, n_ms1_frames=%s, n_ms2_frames=%s,
                    mass_range_min=%s, mass_range_max=%s WHERE hive_path=%s""",
                    (m["instrument_model"], m["instrument_serial"], m["acquisition_method"],
                     m["n_ms1_frames"], m["n_ms2_frames"], m["mass_range_min"], m["mass_range_max"], hp))
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
