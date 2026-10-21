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

THE .sne CARRIES THE DATABASES THEMSELVES (verified 2026-10-21, Spectronaut 13.8 -> 20.1).
Every protein database a Spectronaut search used -- contaminant lists included -- is embedded
in its .sne, entry for entry. `sne_databases()` reads them back: file name, entry count, gene
count and an order-independent content hash. On a bat plasma search that hash equals the hash of
the FASTA file sitting next to the .sne (60,832 entries, 21,396 genes), so this is the database
that was searched, not a guess from a filename. It is the only source for the ~1,800 archived
Spectronaut searches whose exports kept no setup file, and the only way to know entries-per-gene
when the FASTA itself is gone. Searches commonly use two or three databases (contaminants, the
proteome, an iRT or virus file), which is why the result is a LIST: "the" database is whichever
one a reader cares about, not whichever was listed first.
"""
from __future__ import annotations

import glob
import gzip
import hashlib
import os
import re
import struct
import zlib

# A --fasta value on the command line DIA-NN echoes into its log, read the way DIA-NN reads its own
# arguments (diann.cpp 1.8, arguments()): argv is joined with single spaces, UNQUOTED -- the shell or
# Windows already removed any quotes -- and each option's value runs to the next "--", trimmed. So a
# path with spaces is logged as is, and only this reading gets it back: the DIA-NN 1.8.1 log of
# PXD022216 has `--fasta C:\SpectralLib\human - 2021-02-16-reviewed-contam-UP000005640.fasta
# --met-excision`, which \S+ cut to `C:\SpectralLib\human` (and "Universal Protein
# Contaminants.fasta" to "Universal", which then did not even look like a contaminant library).
# "fasta " needs its space, as in DIA-NN: --fasta-search / --fasta-filter are never read as one,
# and DIA-NN has no `--fasta=path` form. The twin of the skill's fran_deposit.logged_fastas.
_DIANN_FASTA = re.compile(r"--fasta (.*?)(?=--|$)", re.M)


def diann_logged_fastas(text: str) -> list[str]:
    """Every --fasta value in DIA-NN log text (see _DIANN_FASTA), trimmed, repeatable; surrounding
    quotes, which only a hand-written command line would carry, are dropped. The ONE reading of a
    logged --fasta in FRAN: detect() and auto_ingest's manifest cross-check both use it."""
    vals = (m.strip().strip("\"'").strip() for m in _DIANN_FASTA.findall(text.replace("\r", "")))
    return [v for v in vals if v]
# "├─ Original File: gg_HoSa_rUP5640.fasta"  (box-drawing prefix varies; anchor on the label)
_SN_ORIGINAL = re.compile(r"Original File:\s*(.+?\.fasta)\s*$", re.I | re.M)
_SN_DB_BLOCK = re.compile(r"Protein Databases Used(.*?)(?:\n\s*[├└]─ \w|\Z)", re.I | re.S)


def _head(path: str, nbytes: int = 400_000) -> str:
    try:
        with open(path, errors="replace") as fh:
            return fh.read(nbytes)
    except OSError:
        return ""


def _cmdline_block(path: str, nlines: int = 200, cap: int = 8_000_000) -> str:
    """The first `nlines` lines of a log, however long any one of them is.

    DIA-NN echoes its entire command line on ONE line a few lines in (line 6 under 2.7.0, after
    the banner -- so reading "the first line" would not do), and --fasta sits after one --f per
    raw file. Measured on real logs that is ~150 bytes per raw: --fasta lands ~5 KB in for 23
    runs, and past a fixed 40 KB window somewhere above ~250. CORPUS_WORKLIST tops out at 239
    runs with four sets over 150, on Flinders paths longer than the ones measured -- near enough
    that the FASTA would simply not be found and detect() would fall through in silence, which
    is the failure mode worth removing rather than re-tuning a byte count for.

    `nlines` is deliberately far past where the command line has ever been seen. Checked on 94
    logs across DIA-NN 2.6.0, 2.6.1 and 2.7.0: it is line 6 in every one, and "--fasta " occurs
    on exactly ONE line per log -- so reading well beyond line 6 cannot pick up a second, wrong
    occurrence, and the margin costs nothing. 1.8.x is not represented in this corpus and its
    banner length is therefore unverified; the margin is for it.

    `cap` is only a guard against a pathological single-line file, not a window on the content.
    """
    out, total = [], 0
    try:
        with open(path, errors="replace") as fh:
            for _ in range(nlines):
                line = fh.readline()
                if not line:
                    break
                out.append(line)
                total += len(line)
                if total >= cap:
                    break
    except OSError:
        return ""
    return "".join(out)


