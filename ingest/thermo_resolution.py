"""thermo_resolution.py — read the REAL Orbitrap resolving power out of a Thermo .raw.

Run as a SUBPROCESS, never imported by the ingest path. pythonnet loads a CoreCLR runtime into
the process that imports it; keeping that in a child means a missing or broken .NET install
degrades to "resolution unknown" instead of taking down every ingest.

WHY THIS EXISTS. raw_metadata.read_thermo used to map ThermoRawFileParser's `mass resolution`
(MS:1000011, from RunHeaderEx.MassResolution) to ms2_resolution. That value is a constant 0.5 on
every Thermo file -- it is not resolving power at all -- and record_raw_metadata's int(round(0.5))
stored it as 0. Measured over the corpus before this was written: ms2_resolution was 0 on 6,992
rows and NULL on 18,581, with ZERO plausible values anywhere, and ms1_resolution was NULL on all
25,573. The real numbers are not in TRFP's output in any format: they live in each scan's trailer
extra, which TRFP reads internally and never emits.

The trailer key is instrument-dependent -- 'Orbitrap Resolution:' on a Fusion Lumos,
'FT Resolution:' on an Exploris 480 -- so match on the word rather than a fixed key.

Only FTMS scans carry it. An ITMS MS2 has no resolving power in this sense and yields None, which
is correct and must not be read as a failure.

    python ingest/thermo_resolution.py --dll-dir <TRFP_dir> FILE [FILE ...]
    python ingest/thermo_resolution.py --dll-dir <TRFP_dir> --from-stdin < paths.txt

Emits one JSON object per line: {"path", "ms1_resolution", "ms2_resolution", "model", "note"}.
Loops inside ONE process: CoreCLR loads once, so cost is startup-dominated and a corpus-scale
backfill is minutes, not hours. Never raises -- a failure is a note on that line.

Needs DOTNET_ROOT pointing at a .NET 8 root and a Python with pythonnet 3.
Verified 2026-09-24 on a Fusion Lumos: MS1 60000 / MS2 15000.
"""
import argparse
import json
import os
import sys

# Scans are SAMPLED ACROSS THE WHOLE RUN, not taken from the front. Two reasons, both measured:
#   * A DDA run's void volume can be MS1-only, so the first scans yield no MS2 at all.
#   * A method can change resolution mid-run. This corpus contains acquisitions that mix settings
#     (one pilot run set carried 120000/15000, 120000/30000 AND 60000/15000), and reading the
#     front only would report the first setting as if it were the whole run.
# Sampled as CONTIGUOUS BLOCKS at several points through the run, never a fixed stride.
# A fixed stride aliases with the acquisition cycle: a DIA run is MS1 -> N x MS2 -> MS1, so a
# stride that happens to be a multiple of the cycle length lands on MS1 every time and reports
# ms2_resolution NULL for a run that plainly has one. Measured -- striding lost the MS2 on a
# Lumos run that the old front-of-file read found. A block spans whole cycles, so it cannot alias.
N_BLOCKS = 6
# BLOCK_LEN must span SEVERAL WHOLE CYCLES, not just one. It was 15, which is fine for reading a
# resolution (any one FTMS scan carries it) but silently broke the cycle-time measurement added
# later: a DIA cycle is 26-35 scans here (Exploris ~26, Lumos 35), so a 15-scan block usually holds
# fewer than two MS1 scans and yields no consecutive MS1 pair to difference. cycle_time_sec came
# back null on every file until this was raised. 120 holds 3-4 full cycles on both instruments.
BLOCK_LEN = 120


def _res_from_trailer(raw, scan):
    """The resolving power in this scan's trailer, or None. Key name varies by instrument."""
    try:
        tr = raw.GetTrailerExtraInformation(scan)
    except Exception:                                    # noqa: BLE001 - a bad scan is not fatal
        return None
    for i in range(tr.Length):
        label = str(tr.Labels[i])
        if "resolut" not in label.lower():
            continue
        try:
            v = float(str(tr.Values[i]).strip())
        except (TypeError, ValueError):
            continue
        # 0.5 is the RunHeaderEx placeholder leaking through; a real setting is thousands.
        if v >= 1000:
            return int(round(v))
    return None


