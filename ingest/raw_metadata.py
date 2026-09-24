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
import sys
import tempfile

# Same idiom the rest of ingest/ uses to import a sibling module: ingest/ is not
# a package, and raw_metadata is imported both as a module by corpus_ingest and
# run directly by record_raw_metadata.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tdf_safe import connect_tdf                            # noqa: E402

TRFP = os.environ.get("FRAN_TRFP", "/quobyte/proteomics-grp/tools/ThermoRawFileParser/ThermoRawFileParser")
TRFP_DIR = os.path.dirname(TRFP)

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


def _thermo_date(v):
    """ThermoRawFileParser emits 'Content Creation Date' as US M/D/Y H:M:S — e.g.
    '02/22/2025 05:15:29' — NOT ISO 8601, and with no timezone.

    Measured, not assumed. `datetime.fromisoformat` rejects it outright, so a naive ISO parse leaves
    the field NULL and the failure is invisible. Returned as ISO so Postgres timestamptz accepts it;
    the absent offset means the DB applies its own timezone, which is fine for a column used as a
    coarse column-aging proxy but is NOT to be trusted for sub-day arithmetic."""
    if not v:
        return None
    s = str(v).strip()
    from datetime import datetime
    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).isoformat(timespec="seconds")
        except ValueError:
            continue
    try:  # last resort: already ISO-ish, possibly with an offset
        return datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat(timespec="seconds")
    except ValueError:
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


def _cycle_time_sec(con):
    """Median seconds between consecutive MS1 frames -- one full diaPASEF cycle.

    MEDIAN, not (span / count). Both agree to the 4th decimal on a clean acquisition, but a pause,
    a segmented method or a truncated analysis.tdf leaves a handful of enormous gaps that drag
    span/count off while the median per-cycle delta stays correct. The median costs nothing here
    because the Frames table is already open.

    Validated 2026-09-23 against Spectronaut's own "Cycle Time (MS1)" on 18 runs spanning four
    instruments and 0.571-1.800 s: median error 0.143%, max 0.574%, 18/18 within 1%.

    Frames.Time is seconds since acquisition start, so no unit conversion. Returns None rather than
    a guess when there are fewer than two MS1 frames.
    """
    try:
        rows = con.execute("SELECT Time FROM Frames WHERE MsMsType=0 ORDER BY Time").fetchall()
    except sqlite3.Error:
        return None
    t = [r[0] for r in rows if r[0] is not None]
    if len(t) < 2:
        return None
    d = sorted(t[i + 1] - t[i] for i in range(len(t) - 1))
    n = len(d)
    med = d[n // 2] if n % 2 else 0.5 * (d[n // 2 - 1] + d[n // 2])
    return round(float(med), 6) if med and med > 0 else None


def read_bruker(path):
    """Bruker .d — analysis.tdf GlobalMetadata.

    Opened immutable, not merely read-only. A plain `mode=ro` open reads *through* any
    stale mid-acquisition analysis.tdf-wal left beside the tdf by an interrupted copy, so
    the instrument metadata recorded here would silently be mid-acquisition state, and it
    drops an analysis.tdf-shm inside the raw .d. See ingest/tdf_safe.py.
    """
    tdf = os.path.join(path, "analysis.tdf")
    if not os.path.exists(tdf):
        return None
    con = connect_tdf(tdf)
    try:
        g = dict(con.execute("SELECT Key, Value FROM GlobalMetadata").fetchall())
        try:
            n_ms1 = con.execute("SELECT count(*) FROM Frames WHERE MsMsType=0").fetchone()[0]
            n_ms2 = con.execute("SELECT count(*) FROM Frames WHERE MsMsType<>0").fetchone()[0]
        except sqlite3.Error:
            n_ms1 = n_ms2 = None
        cyc = _cycle_time_sec(con)
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
        # Points-per-peak needs this as its denominator, and it was 32% populated corpus-wide
        # because the only other source is Spectronaut's RunSummaries TSV, which DIA-NN never
        # writes and 615 of 1,737 archived report dirs do not have.
        "cycle_time_sec": cyc,
        # The .d carries no gradient; corpus_ingest falls back to the EvoSep SPD map or the RT span.
        "gradient_minutes": None,
        "instrument_metadata_json": json.dumps({k: v for k, v in g.items() if k not in _BULKY}),
    }


# Where to find a Python that can load CoreCLR, and the Thermo DLLs. Overridable because the
# .NET side is an environment concern, not a code one -- and if either is absent the reader
# degrades to "resolution unknown" rather than failing an ingest.
_RES_PY = os.environ.get("FRAN_THERMO_RES_PYTHON",
                         os.path.expanduser("~/trfp_probe/pyn/bin/python"))
_RES_DLL = os.environ.get("FRAN_THERMO_DLL_DIR", TRFP_DIR)


