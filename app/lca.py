"""Peptide taxonomic LCA via the Unipept API (pept2lca).

For a tryptic peptide, Unipept returns the lowest common ancestor of every UniProt
taxon whose proteins contain that sequence — i.e. how taxon-specific the peptide is
(species-specific vs mammal-wide vs universal). I/L are equated (isobaric in MS).
Results are immutable -> cached. Network/لookup failure degrades to None.
"""
import json
import threading
import urllib.parse
import urllib.request

_cache: dict[str, dict | None] = {}
_lock = threading.Lock()

_RANKS = ["superkingdom", "kingdom", "phylum", "class", "order",
          "family", "genus", "species"]


def peptide_lca(seq: str) -> dict | None:
    seq = (seq or "").strip().upper()
    if not seq:
        return None
    with _lock:
        if seq in _cache:
            return _cache[seq]
    out = None
    try:
        params = urllib.parse.urlencode(
            {"input[]": seq, "equate_il": "true", "extra": "true", "names": "true"},
            doseq=True,
        )
        url = "https://api.unipept.ugent.be/api/v2/pept2lca.json?" + params
        req = urllib.request.Request(
            url, headers={"User-Agent": "delimp-corpus-browser", "Accept": "application/json"}
        )
        data = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
        if data:
            r = data[0]
            lineage = [{"rank": rk, "name": r.get(rk + "_name"),
                        "taxon_id": r.get(rk + "_id")}
                       for rk in _RANKS if r.get(rk + "_name")]
            out = {"peptide": seq, "taxon_id": r.get("taxon_id"),
                   "taxon_name": r.get("taxon_name"), "taxon_rank": r.get("taxon_rank"),
                   "lineage": lineage}
    except Exception:  # noqa: BLE001 - missing LCA degrades gracefully in the UI
        out = None
    with _lock:
        _cache[seq] = out
    return out


_prot_cache: dict[str, dict | None] = {}
_prot_lock = threading.Lock()


def peptide_proteins(seq: str, max_proteins: int = 3000) -> dict | None:
    """All UniProt proteins containing this tryptic peptide, ACROSS ALL TAXA
    (= the homologue set), via Unipept pept2prot. Grouped by organism. I/L equated.
    Cached. A conserved peptide can hit thousands of proteins -> capped + summarized."""
    seq = (seq or "").strip().upper()
    if not seq:
        return None
    with _prot_lock:
        if seq in _prot_cache:
            return _prot_cache[seq]
    out = None
    try:
        params = urllib.parse.urlencode(
            {"input[]": seq, "equate_il": "true", "extra": "true"}, doseq=True
        )
        url = "https://api.unipept.ugent.be/api/v2/pept2prot.json?" + params
        req = urllib.request.Request(
            url, headers={"User-Agent": "delimp-corpus-browser", "Accept": "application/json"}
        )
        data = json.loads(urllib.request.urlopen(req, timeout=25).read().decode())
        total = len(data)
        by_org: dict[tuple, list] = {}
        for r in data[:max_proteins]:
            key = (r.get("taxon_name") or "unknown", r.get("taxon_id"))
            by_org.setdefault(key, []).append(
                {"uniprot_id": r.get("uniprot_id"), "protein_name": r.get("protein_name")}
            )
        orgs = [{"organism": k[0], "taxon_id": k[1], "n": len(v), "proteins": v[:30]}
                for k, v in by_org.items()]
        orgs.sort(key=lambda x: -x["n"])
        out = {"peptide": seq, "total_proteins": total, "n_organisms": len(by_org),
               "capped": total > max_proteins, "organisms": orgs[:80]}
    except Exception:  # noqa: BLE001
        out = None
    with _prot_lock:
        _prot_cache[seq] = out
    return out
