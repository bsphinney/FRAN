"""engine_version.py — detect the SEARCH ENGINE VERSION for a search being ingested.

`delimp_searches.search_engine_version` existed but nothing ever wrote it: as of 2026-07-27 it was
NULL for all 1,891 Spectronaut searches and 68 of 71 DIA-NN ones. Without it the corpus can't answer
"which Spectronaut built this?" — which matters because identification behaviour changes between
versions (and FRAN mixes SN 15 through 21).

Where the version actually lives (verified on real exports):
  Spectronaut  RunSummaries/*_RunOverview.tsv -> "Analysis Version\t21.0.260604"   <- THE WORKHORSE
                                                 "HTRMS Converter Version\t21.0.260604.94842"
               <name>_Report.setup.txt   -> FIRST LINE: "Spectronaut 21.0.260604.94842"
               *AnalysisLog*.txt         -> sometimes carries a "Spectronaut <ver>" banner
  DIA-NN       report.log.txt / *.log    -> "DIA-NN 1.8.1" / "DIA-NN 2.0"

IMPORTANT (learned the hard way 2026-07-27): **`setup.txt` is a Spectronaut GUI-export artifact.**
The CLI `manageSNE -rs FRAN.rs` exports that produced the whole FRAN archive DO NOT emit one — a
sidecar pull of `C:\fran_sne_export` brought over 12,908 files and **zero** setup.txt. So do not
treat setup.txt as the primary source for archived searches; `RunSummaries/*RunOverview.tsv` is
what actually resolves them (it went 241 -> 8,633 files in that pull and filled 1,088 versions).
setup.txt still works for GUI exports (e.g. the sn21 "everything precursors" dirs).

Coverage ceiling: 615 of 1,737 archived report dirs have no RunSummaries folder at all, so their
searches stay unversioned until their originating export dir is pulled from the Windows box.
"""
from __future__ import annotations

import glob
import os
import re

# "Spectronaut 21.0.260604.94842", "Spectronaut v19.0", "Spectronaut Pulsar X 15.2"
_SN = re.compile(r"Spectronaut(?:\s+Pulsar(?:\s+X)?)?\s+v?(\d+(?:\.\d+)+)", re.I)
_SN_ANALYSIS = re.compile(r"^Analysis Version\s*\t\s*(\d+(?:\.\d+)+)", re.I | re.M)
_DIANN = re.compile(r"DIA-?NN\s+v?(\d+(?:\.\d+)+)", re.I)


def _head(path: str, nbytes: int = 8000) -> str:
    try:
        with open(path, errors="replace") as fh:
            return fh.read(nbytes)
    except OSError:
        return ""


def _first(patterns: list[str], root: str) -> list[str]:
    hits: list[str] = []
    for pat in patterns:
        hits.extend(sorted(glob.glob(os.path.join(root, pat))))
    return hits


