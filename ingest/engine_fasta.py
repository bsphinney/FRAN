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


_STAT_CACHE: dict[tuple, tuple[str | None, int | None]] = {}


def _stat_fasta(path: str) -> tuple[str | None, int | None]:
    """(md5, n_proteins) for a reachable FASTA; (None, None) otherwise. Streams — these files
    run to hundreds of MB and the ingest host should not hold one in memory.

    Memoised on (realpath, size, mtime) because backfill_fasta.py replays this over ~2,000
    searches and a whole lab shares a handful of proteomes: without the memo the same
    UP000005640 is re-hashed once per search."""
    if not path or not os.path.isfile(path):
        return None, None
    try:
        st = os.stat(path)
        key = (os.path.realpath(path), st.st_size, st.st_mtime_ns)
        if key in _STAT_CACHE:
            return _STAT_CACHE[key]
    except OSError:
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
        _STAT_CACHE[key] = (h.hexdigest(), n or None)
        return _STAT_CACHE[key]
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
        # One level down, deliberately: the FASTA roots handed in are shares like
        # /quobyte/proteomics-grp/MRS and a recursive walk there is minutes of stat() per
        # search. A database that is buried deeper stays unresolved (path only, no md5),
        # which is the honest answer.
        for cand in (os.path.join(root, base), *glob.glob(os.path.join(root, "*", base))):
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
                        # Every entry looked like a contaminant list, so the name is doing the
                        # lying, not the search: a proteome built with contaminants appended
                        # ("..._plus_contaminants.fasta") is one file and it IS the database.
                        search_db, contam = names, []
                    name = search_db[0].strip()
                    resolved = _resolve(name, extra)
                    md5, n_prot = _stat_fasta(resolved) if resolved else (None, None)
                    return {"fasta_path": resolved or name,
                            "fasta_md5": md5, "fasta_n_proteins": n_prot,
                            "contaminant_lib": contam[0].strip() if contam else None}
        else:
            # DIA-NN's log, and everything that embeds DIA-NN: FragPipe's bundled copy writes the
            # same log, and Radiant/Fulcrum is a container around one. engine_version.py falls
            # through to the DIA-NN parse for unknown engines for the same reason. The regex
            # requires a literal `--fasta`, so a log from something else simply does not match.
            for root in roots:
                for p in _first(["report.log.txt", "*.log.txt", "*.log",
                                 os.path.join("dia-quant-output", "report.log.txt")], root):
                    hits = _DIANN_FASTA.findall(_head(p, 40_000))
                    if not hits:
                        continue
                    paths = [h.strip("\"'") for h in hits]
                    contam = [x for x in paths if _CONTAM_HINT.search(os.path.basename(x))]
                    main = [x for x in paths if x not in contam]
                    if not main:  # as in the Spectronaut branch: a single "..._contaminants.fasta"
                        main, contam = paths, []  # is the database, not the contaminant library
                    resolved = _resolve(main[0], extra) or (
                        main[0] if os.path.isabs(main[0]) else None)
                    md5, n_prot = _stat_fasta(resolved) if resolved else (None, None)
                    return {"fasta_path": resolved or main[0],
                            "fasta_md5": md5, "fasta_n_proteins": n_prot,
                            "contaminant_lib": os.path.basename(contam[0]) if contam else None}
    except Exception:  # noqa: BLE001 - provenance is never worth failing an ingest over
        return None
    return None


# UniProt header: ">sp|P02769|ALBU_BOVIN Serum albumin OS=Bos taurus OX=9913 GN=ALB PE=1 SV=4"
_OS = re.compile(r"\bOS=(.+?)(?=\s+(?:OX|GN|PE|SV)=|\s*$)")
_OX = re.compile(r"\bOX=(\d+)")
_SPECIES_CACHE: dict[tuple, tuple[str, int | None, float] | None] = {}
SPECIES_MIN_SHARE = 0.80      # the top OS= must hold this share of the species-labelled entries
SPECIES_MIN_LABELLED = 0.50   # and labelled entries must be at least half of the database


def fasta_species(path: str | None) -> tuple[str, int | None, float] | None:
    """(organism_name, taxon_id, share) of a READABLE search database, from its UniProt OS=/OX=
    headers; None when the file is unreachable or does not name one dominant species.

    This is the species of the database the search ran against, which is what "the organism of
    this search" means -- better evidence than a vote over identifications whenever the file can
    be read. Contaminant-library entries (Cont_/CON__/cRAP) are ignored, as they are in
    organism.vote_organism(). Returns None rather than guessing when:
      * fewer than SPECIES_MIN_LABELLED of the entries carry OS= (custom ORF / translated
        databases have none), or
      * no species holds SPECIES_MIN_SHARE of them (genuinely multi-organism databases, e.g.
        host + pathogen or entrapment searches; the identification vote decides those).
    Never raises.
    """
    if not path or not os.path.isfile(path):
        return None
    try:
        from organism import canonical_organism, is_contaminant_group
    except ImportError:  # imported as ingest.engine_fasta
        from .organism import canonical_organism, is_contaminant_group
    try:
        st = os.stat(path)
        key = (os.path.realpath(path), st.st_size, st.st_mtime_ns)
        if key in _SPECIES_CACHE:
            return _SPECIES_CACHE[key]
        names: dict[str, int] = {}
        taxa: dict[str, int] = {}
        n_entries = n_labelled = 0
        with open(path, errors="replace") as fh:
            for line in fh:
                if not line.startswith(">"):
                    continue
                acc = line[1:].split(None, 1)[0] if len(line) > 1 else ""
                if is_contaminant_group(acc):
                    continue
                n_entries += 1
                m = _OS.search(line)
                name = canonical_organism(m.group(1)) if m else None
                if not name:
                    continue
                n_labelled += 1
                names[name] = names.get(name, 0) + 1
                x = _OX.search(line)
                if x and name not in taxa:
                    taxa[name] = int(x.group(1))
        result = None
        if n_entries and n_labelled >= SPECIES_MIN_LABELLED * n_entries:
            top = max(names, key=names.get)
            share = names[top] / n_labelled
            if share >= SPECIES_MIN_SHARE:
                result = (top, taxa.get(top), share)
        _SPECIES_CACHE[key] = result
        return result
    except Exception:  # noqa: BLE001 - provenance is never worth failing an ingest over
        return None


if __name__ == "__main__":
    import sys
    for arg in sys.argv[1:]:
        d = arg if os.path.isdir(arg) else os.path.dirname(arg)
        for eng in ("spectronaut", "diann"):
            got = detect(eng, None, d)
            if got:
                print(f"{eng:12s} {got}  <- {arg}")