def _thermo_resolution(path):
    """Real Orbitrap resolving power (ms1, ms2), via thermo_resolution.py in a CHILD process.

    NOT importable inline: pythonnet loads a CoreCLR runtime into whatever process imports it, and
    raw_metadata is imported by every ingest. A subprocess keeps a broken .NET install from taking
    down ingestion -- the failure mode is a NULL resolution, which is honest.

    Why this is not read from TRFP like everything else in read_thermo: TRFP's `mass resolution`
    (MS:1000011, RunHeaderEx.MassResolution) is a constant 0.5 on every Thermo file and is not
    resolving power at all. The real value is in each scan's trailer extra, which TRFP reads
    internally and never emits in any output format. Measured before this was added: ms2_resolution
    was 0 on 6,992 rows and NULL on 18,581 -- not one plausible value corpus-wide.

    Verified 2026-09-24 against six runs: Fusion Lumos 60000/15000, Exploris 480 120000/15000.
    Both agree with the per-run resolutions the DIA-NN acquisition probe measured independently.
    """
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "thermo_resolution.py")
    if not (os.path.exists(script) and os.path.exists(_RES_PY)):
        return None, None
    try:
        r = subprocess.run([_RES_PY, script, "--dll-dir", _RES_DLL, path],
                           capture_output=True, text=True, timeout=120)
        line = next((ln for ln in r.stdout.splitlines() if ln.startswith("{")), None)
        if not line:
            return None, None
        d = json.loads(line)
        return d.get("ms1_resolution"), d.get("ms2_resolution")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, None


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
    _ms1_res, _ms2_res = _thermo_resolution(path)
    # Key names and formats below are MEASURED from a real ThermoRawFileParser 1.4.x run on an
    # Orbitrap Exploris 480, not guessed. The earlier guesses ("creation date") matched nothing, so
    # acquisition_date would have silently stayed NULL for all ~7,100 Orbitrap raws.
    return {
        "platform": "orbitrap",
        "instrument_model": flat.get("Thermo Scientific instrument model"),
        "instrument_serial": flat.get("instrument serial number"),
        # DELIBERATELY None — the raw header is the WRONG PLACE to get this, twice over.
        #
        # 1. It is unreliable from a Thermo file. The canonical marker is the ScanFilter `d` flag
        #    ("FTMS + c NSI d Full ms2 …" — `d` = data-dependent; DIA omits it), and `-m 0` metadata
        #    does not include scan filters at all. What is left is weak: a "dia"/"dda" substring in
        #    the method name, or an MS2:MS1 ratio (34:1 on the file measured here vs STAN's >=15
        #    heuristic). STAN attempts this and does not get it right every time.
        #
        # 2. It is unnecessary. The acquisition type is a property of the SEARCH, not the raw: a
        #    result ingested from Spectronaut or DIA-NN is DIA. `delimp_searches.search_engine` is
        #    already recorded and is a far stronger signal than any header heuristic.
        #    Caveat to carry: DIA-NN can now run DDA, so "engine == diann" is not *proof* of DIA —
        #    the authoritative answer for those lives in the DIA-NN log, which `engine_version.py`
        #    already opens for the version string and could be extended to read.
        #
        # Returning None makes the writer's COALESCE preserve what is stored rather than stamping a
        # guess over it, while the evidence (n_ms1_frames, n_ms2_frames, full method path) is
        # persisted regardless, so a better classifier can run later from the database without
        # re-reading 7,000 raws.
        "acquisition_method": None,
        # The Xcalibur instrument method, e.g. C:\Xcalibur\methods\gabri\pS_DIA\ela_DiaOlsW22_30m.m.
        # Strictly this is the combined LC+MS instrument method, not a pure LC gradient program — but
        # it is the best "were these runs acquired the same way" signal the raw header carries, which
        # is what STORAGE_DESIGN.md §3 wants lc_method for.
        "lc_method": flat.get("device acquisition method"),
        "acquisition_date": _thermo_date(flat.get("Content Creation Date")),
        # e.g. "HCD". Column exists in raw_files and is 0% populated corpus-wide.
        "activation_method": (flat.get("beam-type collision-induced dissociation")
                              or flat.get("collision-induced dissociation") or None),
        "mass_range_min": _flt(flat.get("MS min MZ")),
        "mass_range_max": _flt(flat.get("MS max MZ")),
        "mobility_min": None,
        "mobility_max": None,
        "n_ms1_frames": _int(flat.get("Number of MS1 spectra")),
        "n_ms2_frames": _int(flat.get("Number of MS2 spectra")),
        # DELIBERATELY None, unlike the Bruker path. The only estimate available from `-m 0` is
        # (MS max RT x 60 / n_MS1), which was MEASURED against Spectronaut's reported cycle time on
        # 3,379 Orbitrap runs at 11.2% mean error -- "MS max RT" spans the whole run including wash,
        # and the first MS1 is not at t=0. Exact scan times would need a heavier parse than -m 0.
        # An 11%-wrong denominator silently becomes an 11%-wrong points-per-peak, so store nothing.
        "cycle_time_sec": None,
        # Orbitrap runs have no IM, so max RT is the honest gradient estimate. Note corpus_ingest
        # already fills gradient_minutes for 98.7% of rows from the EvoSep SPD map or the observed RT
        # span, and the backfill COALESCEs, so this only fills genuine gaps.
        "gradient_minutes": _flt(flat.get("MS max RT"), 3),
        # ms1_resolution / ms2_resolution are filled below from the scan trailers, NOT from
        # flat["mass resolution"] -- that key is a constant 0.5 placeholder, not resolving power.
        "ms1_resolution": _ms1_res,
        "ms2_resolution": _ms2_res,
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
