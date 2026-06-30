"""Protein sequence-coverage mapping for the corpus browser.

The corpus has no stored protein sequences and no explicit peptide->protein link,
so we fetch the canonical sequence from UniProt by accession and map observed
corpus peptides onto it by (I/L-normalized) substring match. FASTA-independent;
works for any UniProt accession the user opens. Sequences are immutable -> cached.
"""
import re
import threading
import urllib.parse
import urllib.request

_seq_cache: dict[str, str] = {}
_lock = threading.Lock()

# RefSeq / GenBank protein accessions (NCBI nr) — UniProt doesn't serve these, so they need
# NCBI efetch. RefSeq protein prefixes: NP_/XP_/WP_/YP_/AP_/ZP_/XR_; GenBank protein = 3 letters
# + 5+ digits (e.g. KAB1234567.1, EAW12345.1). Versioned (".1") accessions are common from nr.
_REFSEQ_RE = re.compile(r"^(?:[NXYZAW]P|XR)_\d", re.I)
_GENBANK_PROT_RE = re.compile(r"^[A-Z]{3}\d{5,}(?:\.\d+)?$")


def _fetch_fasta(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "delimp-corpus-browser"})
    txt = urllib.request.urlopen(req, timeout=8).read().decode()
    return "".join(l.strip() for l in txt.splitlines() if l and not l.startswith(">"))


def _fetch_ncbi_sequence(acc: str) -> str:
    """RefSeq/GenBank protein sequence via NCBI E-utilities efetch (db=protein, FASTA)."""
    try:
        url = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
               f"?db=protein&id={urllib.parse.quote(acc)}&rettype=fasta&retmode=text"
               "&tool=delimp-corpus-browser")
        return _fetch_fasta(url)
    except Exception:  # noqa: BLE001
        return ""


def fetch_uniprot_sequence(accession: str) -> str:
    """Canonical protein sequence for the leading accession of a group. Uses UniProt for
    UniProt accessions and NCBI efetch for RefSeq/GenBank (nr) accessions like XP_/WP_/NP_,
    with a cross-fallback if the first source returns nothing. '' on failure. Cached."""
    acc = (accession or "").split(";")[0].strip()
    # strip contaminant-DB prefixes (Cont_/cRAP-) so 'Cont_P35527' resolves to the real entry
    # 'P35527' (KRT9) — without this, the coverage map 404s for every cRAP/contaminant protein.
    acc = re.sub(r"^(crap|cont)[-_]?", "", acc, flags=re.I) or acc
    if not acc:
        return ""
    with _lock:
        if acc in _seq_cache:
            return _seq_cache[acc]
    is_ncbi = bool(_REFSEQ_RE.match(acc) or _GENBANK_PROT_RE.match(acc))
    seq = ""
    try:
        if is_ncbi:
            seq = _fetch_ncbi_sequence(acc)
        else:
            seq = _fetch_fasta(f"https://rest.uniprot.org/uniprotkb/{acc}.fasta")
        if not seq:  # the prefix heuristic isn't perfect -> try the other source
            seq = (_fetch_fasta(f"https://rest.uniprot.org/uniprotkb/{acc}.fasta")
                   if is_ncbi else _fetch_ncbi_sequence(acc))
    except Exception:  # noqa: BLE001 - missing sequence degrades gracefully in the UI
        seq = ""
    with _lock:
        _seq_cache[acc] = seq
    return seq


def _il(s: str) -> str:
    # I and L are isobaric / often indistinguishable in MS — normalize for matching
    return s.replace("I", "L")


def map_coverage(sequence: str, peptides: list[dict]) -> dict:
    """Map peptides (each dict carries 'stripped_seq' + count fields) onto the
    sequence; return per-peptide positions + overall coverage %."""
    L = len(sequence)
    if not L:
        return {"length": 0, "coverage_pct": 0.0, "n_mapped": 0, "peptides": []}
    nseq = _il(sequence)
    covered = bytearray(L)
    out = []
    for p in peptides:
        pep = (p.get("stripped_seq") or "").strip().upper()
        if not pep:
            continue
        npep = _il(pep); plen = len(pep); starts = []
        i = nseq.find(npep)
        while i != -1:
            starts.append(i)
            for j in range(i, min(i + plen, L)):
                covered[j] = 1
            i = nseq.find(npep, i + 1)
        if starts:
            rec = {k: p[k] for k in p if k != "stripped_seq"}
            rec.update({"stripped_seq": pep, "start": starts[0] + 1,
                        "end": starts[0] + plen, "n_occ": len(starts)})
            out.append(rec)
    cov = 100.0 * sum(covered) / L
    out.sort(key=lambda x: x["start"])
    return {"length": L, "coverage_pct": round(cov, 1), "n_mapped": len(out), "peptides": out}
