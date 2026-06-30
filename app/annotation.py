"""Human-readable protein identity/function for a peptide.

Uses the peptide's homologue set (Unipept pept2prot, via lca.peptide_proteins) to
pick a representative protein, then fetches that protein's UniProt annotation
(gene, full name, keywords, function, subcellular location, GO) to render a plain-
language summary — "ALB — Serum albumin · Blood, Secreted · binds/transports...".
Keywords give the 'commonalities' (Blood protein, Enzyme, Transport, Immunity…).
Cached; degrades to None on failure.
"""
import json
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter

# Wikipedia asks for a descriptive User-Agent with contact; a bare UA can be rate-limited/blocked
# (which silently poisoned the wiki cache with None for EVERY organism). https://w.wiki/CX6
_WIKI_UA = "FRAN-corpus/1.0 (https://fran.stan-proteomics.org; UC Davis Proteomics Core) python-urllib"


def clean_accession(acc: str) -> str:
    """Strip contaminant-DB prefixes (cRAP-, Cont_/Cont-) to leave the real UniProt accession,
    e.g. 'cRAP-P02768' -> 'P02768', 'Cont_P04264' -> 'P04264'. Unchanged if no such prefix.
    Without this, contaminant accessions 404 on UniProt and produce dead protein links."""
    a = (acc or "").strip()
    stripped = re.sub(r"^(crap|cont)[-_]?", "", a, flags=re.I)
    return stripped or a

_cache: dict[str, dict | None] = {}
_lock = threading.Lock()
_wiki_cache: dict[str, tuple] = {}  # title -> (result_or_None, monotonic_ts)
_wiki_lock = threading.Lock()


_WIKI_NEG_TTL = 900  # seconds to honor a NEGATIVE (None) result before retrying — so a transient
                     # network/rate-limit blip doesn't permanently mark an organism "no page".


