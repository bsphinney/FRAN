"""engine_fasta.py — detect the SEARCH DATABASE (FASTA) for a search being ingested.

Same shape, and the same reason, as `engine_version.py`: the columns exist and nothing
writes them. As of 2026-08-25 `delimp_searches.fasta_path` is populated for 157 of 2,014
searches (7.8%) and `fasta_md5` / `fasta_n_proteins` for **none of them** — and every one
of those 157 was ingested between 2026-06-13 and 2026-06-30, i.e. they came from a one-off
historical load, not from the ingest path. `corpus_ingest.py` never referenced FASTA at all.

Why the corpus needs it: a protein or gene count is only comparable across searches if you
know how redundant the database was. A one-protein-per-gene human proteome carries ~1.00
entries per gene; a full proteome with unreviewed isoforms can carry >2. Measured on two of
our own studies, that single difference moved a cross-engine protein-group gap from +5% to
+42% while the underlying peptide-level gap barely changed. Without the database on record,
FRAN cannot tell a user their comparison is not like-for-like.

Where the database actually lives (verified against real exports, 2026-08-25):

  DIA-NN       report.log.txt — line 1 is the full command line, containing `--fasta <path>`.
               Repeatable: DIA-NN accepts several `--fasta` flags, so collect them all.
               Verified on a real 2.6.0 log.

  Spectronaut  <name>_ExperimentSetupOverview_*.txt — a settings tree containing:
                   ├─ Protein Databases Used
                   │  │  ├─ Original File: gg_HoSa_rUP5640.fasta
                   │  └─ Universal Contaminant Protein FASTA
                   │     ├─ Original File: Universal Contaminant Protein FASTA.fasta
               The contaminant database is listed as a separate entry and is returned
               separately, not concatenated into the search database.
               Also present in a GUI `<name>_Report.setup.txt`.
               Verified on a real 20.6 export.

NEGATIVE RESULT, recorded so nobody re-derives it: `RunSummaries/*_RunOverview.tsv` does
**not** carry the database. That file is what rescued version detection for the archived CLI
exports (see engine_version.py), so it is the obvious place to look — and it is not there.
Its row labels stop at run-level metrics (Precursors, Protein Groups, Cycle Time, Instrument
Name...). Coverage for FASTA is therefore bounded by how many report dirs kept an
ExperimentSetupOverview or setup.txt, which is a strictly smaller set. Expect partial
coverage and do not treat a NULL as an ingest failure.

md5 and entry count are only computable when the file is actually reachable. Spectronaut
records a BARE FILENAME with no directory, so for Spectronaut those stay None unless the
name resolves under one of `search_roots`. Never guess a path that was not read.
"""
from __future__ import annotations

import glob
import hashlib
import os
import re

# `--fasta path` / `--fasta=path`, quoted or bare, repeatable.
_DIANN_FASTA = re.compile(r"--fasta[=\s]+(\"[^\"]+\"|'[^']+'|\S+)")
# "├─ Original File: gg_HoSa_rUP5640.fasta"  (box-drawing prefix varies; anchor on the label)
_SN_ORIGINAL = re.compile(r"Original File:\s*(.+?\.fasta)\s*$", re.I | re.M)
_SN_DB_BLOCK = re.compile(r"Protein Databases Used(.*?)(?:\n\s*[├└]─ \w|\Z)", re.I | re.S)
_CONTAM_HINT = re.compile(r"contaminant", re.I)


def _head(path: str, nbytes: int = 400_000) -> str:
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


def _stat_fasta(path: str) -> tuple[str | None, int | None]:
    """(md5, n_proteins) for a reachable FASTA; (None, None) otherwise. Streams — these files
    run to hundreds of MB and the ingest host should not hold one in memory."""
    if not path or not os.path.isfile(path):
        return None, None
    try:
        h = hashlib.md5()  # noqa: S324 - provenance fingerprint, not a security control
        n = 0
        tail = b""          # last byte of the previous chunk, so a ">" that lands exactly on a
                            # chunk boundary is still counted
        with open(path, "rb") as fh:
            first = True
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
                if first and chunk.startswith(b">"):
                    n += 1
                first = False
                n += (tail + chunk).count(b"\n>")
                tail = chunk[-1:]
        return h.hexdigest(), n or None
    except OSError:
        return None, None


