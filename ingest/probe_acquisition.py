"""probe_acquisition.py — measure what an acquisition ACTUALLY isolated, per run, for both vendors.

WHY THIS EXISTS AS A COMMITTED SCRIPT. The corpus already had a `probe.tsv` covering 10,679 runs,
and the script that produced it is nowhere in the repo or the scratch tree. It cannot be re-run,
extended, or checked. That is the same failure recorded in ingest/INGEST_STATUS.md for the
`shortcourse_diann19` XIC lane -- work done ad hoc, useful, and unrepeatable. This replaces it with
something that can be run again.

WHAT IT MEASURES, AND WHY NOT THE DATABASE COLUMN. `raw_files.mass_range_min/max` is the wrong
number for this purpose in both vendors: on Bruker it is MzAcqRangeLower/Upper, the instrument's
full acquisition range; on Thermo it is the MS1 scan range. Neither is the m/z the method actually
isolated for MS2. The corpus-wide DIA-NN re-search keys its spectral-library cache on
--min-pr-mz/--max-pr-mz, so using either would hand a search a library covering a different span
than was acquired -- identifications missing, nothing failing. Pilot round 3 nearly shipped exactly
that: a selection recorded MzAcqRange (99.99-1700.0) where the real DIA range was 299.5-1200.5.

  Bruker .d   analysis.tdf table DiaFrameMsMsWindows (IsolationMz, IsolationWidth) -> the union of
              the isolation windows. Opened through tdf_safe.connect_tdf, i.e.
              file:<path>?mode=ro&immutable=1 ONLY -- a plain mode=ro read follows a stale -wal and
              drops a -shm inside the raw, which has destroyed .d files in this corpus before.
  Thermo .raw delegated to thermo_resolution.py, which reads the scan event's reaction. Batched
              into ONE child process: it loads a CoreCLR runtime, and the cost is startup-dominated
              (~2.7 s for the first file, well under a second thereafter).

A run with no DIA windows at all -- a DDA run, a blank, an unfinished acquisition -- reports nulls
and says so in `note`. That is a real answer and the caller must not read it as a failure to probe.

    python ingest/probe_acquisition.py PATH [PATH ...]
    python ingest/probe_acquisition.py --from-stdin < paths.txt
    python ingest/probe_acquisition.py --from-stdin --tsv > corpus_probe.tsv

One JSON object per line by default; --tsv writes a header and tab-separated rows instead.
"""
import argparse
import functools
import json
import os
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tdf_safe import connect_tdf                                  # noqa: E402

print = functools.partial(print, flush=True)                      # noqa: A001

FIELDS = ("path", "kind", "instrument", "dia_mz_lo", "dia_mz_hi", "dia_n_windows",
          "dia_width_med", "ms1_resolution", "ms2_resolution", "cycle_time_sec", "note")

RES_PY = os.environ.get("FRAN_THERMO_RES_PYTHON",
                        os.path.expanduser("~/trfp_probe/pyn/bin/python"))
DLL_DIR = os.environ.get("FRAN_THERMO_DLL_DIR",
                         "/quobyte/proteomics-grp/tools/ThermoRawFileParser")


def _blank(path, kind, note=None):
    d = {k: None for k in FIELDS}
    d["path"], d["kind"], d["note"] = path, kind, note
    return d


def probe_bruker(path):
    out = _blank(path, "d")
    tdf = os.path.join(path, "analysis.tdf")
    if not os.path.exists(tdf):
        out["note"] = "no analysis.tdf"
        return out
    try:
        con = connect_tdf(tdf)
    except Exception as e:                                        # noqa: BLE001
        out["note"] = f"tdf open failed: {str(e)[:90]}"
        return out
    try:
        try:
            g = dict(con.execute("SELECT Key, Value FROM GlobalMetadata").fetchall())
            out["instrument"] = g.get("InstrumentName")
        except sqlite3.Error:
            pass
        try:
            rows = con.execute(
                "SELECT IsolationMz, IsolationWidth FROM DiaFrameMsMsWindows").fetchall()
        except sqlite3.Error:
            out["note"] = "no DiaFrameMsMsWindows (not a dia-PASEF acquisition)"
            return out
        wins = {(round(mz - w / 2, 2), round(mz + w / 2, 2))
                for mz, w in rows if mz is not None and w}
        if not wins:
            out["note"] = "DiaFrameMsMsWindows present but empty"
            return out
        wid = sorted(round(hi - lo, 2) for lo, hi in wins)
        n = len(wid)
        out["dia_mz_lo"] = min(lo for lo, _ in wins)
        out["dia_mz_hi"] = max(hi for _, hi in wins)
        out["dia_n_windows"] = len(wins)
        out["dia_width_med"] = wid[n // 2] if n % 2 else round(0.5 * (wid[n // 2 - 1] + wid[n // 2]), 2)
    finally:
        try:
            con.close()
        except Exception:                                         # noqa: BLE001
            pass
    return out


def probe_thermo_batch(paths):
    """All Thermo raws in ONE child, so CoreCLR loads once rather than per file."""
    reader = os.path.join(os.path.dirname(os.path.abspath(__file__)), "thermo_resolution.py")
    if not (os.path.exists(reader) and os.path.exists(RES_PY)):
        return {p: _blank(p, "raw", "thermo reader unavailable") for p in paths}
    got = {}
    try:
        pr = subprocess.Popen([RES_PY, reader, "--dll-dir", DLL_DIR, "--from-stdin"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        pr.stdin.write("\n".join(paths) + "\n")
        pr.stdin.close()
        for line in pr.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            d = json.loads(line)
            o = _blank(d.get("path"), "raw", d.get("note"))
            o["instrument"] = d.get("model")
            for k in ("dia_mz_lo", "dia_mz_hi", "dia_n_windows", "dia_width_med",
                      "ms1_resolution", "ms2_resolution", "cycle_time_sec"):
                o[k] = d.get(k)
            got[o["path"]] = o
        pr.wait()
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        for p in paths:
            got.setdefault(p, _blank(p, "raw", f"reader failed: {str(e)[:80]}"))
    for p in paths:
        got.setdefault(p, _blank(p, "raw", "reader returned no line for this path"))
    return got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-stdin", action="store_true")
    ap.add_argument("--tsv", action="store_true", help="TSV with a header instead of JSON lines")
    ap.add_argument("--thermo-chunk", type=int, default=400,
                    help="raws per child process; bounds memory without paying startup often")
    ap.add_argument("paths", nargs="*")
    a = ap.parse_args()
    paths = [ln.strip() for ln in sys.stdin if ln.strip()] if a.from_stdin else a.paths
    if not paths:
        return 0

    bruker = [p for p in paths if p.lower().rstrip("/").endswith(".d")]
    thermo = [p for p in paths if p.lower().rstrip("/").endswith(".raw")]
    other = [p for p in paths if p not in set(bruker) | set(thermo)]

    if a.tsv:
        print("\t".join(FIELDS))

    def emit(d):
        if a.tsv:
            print("\t".join("" if d.get(k) is None else str(d.get(k)) for k in FIELDS))
        else:
            print(json.dumps(d))

    for p in bruker:
        emit(probe_bruker(p))
    for i in range(0, len(thermo), a.thermo_chunk):
        chunk = thermo[i:i + a.thermo_chunk]
        res = probe_thermo_batch(chunk)
        for p in chunk:
            emit(res[p])
    for p in other:
        emit(_blank(p, "?", "not a .d or .raw"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