def _http_json(url: str, timeout: int = 12):
    req = urllib.request.Request(url, headers={"User-Agent": _WIKI_UA, "Accept": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())


def _wiki_summary(title: str) -> dict | None:
    """One Wikipedia REST summary lookup -> normalized dict or None."""
    try:
        d = _http_json("https://en.wikipedia.org/api/rest_v1/page/summary/"
                       + urllib.parse.quote(title.replace(" ", "_")))
    except Exception:  # noqa: BLE001
        return None
    if d.get("type") == "standard" and d.get("extract"):
        return {"extract": d["extract"],
                "url": (d.get("content_urls", {}).get("desktop", {}) or {}).get("page"),
                "title": d.get("title"),
                "image": ((d.get("thumbnail") or {}).get("source")
                          or (d.get("originalimage") or {}).get("source")),
                "source": "Wikipedia"}
    return None


def _wiki_opensearch(query: str) -> str | None:
    """Best-matching Wikipedia article title for a query (catches name variants), else None."""
    try:
        d = _http_json("https://en.wikipedia.org/w/api.php?action=opensearch&limit=1&namespace=0"
                       "&format=json&search=" + urllib.parse.quote(query))
        return d[1][0] if d and len(d) > 1 and d[1] else None
    except Exception:  # noqa: BLE001
        return None


def _gbif_blurb(name: str) -> dict | None:
    """Fallback for organisms with no Wikipedia page: GBIF taxonomy + a sourced description.
    GBIF covers virtually every named species (real source, not fabricated)."""
    try:
        m = _http_json("https://api.gbif.org/v1/species/match?name=" + urllib.parse.quote(name))
        key = m.get("usageKey")
        if not key:
            return None
        rank = (m.get("rank") or "").lower()
        lineage = " · ".join(filter(None, [m.get("kingdom"), m.get("phylum"), m.get("class"),
                                           m.get("order"), m.get("family")]))
        extract = None
        try:  # GBIF stores free-text descriptions for many taxa
            ds = _http_json(f"https://api.gbif.org/v1/species/{key}/descriptions?limit=20")
            for r in (ds.get("results") or []):
                if r.get("description") and len(r["description"]) > 40:
                    extract = re.sub(r"<[^>]+>", "", r["description"]).strip()
                    break
        except Exception:  # noqa: BLE001
            pass
        vern = None
        try:
            vd = _http_json(f"https://api.gbif.org/v1/species/{key}/vernacularNames?limit=20")
            for r in (vd.get("results") or []):
                if (r.get("language") == "eng") and r.get("vernacularName"):
                    vern = r["vernacularName"]; break
        except Exception:  # noqa: BLE001
            pass
        if not extract:
            # no free-text description anywhere -> synthesize a factual one-liner from the taxonomy
            sci = m.get("scientificName") or name
            extract = (f"{sci}" + (f" ({vern})" if vern else "")
                       + (f" — a {rank} in the lineage {lineage}." if lineage else
                          " — a taxon recorded in the GBIF backbone."))
        elif vern and vern.lower() not in extract.lower():
            extract = f"{vern}. {extract}"
        return {"extract": extract, "title": m.get("scientificName") or name,
                "url": f"https://www.gbif.org/species/{key}", "image": None, "source": "GBIF"}
    except Exception:  # noqa: BLE001
        return None


def fetch_wikipedia(title: str) -> dict | None:
    """Encyclopedia blurb for an organism/gene: Wikipedia summary -> opensearch best-title ->
    GBIF taxonomy/description fallback. Returns {extract,url,title,image,source} or None.
    Positive results cached forever; NEGATIVE results cached only ~15 min so a transient
    failure (the bug that showed 'no page' for Human/everything) doesn't stick."""
    title = (title or "").strip()
    if not title:
        return None
    now = time.monotonic()
    with _wiki_lock:
        hit = _wiki_cache.get(title)
        if hit is not None:
            out, ts = hit
            if out is not None or (now - ts) < _WIKI_NEG_TTL:
                return out
    out = _wiki_summary(title)
    if out is None:
        alt = _wiki_opensearch(title)
        if alt and alt.lower() != title.lower():
            out = _wiki_summary(alt)
    if out is None:
        out = _gbif_blurb(title)
    with _wiki_lock:
        _wiki_cache[title] = (out, now)
    return out


def fetch_uniprot_entry(acc: str) -> dict | None:
    # strip contaminant prefixes (cRAP-/Cont_) so 'cRAP-P02768' resolves to the real entry
    acc = clean_accession((acc or "").split(";")[0].strip())
    if not acc:
        return None
    with _lock:
        if acc in _cache:
            return _cache[acc]
    out = None
    try:
        url = (f"https://rest.uniprot.org/uniprotkb/{acc}.json"
               "?fields=gene_names,protein_name,keyword,cc_function,go,cc_subcellular_location,cc_interaction")
        req = urllib.request.Request(url, headers={"User-Agent": "fran-corpus", "Accept": "application/json"})
        d = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
        gene = None
        if d.get("genes"):
            gene = d["genes"][0].get("geneName", {}).get("value")
        pdsc = d.get("proteinDescription", {})
        name = (pdsc.get("recommendedName", {}).get("fullName", {}).get("value")
                or (pdsc.get("submissionNames", [{}])[0].get("fullName", {}).get("value")
                    if pdsc.get("submissionNames") else None))
        kws = [k.get("name") for k in d.get("keywords", []) if k.get("name")]
        func, subcell, interactions = None, [], []
        for c in d.get("comments", []):
            if c.get("commentType") == "FUNCTION" and c.get("texts"):
                func = c["texts"][0].get("value")
            if c.get("commentType") == "SUBCELLULAR LOCATION":
                for loc in c.get("subcellularLocations", []):
                    v = (loc.get("location") or {}).get("value")
                    if v:
                        subcell.append(v)
            if c.get("commentType") == "INTERACTION":
                for it in c.get("interactions", []):
                    two = it.get("interactantTwo", {})
                    g = two.get("geneName") or two.get("uniProtKBAccession")
                    if g:
                        interactions.append(g)
        go = {"P": [], "F": [], "C": []}
        for x in d.get("uniProtKBCrossReferences", []):
            if x.get("database") == "GO":
                for p in x.get("properties", []):
                    if p.get("key") == "GoTerm" and isinstance(p.get("value"), str) and p["value"][:2] in ("P:", "F:", "C:"):
                        go[p["value"][0]].append(p["value"][2:])
        out = {"accession": acc, "gene": gene, "protein_name": name, "keywords": kws[:12],
               "function": func, "subcellular": subcell[:4], "interactions": interactions[:12],
               "go_process": go["P"][:6], "go_function": go["F"][:6], "go_component": go["C"][:6]}
    except Exception:  # noqa: BLE001
        out = None
    with _lock:
        _cache[acc] = out
    return out


_ortho_cache: dict[str, str | None] = {}
_ortho_lock = threading.Lock()


def fetch_reviewed_ortholog(gene: str, protein_name: str | None = None) -> str | None:
    """Accession of a REVIEWED (SwissProt) entry — prefer human — to enrich sparse
    TrEMBL entries (e.g. dog proteins) with curated function + a Wikipedia-able name.
    Searches by gene AND by protein name, because dog genes are often NCBI 'LOCxxxx'
    placeholders (no reviewed match) — so name search (with the '-like' suffix stripped)
    is what finds the human ortholog, e.g. 'Pregnancy zone protein-like' -> human PZP."""
    gene = (gene or "").strip()
    pn = (protein_name or "").strip()
    pn = __import__("re").sub(r"[-\s]*(like|homolog)$", "", pn, flags=__import__("re").I).strip()
    ck = f"{gene}|{pn}"
    with _ortho_lock:
        if ck in _ortho_cache:
            return _ortho_cache[ck]
    qs = []
    real_gene = gene and not gene.upper().startswith("LOC") and not gene.isdigit()
    if real_gene:
        qs += [f"gene:{gene} AND reviewed:true AND organism_id:9606", f"gene:{gene} AND reviewed:true"]
    if pn:
        qs += [f'protein_name:"{pn}" AND reviewed:true AND organism_id:9606', f'protein_name:"{pn}" AND reviewed:true']
    acc = None
    for q in qs:
        try:
            url = ("https://rest.uniprot.org/uniprotkb/search?format=json&size=1&fields=accession&query="
                   + urllib.parse.quote(q))
            req = urllib.request.Request(url, headers={"User-Agent": "fran-corpus", "Accept": "application/json"})
            res = json.loads(urllib.request.urlopen(req, timeout=12).read().decode()).get("results") or []
            if res:
                acc = res[0].get("primaryAccession")
                break
        except Exception:  # noqa: BLE001
            continue
    with _ortho_lock:
        _ortho_cache[ck] = acc
    return acc


def _reviewed_orthologs(gene: str | None, protein_name: str | None, size: int = 6) -> list[str]:
    """Reviewed (SwissProt) accessions matching by gene and/or protein name, human
    first. Multi-result version of fetch_reviewed_ortholog so the caller can pick
    among same-named families (e.g. 'thiol proteinase inhibitor' = cystatin OR
    kininogen) instead of blindly taking the first hit."""
    import re
    gene = (gene or "").strip()
    pn = re.sub(r"[-\s]*(like|homolog)$", "", (protein_name or "").strip(), flags=re.I).strip()
    qs = []
    real_gene = gene and not gene.upper().startswith("LOC") and not gene.isdigit()
    if real_gene:
        qs += [f"gene:{gene} AND reviewed:true AND organism_id:9606", f"gene:{gene} AND reviewed:true"]
    if pn:
        qs += [f'protein_name:"{pn}" AND reviewed:true AND organism_id:9606', f'protein_name:"{pn}" AND reviewed:true']
    out: list[str] = []
    for q in qs:
        try:
            url = (f"https://rest.uniprot.org/uniprotkb/search?format=json&size={size}&fields=accession&query="
                   + urllib.parse.quote(q))
            req = urllib.request.Request(url, headers={"User-Agent": "fran-corpus", "Accept": "application/json"})
            res = json.loads(urllib.request.urlopen(req, timeout=12).read().decode()).get("results") or []
            for r in res:
                acc = r.get("primaryAccession")
                if acc and acc not in out:
                    out.append(acc)
        except Exception:  # noqa: BLE001
            continue
        if len(out) >= size:
            break
    return out[:size]


def _enrich_peptide(ann: dict | None, homolog_accs: list[str]) -> dict | None:
    """Peptide-aware enrichment. When the representative is a sparse TrEMBL entry,
    pick the reviewed ortholog whose sequence LENGTH best matches the corpus
    homologues — this disambiguates same-named families (cystatin ~100 aa vs
    kininogen ~640 aa) so we don't assert the wrong protein's function."""
    if not ann or (ann.get("function") and ann.get("keywords")):
        return ann
    from . import coverage
    cands = _reviewed_orthologs(ann.get("gene"), ann.get("protein_name"))
    if not cands:
        return ann
    # reference length = longest resolvable homologue (the full-length form)
    ref_len = 0
    for acc in homolog_accs[:12]:
        ln = len(coverage.fetch_uniprot_sequence(acc))
        if ln > ref_len:
            ref_len = ln
    chosen, by_length = cands[0], False
    if ref_len:
        best = None
        for acc in cands:
            ln = len(coverage.fetch_uniprot_sequence(acc))
            if not ln:
                continue
            d = abs(ln - ref_len)
            if best is None or d < best[0]:
                best = (d, acc)
        if best:
            chosen, by_length = best[1], (len(cands) > 1)
    if chosen == ann.get("accession"):
        return ann
    rich = fetch_uniprot_entry(chosen)
    if not rich:
        return ann
    merged = dict(ann)
    for k in ("function", "keywords", "subcellular", "interactions",
              "go_process", "go_function", "go_component"):
        if not merged.get(k):
            merged[k] = rich.get(k)
    merged["enriched_from"] = chosen
    merged["ortholog_protein_name"] = rich.get("protein_name")
    if by_length:
        merged["ortholog_by_length"] = True  # chosen among same-named families by sequence length
    return merged


def _enrich(ann: dict | None) -> dict | None:
    """If the entry is sparse (TrEMBL — no function/keywords), fill function/keywords/
    location/interactions/GO from a reviewed ortholog (keeps the original accession +
    name for the corpus link; records which ortholog the annotation came from)."""
    if not ann or (ann.get("function") and ann.get("keywords")):
        return ann
    ortho = fetch_reviewed_ortholog(ann.get("gene"), ann.get("protein_name"))
    if not ortho or ortho == ann.get("accession"):
        return ann
    rich = fetch_uniprot_entry(ortho)
    if not rich:
        return ann
    merged = dict(ann)
    for k in ("function", "keywords", "subcellular", "interactions",
              "go_process", "go_function", "go_component"):
        if not merged.get(k):
            merged[k] = rich.get(k)
    merged["enriched_from"] = ortho
    merged["ortholog_protein_name"] = rich.get("protein_name")
    return merged


def peptide_summary(seq: str) -> dict | None:
    from . import lca
    p = lca.peptide_proteins(seq)
    if not p or not p.get("organisms"):
        return None
    names = Counter()
    rep = None
    homolog_accs = []  # corpus homologue accessions, for length-based ortholog matching
    species = []  # [{name, n}] ordered by protein count, for the plain-language narrative
    for o in p["organisms"]:
        species.append({"name": o.get("organism"), "n": o.get("n")})
        for pr in o.get("proteins", []):
            if pr.get("protein_name"):
                names[pr["protein_name"]] += 1
            if pr.get("uniprot_id"):
                homolog_accs.append(pr["uniprot_id"])
                if rep is None:
                    rep = pr["uniprot_id"]
    species.sort(key=lambda s: (s["n"] or 0), reverse=True)
    consensus = names.most_common(1)[0][0] if names else None
    # distinct protein-name families this peptide spans (so the UI can say
    # "spans 2 different protein types" when a peptide is shared across families)
    distinct_names = [n for n, _ in names.most_common(5)]
    # Enrich the representative. Dog/fox TrEMBL entries often have NO gene/name in
    # their own UniProt record (so the ortholog lookup had nothing to search on and
    # function came back empty). Inject the Unipept consensus name as a fallback so
    # _enrich can find a reviewed ortholog by name -> function/keywords/location.
    ann = fetch_uniprot_entry(rep) if rep else None
    if ann is not None and not ann.get("protein_name") and consensus:
        ann = {**ann, "protein_name": consensus}
    ann = _enrich_peptide(ann, homolog_accs)
    return {"consensus_protein_name": consensus, "n_proteins": p.get("total_proteins"),
            "n_organisms": p.get("n_organisms"), "representative": rep,
            "species": species[:6], "distinct_protein_names": distinct_names,
            "n_distinct_names": len(names), "annotation": ann}


def gene_summary(gene: str) -> dict | None:
    """Plain-language identity of a GENE: pull the reviewed (human-first) UniProt entry
    for the gene -> function/keywords/location/GO + Wikipedia trivia, plus the external
    resources proteomics biologists actually use (UniProt, NCBI Gene, GeneCards, Ensembl,
    STRING, Human Protein Atlas tissue expression, GTEx)."""
    g = (gene or "").strip()
    if not g:
        return None
    acc = fetch_reviewed_ortholog(g, None)
    ann = _enrich(fetch_uniprot_entry(acc)) if acc else None
    wiki = None
    for title in ((ann or {}).get("protein_name"), g):
        if title:
            wiki = fetch_wikipedia(title)
            if wiki:
                break
    gq = urllib.parse.quote(g)
    links = {
        "uniprot": f"https://www.uniprot.org/uniprotkb?query=gene:{gq}+AND+reviewed:true",
        "ncbi_gene": f"https://www.ncbi.nlm.nih.gov/gene/?term={gq}",
        "genecards": f"https://www.genecards.org/cgi-bin/carddisp.pl?gene={gq}",
        "ensembl": f"https://www.ensembl.org/Multi/Search/Results?q={gq}",
        "string": f"https://string-db.org/cgi/network?identifiers={gq}",
        # Human Protein Atlas = tissue/subcellular expression; GTEx = RNA expression by tissue
        "protein_atlas": f"https://www.proteinatlas.org/search/{gq}",
        "gtex": f"https://www.gtexportal.org/home/gene/{gq}",
    }
    return {"gene": g, "annotation": ann, "wikipedia": wiki, "links": links}


def protein_summary(accession: str) -> dict | None:
    """Rich human-readable protein summary: UniProt function/location/interactions +
    Wikipedia trivia (discovery/history for well-known proteins) + external links."""
    ann = _enrich(fetch_uniprot_entry(accession))
    if not ann:
        return None
    wiki = None
    for title in (ann.get("ortholog_protein_name"), ann.get("gene"), ann.get("protein_name")):
        if title:
            wiki = fetch_wikipedia(title)
            if wiki:
                break
    acc = ann["accession"]
    links = {"uniprot": f"https://www.uniprot.org/uniprotkb/{acc}/entry",
             "ncbi_protein": f"https://www.ncbi.nlm.nih.gov/protein/{acc}",
             "ncbi_gene": (f"https://www.ncbi.nlm.nih.gov/gene/?term={urllib.parse.quote(ann['gene'])}"
                           if ann.get("gene") else None),
             # STRING-db = protein-protein interaction network / functional clusters
             "string": (f"https://string-db.org/cgi/network?identifiers={urllib.parse.quote(ann['gene'])}"
                        if ann.get("gene") else None)}
    return {"annotation": ann, "wikipedia": wiki, "links": links}
