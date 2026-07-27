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