def _cycle_from_scans(ms1_scans):
    """Median seconds between consecutive MS1 scans = one acquisition cycle.

    The Bruker path gets this from the Frames table; Thermo has no equivalent in TRFP's `-m 0`
    output, which is why cycle_time_sec sat at 34.4% on Orbitrap rows while timsTOF reached 98.6%.
    The alternative available from -m 0 -- MS max RT x 60 / n_MS1 -- was MEASURED at 11.2% mean
    error over 3,379 runs and rejected; this method measured 0.55-1.42% against Spectronaut's own
    reported value on four runs, part of which is that value's own 2-decimal rounding.

    `ms1_scans` is [(scan_number, retention_time_minutes)] and spans SEVERAL DISCONTIGUOUS blocks,
    so only consecutive pairs from the same block are real gaps -- the jump between blocks is
    minutes wide and would swamp the median. Hence the scan-number proximity test.
    """
    gaps = [(t2 - t1) * 60.0 for (s1, t1), (s2, t2) in zip(ms1_scans, ms1_scans[1:])
            if 0 < s2 - s1 <= 60 and t2 > t1]   # <=60: one cycle (26-35 scans here), not a block jump
    if len(gaps) < 3:
        return None
    gaps.sort()
    n = len(gaps)
    med = gaps[n // 2] if n % 2 else 0.5 * (gaps[n // 2 - 1] + gaps[n // 2])
    return round(float(med), 6) if 0.05 < med < 60 else None


def read_one(raw_adapter, device, path):
    out = {"path": path, "ms1_resolution": None, "ms2_resolution": None,
           "cycle_time_sec": None, "model": None, "note": None}
    try:
        raw = raw_adapter.FileFactory(path)
        raw.SelectInstrument(device, 1)
    except Exception as e:                               # noqa: BLE001
        out["note"] = f"open failed: {str(e)[:120]}"
        return out
    try:
        out["model"] = str(raw.GetInstrumentData().Model)
        first = raw.RunHeaderEx.FirstSpectrum
        last = raw.RunHeaderEx.LastSpectrum
        n = max(1, last - first + 1)
        if n <= N_BLOCKS * BLOCK_LEN:
            scans = range(first, last + 1)
        else:
            starts = [first + (n - BLOCK_LEN) * i // max(1, N_BLOCKS - 1) for i in range(N_BLOCKS)]
            scans = sorted({sc for st in starts for sc in range(st, min(st + BLOCK_LEN, last + 1))})
        seen = {"ms1_resolution": set(), "ms2_resolution": set()}
        ms1_scans = []
        for scan in scans:
            try:
                filt = str(raw.GetFilterForScanNumber(scan).ToString())
            except Exception:                            # noqa: BLE001
                continue
            # Only FTMS scans have a resolving-power setting. An ITMS ms2 legitimately has none.
            if "FTMS" not in filt.upper():
                continue
            is_ms2 = " ms2 " in filt.lower()
            key = "ms2_resolution" if is_ms2 else "ms1_resolution"
            r = _res_from_trailer(raw, scan)
            if r is not None:
                seen[key].add(r)
            if not is_ms2:
                try:    # RetentionTimeFromScanNumber is MINUTES
                    ms1_scans.append((scan, float(raw.RetentionTimeFromScanNumber(scan))))
                except Exception:      # noqa: BLE001
                    pass
        out["cycle_time_sec"] = _cycle_from_scans(ms1_scans)
        notes = []
        for key, vals in seen.items():
            if len(vals) == 1:
                out[key] = vals.pop()
            elif len(vals) > 1:
                # Report nothing rather than one of several. A single number here would be read as
                # "the resolution of this run", and for a mixed-method acquisition there isn't one.
                notes.append(f"{key}: {len(vals)} distinct values {sorted(vals)} -- reporting null")
        if not notes and out["ms1_resolution"] is None and out["ms2_resolution"] is None:
            notes.append(f"no FTMS resolution in {N_BLOCKS}x{BLOCK_LEN} sampled scans")
        if notes:
            out["note"] = "; ".join(notes)
    except Exception as e:                               # noqa: BLE001
        out["note"] = f"read failed: {str(e)[:120]}"
    finally:
        try:
            raw.Dispose()
        except Exception:                                # noqa: BLE001
            pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dll-dir", default=os.environ.get(
        "FRAN_THERMO_DLL_DIR", "/quobyte/proteomics-grp/tools/ThermoRawFileParser"))
    ap.add_argument("--from-stdin", action="store_true")
    ap.add_argument("files", nargs="*")
    a = ap.parse_args()

    paths = [ln.strip() for ln in sys.stdin if ln.strip()] if a.from_stdin else a.files
    if not paths:
        return 0

    # Import INSIDE main so --help works without a .NET runtime present.
    try:
        from pythonnet import load
        load("coreclr")
        import clr
        sys.path.append(a.dll_dir)
        clr.AddReference("ThermoFisher.CommonCore.RawFileReader")
        clr.AddReference("ThermoFisher.CommonCore.Data")
        from ThermoFisher.CommonCore.Data.Business import Device
        from ThermoFisher.CommonCore.RawFileReader import RawFileReaderAdapter
    except Exception as e:                               # noqa: BLE001
        # Degrade, do not fail: emit a null result per path so the caller records "unknown"
        # rather than treating an environment gap as a property of the data.
        for p in paths:
            print(json.dumps({"path": p, "ms1_resolution": None, "ms2_resolution": None,
                              "cycle_time_sec": None, "model": None,
                              "note": f"pythonnet/coreclr unavailable: {str(e)[:100]}"}))
        return 0

    for p in paths:
        print(json.dumps(read_one(RawFileReaderAdapter, Device.MS, p)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
