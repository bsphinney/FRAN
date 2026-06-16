"""Protein sequence-coverage mapping for the corpus browser.

The corpus has no stored protein sequences and no explicit peptide->protein link,
so we fetch the canonical sequence from UniProt by accession and map observed
corpus peptides onto it by (I/L-normalized) substring match. FASTA-independent;
works for any UniProt accession the user opens. Sequences are immutable -> cached.
"""
import threading
import urllib.request

_seq_cache: dict[str, str] = {}
_lock = threading.Lock()


def fetch_uniprot_sequence(accession: str) -> str:
    """Canonical protein sequence for a UniProt accession (leading accession of a
    group). Returns '' on any failure (network, unknown accession). Cached."""
    acc = (accession or "").split(";")[0].strip()
    if not acc:
        return ""
    with _lock:
        if acc in _seq_cache:
            return _seq_cache[acc]
    seq = ""
    try:
        url = f"https://rest.uniprot.org/uniprotkb/{acc}.fasta"
        req = urllib.request.Request(url, headers={"User-Agent": "delimp-corpus-browser"})
        txt = urllib.request.urlopen(req, timeout=15).read().decode()
        seq = "".join(l.strip() for l in txt.splitlines() if l and not l.startswith(">"))
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