def detect(engine: str, report_path: str | None, search_dir: str | None = None) -> str | None:
    """Best-effort engine version string, or None. Never raises — a missing version must never
    fail an ingest."""
    roots: list[str] = []
    for cand in (search_dir, os.path.dirname(report_path or "")):
        if cand and os.path.isdir(cand) and cand not in roots:
            roots.append(cand)
    if not roots:
        return None

    try:
        if (engine or "").lower().startswith("spectronaut"):
            for root in roots:
                # 1) setup.txt — the authoritative full build string, on line 1
                for p in _first(["*setup.txt", "*.setup.txt"], root):
                    m = _SN.search(_head(p, 2000))
                    if m:
                        return m.group(1)
                # 2) RunSummaries RunOverview — "Analysis Version"
                for p in _first([os.path.join("RunSummaries", "*RunOverview.tsv"),
                                 "*RunOverview.tsv"], root):
                    m = _SN_ANALYSIS.search(_head(p, 20000))
                    if m:
                        return m.group(1)
                # 3) any Analysis log banner
                for p in _first(["*[Aa]nalysis*og*.txt", "*.log.txt"], root):
                    m = _SN.search(_head(p))
                    if m:
                        return m.group(1)
        elif (engine or "").lower() == "fragpipe":
            # FragPipe DIA is a CHAIN: diaTracer -> MSFragger -> MSBooster -> Percolator ->
            # ProteinProphet -> EasyPQP -> DIA-NN. The IDs come from MSFragger but the QUANT comes
            # from the bundled DIA-NN, two major versions behind the DIA-NN rows already in the
            # corpus. Recording only "24.0" lets a FRAN user compare a FragPipe search against
            # DIA-NN 2.6 with no way to see that, so name every component we can actually read.
            fp = dn = dt = None
            for root in roots:
                for p in _first(["*.workflow.provenance.json",
                                 os.path.join("..", "*.workflow.provenance.json"),
                                 os.path.join("..", "..", "*.workflow.provenance.json")], root):
                    m = re.search(r"FragPipe[ v]*([0-9]+\.[0-9]+(?:\.[0-9]+)?)", _head(p), re.I)
                    if m:
                        fp = m.group(1); break
                for p in _first(["report.log.txt",
                                 os.path.join("dia-quant-output", "report.log.txt")], root):
                    # DIA-NN's own banner, line 1: "DIA-NN 1.8.2 beta 8 (...)". The beta suffix is
                    # part of the version -- 1.8.2b8 is not 1.8.2 -- so capture it.
                    m = re.search(r"DIA-NN\s+([0-9][0-9.]*(?:\s+beta\s+[0-9]+)?)", _head(p, 400), re.I)
                    if m:
                        dn = re.sub(r"\s+beta\s+", "b", m.group(1).strip()); break
                for p in _first([os.path.join("..", "dt", "diatracer*.log"),
                                 os.path.join("dt", "diatracer*.log"),
                                 os.path.join("..", "..", "dt", "diatracer*.log")], root):
                    m = re.search(r"diaTracer[- ]v?([0-9]+\.[0-9]+\.[0-9]+)", _head(p, 2000), re.I)
                    if m:
                        dt = m.group(1); break
            parts = [f"diaTracer {dt}"] if dt else []
            if dn:
                parts.append(f"DIA-NN {dn}")
            if fp and parts:
                return f"{fp} ({', '.join(parts)})"
            return fp or (", ".join(parts) or None)
        elif (engine or "").lower() == "radiant":
            # Radiant/Fulcrum ships as a container, so there is no in-file banner. The pipeline
            # writes search_provenance.json next to the results; parse it rather than guessing from
            # an image filename that may have been renamed.
            import json as _json
            for root in roots:
                for p in _first(["search_provenance.json",
                                 os.path.join("..", "search_provenance.json")], root):
                    try:
                        d = _json.loads(_head(p, 4000))
                        if str(d.get("engine", "")).lower().startswith("radiant") and d.get("version"):
                            return f"{d['version']} (Fulcrum)"
                    except Exception:      # noqa: BLE001 - fall through to the filename sniff
                        pass
                for p in _first(["*.radiantConfig", "*.log", "*.sif"], root):
                    m = re.search(r"radiant[-_](?:fulcrum[-_])?v?([0-9]+\.[0-9]+\.[0-9]+)",
                                  _head(p), re.I)
                    if m:
                        return f"{m.group(1)} (Fulcrum)"
            return None
        else:
            for root in roots:
                for p in _first(["report.log.txt", "*.log.txt", "*.log"], root):
                    m = _DIANN.search(_head(p))
                    if m:
                        return m.group(1)
    except Exception:  # noqa: BLE001 - version detection is never worth failing an ingest over
        return None
    return None


if __name__ == "__main__":
    import sys
    for arg in sys.argv[1:]:
        d = arg if os.path.isdir(arg) else os.path.dirname(arg)
        print(f"spectronaut={detect('spectronaut', None, d)}  diann={detect('diann', None, d)}  <- {arg}")