def _first(patterns: list[str], root: str) -> list[str]:
    hits: list[str] = []
    for pat in patterns:
        hits.extend(sorted(glob.glob(os.path.join(root, pat))))
    return hits


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


# ---- every database a search used, with what is in it ---------------------------------------
#
# The same three numbers for a FASTA file on disk and for a database embedded in a .sne, so the
# two can be compared: entries, distinct genes, and a content hash that does not depend on line
# wrapping or entry order (sha256 over the sorted per-entry sha256 of "header\nSEQUENCE").

_GN = re.compile(r"\bGN=(\S+)")
# The first token of a header names a contaminant-library entry: "sp|Cont_P00722|BGAL_ECOLI",
# "CON__P02769". Same prefixes organism.is_contaminant_group() recognises.
_CONT_ACC = re.compile(r"(?:^|\|)(?:Cont_|CON__)", re.I)
# Contaminant lists by NAME: "Universal Contaminant Protein FASTA", and the cRAP derivatives this
# lab builds ("Crap_uniprot_No_human_MRSonbeadV2.fasta"), which the old "contaminant" hint missed.
_CONTAM_NAME = re.compile(r"contaminant|crap", re.I)
GENE_MIN_SHARE = 0.50   # below this share of entries carrying GN=, a gene count is not reported


class _Digest:
    """Accumulates entries one at a time."""

    def __init__(self):
        self.n = 0
        self.n_gn = 0
        self.n_cont = 0
        self.genes: set[str] = set()
        self.digests: list[bytes] = []

    def add(self, header: str, seq: str) -> None:
        self.n += 1
        m = _GN.search(header)
        if m:
            self.n_gn += 1
            self.genes.add(m.group(1))
        if _CONT_ACC.search(header.split(None, 1)[0] if header else ""):
            self.n_cont += 1
        self.digests.append(hashlib.sha256((header + "\n" + seq.upper()).encode()).digest())

    def result(self) -> dict:
        genes_ok = bool(self.n) and self.n_gn >= GENE_MIN_SHARE * self.n
        return {
            "n_entries": self.n,
            "n_genes": len(self.genes) if genes_ok else None,
            # entries carrying GN=: the numerator for entries-per-gene. n_entries is the wrong one
            # -- unnamed entries have no gene to share, so dividing by n_genes inflates the
            # ratio: a one-protein-per-gene dog proteome read 1.63 (20,992 / 12,859).
            "n_gene_entries": self.n_gn if genes_ok else None,
            "content_sha256": hashlib.sha256(b"".join(sorted(self.digests))).hexdigest(),
            "contaminant_entry_share": round(self.n_cont / self.n, 3) if self.n else None,
        }


# A name only marks a contaminant LIST when the file is list-sized. "Human_HIV_crap_nonredun.fasta"
# (74,792 entries) and a human+vaccinia+contaminants file are proteomes with contaminants
# appended -- they ARE the search database, the same trap detect() documents below.
CONTAM_LIST_MAX_ENTRIES = 2000
# With no entry count to go on, a proteome identifier in the name outweighs a contaminant word
_PROTEOME_NAME = re.compile(r"UP\d{6,}|proteome|uniprotkb", re.I)


def is_contaminant_db(file_name: str | None, contaminant_entry_share: float | None,
                      n_entries: int | None = None) -> bool:
    """A contaminant list: most of its entries carry contaminant accessions, or its name says so
    and it is list-sized. Size unknown (a name from a setup file, a FASTA not readable here): the
    name decides, unless it also names a proteome. Recorded alongside the search databases,
    never dropped."""
    if (contaminant_entry_share or 0) >= 0.5:
        return True
    name = file_name or ""
    if not _CONTAM_NAME.search(name):
        return False
    if n_entries is None:
        return not _PROTEOME_NAME.search(name)
    return n_entries <= CONTAM_LIST_MAX_ENTRIES


