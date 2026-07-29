"""raw_metadata.py — read instrument metadata straight out of a raw file.

`corpus_ingest.py` has imported `read_raw_metadata` from here since the go-forward raw-metadata block
was added, but the module was never committed. The import sat inside a bare `except Exception:` that
set `read_raw_metadata = None`, so every ingest silently wrote NULL for instrument_model,
instrument_serial, acquisition_date, mobility_*, n_ms*_frames, file_size_bytes and
instrument_metadata_json — and the `COALESCE(EXCLUDED.x, raw_files.x)` upsert hid it by preserving
whatever an earlier `record_raw_metadata.py` pass had filled. That is why instrument_model stalled at
63.1% and acquisition_date at 36.4% of 19,874 raw_files rows: not uncollected, dropped on every run.

Instrument metadata is NOT in the Spectronaut/DIA-NN reports — only in the raw header. So:
  Bruker .d   -> analysis.tdf, table GlobalMetadata (a plain SQLite key/value table)
  Thermo .raw -> ThermoRawFileParser -m (needs the dotnet-core-sdk/8.0.4 module + DOTNET_ROOT on Hive)

`read_raw_metadata(path, with_size=True)` returns a dict keyed exactly as the `raw_files` INSERT in
`corpus_ingest.py` expects, or None if the path is not a readable raw.
"""
import glob
import json
import os
import sqlite3
import subprocess
import tempfile

TRFP = os.environ.get("FRAN_TRFP", "/quobyte/proteomics-grp/tools/ThermoRawFileParser/ThermoRawFileParser")

# Kept out of instrument_metadata_json: multi-KB blobs that would bloat every row to no benefit.
_BULKY = {"DigitizerSaturationHandling"}


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _flt(v, nd=6):
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


def _dir_size(path):
    """Total bytes under a Bruker .d directory. Walks, so it is gated behind with_size."""
    tot = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                tot += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return tot or None


def read_bruker(path):
    """Bruker .d — analysis.tdf GlobalMetadata. Opened read-only so a live acquisition is safe."""
    tdf = os.path.join(path, "analysis.tdf")
    if not os.path.exists(tdf):
        return None
    con = sqlite3.connect(f"file:{tdf}?mode=ro", uri=True)
    try:
        g = dict(con.execute("SELECT Key, Value FROM GlobalMetadata").fetchall())
        try:
            n_ms1 = con.execute("SELECT count(*) FROM Frames WHERE MsMsType=0").fetchone()[0]
            n_ms2 = con.execute("SELECT count(*) FROM Frames WHERE MsMsType<>0").fetchone()[0]
        except sqlite3.Error:
            n_ms1 = n_ms2 = None
    finally:
        con.close()
    return {
        "platform": "timstof",
        "instrument_model": g.get("InstrumentName"),
        "instrument_serial": g.get("InstrumentSerialNumber"),
        # The exact acquisition method, e.g. DIA_11x3-k07t13Ra85.m — not the "DIA"/"diaPASEF" fallback.
        "acquisition_method": g.get("MethodName"),
        # ISO 8601 with offset, e.g. 2026-04-24T21:55:58.485-07:00. Postgres timestamptz parses it
        # directly. This is also the column-aging proxy: runs close in time share a column.
        "acquisition_date": g.get("AcquisitionDateTime") or None,
        "mass_range_min": _flt(g.get("MzAcqRangeLower")),
        "mass_range_max": _flt(g.get("MzAcqRangeUpper")),
        "mobility_min": _flt(g.get("OneOverK0AcqRangeLower")),
        "mobility_max": _flt(g.get("OneOverK0AcqRangeUpper")),
        "n_ms1_frames": n_ms1,
        "n_ms2_frames": n_ms2,
        # The .d carries no gradient; corpus_ingest falls back to the EvoSep SPD map or the RT span.
        "gradient_minutes": None,
        "instrument_metadata_json": json.dumps({k: v for k, v in g.items() if k not in _BULKY}),
    }


def read_thermo(path):
    """Thermo .raw — ThermoRawFileParser metadata mode. Returns None if the tool is unavailable."""
    out = tempfile.mkdtemp()
    try:
        subprocess.run([TRFP, "-i", path, "-m", "0", "-o", out],
                       capture_output=True, text=True, timeout=240)
        js = glob.glob(out + "/*etadata*") + glob.glob(out + "/*.json")
        if not js:
            return None
        d = json.load(open(js[0]))
        flat = {it.get("name"): it.get("value")
                for sec in d.values() if isinstance(sec, list)
                for it in sec if isinstance(it, dict)}
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    finally:
        for f in glob.glob(out + "/*"):
            try:
                os.remove(f)
            except OSError:
                pass
        try:
            os.rmdir(out)
        except OSError:
            pass
    return {
        "platform": "orbitrap",
        "instrument_model": flat.get("Thermo Scientific instrument model"),
        "instrument_serial": flat.get("instrument serial number"),
        # TRFP exposes no method name; corpus_ingest's platform fallback supplies "DIA".
        "acquisition_method": "DIA",
        "acquisition_date": flat.get("creation date") or flat.get("Creation date") or None,
        "mass_range_min": _flt(flat.get("MS min MZ")),
        "mass_range_max": _flt(flat.get("MS max MZ")),
        "mobility_min": None,
        "mobility_max": None,
        "n_ms1_frames": _int(flat.get("Number of MS1 spectra")),
        "n_ms2_frames": _int(flat.get("Number of MS2 spectra")),
        # Orbitrap runs have no IM, so max RT is the honest gradient estimate.
        "gradient_minutes": _flt(flat.get("MS max RT"), 3),
        "instrument_metadata_json": json.dumps(flat),
    }


def read_raw_metadata(path, with_size=True):
    """Dispatch on extension. Returns None for anything that is not a readable .d or .raw."""
    if not path:
        return None
    ext = os.path.splitext(path)[1].lower()
    if ext == ".d" and os.path.isdir(path):
        md = read_bruker(path)
    elif ext == ".raw" and os.path.isfile(path):
        md = read_thermo(path)
    else:
        return None
    if md is None:
        return None
    if with_size:
        md["file_size_bytes"] = _dir_size(path) if ext == ".d" else (
            os.path.getsize(path) if os.path.exists(path) else None)
    else:
        md["file_size_bytes"] = None
    return md