def _resolve(name: str, search_roots: list[str]) -> str | None:
    """Try to turn a bare filename into a real path. Returns None rather than guessing."""
    if os.path.isabs(name) and os.path.isfile(name):
        return name
    base = os.path.basename(name.replace("\\", "/"))
    for root in search_roots:
        if not root or not os.path.isdir(root):
            continue
        for cand in (os.path.join(root, base), *glob.glob(os.path.join(root, "**", base),
                                                          recursive=False)):
            if os.path.isfile(cand):
                return cand
    return None


def detect(engine: str, report_path: str | None, search_dir: str | None = None,
           search_roots: list[str] | None = None) -> dict | None:
    """Best-effort database provenance, or None. Never raises — a missing FASTA must never
    fail an ingest, exactly as with engine_version.detect().

    Returns {"fasta_path", "fasta_md5", "fasta_n_proteins", "contaminant_lib"} with any
    unknown field set to None.
    """
    roots: list[str] = []
    for cand in (search_dir, os.path.dirname(report_path or "")):
        if cand and os.path.isdir(cand) and cand not in roots:
            roots.append(cand)
    if not roots:
        return None
    extra = list(search_roots or []) + roots

    try:
        eng = (engine or "").lower()
        if eng.startswith("spectronaut"):
            for root in roots:
                for p in _first(["*ExperimentSetupOverview*.txt", "*setup.txt",
                                 "*.setup.txt"], root):
                    txt = _head(p)
                    block = _SN_DB_BLOCK.search(txt)
                    names = _SN_ORIGINAL.findall(block.group(1) if block else txt)
                    if not names:
                        continue
                    search_db = [n for n in names if not _CONTAM_HINT.search(n)]
                    contam = [n for n in names if _CONTAM_HINT.search(n)]
                    if not search_db:
                        continue
                    name = search_db[0].strip()
                    resolved = _resolve(name, extra)
                    md5, n_prot = _stat_fasta(resolved) if resolved else (None, None)
                    return {"fasta_path": resolved or name,
                            "fasta_md5": md5, "fasta_n_proteins": n_prot,
                            "contaminant_lib": contam[0].strip() if contam else None}
        elif eng in ("diann", "dia-nn", "fragpipe"):
            # FragPipe's bundled DIA-NN writes the same log, so the same parse serves both.
            for root in roots:
                for p in _first(["report.log.txt", "*.log.txt", "*.log",
                                 os.path.join("dia-quant-output", "report.log.txt")], root):
                    hits = _DIANN_FASTA.findall(_head(p, 40_000))
                    if not hits:
                        continue
                    paths = [h.strip("\"'") for h in hits]
                    contam = [x for x in paths if _CONTAM_HINT.search(os.path.basename(x))]
                    main = [x for x in paths if x not in contam] or paths
                    resolved = _resolve(main[0], extra) or (
                        main[0] if os.path.isabs(main[0]) else None)
                    md5, n_prot = _stat_fasta(resolved) if resolved else (None, None)
                    return {"fasta_path": resolved or main[0],
                            "fasta_md5": md5, "fasta_n_proteins": n_prot,
                            "contaminant_lib": os.path.basename(contam[0]) if contam else None}
    except Exception:  # noqa: BLE001 - provenance is never worth failing an ingest over
        return None
    return None


if __name__ == "__main__":
    import sys
    for arg in sys.argv[1:]:
        d = arg if os.path.isdir(arg) else os.path.dirname(arg)
        for eng in ("spectronaut", "diann"):
            got = detect(eng, None, d)
            if got:
                print(f"{eng:12s} {got}  <- {arg}")