_DIGEST_CACHE: dict[tuple, dict] = {}


def fasta_digest(path: str) -> dict | None:
    """{md5, n_entries, n_genes, content_sha256, contaminant_entry_share} for a readable FASTA
    file; None otherwise. One streaming pass; memoised on (realpath, size, mtime) because a lab
    shares a handful of proteomes across thousands of searches."""
    if not path or not os.path.isfile(path):
        return None
    try:
        st = os.stat(path)
        key = (os.path.realpath(path), st.st_size, st.st_mtime_ns)
        if key in _DIGEST_CACHE:
            return _DIGEST_CACHE[key]
        md5 = hashlib.md5()  # noqa: S324 - provenance fingerprint, not a security control
        dg = _Digest()
        header, seq = None, []
        with open(path, "rb") as fh:
            for raw in fh:
                md5.update(raw)
                line = raw.decode("utf-8", "replace")
                if line.startswith(">"):
                    if header is not None:
                        dg.add(header, "".join(seq))
                    header, seq = line[1:].strip(), []
                elif header is not None:
                    seq.append("".join(line.split()))
        if header is not None:
            dg.add(header, "".join(seq))
        out = {"md5": md5.hexdigest(), **dg.result()}
        _DIGEST_CACHE[key] = out
        return out
    except OSError:
        return None


# ---- the .sne reader ---------------------------------------------------------------------------
#
# Layout, read off real files (Spectronaut 13.8 .. 20.1); there is no published format.
#
#   * Text is length-prefixed UTF-16LE: an int32 byte count, then the string. (Plain `strings`
#     finds nothing in a .sne for this reason; `strings -el` does.)
#   * Each protein database is a run of such strings -- display name, original file name
#     ("gg_rousetusAegiptiacus-tUP000593571.fasta"), parsing rule, "UniProt FASTA"/"NCBI" -- then
#     ONE gzip member holding every entry as alternating length-prefixed UTF-16 records:
#     ">header", then the sequence.
#   * From Spectronaut 19 the whole file after a short header is additionally wrapped: a chain of
#     [int32 compressed length][gzip member], each member inflating to the next piece of the same
#     stream described above. Unwrap that first and everything else is unchanged.
#
# Read cost: the databases sit in the first ~20 MB of the stream in every file checked, so
# reading stops a fixed distance past the last database found instead of reading multi-GB files
# to the end.

_GZ = b"\x1f\x8b\x08"
_FASTA_EXT = (".fasta", ".fas", ".fa", ".faa", ".fasta.gz")
_SNE_BLOCK = 8 << 20
_SNE_NAME_WINDOW = 1 << 16


def _sne_strings(buf: bytes, lo: int, hi: int) -> list[str]:
    """The length-prefixed UTF-16 strings in buf[lo:hi], read as the file stores them.

    Tokenising matters: a pattern match on "printable UTF-16 ending in .fasta" both missed and
    misread names in real files. The int32 length that follows a name can itself look like a
    UTF-16 letter (72 bytes = 'H'), so a lookahead rejected "uniprot-proteome_UP000009084.fasta";
    and taking the last match picked a later setting ("iRT peptides-fusion.fasta Rule") over the
    file name ("iRT.fasta")."""
    out, q = [], lo
    while q + 4 <= hi:
        (n,) = struct.unpack_from("<i", buf, q)
        if 2 <= n <= 1600 and not n % 2 and q + 4 + n <= hi:
            t = buf[q + 4:q + 4 + n]
            if not any(t[1::2]) and all(32 <= c < 127 for c in t[0::2]):
                out.append(t.decode("utf-16le"))
                q += 4 + n
                continue
        q += 1
    return out


def _sne_stream(fh, block: int = _SNE_BLOCK):
    """Yield a .sne's logical byte stream: the file itself up to Spectronaut 18, the inflated
    member chain from 19 on."""
    head = fh.read(1 << 16)
    j = head.find(_GZ)
    chained = False
    if j >= 4:
        (n,) = struct.unpack_from("<i", head, j - 4)
        if n > 0:
            fh.seek(j + n)
            nxt = fh.read(7)
            chained = len(nxt) == 7 and nxt[4:7] == _GZ
    if not chained:
        fh.seek(0)
        while True:
            chunk = fh.read(block)
            if not chunk:
                return
            yield chunk
    fh.seek(j - 4)
    yield head[:j - 4]
    while True:
        lb = fh.read(4)
        if len(lb) < 4:
            return
        (n,) = struct.unpack("<i", lb)
        if n <= 0 or n > (1 << 30):
            return
        member = fh.read(n)
        if not member.startswith(_GZ):
            return
        try:
            yield gzip.decompress(member)
        except (OSError, EOFError, zlib.error):
            return


class _Buf:
    """A window onto the stream that can be extended on demand and trimmed from the front."""

    def __init__(self, it):
        self.it = it
        self.buf = b""
        self.pos = 0          # stream offset of buf[0]
        self.done = False

    def fill(self, n: int) -> None:
        parts = [self.buf]
        have = len(self.buf)
        while have < n and not self.done:
            try:
                c = next(self.it)
            except StopIteration:
                self.done = True
                break
            parts.append(c)
            have += len(c)
        if len(parts) > 1:
            self.buf = b"".join(parts)

    def drop(self, k: int) -> None:
        if k > 0:
            self.buf = self.buf[k:]
            self.pos += k


def _sne_entries(dg: _Digest, data: bytes, carry: list) -> bool:
    """Feed inflated database bytes; `carry` holds [unconsumed bytes, pending header]. False when
    the records stop looking like records (the parse is then reported as not ok)."""
    b = carry[0] + data
    i = 0
    while i + 4 <= len(b):
        (n,) = struct.unpack_from("<i", b, i)
        if n < 0 or n % 2 or n > (64 << 20):
            return False
        if i + 4 + n > len(b):
            break
        s = b[i + 4:i + 4 + n].decode("utf-16le", "replace")
        i += 4 + n
        if s.startswith(">"):
            if carry[1] is not None:
                dg.add(carry[1], "")
            carry[1] = s[1:].strip()
        elif carry[1] is not None:
            dg.add(carry[1], s)
            carry[1] = None
    carry[0] = b[i:]
    return True


def _is_entry_stream(peek: bytes) -> bool:
    if len(peek) < 6 or peek[4:6] != b">\x00":
        return False
    (n,) = struct.unpack_from("<i", peek, 0)
    return 2 <= n <= (1 << 16) and n % 2 == 0


def sne_databases(path: str, *, after_last: int = 64 << 20, give_up: int = 768 << 20,
                  ) -> list[dict] | None:
    """Every protein database embedded in a Spectronaut .sne, in the order stored:
    [{file_name, n_entries, n_genes, content_sha256, contaminant_entry_share, is_contaminant,
      parse_ok}].

    [] when the file was read and holds no recognisable database; None when it could not be
    read. Never raises. `after_last` bounds how far past the last database the stream is read,
    `give_up` how far it is read when no database has turned up at all."""
    if not path or not os.path.isfile(path):
        return None
    dbs: list[dict] = []
    try:
        with open(path, "rb") as fh:
            s = _Buf(_sne_stream(fh))
            i = 0                 # scan position within s.buf
            prev_end = 0          # stream offset where the previous database ended
            last_end = None
            while True:
                s.fill(i + _SNE_BLOCK)
                j = s.buf.find(_GZ, i)
                if j < 0:
                    if s.done:
                        break
                    i = max(i, len(s.buf) - 2)    # a magic may straddle the block edge
                else:
                    s.fill(j + 65536)
                    try:
                        peek = zlib.decompressobj(31).decompress(s.buf[j:j + 65536], 64)
                    except zlib.error:
                        peek = b""
                    if not _is_entry_stream(peek):
                        i = j + 1
                    else:
                        # A database block opens [display name, file name, organism, ...]: the
                        # file name is the FIRST FASTA-named string after the previous database.
                        lo = max(0, prev_end - s.pos, j - _SNE_NAME_WINDOW)
                        names = [t for t in _sne_strings(s.buf, lo, j)
                                 if t.lower().endswith(_FASTA_EXT)]
                        name = names[0].replace("\\", "/").rsplit("/", 1)[-1] if names else None
                        d = zlib.decompressobj(31)
                        dg, carry, ok, k = _Digest(), [b"", None], True, j
                        while not d.eof:
                            s.fill(k + _SNE_BLOCK)
                            piece = s.buf[k:k + _SNE_BLOCK]
                            if not piece:
                                ok = False          # the stream ended inside the database
                                break
                            out = d.decompress(piece)
                            if ok:
                                ok = _sne_entries(dg, out, carry)
                            k += len(piece) - len(d.unused_data)
                            if k > 2 * _SNE_BLOCK:  # big databases: do not hold them whole
                                s.drop(k - _SNE_BLOCK)
                                k = _SNE_BLOCK
                        if carry[1] is not None:
                            dg.add(carry[1], "")
                        r = dg.result()
                        dbs.append({"file_name": name, **r,
                                    "is_contaminant": is_contaminant_db(
                                        name, r["contaminant_entry_share"], r["n_entries"]),
                                    "parse_ok": ok and r["n_entries"] > 0})
                        prev_end = last_end = s.pos + k
                        i = k
                here = s.pos + i
                if last_end is not None and here - last_end > after_last:
                    break
                if last_end is None and here > give_up:
                    break
                if i > _SNE_BLOCK + _SNE_NAME_WINDOW:
                    s.drop(i - _SNE_NAME_WINDOW)
                    i = _SNE_NAME_WINDOW
    except (OSError, zlib.error, struct.error):
        return dbs or None
    return dbs


def detect(engine: str, report_path: str | None, search_dir: str | None = None,
           search_roots: list[str] | None = None) -> dict | None:
    """Best-effort database provenance, or None. Never raises — a missing FASTA must never
    fail an ingest, exactly as with engine_version.detect().

    Returns {"fasta_path", "fasta_md5", "fasta_n_proteins", "contaminant_lib"} with any
    unknown field set to None: the delimp_searches summary of detect_all(), naming the
    database main_database() picks. The full list is detect_all()'s.
    """
    found = detect_all(engine, report_path, search_dir, search_roots)
    return summarize(found["databases"]) if found else None


# iRT / retention-time calibrant files: spiked-in standards, never the search database
_IRT_NAME = re.compile(r"(?:^|[^a-z])irt(?:[^a-z]|$)", re.I)


def main_database(dbs: list[dict]) -> dict | None:
    """The database a single-valued summary should name: the LARGEST that is neither a
    contaminant list nor an iRT file -- never simply the first listed, which recorded cRAP lists
    (54 searches), iRT.fasta (5) and a 10-entry virus file as "the" database. Falls back to the
    largest of anything when every entry is a contaminant or iRT list."""
    if not dbs:
        return None
    real = [d for d in dbs if not d.get("is_contaminant")
            and not _IRT_NAME.search(d.get("file_name") or "")]
    pool = real or dbs
    # unknown counts sort below known ones; ties keep the engine's own order
    return max(enumerate(pool), key=lambda t: (t[1].get("n_entries") is not None,
                                               t[1].get("n_entries") or 0, -t[0]))[1]


def summarize(dbs: list[dict]) -> dict | None:
    """The delimp_searches.fasta_* summary for a list of databases."""
    main = main_database(dbs)
    if main is None:
        return None
    contam = next((d for d in dbs if d.get("is_contaminant") and d is not main), None)
    return {"fasta_path": main.get("file_path") or main.get("file_name"),
            "fasta_md5": main.get("file_md5"),
            "fasta_n_proteins": main.get("n_entries"),
            "contaminant_lib": contam.get("file_name") if contam else None}


def _db_from_file(name: str, path: str | None, roots: list[str]) -> dict:
    """A database known by name (a setup file, a DIA-NN --fasta), counted when it can be read."""
    resolved = _resolve(path or name, roots) or (path if path and os.path.isfile(path) else None)
    dg = fasta_digest(resolved) if resolved else None
    base = os.path.basename((path or name).replace("\\", "/"))
    row = {"file_name": base, "file_path": path or resolved, "file_md5": None,
           "n_entries": None, "n_genes": None, "n_gene_entries": None, "content_sha256": None,
           "contaminant_entry_share": None}
    if dg:
        row.update({k: dg[k] for k in ("n_entries", "n_genes", "n_gene_entries",
                                       "content_sha256", "contaminant_entry_share")},
                   file_path=resolved, file_md5=dg["md5"])
    row["is_contaminant"] = is_contaminant_db(base, row["contaminant_entry_share"],
                                              row["n_entries"])
    return row


def _verify_on_disk(db: dict, roots: list[str]) -> None:
    """Attach file_path/file_md5 to a .sne database only for a file whose CONTENT matches: a
    FASTA with the right name beside the .sne is not proof (proteomes get re-downloaded under
    the same name)."""
    db.setdefault("file_path", None)
    db.setdefault("file_md5", None)
    if not db.get("file_name") or not db.get("content_sha256"):
        return
    cand = _resolve(db["file_name"], roots)
    dg = fasta_digest(cand) if cand else None
    if dg and dg["content_sha256"] == db["content_sha256"]:
        db["file_path"], db["file_md5"] = cand, dg["md5"]


_DETECT_CACHE: dict[tuple, dict | None] = {}


def detect_all(engine: str, report_path: str | None, search_dir: str | None = None,
               search_roots: list[str] | None = None) -> dict | None:
    """Every database the search used: {"databases": [...], "source", "source_path"}, or None.

    Each database is {file_name, file_path, file_md5, n_entries, n_genes, content_sha256,
    contaminant_entry_share, is_contaminant}, in the engine's own order, contaminant lists
    included. Sources, best first:
      sne        search_dir IS a Spectronaut .sne (the Windows ingest records the .sne as
                 output_dir): the embedded databases, exact counts even with the FASTA gone.
      sn_setup   an ExperimentSetupOverview / setup.txt beside the report: names, counted only
                 where the file resolves.
      diann_log  every --fasta on DIA-NN's logged command line.
    Never raises. Memoised per process: corpus_ingest asks twice (organism, then provenance)."""
    key = (engine, report_path, search_dir, tuple(search_roots or ()))
    if key in _DETECT_CACHE:
        return _DETECT_CACHE[key]
    out = None
    try:
        out = _detect_all(engine, report_path, search_dir, search_roots)
    except Exception:  # noqa: BLE001 - provenance is never worth failing an ingest over
        out = None
    _DETECT_CACHE[key] = out
    return out


def _detect_all(engine, report_path, search_dir, search_roots):
    eng = (engine or "").lower()
    if (eng.startswith("spectronaut") and search_dir and search_dir.lower().endswith(".sne")
            and os.path.isfile(search_dir)):
        dbs = sne_databases(search_dir)
        if dbs:
            roots = list(search_roots or []) + [os.path.dirname(search_dir)]
            for d in dbs:
                _verify_on_disk(d, roots)
            return {"databases": dbs, "source": "sne", "source_path": search_dir}

    roots: list[str] = []
    for cand in (search_dir, os.path.dirname(report_path or "")):
        if cand and os.path.isdir(cand) and cand not in roots:
            roots.append(cand)
    if not roots:
        return None
    extra = list(search_roots or []) + roots

    if eng.startswith("spectronaut"):
        for root in roots:
            for p in _first(["*ExperimentSetupOverview*.txt", "*setup.txt", "*.setup.txt"], root):
                txt = _head(p)
                block = _SN_DB_BLOCK.search(txt)
                names = _SN_ORIGINAL.findall(block.group(1) if block else txt)
                if names:
                    return {"databases": [_db_from_file(n.strip(), None, extra) for n in names],
                            "source": "sn_setup", "source_path": p}
        return None
    # DIA-NN's log, and everything that embeds DIA-NN: FragPipe's bundled copy writes the same
    # log, and Radiant/Fulcrum is a container around one. engine_version.py falls through to the
    # DIA-NN parse for unknown engines for the same reason. The regex requires a literal
    # `--fasta`, so a log from something else simply does not match.
    for root in roots:
        for p in _first(["report.log.txt", "*.log.txt", "*.log",
                         os.path.join("dia-quant-output", "report.log.txt")], root):
            paths = diann_logged_fastas(_cmdline_block(p))
            if paths:
                return {"databases": [_db_from_file(x, x, extra) for x in paths],
                        "source": "diann_log", "source_path": p}
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
