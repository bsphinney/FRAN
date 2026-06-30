"""
All SQL for the corpus browser. Every function:
  - names its `tables` so db.query() can enforce the public-layer allowlist,
  - uses %s / %(name)s placeholders only (no string interpolation of input),
  - leans on the schema's indexes (idx_prec_stripped_charge, idx_prec_search,
    idx_proteins_group, idx_proteins_gene, idx_consensus_stripped, etc.).
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Any

from .db import (
    CACHE,
    SLOW_CACHE,
    elevated,
    estimate_distinct,
    estimate_non_null,
    estimate_rows,
    estimate_value_distribution,
    query,
)
from . import collab


def _exact_or_none(sql: str, table: str):
    """Exact COUNT for a small table; on any error (e.g. timeout) return None so
    the dashboard card shows "—" instead of 503-ing the whole overview."""
    try:
        return query(sql, tables=[table], fetch="val")
    except Exception:  # noqa: BLE001
        return None


MAX_PAGE = 200  # never let the browser pull more than this many rows at once

# A genuinely IDENTIFIED organism: non-null, non-empty, and not a junk sentinel string.
# Ingest canonicalizes these to NULL (organism.py), but an in-flight ingest (or a legacy
# row) can still carry the literal string "Unknown"/"nan"/etc — this read-side predicate
# is the safety net so such a value can NEVER render as a bogus species on any list/count.
# Single definition; reuse everywhere a species list/count is built (CLAUDE.md rule #3).
_SENTINEL_ORGS = ("unknown", "unknwon", "none", "nan", "null", "n/a", "na", "undetermined", "")
_REAL_ORG_PRED = (
    "organism_name IS NOT NULL AND organism_name <> '' "
    "AND lower(trim(organism_name)) NOT IN ('unknown','unknwon','none','nan','null','n/a','na','undetermined','standard') "
    # parse-artifact junk, not an organism. Uses starts_with() (not ILIKE '...%') ON PURPOSE: a
    # literal % in a predicate string collides with psycopg2's %s parameter parsing whenever the
    # surrounding query also binds params (e.g. LIMIT %s) -> IndexError, which silently emptied the
    # dashboard species doughnut. starts_with avoids the % entirely.
    "AND NOT starts_with(lower(trim(organism_name)), 'translate_table')"
)
# Complement: a run whose organism is NOT identified (NULL/empty/sentinel) — counted as
# "pending species ID", never shown as a species.
_UNIDENTIFIED_ORG_PRED = (
    "(organism_name IS NULL OR organism_name = '' "
    "OR lower(trim(organism_name)) IN ('unknown','unknwon','none','nan','null','n/a','na','undetermined'))"
)

# Clean "distinct genes" = proteome depth. The raw `gene` field is dirty: DIA-NN writes accessions,
# isoform suffixes, and semicolon-joined MULTI-gene strings into it, so raw COUNT(DISTINCT gene) for
# human is ~29k — impossible (the human proteome is ~20k). Clean = first ';' token, upper-cased,
# dropping PURE UniProt accessions (10-char TrEMBL like A0A…, or 6-char [OPQ]#### ) → human ~19k.
# (No literal % so it's safe to interpolate alongside %s params.)
_N_GENES_SQL = ("COUNT(DISTINCT upper(split_part(gene,';',1))) FILTER (WHERE "
                "split_part(gene,';',1) ~ '^[A-Za-z]' AND "
                "upper(split_part(gene,';',1)) !~ '(^[A-Z][0-9][A-Z0-9]{8}$)|(^[OPQ][0-9][A-Z0-9]{3}[0-9]$)')")

# Python mirror of the clean-gene rule, for code paths that dedup genes in Python (species_detail).
_ACC_RE = re.compile(r"(^[A-Z][0-9][A-Z0-9]{8}$)|(^[OPQ][0-9][A-Z0-9]{3}[0-9]$)")
def _clean_gene_symbol(g: str | None) -> str | None:
    tok = (g or "").split(";")[0].strip()
    if not tok or not tok[0].isalpha():
        return None
    u = tok.upper()
    return None if _ACC_RE.match(u) else u


def _lab_institute_overrides() -> dict[str, str]:
    """pi_key (lowercased 'first last') -> institution, for labs whose CoreOmics `institute` is blank
    (AI-researched, stored in delimp_lab_institute_override). Cached; empty if the table is absent."""
    def _p() -> dict[str, str]:
        try:
            rows = query("SELECT pi_key, institute FROM delimp_lab_institute_override",
                         tables=["delimp_lab_institute_override"])
            return {r["pi_key"]: r["institute"] for r in rows if r.get("pi_key") and r.get("institute")}
        except Exception:  # noqa: BLE001 — table not created yet
            return {}
    return CACHE.get_or_set("lab_institute_overrides", _p)


def _proteome_reference() -> dict[int, dict[str, Any]]:
    """taxon_id -> {ncbi, reviewed, isoforms}: reference proteome sizes. ncbi = NCBI #protein-coding
    genes (the authoritative '% of proteome' denominator); reviewed/isoforms = UniProt Swiss-Prot
    (shown for context). Cached; empty if the table is absent."""
    def _p() -> dict[int, dict[str, Any]]:
        try:
            rows = query("SELECT taxon_id, ncbi_protein_coding, reviewed_count, reviewed_isoforms "
                         "FROM delimp_proteome_reference", tables=["delimp_proteome_reference"])
            return {int(r["taxon_id"]): {"ncbi": r["ncbi_protein_coding"], "reviewed": r["reviewed_count"],
                                         "isoforms": r["reviewed_isoforms"]}
                    for r in rows if r.get("taxon_id")}
        except Exception:  # noqa: BLE001
            return {}
    return CACHE.get_or_set("proteome_reference", _p)


def _pct_proteome(n_genes, taxon_id, ref=None):
    """'% of proteome identified' = cleaned distinct genes / NCBI protein-coding genes, plus the
    reference numbers (incl. UniProt isoform-inclusive count). None when no NCBI reference for the taxon."""
    try:
        tid = int(taxon_id) if taxon_id else None
    except (TypeError, ValueError):
        tid = None
    r = (ref if ref is not None else _proteome_reference()).get(tid) if tid else None
    denom = (r or {}).get("ncbi") or 0
    if not denom or not n_genes:
        return None
    iso = (r or {}).get("isoforms")
    return {"pct": min(100, round(100 * n_genes / denom)), "ref_genes": denom,
            "reviewed": (r or {}).get("reviewed"), "reviewed_isoforms": iso,
            # honest second number: gene coverage is NOT proteoform coverage. At isoform
            # resolution the same observed-gene set covers far less (human ~45% vs ~96%).
            "isoform_pct": (min(100, round(100 * n_genes / iso)) if iso else None)}


def _page(limit: int | None, offset: int | None) -> tuple[int, int]:
    lim = min(int(limit or 50), MAX_PAGE)
    off = max(int(offset or 0), 0)
    return lim, off


# ---------------------------------------------------------------------------
# Overview dashboard (cached, cheap aggregates) — "watch it populate"
# ---------------------------------------------------------------------------
def overview_counts() -> dict[str, Any]:
    def _producer() -> dict[str, Any]:
        # Snapshot dashboard. Small tables get exact COUNT(*); the big
        # delimp_precursors / delimp_proteins counts (millions of rows) use the
        # planner's estimates (pg_class.reltuples / pg_stats.n_distinct) so a
        # page load is instant and never trips the 30s statement timeout — the
        # bug that left every card blank. Each value degrades to None ("—")
        # independently; the dashboard never 503s because one count is slow.
        searches = _exact_or_none(
            "SELECT COUNT(*) FROM delimp_searches", "delimp_searches"
        )
        raw_files = _exact_or_none("SELECT COUNT(*) FROM raw_files", "raw_files")
        # Count by organism NAME, not taxon_id — many species (e.g. Oncorhynchus, Aspergillus)
        # have a name but no taxon mapping, so counting taxon_id under-reports (showed "3").
        organisms = _exact_or_none(
            f"SELECT COUNT(DISTINCT organism_name) FROM delimp_sample_metadata WHERE {_REAL_ORG_PRED}",
            "delimp_sample_metadata",
        )
        # Runs whose organism isn't identified yet (NULL/empty/sentinel) — shown as a caption
        # under the species chart, not as a bogus "Unknown" species slice. Only count runs an
        # actual search references (mirror species_distribution's orphan exclusion).
        unidentified_runs = _exact_or_none(
            f"SELECT COUNT(*) FROM delimp_sample_metadata m WHERE {_UNIDENTIFIED_ORG_PRED} "
            "AND EXISTS (SELECT 1 FROM search_raw_files srf WHERE srf.raw_path = m.raw_path)",
            "delimp_sample_metadata",
        )
        # EXACT counts come from the precomputed delimp_mv_corpus_stats view. The planner's
        # estimates are unreliable here — n_distinct is ~8x low for high-cardinality columns
        # (peptides, protein groups) and reltuples runs ~6% low for raw row counts. A
        # COUNT(DISTINCT)/COUNT(*) is too slow to run live on PG Farm, so we read this 1-row
        # view. Per-field fallback to the planner estimate if the view isn't built yet.
        s: dict[str, Any] = {}
        try:
            srow = query("SELECT distinct_peptides, distinct_protein_groups, total_precursors, "
                         "total_proteins, im_bearing_precursors FROM delimp_mv_corpus_stats LIMIT 1",
                         tables=["delimp_mv_corpus_stats"])
            if srow:
                s = srow[0]
        except Exception:  # noqa: BLE001 - view not built yet -> estimate below
            pass

        def _exact(key, fallback):
            v = s.get(key)
            return v if v is not None else fallback()

        return {
            "searches": searches,
            "raw_files": raw_files,
            "organisms": organisms,
            "unidentified_runs": unidentified_runs,
            "proteins": _exact("total_proteins", lambda: estimate_rows("delimp_proteins")),
            "precursors": _exact("total_precursors", lambda: estimate_rows("delimp_precursors")),
            "distinct_peptides": _exact("distinct_peptides",
                lambda: estimate_distinct("delimp_precursors", "stripped_seq")),
            "distinct_protein_groups": _exact("distinct_protein_groups",
                lambda: estimate_distinct("delimp_proteins", "protein_group")),
            "im_bearing_precursors": _exact("im_bearing_precursors",
                lambda: estimate_non_null("delimp_precursors", "im")),
            "estimated": not bool(s),  # exact when served from the view
        }

    return CACHE.get_or_set("overview_counts", _producer)


def species_distribution(limit: int = 15) -> list[dict[str, Any]]:
    def _producer() -> list[dict[str, Any]]:
        # Only count raw files that an actual search references — excludes ORPHAN
        # sample_metadata rows left behind when searches are deleted (raw_files /
        # sample_metadata are keyed by raw_path, not search_id, so they don't cascade).
        # Those orphans were inflating the "Unknown" slice.
        # IDENTIFIED species only — unidentified runs (organism_name NULL/empty) are NOT a
        # species, so they don't belong as a doughnut slice. Their count is surfaced
        # separately via overview_counts()["unidentified_runs"] and shown as an honest
        # caption ("N runs pending species ID"), never silently dropped.
        # group by NAME only (not name+taxid): the same species can carry both a resolved taxid and
        # NULL across runs, which split "Homo sapiens" into two rows. MAX(taxon_id) keeps the resolved
        # id; COUNT(*) sums runs across both. (This comment MUST stay in Python, NOT in the SQL — a
        # leading `--` makes the query not start with SELECT and db.query's read-only guard rejects it,
        # which is exactly what silently emptied the dashboard species doughnut.)
        rows = query(
            f"""
            SELECT organism_name AS organism,
                   MAX(organism_taxon_id) AS organism_taxon_id,
                   COUNT(*) AS n_runs
            FROM delimp_sample_metadata m
            WHERE EXISTS (SELECT 1 FROM search_raw_files srf WHERE srf.raw_path = m.raw_path)
              AND {_REAL_ORG_PRED}
            GROUP BY organism_name
            ORDER BY n_runs DESC
            LIMIT {int(limit)}
            """,
            tables=["delimp_sample_metadata", "search_raw_files"],
        )
        return rows

    return CACHE.get_or_set(f"species_dist_{limit}", _producer)


# Common (English) names for organisms — used so the global search matches "dog"/"yeast"/"bat".
# Display-side equivalent lives in app.js COMMON_NAMES; keep them roughly in sync (this set only
# needs the searchable terms, not every species).
_COMMON_NAMES = {
    "Homo sapiens": "human", "Mus musculus": "house mouse", "Rattus norvegicus": "rat",
    "Canis lupus familiaris": "dog", "Bos taurus": "cattle cow", "Sus scrofa": "pig",
    "Ovis aries": "sheep", "Oryctolagus cuniculus": "rabbit", "Macaca mulatta": "rhesus macaque monkey",
    "Macaca fascicularis": "crab-eating macaque monkey", "Gallus gallus": "chicken",
    "Saccharomyces cerevisiae": "baker's yeast", "Escherichia coli": "e. coli",
    "Oncorhynchus kisutch": "coho salmon", "Oreochromis niloticus": "nile tilapia",
    "Thunnus albacares": "yellowfin tuna", "Thunnus thynnus": "bluefin tuna",
    "Aspergillus oryzae": "koji mold", "Komagataella phaffii": "pichia yeast",
    "Komagataella pastoris": "pichia yeast", "Hypocrea jecorina": "trichoderma fungus",
    "Trichechus manatus latirostris": "florida manatee sea cow",
    "Octopus vulgaris": "common octopus", "Hypsibius dujardini": "tardigrade water bear",
    "Meloidogyne javanica": "root-knot nematode worm", "Toxoplasma gondii": "toxoplasma parasite",
    "Cannabis sativa": "cannabis hemp", "Cicer arietinum": "chickpea", "Gossypium hirsutum": "cotton",
    "Helianthus annuus": "sunflower", "Solanum tuberosum": "potato", "Pisum sativum": "pea",
    "Nicotiana tabacum": "tobacco", "Lupinus angustifolius": "lupin", "Digitaria exilis": "fonio millet",
    "Vigna radiata": "mung bean", "Cucurbita maxima": "squash pumpkin", "Cucurbita moschata": "squash",
    "Desmodus rotundus": "common vampire bat", "Diphylla ecaudata": "hairy-legged vampire bat",
    "Artibeus jamaicensis": "jamaican fruit bat", "Tadarida brasiliensis": "mexican free-tailed bat",
    "Pteronotus mesoamericanus": "mesoamerican mustached bat", "Eptesicus furinalis": "argentine brown bat",
    "Molossus nigricans": "black mastiff bat", "Haemorhous mexicanus": "house finch",
    "Zonotrichia querula": "harris's sparrow", "Junco hyemalis": "dark-eyed junco",
}


def search_species(q: str, limit: int = 60) -> dict[str, Any]:
    """Search identified species by scientific OR common name (e.g. 'dog' -> Canis lupus
    familiaris). Returns each match with run count + protein-group count, clickable through to
    its species detail page. Sentinel/unidentified rows never match (uses _REAL_ORG_PRED)."""
    q2 = (q or "").strip()
    if len(q2) < 2:
        return {"rows": [], "total": 0}
    lim = min(int(limit or 60), MAX_PAGE)
    ql = q2.lower()
    common_hits = [sci for sci, cn in _COMMON_NAMES.items() if ql in cn.lower()]
    params: list[Any] = [f"%{q2}%"]
    extra = ""
    if common_hits:
        extra = " OR organism_name = ANY(%s)"
        params.append(common_hits)
    rows = query(
        f"""
        SELECT organism_name AS organism, organism_taxon_id, COUNT(*) AS n_runs
        FROM delimp_sample_metadata m
        WHERE EXISTS (SELECT 1 FROM search_raw_files srf WHERE srf.raw_path = m.raw_path)
          AND {_REAL_ORG_PRED}
          AND (organism_name ILIKE %s{extra})
        GROUP BY organism_name, organism_taxon_id
        ORDER BY n_runs DESC
        LIMIT %s
        """,
        (*params, lim),
        tables=["delimp_sample_metadata", "search_raw_files"],
    )
    # attach protein-group counts from the indexed matview
    if rows:
        names = [r["organism"] for r in rows]
        try:
            prot = query(
                f"""SELECT organism_name AS organism, COUNT(DISTINCT protein_group) AS n_protein_groups
                    FROM delimp_mv_species_proteins WHERE organism_name = ANY(%s)
                    GROUP BY organism_name""",
                (names,), tables=["delimp_mv_species_proteins"])
            pmap = {r["organism"]: int(r["n_protein_groups"] or 0) for r in prot}
        except Exception:  # noqa: BLE001
            pmap = {}
        for r in rows:
            r["n_protein_groups"] = pmap.get(r["organism"])
            r["common_name"] = _COMMON_NAMES.get(r["organism"])
            r["taxon_group"] = _taxon_group(r["organism"])
    return {"rows": rows, "total": len(rows)}


# Functional buckets by gene symbol (heuristic, first match wins). Lets the species page show a
# "blood proteins / liver proteins / muscle ..." breakdown without external tissue annotation.
_FUNC_CLASSES = [
    ("Blood / plasma", {"ALB", "TF", "HP", "PLG", "F2", "C3", "C4A", "C4B", "TTR", "AHSG", "ORM1",
                        "SERPINA1", "SERPINC1", "APOA1", "APOA2", "APOB", "APOE", "FGA", "FGB", "FGG"},
        ("HBA", "HBB", "HBD")),
    ("Liver / detox", set(), ("CYP", "ALDH", "ADH", "UGT", "GST")),
    ("Muscle", {"TTN", "DES", "CKM", "ACTA1", "ACTA2"}, ("MYH", "MYL", "TNN", "ACTN", "MYBPC")),
    ("Immune / antibody", {"LYZ", "JCHAIN", "C1QA", "C1QB"}, ("IGH", "IGK", "IGL", "HLA", "CD")),
    ("Structural / cytoskeleton", {"VIM", "ACTB", "ACTG1"}, ("KRT", "TUB", "FLN", "SPTB", "COL")),
    ("Energy / mitochondria", set(), ("ATP5", "NDUF", "COX", "SDH", "UQCR", "MT-")),
    ("Core metabolism", {"GAPDH", "PKM", "ENO1", "ENO2", "ALDOA", "LDHA", "LDHB", "PGK1", "TPI1", "PGAM1"}, ()),
    ("Histone / nuclear", set(), ("HIST", "H1", "H2A", "H2B", "H3", "H4")),
    ("Ribosome / translation", set(), ("RPL", "RPS", "EEF", "EIF", "MRPL", "MRPS")),
    ("Chaperone / stress", set(), ("HSP", "DNAJ", "CCT")),
]
# Genes with genuinely fun protein names — shown as the "coolest-named protein" when present.
_FUN_PROTEINS = {
    "SHH": ("Sonic Hedgehog", "yes — named after the SEGA video-game character"),
    "DHH": ("Desert Hedgehog", "of the whimsically-named Hedgehog signaling family"),
    "IHH": ("Indian Hedgehog", "another of the Hedgehog family"),
    "EGFLAM": ("Pikachurin", "named after Pikachu — it helps the eye track fast motion"),
    "LFNG": ("Lunatic Fringe", "a glycosyltransferase with a delightfully unhinged name"),
    "MFNG": ("Manic Fringe", "sibling of Lunatic Fringe"),
    "RFNG": ("Radical Fringe", "the third of the 'Fringe' family"),
    "ZBTB7A": ("Pokemon (POK factor)", "originally nicknamed 'Pokemon' until Nintendo objected"),
    "NKX2-5": ("Tinman", "its fly homolog is 'tinman' — no heart without it"),
    "DNAH1": ("Dynein heavy chain", "a molecular motor that literally walks along tracks"),
    "TTN": ("Titin", "the largest known protein — its full chemical name is ~189,819 letters long"),
}


# Flat O(1) lookups built once from _FUNC_CLASSES (the per-gene class loop was the proteins-showcase
# hot spot — ~200k DISTINCT genes × ~40 startswith each). First class in the list wins (setdefault).
_EXACT_LABEL: dict[str, str] = {}
_PREFIX_LABEL: dict[str, str] = {}
for _lbl, _exact, _prefixes in _FUNC_CLASSES:
    for _g in _exact:
        _EXACT_LABEL.setdefault(_g, _lbl)
    for _p in _prefixes:
        _PREFIX_LABEL.setdefault(_p, _lbl)
_PREFIX_LENS = sorted({len(p) for p in _PREFIX_LABEL}, reverse=True)  # try longest (most specific) first
# SQL fragments to pre-filter to class-relevant genes (so function_breakdown fetches thousands, not 200k)
_CLASS_EXACT = sorted(_EXACT_LABEL)
_CLASS_PREFIX_RE = "^(" + "|".join(sorted(_PREFIX_LABEL, key=len, reverse=True)) + ")"


def _classify_gene(gene: str) -> str:
    g = (gene or "").split(";")[0].strip().upper()
    if not g:
        return "Other"
    lab = _EXACT_LABEL.get(g)
    if lab:
        return lab
    for L in _PREFIX_LENS:
        lab = _PREFIX_LABEL.get(g[:L])
        if lab:
            return lab
    return "Other"


def species_detail(name: str) -> dict[str, Any]:
    """Protein 'cool stats' for one organism for the species detail page: counts, most/least
    abundant (by mean protein intensity), most-detected, a function breakdown (blood/liver/...),
    and a whimsically-named protein if present. Cached (per-species snapshot)."""
    def _p():
        # Read the PRECOMPUTED per-species protein matview (refreshed offline by
        # refresh_leaderboards). A live GROUP BY over delimp_proteins for a big species could
        # exceed the 30s live timeout -> exception -> a cached empty result -> a 30-min 404.
        try:
            rows = query(
                """SELECT protein_group, gene, n_runs, n_searches, sum_prec, max_pep, mean_int, contam
                   FROM delimp_mv_species_proteins WHERE organism_name = %s LIMIT 25000""",
                (name,), tables=["delimp_mv_species_proteins"])
        except Exception:  # noqa: BLE001
            rows = []
        # FALLBACK: a species ingested AFTER the matview's last refresh is on the dashboard but
        # not yet in the matview -> would 404. Compute it live (capped + bounded timeout). New
        # species are small, so this is fast; the matview still serves the big ones. This also
        # means we never cache an empty result for a real species (no 30-min cache-poisoning).
        if not rows:
            try:
                rows = query(
                    """SELECT p.protein_group, MAX(p.gene) AS gene,
                              COUNT(DISTINCT p.raw_path) AS n_runs, COUNT(DISTINCT p.search_id) AS n_searches,
                              SUM(p.n_precursors) AS sum_prec, MAX(p.n_unique_peptides) AS max_pep,
                              AVG(NULLIF(p.intensity,0)) AS mean_int, bool_or(p.is_contaminant) AS contam
                       FROM delimp_proteins p JOIN delimp_sample_metadata m ON m.raw_path = p.raw_path
                       WHERE m.organism_name = %s AND p.protein_group IS NOT NULL AND p.protein_group <> ''
                       GROUP BY p.protein_group LIMIT 25000""",
                    (name,), tables=["delimp_proteins", "delimp_sample_metadata"], timeout_ms=20000)
            except Exception:  # noqa: BLE001
                rows = []
        if not rows:
            return {"organism": name, "n_proteins": 0}
        n_runs = max((r["n_runs"] or 0 for r in rows), default=0)
        n_searches = max((r["n_searches"] or 0 for r in rows), default=0)
        # function breakdown (exclude contaminants from the biology buckets)
        buckets: dict[str, int] = {}
        for r in rows:
            if r.get("contam"):
                continue
            buckets[_classify_gene(r.get("gene"))] = buckets.get(_classify_gene(r.get("gene")), 0) + 1
        func = sorted(({"label": k, "n": v} for k, v in buckets.items() if k != "Other"),
                      key=lambda x: -x["n"])
        # most-detected (breadth) and most/least abundant (by mean intensity, real proteins only)
        real = [r for r in rows if not r.get("contam")]
        top_seen = sorted(real, key=lambda r: (-(r["n_runs"] or 0), -(r["sum_prec"] or 0)))[:10]
        with_int = [r for r in real if r.get("mean_int")]
        most_ab = max(with_int, key=lambda r: r["mean_int"], default=None)
        least_ab = min((r for r in with_int if (r["n_runs"] or 0) >= 2), key=lambda r: r["mean_int"], default=None)
        # whimsical protein, else longest gene symbol as a light "longest name" pick
        cool = None
        genes = {(r.get("gene") or "").split(";")[0].upper(): r for r in real}
        for g, (nm, why) in _FUN_PROTEINS.items():
            if g in genes:
                cool = {"gene": g, "name": nm, "why": why, "protein_group": genes[g]["protein_group"]}
                break
        if not cool:
            lg = max((r for r in real if r.get("gene")), key=lambda r: len(r["gene"].split(";")[0]), default=None)
            if lg:
                cool = {"gene": lg["gene"].split(";")[0], "name": None,
                        "why": "longest gene symbol we identified here", "protein_group": lg["protein_group"]}

        def _clean_gene(g):
            # junk gene strings ('NaN'/'nan'/''/'None') -> None so the UI shows "—", not "NaN"
            return None if (g is None or str(g).strip().lower() in ("nan", "none", "na", "null", "")) else g
        def _slim(r):
            return None if not r else {"protein_group": r["protein_group"], "gene": _clean_gene(r.get("gene")),
                                       "n_runs": r["n_runs"], "n_searches": r.get("n_searches"),
                                       "sum_prec": r["sum_prec"],
                                       "max_pep": r["max_pep"], "mean_int": r.get("mean_int")}
        # distinct CLEAN genes = honest proteome depth: dedup semicolon multi-gene + drop
        # accessions-as-genes (raw was impossibly high — human ~29k vs ~20k proteome). See _clean_gene_symbol.
        n_genes = len({cg for r in rows if (cg := _clean_gene_symbol(r.get("gene")))})
        # % of proteome identified — resolve this species' taxon, then clean_genes / NCBI protein-coding.
        try:
            tax = query("SELECT MAX(organism_taxon_id) FROM delimp_sample_metadata WHERE organism_name=%s",
                        (name,), tables=["delimp_sample_metadata"], fetch="val")
        except Exception:  # noqa: BLE001
            tax = None
        pct_proteome = _pct_proteome(n_genes, tax)
        return {"organism": name, "n_proteins": len(rows), "n_protein_groups": len({r["protein_group"] for r in rows}),
                "n_genes": n_genes, "pct_proteome": pct_proteome, "organism_taxon_id": tax,
                "n_runs": n_runs, "n_searches": n_searches, "n_contaminants": sum(1 for r in rows if r.get("contam")),
                "function_breakdown": func, "top_seen": [_slim(r) for r in top_seen],
                "most_abundant": _slim(most_ab), "least_abundant": _slim(least_ab), "coolest_protein": cool}
    return SLOW_CACHE.get_or_set(f"species_detail_{name}", _p)


# ---------------------------------------------------------------------------
# Peptide "trading card" fun facts — physico-chem computed from the sequence
# (monoisotopic residue masses; never fabricated) + live corpus breadth stats.
# ---------------------------------------------------------------------------
_AA_MONO = {
    "G": 57.02146, "A": 71.03711, "S": 87.03203, "P": 97.05276, "V": 99.06841,
    "T": 101.04768, "C": 103.00919, "L": 113.08406, "I": 113.08406, "N": 114.04293,
    "D": 115.02694, "Q": 128.05858, "K": 128.09496, "E": 129.04259, "M": 131.04049,
    "H": 137.05891, "F": 147.06841, "R": 156.10111, "Y": 163.06333, "W": 186.07931,
}
_WATER_MONO = 18.010565
_PROTON_MONO = 1.007276
_KD_HYDRO = {
    "A": 1.8, "R": -4.5, "N": -3.5, "D": -3.5, "C": 2.5, "Q": -3.5, "E": -3.5,
    "G": -0.4, "H": -3.2, "I": 4.5, "L": 3.8, "K": -3.9, "M": 1.9, "F": 2.8,
    "P": -1.6, "S": -0.8, "T": -0.7, "W": -0.9, "Y": -1.3, "V": 4.2,
}


def peptide_physchem(stripped_seq: str) -> dict[str, Any]:
    """Sequence-intrinsic physico-chemical fun facts computed (not fabricated) from standard
    monoisotopic residue masses + Kyte-Doolittle hydropathy. No DB access."""
    seq = (stripped_seq or "").strip().upper()
    aa = [c for c in seq if c in _AA_MONO]
    n = len(aa)
    if not n:
        return {"valid": False, "length": 0}
    mass = round(sum(_AA_MONO[c] for c in aa) + _WATER_MONO, 4)
    gravy = round(sum(_KD_HYDRO[c] for c in aa) / n, 3)
    last = seq[-1]
    return {"valid": True, "length": n, "monoisotopic_mass": mass,
            "mz_2plus": round((mass + 2 * _PROTON_MONO) / 2, 4),
            "mz_3plus": round((mass + 3 * _PROTON_MONO) / 3, 4),
            "gravy": gravy, "hydrophobic": gravy > 0,
            "counts": {r: seq.count(r) for r in ("C", "W", "H", "P", "M", "K", "R")},
            "tryptic": last in ("K", "R"), "c_terminus": last}


def peptide_fun_facts(stripped_seq: str) -> dict[str, Any]:
    """Peptide trading-card data: physico-chem (computed) + live corpus breadth (cross-species,
    observations/charges/engines, ion mobility & RT/iRT spread, most-intense observation)."""
    seq = (stripped_seq or "").strip().upper()
    physchem = peptide_physchem(seq)
    if not seq.isalpha():
        return {"stripped_seq": seq, "found": False, "physchem": physchem}
    breadth = query(
        """SELECT COUNT(*) AS n_obs, COUNT(DISTINCT search_id) AS n_searches,
                  COUNT(DISTINCT raw_path) AS n_runs,
                  array_agg(DISTINCT charge ORDER BY charge) FILTER (WHERE charge BETWEEN 1 AND 8) AS charges,
                  MAX(n_engines_confirming) AS max_engines,
                  MIN(im) FILTER (WHERE im > 0.3) AS im_min, MAX(im) FILTER (WHERE im > 0.3) AS im_max,
                  COUNT(*) FILTER (WHERE im > 0.3) AS n_im,
                  MIN(rt) AS rt_min, MAX(rt) AS rt_max, MIN(irt) AS irt_min, MAX(irt) AS irt_max,
                  COUNT(*) FILTER (WHERE irt IS NOT NULL) AS n_irt, MAX(intensity) AS max_intensity
           FROM delimp_precursors WHERE stripped_seq = %s""",
        (seq,), tables=["delimp_precursors"], fetch="one")
    if not breadth or not breadth.get("n_obs"):
        return {"stripped_seq": seq, "found": False, "physchem": physchem}
    organisms = query(
        """SELECT COALESCE(NULLIF(sm.organism_name, ''), 'Unknown') AS organism,
                  COUNT(DISTINCT pr.raw_path) AS n_runs
           FROM delimp_precursors pr LEFT JOIN delimp_sample_metadata sm ON sm.raw_path = pr.raw_path
           WHERE pr.stripped_seq = %s GROUP BY 1 ORDER BY n_runs DESC LIMIT 40""",
        (seq,), tables=["delimp_precursors", "delimp_sample_metadata"])
    named = [o for o in organisms if o["organism"] != "Unknown"]
    top = query(
        """SELECT pr.intensity, pr.raw_path, pr.charge,
                  COALESCE(NULLIF(sm.organism_name, ''), 'Unknown') AS organism
           FROM delimp_precursors pr LEFT JOIN delimp_sample_metadata sm ON sm.raw_path = pr.raw_path
           WHERE pr.stripped_seq = %s AND pr.intensity IS NOT NULL ORDER BY pr.intensity DESC LIMIT 1""",
        (seq,), tables=["delimp_precursors", "delimp_sample_metadata"], fetch="one")
    return {"stripped_seq": seq, "found": True, "physchem": physchem,
            "breadth": {k: breadth.get(k) for k in ("n_obs", "n_searches", "n_runs", "max_engines",
                        "n_im", "im_min", "im_max", "rt_min", "rt_max", "n_irt", "irt_min", "irt_max",
                        "max_intensity")} | {"charges": list(breadth.get("charges") or [])},
            "n_organisms": len(named), "organisms": named[:24],
            "n_unknown_runs": sum(o["n_runs"] for o in organisms if o["organism"] == "Unknown"),
            "most_intense": ({"organism": top["organism"], "raw_path": top["raw_path"],
                              "charge": top["charge"], "intensity": top["intensity"]} if top else None)}


def platform_distribution() -> list[dict[str, Any]]:
    def _producer() -> list[dict[str, Any]]:
        return query(
            """
            SELECT COALESCE(platform, 'other') AS platform,
                   COALESCE(acquisition_method, 'unknown') AS acquisition_method,
                   COALESCE(instrument_model, 'unknown') AS instrument_model,
                   COUNT(*) AS n_runs
            FROM raw_files
            GROUP BY platform, acquisition_method, instrument_model
            ORDER BY n_runs DESC
            """,
            tables=["raw_files"],
        )

    return CACHE.get_or_set("platform_dist", _producer)


def engine_distribution() -> list[dict[str, Any]]:
    def _producer() -> list[dict[str, Any]]:
        return query(
            """
            SELECT search_engine, COUNT(*) AS n_searches,
                   COALESCE(SUM(n_precursors_total), 0) AS n_precursors
            FROM delimp_searches
            GROUP BY search_engine
            ORDER BY n_searches DESC
            """,
            tables=["delimp_searches"],
        )

    return CACHE.get_or_set("engine_dist", _producer)


def recent_searches(limit: int = 10) -> list[dict[str, Any]]:
    # NOT cached (or very short) so freshly-ingested searches appear fast.
    return query(
        """
        SELECT id, search_name, search_engine, search_engine_version,
               pipeline_id, status, sharing_status,
               n_raw_files, n_precursors_total, n_proteins_total,
               completed_at, submitted_at, ingested_at, delimp_version
        FROM delimp_searches
        ORDER BY COALESCE(ingested_at, submitted_at) DESC
        LIMIT %s
        """,
        (limit,),
        tables=["delimp_searches"],
    )


def im_rt_density_sample(search_id: str | None = None, sample_n: int = 6000) -> dict[str, Any]:
    """ONE point per distinct precursor for the RT/iRT × 1/K0 scatter (DISTINCT ON
    dedups — avoids the old clump-per-peptide artifact from a bare LIMIT). PREFERS
    iRT (cross-run-comparable) when that column exists AND is populated, else raw RT;
    returns {points, x_axis} so the axis auto-switches to true iRT once iRT is ingested.
    Cached snapshot. Falls back to [] on timeout."""
    where = "AND search_id = %s" if search_id else ""
    extra = (search_id,) if search_id else ()

    def _q(with_irt):
        # Global view (no search_id): read the precomputed sample matview (the live
        # DISTINCT ON over millions of rows times out on PG Farm). Per-search view stays
        # live (small). The mv carries rt+irt so the axis logic below still applies.
        cols = "rt, im, charge, precursor_mz, intensity_log2" + (", irt" if with_irt else "")
        if not search_id:
            try:  # precomputed sample (best)
                return query(f"SELECT {cols} FROM delimp_mv_im_scatter LIMIT %s",
                             (sample_n,), tables=["delimp_mv_im_scatter"])
            except Exception:  # noqa: BLE001 - mv not built -> fast random TABLESAMPLE (no full scan/sort)
                return query(
                    f"SELECT {cols} FROM delimp_precursors TABLESAMPLE SYSTEM (2) "
                    f"WHERE im > 0.3 AND rt IS NOT NULL LIMIT %s",
                    (sample_n,), tables=["delimp_precursors"], timeout_ms=20000)
        sel = "stripped_seq, charge, rt, im, precursor_mz, intensity_log2" + (", irt" if with_irt else "")
        out = "rt, im, charge, precursor_mz, intensity_log2" + (", irt" if with_irt else "")
        return query(
            f"""SELECT {out} FROM (
                  SELECT DISTINCT ON (stripped_seq, charge) {sel}
                  FROM delimp_precursors
                  WHERE im > 0.3 AND rt IS NOT NULL {where}
                  ORDER BY stripped_seq, charge
                ) q LIMIT %s""",
            (*extra, sample_n), tables=["delimp_precursors"], timeout_ms=28000)

    def _producer():
        rows = None
        for with_irt in (True, False):     # try iRT-aware; fall back if the column doesn't exist yet
            try:
                rows = _q(with_irt)
                break
            except Exception:  # noqa: BLE001
                continue
        if not rows:
            return {"points": [], "x_axis": "Retention time (min)"}

        def _pt(r, x):
            return {"rt": x, "im": r["im"], "charge": r["charge"],
                    "precursor_mz": r.get("precursor_mz"), "intensity_log2": r.get("intensity_log2")}

        # NEVER mix scales on one axis. Use iRT only when it covers the MAJORITY of the
        # sample, and then plot ONLY iRT-bearing precursors. Otherwise plot raw RT for all.
        # (Mixing iRT minutes-scale clump + raw RT was the two-population artifact.)
        n_irt = sum(1 for r in rows if r.get("irt") is not None)
        if n_irt and n_irt >= 0.5 * len(rows):
            return {"points": [_pt(r, r["irt"]) for r in rows if r.get("irt") is not None],
                    "x_axis": "Indexed retention time (iRT)"}
        return {"points": [_pt(r, r["rt"]) for r in rows],
                "x_axis": "Retention time (min)"}

    if search_id:
        return _producer()
    return SLOW_CACHE.get_or_set(f"im_scatter_{sample_n}", _producer)


def charge_distribution() -> list[dict[str, Any]]:
    def _producer() -> list[dict[str, Any]]:
        # Estimate from pg_stats (instant) — an exact GROUP BY over the 2.7M-row
        # precursors table hits the statement timeout and blanked the dashboard.
        est = estimate_value_distribution("delimp_precursors", "charge")
        if est:
            out = []
            for r in est:
                try:
                    ch = int(float(r["value"]))
                except (TypeError, ValueError):
                    continue
                out.append({"charge": ch, "n": r["n"]})
            return sorted(out, key=lambda x: x["charge"])
        return []

    return CACHE.get_or_set("charge_dist", _producer)


# ---------------------------------------------------------------------------
# Search / browse
# ---------------------------------------------------------------------------
def search_peptides(
    seq: str, exact: bool = False, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    lim, off = _page(limit, offset)
    seq = (seq or "").strip().upper()
    if not seq:
        return {"rows": [], "total": 0}

    if exact:
        where = "stripped_seq = %s"
        param: Any = seq
        tmo: int | None = None  # indexed equality — no bound needed
    else:
        # NOTE: there is NO trigram index on stripped_seq, so a substring `ILIKE '%seq%'` is a full
        # scan of the 300M-row precursors table. Bound it with a SHORT statement timeout and degrade
        # gracefully (like search_proteins) instead of riding the 30s connection cap to a 503. (bug-sql #1)
        where = "stripped_seq ILIKE %s"
        param = f"%{seq}%"
        tmo = _PROTEIN_FALLBACK_TIMEOUT_MS
    try:
        rows = query(
            f"""
            SELECT stripped_seq,
                   COUNT(*)                         AS n_precursors,
                   COUNT(DISTINCT modified_seq_proforma) AS n_modforms,
                   COUNT(DISTINCT charge)           AS n_charges,
                   COUNT(DISTINCT raw_path)         AS n_runs,
                   COUNT(DISTINCT search_id)        AS n_searches,
                   MIN(q_value)                     AS best_q_value,
                   bool_or(im IS NOT NULL)          AS has_im,
                   MAX(n_engines_confirming)        AS max_engines
            FROM delimp_precursors
            WHERE {where}
            GROUP BY stripped_seq
            ORDER BY n_precursors DESC
            LIMIT %s OFFSET %s
            """,
            (param, lim, off),
            tables=["delimp_precursors"],
            timeout_ms=tmo,
        )
        total = query(
            f"SELECT COUNT(DISTINCT stripped_seq) FROM delimp_precursors WHERE {where}",
            (param,),
            tables=["delimp_precursors"],
            fetch="val",
            timeout_ms=tmo,
        )
    except Exception:  # noqa: BLE001 — substring scan exceeded the short bound
        return {"rows": [], "total": 0, "limit": lim, "offset": off, "degraded": True,
                "hint": "Substring peptide search is unindexed and timed out — search an EXACT "
                        "sequence (toggle Exact) or use a longer, more specific substring."}
    return {"rows": rows, "total": total or 0, "limit": lim, "offset": off}


# Columns selected/aggregated by the protein search (same shape for fast path + fallback).
_PROTEIN_SEARCH_COLS = """
    protein_group,
    MAX(gene)                  AS gene,
    COUNT(DISTINCT search_id)  AS n_searches,
    COUNT(DISTINCT raw_path)   AS n_runs,
    SUM(n_unique_peptides)     AS sum_unique_peptides,
    SUM(n_precursors)          AS sum_precursors,
    bool_or(is_contaminant)    AS any_contaminant
"""

# Tight timeout for the OPTIONAL substring fallback (a full ILIKE '%term%' is a 17M-row seq scan
# on PG Farm). Bounded so a miss degrades gracefully instead of riding the 30s connection cap to a
# 503 — mirrors the per-statement timeout_ms pattern used in protein_detail().
_PROTEIN_FALLBACK_TIMEOUT_MS = int(os.environ.get("DELIMP_PROTEIN_FALLBACK_TIMEOUT_MS", "4000"))


def _case_variants(term: str) -> list[str]:
    """Case spellings of `term` to probe the case-sensitive btree on gene with an indexed equality
    (idx_proteins_gene). Genes are conventionally UPPER (human) or Title (mouse, e.g. 'Apoe'); we
    also include the verbatim and lower forms. De-duplicated, order-stable."""
    cands = [term, term.upper(), term.lower(), term.capitalize(), term.title()]
    seen: set[str] = set()
    out: list[str] = []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


# Sentinel appended to a prefix to build a collation-safe upper bound for an indexed range scan.
# This is subtle on the DB's en_US.utf8 (UCA) collation, where two "obvious" choices are WRONG:
#   * byte-incrementing the last char ('P02649' -> 'P0264:') breaks when it crosses a collation
#     class: ':' sorts BEFORE '9', so [prefix, prefix++) is empty/inverted -> full accessions were
#     silently falling through to the slow substring scan.
#   * U+FFFF / U+10FFFF are UCA-IGNORABLE (zero weight), so 'A0A077'||U+FFFF collates ~equal to
#     'A0A077' and sorts BEFORE 'A0A077S9R2' -> real continuations were excluded.
# U+00FF ('ÿ') is a real Latin letter whose primary collation weight sorts AFTER all digits and basic
# Latin letters, so `col >= prefix AND col < prefix||U+00FF` reliably covers every ASCII continuation
# of the prefix AND is served by the default-collation btree (idx_proteins_group) as an index range
# scan. Verified with EXPLAIN + row counts against accession prefixes (A0A077, P02649, P0, ...).
_PREFIX_HI = "ÿ"


def _prefix_range(prefix: str) -> tuple[str, str] | None:
    """Turn a prefix into a half-open, collation-SAFE btree range [prefix, prefix||U+FFFF) so the
    DEFAULT-collation btree (idx_proteins_group, en_US.utf8) can serve it via an index range scan.
    A plain LIKE 'p%' / ILIKE can NOT use this btree (no text_pattern_ops opclass), and a naive
    char-increment upper bound breaks under locale collation — see _PREFIX_HI."""
    if not prefix:
        return None
    return prefix, prefix + _PREFIX_HI


def _protein_prefix_clauses(term: str) -> tuple[str, list[Any]]:
    """Build the OR'd indexed protein_group prefix-range clauses for the case variants of `term`.
    Each variant contributes a `(protein_group >= %s AND protein_group < %s)` that the btree serves;
    Postgres ORs them under a single BitmapOr. Returns (sql_fragment, params)."""
    clauses: list[str] = []
    params: list[Any] = []
    seen: set[tuple[str, str]] = set()
    for v in _case_variants(term):
        rng = _prefix_range(v)
        if rng and rng not in seen:
            seen.add(rng)
            clauses.append("(protein_group >= %s AND protein_group < %s)")
            params.extend(rng)
    return (" OR ".join(clauses), params)


def search_proteins(
    term: str, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    """Find protein groups by accession (protein_group) prefix or by exact gene symbol.

    INDEX-FAST common case (no pg_trgm needed): the search resolves to an indexed equality on `gene`
    (idx_proteins_gene) plus an indexed prefix-range on `protein_group` (idx_proteins_group). Both are
    btree-served via a BitmapOr — never the 17M-row seq scan that ILIKE '%term%' forced (the 503 cause).

    Only if that returns nothing do we OPTIONALLY try a substring fallback, bounded by a SHORT
    statement_timeout; a timeout there degrades gracefully ({"degraded": true, ...}) rather than 503.

    Return shape stays backward-compatible: {rows, total, limit, offset}. Extra keys
    (matched: "fast"|"substring", degraded, hint) are additive."""
    lim, off = _page(limit, offset)
    term = (term or "").strip()
    if not term:
        return {"rows": [], "total": 0, "limit": lim, "offset": off}

    # ---- FAST PATH: indexed gene-equality OR protein_group prefix-range ----
    gene_vars = _case_variants(term)
    prefix_sql, prefix_params = _protein_prefix_clauses(term)
    where_parts = ["gene = ANY(%s)"]
    where_params: list[Any] = [gene_vars]
    if prefix_sql:
        where_parts.append(prefix_sql)
        where_params.extend(prefix_params)
    fast_where = " OR ".join(where_parts)

    rows = query(
        f"""
        SELECT {_PROTEIN_SEARCH_COLS}
        FROM delimp_proteins
        WHERE {fast_where}
        GROUP BY protein_group
        ORDER BY sum_precursors DESC NULLS LAST
        LIMIT %s OFFSET %s
        """,
        (*where_params, lim, off),
        tables=["delimp_proteins"],
    )
    if rows:
        total = query(
            f"SELECT COUNT(DISTINCT protein_group) FROM delimp_proteins WHERE {fast_where}",
            tuple(where_params),
            tables=["delimp_proteins"],
            fetch="val",
        )
        return {"rows": rows, "total": total or 0, "limit": lim, "offset": off,
                "matched": "fast"}

    # ---- OPTIONAL SUBSTRING FALLBACK (only when the fast path found nothing) ----
    # A real infix match (e.g. searching "POE" to hit "APOE") needs a substring scan that the
    # btrees can't serve. Guard it with a SHORT timeout and degrade gracefully — NEVER 503.
    # (The real fix is a pg_trgm GIN index; see deploy/optimize_protein_search.sql.)
    like = f"%{term}%"
    try:
        rows = query(
            f"""
            SELECT {_PROTEIN_SEARCH_COLS}
            FROM delimp_proteins
            WHERE protein_group ILIKE %s OR gene ILIKE %s
            GROUP BY protein_group
            ORDER BY sum_precursors DESC NULLS LAST
            LIMIT %s OFFSET %s
            """,
            (like, like, lim, off),
            tables=["delimp_proteins"],
            timeout_ms=_PROTEIN_FALLBACK_TIMEOUT_MS,
        )
    except Exception:  # noqa: BLE001 - substring scan timed out/stalled -> degrade, never 503
        return {"rows": [], "total": 0, "limit": lim, "offset": off,
                "matched": "substring", "degraded": True,
                "hint": "Substring search timed out on this large table. Try the exact gene symbol "
                        "(e.g. APOE) or the accession prefix (e.g. P02649). A pg_trgm index "
                        "(deploy/optimize_protein_search.sql) makes substring search fast."}
    if not rows:
        return {"rows": [], "total": 0, "limit": lim, "offset": off, "matched": "substring"}
    # We have substring rows; an exact COUNT(DISTINCT) is the same heavy scan, so bound it too and
    # fall back to the page length rather than 503 if it stalls.
    try:
        total = query(
            "SELECT COUNT(DISTINCT protein_group) FROM delimp_proteins "
            "WHERE protein_group ILIKE %s OR gene ILIKE %s",
            (like, like),
            tables=["delimp_proteins"],
            fetch="val",
            timeout_ms=_PROTEIN_FALLBACK_TIMEOUT_MS,
        )
    except Exception:  # noqa: BLE001 - count stalled; return rows-so-far with a lower-bound total
        return {"rows": rows, "total": off + len(rows), "limit": lim, "offset": off,
                "matched": "substring", "degraded": True,
                "hint": "Exact match count unavailable (large-table scan timed out); total is a "
                        "lower bound. Install the pg_trgm index for fast counts."}
    return {"rows": rows, "total": total or 0, "limit": lim, "offset": off,
            "matched": "substring"}


# ---------------------------------------------------------------------------
# Detail views
# ---------------------------------------------------------------------------
def protein_detail(protein_group: str) -> dict[str, Any]:
    pg = (protein_group or "").strip()
    # All queries here hit the LIVE delimp_proteins table, which is heavily written during
    # ingestion -> on the shared PG Farm cluster even a tiny indexed read can stall for tens of
    # seconds. Give each a tight timeout + retry so a transient I/O stall degrades gracefully
    # instead of 503-ing the whole protein page. summary is retried (the page needs it); the
    # rest degrade to [].
    summary = query(
        """
        SELECT protein_group,
               MAX(gene) AS gene,
               COUNT(DISTINCT search_id) AS n_searches,
               COUNT(DISTINCT raw_path)  AS n_runs,
               SUM(n_unique_peptides)    AS sum_unique_peptides,
               SUM(n_precursors)         AS sum_precursors,
               AVG(NULLIF(intensity, 0)) AS avg_intensity,
               MIN(pg_q_value)           AS best_pg_q,
               bool_or(is_contaminant)   AS any_contaminant
        FROM delimp_proteins
        WHERE protein_group = %s
        GROUP BY protein_group
        """,
        (pg,),
        tables=["delimp_proteins"],
        fetch="one",
        timeout_ms=8000,
    )
    try:
        per_search = query(
            """
            SELECT p.search_id, s.search_name, s.search_engine,
                   p.raw_path, p.gene, p.n_unique_peptides, p.n_precursors,
                   p.intensity, p.normalized_intensity, p.pg_q_value
            FROM delimp_proteins p
            JOIN delimp_searches s ON s.id = p.search_id
            WHERE p.protein_group = %s
            ORDER BY p.intensity DESC NULLS LAST
            LIMIT %s
            """,
            (pg, MAX_PAGE),
            tables=["delimp_proteins", "delimp_searches"],
            timeout_ms=8000,
        )
    except Exception:  # noqa: BLE001 - transient ingestion-load stall -> per-run table degrades
        per_search = []
    # NOTE: the observed-peptide list is intentionally NOT computed here. api_protein computes it
    # ONCE via protein_coverage_peptides() (bounded + timed) and maps it onto the canonical
    # sequence. Computing it here too was pure waste — for a protein seen in many big runs it ran
    # a second multi-second precursor scan whose result api_protein discarded, ~doubling page time
    # and causing 503s on large proteins (e.g. dog proteins across 300+ runs).
    return {"summary": summary, "per_search": per_search, "peptides": []}


# ---------------------------------------------------------------------------
# Protein "trading card" — fun + cool stats for one protein group (real corpus data).
# ---------------------------------------------------------------------------
_CONTAMINANT_FLAIR = (
    ("krt", "keratin", "Keratin — basically skin, hair, wool or a stray dust mote. It's the "
            "proteomic fingerprint of whoever (or whatever) touched the sample, not the biology you were after."),
    ("k1c", "keratin", "Keratin — skin/hair/wool contamination; the fingerprint of whoever handled the sample."),
    ("k2c", "keratin", "Keratin — skin/hair/wool contamination; the fingerprint of whoever handled the sample."),
    ("tryp", "trypsin", "Trypsin — the digestion enzyme itself, autolysing and showing up in its own "
              "results. The one protein every bottom-up experiment is guaranteed to find."),
    ("alb", "albumin", "Serum albumin — the most abundant protein in blood, and a notorious tag-along "
             "from serum, FBS in cell media, or a fingertip."),
    ("cas", "casein", "Casein — milk protein, a classic contaminant from blocking buffers or a nearby coffee."),
)


# Gene-SPECIFIC contaminant identities (checked before the generic needles). Keyed by exact gene
# symbol (cRAP-/Cont_ prefix stripped, first gene of a group). Says WHAT it is + where it comes from.
_CONTAM_GENE = {
    # --- specific non-keratin contaminants ---
    "ALB":  ("albumin", "Serum albumin (ALB) — the most abundant blood protein; a classic tag-along from serum, FBS in cell media, or a fingertip."),
    "TF":   ("transferrin", "Serotransferrin (TF) — an abundant blood/serum protein; usually a serum or FBS carry-over."),
    "PRSS1":("trypsin", "Trypsin (PRSS1) — the digestion enzyme itself, autolysing and showing up in its own results. Every bottom-up run finds it."),
    "PRSS2":("trypsin", "Trypsin (PRSS2) — the digestion enzyme autolysing into the results."),
    "TRY1": ("trypsin", "Trypsin — the digestion enzyme autolysing into its own results."),
    "LYZ":  ("lysozyme", "Lysozyme (LYZ) — from tears, saliva or egg white; an environmental/handling tag-along."),
    "DCD":  ("sweat", "Dermcidin (DCD) — a sweat protein; literally fingerprint contamination from handling."),
    "HRNR": ("skin", "Hornerin (HRNR) — a cornified-envelope skin protein; skin/handling contamination."),
    "FLG":  ("skin", "Filaggrin (FLG) — an epidermal skin protein; skin-flake/handling contamination."),
    "FLG2": ("skin", "Filaggrin-2 (FLG2) — epidermal skin protein; skin/handling contamination."),
    "SBSN": ("skin", "Suprabasin (SBSN) — a skin/epidermis protein; handling contamination."),
    "JCHAIN":("antibody", "Ig J-chain (JCHAIN) — immunoglobulin; blood/serum or antibody-reagent carry-over."),
    "CSN1S1":("milk", "Casein αS1 (CSN1S1) — milk protein; from blocking buffers, BSA prep, or a nearby coffee."),
    "CSN2": ("milk", "Casein β (CSN2) — milk protein; from blocking buffer or BSA contamination."),
    "CSN3": ("milk", "Casein κ (CSN3) — milk protein; blocking-buffer/BSA contamination."),
}
# Keratin tissue by gene number — type-I hair (KRT31–40) & type-II hair (KRT71–86) + KRTAP = HAIR;
# the suprabasal/cornified epidermal keratins = SKIN flakes; cornea/mucosa; internal/simple epithelia.
_KERATIN_SKIN = {1, 2, 5, 6, 9, 10, 14, 15, 16, 17}     # epidermis (6/16/17 also nail)
_KERATIN_CORNEA = {3, 4, 12, 13}                         # cornea / mucosa
_KERATIN_SIMPLE = {7, 8, 18, 19, 20}                     # internal simple epithelia (may be real)


def _keratin_flair(gene_u: str) -> dict[str, str] | None:
    """Specific keratin identity from the gene symbol: hair vs skin vs cornea vs internal."""
    import re as _re_k
    if gene_u.startswith("KRTAP"):
        return {"kind": "hair keratin", "note": f"Hair keratin-associated protein ({gene_u}) — from hair; classic handling contamination."}
    m = _re_k.match(r"KRT(\d+)", gene_u)
    if not m:
        return None
    n = int(m.group(1))
    if 31 <= n <= 40 or 71 <= n <= 86:
        return {"kind": "hair keratin", "note": f"Hair keratin ({gene_u}) — a hard keratin of the hair shaft/follicle; a stray hair in the sample."}
    if n in _KERATIN_SKIN:
        nail = " (also nail)" if n in (6, 16, 17) else ""
        return {"kind": "skin keratin", "note": f"Skin keratin ({gene_u}){nail} — an epidermal keratin from skin flakes/dander; the fingerprint of whoever handled the sample."}
    if n in _KERATIN_CORNEA:
        return {"kind": "keratin (mucosa)", "note": f"Cornea/mucosa keratin ({gene_u}) — an epithelial keratin; usually handling/contact contamination."}
    if n in _KERATIN_SIMPLE:
        return {"kind": "keratin (epithelial)", "note": f"Simple-epithelium keratin ({gene_u}) — internal epithelial keratin; often a true ID, but listed in contaminant libraries."}
    return {"kind": "keratin", "note": f"Keratin ({gene_u}) — skin/hair contamination; the fingerprint of whoever handled the sample."}


def _contaminant_flair(protein_group: str, gene) -> dict[str, str]:
    """Map a known-contaminant identity to a SPECIFIC, plain-language note (one definition; UI renders it)."""
    g = (gene or "").split(";")[0].strip()
    g = re.sub(r"^(crap[-_]?|cont[_-])", "", g, flags=re.I).strip()
    gu = g.upper()
    if gu.startswith("KRT"):
        k = _keratin_flair(gu)
        if k:
            return k
    if gu in _CONTAM_GENE:
        kind, note = _CONTAM_GENE[gu]
        return {"kind": kind, "note": note}
    # fall back to substring needles, then generic
    hay = f"{(protein_group or '').lower()} {(gene or '').lower()}"
    for needle, label, note in _CONTAMINANT_FLAIR:
        if needle in hay:
            return {"kind": label, "note": note}
    return {"kind": "contaminant",
            "note": "Flagged as a common contaminant (cRAP / contaminant library) — usually a reagent or "
                    "environmental tag-along rather than your sample's biology."}


# Placeholder/custom-FASTA accessions: when a recombinant/engineered construct is searched it gets
# a made-up accession (P0000, P99999) + a throwaway gene ('whoKnows'), since it has no public entry.
# Detect these so the protein page shows a "custom construct" note instead of a dead UniProt link.
_PLACEHOLDER_GENES = {"whoknows", "who_knows", "test", "testing", "unknown", "custom", "construct",
                      "target", "recombinant", "placeholder", "mock", "dummy", "tbd", "xxx",
                      "na", "none", "fusion", "myprotein", "protein1", "seq1"}


def is_custom_accession(protein_group: str, gene=None) -> bool:
    """True if the leading accession looks like a custom-FASTA placeholder (no public DB entry)."""
    a = (protein_group or "").split(";")[0].strip()
    a = re.sub(r"^(crap|cont)[-_]?", "", a, flags=re.I)
    g = (gene or "").split(";")[0].strip().lower()
    if g in _PLACEHOLDER_GENES:
        return True
    if not a:
        return False
    # obviously-fake: a run of zeros/nines, or too short to be a real UniProt/RefSeq/GenBank accession
    if re.search(r"0{3,}", a) or re.search(r"9{4,}", a):
        return True
    if len(a) < 6 and re.match(r"^[A-Za-z]+\d+$", a):
        return True
    return False


def _looks_contaminant(protein_group: str) -> bool:
    pgl = (protein_group or "").lower()
    return pgl.startswith("crap") or pgl.startswith("cont_") or pgl.startswith("cont-")


def protein_card_stats(protein_group: str) -> dict[str, Any]:
    """Cool, real stats for the protein trading card: cross-species conservation, abundance percentile
    (sampled) vs the corpus, peak run/organism, detection breadth, best confidence, contaminant flair.
    Each sub-block degrades to None/[] independently. Cached briefly."""
    pg = (protein_group or "").strip()
    if not pg:
        return {}

    def _p() -> dict[str, Any]:
        out: dict[str, Any] = {"protein_group": pg}
        try:
            agg = query(
                """SELECT MAX(gene) AS gene, COUNT(DISTINCT search_id) AS n_searches,
                          COUNT(DISTINCT raw_path) AS n_runs, MAX(n_unique_peptides) AS max_unique_peptides,
                          SUM(n_precursors) AS sum_precursors, MIN(pg_q_value) AS best_pg_q,
                          AVG(NULLIF(intensity, 0)) AS mean_intensity, MAX(intensity) AS max_intensity,
                          bool_or(is_contaminant) AS any_contaminant
                   FROM delimp_proteins WHERE protein_group = %s GROUP BY protein_group""",
                (pg,), tables=["delimp_proteins"], fetch="one")
        except Exception:  # noqa: BLE001
            agg = None
        if not agg:
            return out
        out.update({k: agg.get(k) for k in ("gene", "n_searches", "n_runs", "max_unique_peptides",
                    "sum_precursors", "best_pg_q", "mean_intensity", "any_contaminant")})
        try:
            species = query(
                """SELECT m.organism_name AS organism, m.organism_taxon_id AS taxon_id,
                          COUNT(DISTINCT p.raw_path) AS n_runs
                   FROM delimp_proteins p JOIN delimp_sample_metadata m ON m.raw_path = p.raw_path
                   WHERE p.protein_group = %s AND m.organism_name IS NOT NULL
                   GROUP BY m.organism_name, m.organism_taxon_id ORDER BY n_runs DESC LIMIT 60""",
                (pg,), tables=["delimp_proteins", "delimp_sample_metadata"]) or []
        except Exception:  # noqa: BLE001
            species = []
        out["species"] = species
        out["n_organisms"] = len(species)
        try:
            top_run = query(
                """SELECT p.raw_path, p.intensity, p.search_id, s.search_name, m.organism_name
                   FROM delimp_proteins p JOIN delimp_searches s ON s.id = p.search_id
                   LEFT JOIN delimp_sample_metadata m ON m.raw_path = p.raw_path
                   WHERE p.protein_group = %s AND p.intensity IS NOT NULL
                   ORDER BY p.intensity DESC LIMIT 1""",
                (pg,), tables=["delimp_proteins", "delimp_searches", "delimp_sample_metadata"], fetch="one")
        except Exception:  # noqa: BLE001
            top_run = None
        out["top_run"] = top_run
        mine = agg.get("mean_intensity")
        if mine is not None:
            try:
                pr = query(
                    """SELECT COUNT(*) FILTER (WHERE intensity < %s) AS below,
                              COUNT(*) FILTER (WHERE intensity IS NOT NULL) AS total
                       FROM delimp_proteins TABLESAMPLE SYSTEM (2)""",
                    (float(mine),), tables=["delimp_proteins"], fetch="one", timeout_ms=12000)
            except Exception:  # noqa: BLE001
                pr = None
            if pr and pr.get("total"):
                out["abundance_percentile"] = round(100.0 * pr["below"] / pr["total"], 1)
                out["abundance_sample_n"] = int(pr["total"])
                out["abundance_sampled"] = True
        if agg.get("any_contaminant") or _looks_contaminant(pg):
            out["contaminant"] = _contaminant_flair(pg, agg.get("gene"))
        return out

    return CACHE.get_or_set(f"prot_card_{pg}", _p)


# ---------------------------------------------------------------------------
# Proteins Showcase — a delightful, fact-filled tour (conservation / whimsy /
# function / contaminants / superlatives). One aggregate over the fast indexed
# delimp_mv_species_proteins matview, bucketed in Python, cached (SLOW_CACHE) —
# never a live GROUP BY over delimp_proteins. Reuses _FUN_PROTEINS / _classify_gene
# / _CONTAMINANT_FLAIR (one definition each).
# ---------------------------------------------------------------------------
import re as _re_showcase

_SHOWCASE_CONTAM_PREFIX = _re_showcase.compile(r"^(crap|cont[_-])", _re_showcase.I)


@lru_cache(maxsize=200_000)
def _showcase_first_gene(gene) -> str:
    return (gene or "").split(";")[0].strip()


@lru_cache(maxsize=200_000)
def _showcase_clean_gene(gene):
    g = _re_showcase.sub(r"^(crap-|cont[_-])", "", _showcase_first_gene(gene), flags=_re_showcase.I).strip()
    return None if g.lower() in ("", "nan") else g


@lru_cache(maxsize=500_000)  # protein_group is unique per row, but the regex match is the cost
def _showcase_is_contaminant(protein_group, contam) -> bool:
    return bool(contam) or bool(_SHOWCASE_CONTAM_PREFIX.match(protein_group or ""))


def proteins_showcase() -> dict[str, Any]:
    """Fun, data-grounded tour of the corpus proteins: hero stats, most well-traveled
    (cross-species), biology-only conservation, whimsically-named gallery, function breakdown,
    contaminant gallery, and superlatives. One aggregate over delimp_mv_species_proteins, cached."""
    # Reads the PRECOMPUTED per-protein-group rollup delimp_mv_protein_agg (refreshed offline) via tiny
    # top-N slices + a couple of COUNTs — NEVER a live 499k-row GROUP BY pulled into the web server
    # (that timed out → empty page on the App Service). Same "precompute, read small" pattern as the
    # peptides snapshot. CONT = the precomputed, INDEXED is_cont boolean (flag OR Cont_/cRAP prefix) —
    # using the column not a regex so the top-N gallery queries hit the (is_cont, …) indexes.
    CONT = "is_cont"
    COLS = "protein_group, gene, n_species, sum_runs, sum_searches, max_pep, peak_mean_int, contam, top_organism"

    def _p() -> dict[str, Any]:
        try:
            cc = query(
                f"""SELECT count(*) AS total, count(*) FILTER (WHERE n_species>1) AS n_multi,
                           count(*) FILTER (WHERE {CONT}) AS n_contam,
                           count(*) FILTER (WHERE NOT {CONT}) AS n_bio, max(n_species) AS max_reach
                    FROM delimp_mv_protein_agg""",
                tables=["delimp_mv_protein_agg"], timeout_ms=20000)
        except Exception:  # noqa: BLE001 - matview not built yet / transient timeout
            return {}
        if not cc or not cc[0]["total"]:
            return {}
        cc = cc[0]
        try:
            n_org = query("SELECT COUNT(DISTINCT organism_name) AS n FROM delimp_mv_species_proteins",
                          tables=["delimp_mv_species_proteins"], fetch="val") or 0
        except Exception:  # noqa: BLE001
            n_org = 0

        def _topq(where, order, lim):
            try:
                return query(f"SELECT {COLS} FROM delimp_mv_protein_agg WHERE {where} ORDER BY {order} LIMIT {int(lim)}",
                             tables=["delimp_mv_protein_agg"], timeout_ms=15000)
            except Exception:  # noqa: BLE001
                return []

        def _item(r):
            is_cont = _showcase_is_contaminant(r["protein_group"], r["contam"])
            if is_cont:
                blurb = _contaminant_flair(r["protein_group"], r["gene"])["note"]
            else:
                cls = _classify_gene(r["gene"])
                blurb = "" if cls == "Other" else cls
            return {"protein_group": r["protein_group"], "gene": _showcase_clean_gene(r["gene"]),
                    "n_species": r["n_species"], "sum_runs": int(r["sum_runs"] or 0),
                    "top_organism": r.get("top_organism"), "is_contaminant": is_cont, "blurb": blurb}

        well_traveled = [_item(r) for r in _topq("protein_group <> 'UNKNOWN' AND n_species>=1",
                                                 "n_species DESC, sum_runs DESC", 20)]
        biology_conserved = [_item(r) for r in _topq(f"NOT {CONT} AND n_species>=1",
                                                     "n_species DESC, sum_runs DESC", 15)]
        contaminant_gallery = []
        for r in _topq(CONT, "n_species DESC, sum_runs DESC", 24):
            fl = _contaminant_flair(r["protein_group"], r["gene"])
            contaminant_gallery.append({"protein_group": r["protein_group"], "gene": _showcase_clean_gene(r["gene"]),
                                        "n_species": r["n_species"], "sum_runs": int(r["sum_runs"] or 0),
                                        "kind": fl["kind"], "note": fl["note"]})
        most_abundant = [{"protein_group": r["protein_group"], "gene": _showcase_clean_gene(r["gene"]),
                          "peak_mean_int": float(r["peak_mean_int"]), "n_species": r["n_species"],
                          "top_organism": r.get("top_organism")}
                         for r in _topq(f"NOT {CONT} AND peak_mean_int IS NOT NULL", "peak_mean_int DESC", 12)]
        most_peptides = [{"protein_group": r["protein_group"], "gene": _showcase_clean_gene(r["gene"]),
                          "max_pep": int(r["max_pep"]), "n_species": r["n_species"],
                          "top_organism": r.get("top_organism")}
                         for r in _topq(f"NOT {CONT} AND max_pep IS NOT NULL", "max_pep DESC", 12)]
        # whimsical: only the specific named genes (small targeted fetch, not a full scan)
        whimsical, seen_genes = [], set()
        try:
            whim = query(f"SELECT {COLS} FROM delimp_mv_protein_agg WHERE NOT {CONT} "
                         "AND upper(split_part(gene,';',1)) = ANY(%s)", (list(_FUN_PROTEINS.keys()),),
                         tables=["delimp_mv_protein_agg"], timeout_ms=15000)
        except Exception:  # noqa: BLE001
            whim = []
        for r in sorted(whim, key=lambda r: -(r["sum_runs"] or 0)):
            g = _showcase_first_gene(r["gene"]).upper()
            if g in _FUN_PROTEINS and g not in seen_genes:
                seen_genes.add(g)
                nm, why = _FUN_PROTEINS[g]
                whimsical.append({"protein_group": r["protein_group"], "gene": g, "name": nm, "why": why,
                                  "n_species": r["n_species"], "sum_runs": int(r["sum_runs"] or 0),
                                  "top_organism": r.get("top_organism")})
        # function breakdown: PRE-FILTER in SQL to class-relevant genes only (exact set OR class prefix),
        # so we fetch a few thousand rows — NOT all ~200k distinct genes (that fetch+classify was the
        # remaining 45s cold-load cost). Then classify those (now O(1) via _EXACT/_PREFIX_LABEL).
        buckets: dict[str, int] = {}
        try:
            fb = query(
                f"""SELECT upper(split_part(gene,';',1)) AS g, count(*) AS n FROM delimp_mv_protein_agg
                    WHERE NOT {CONT} AND (upper(split_part(gene,';',1)) = ANY(%s)
                          OR upper(split_part(gene,';',1)) ~ %s) GROUP BY 1""",
                (_CLASS_EXACT, _CLASS_PREFIX_RE), tables=["delimp_mv_protein_agg"], timeout_ms=20000)
        except Exception:  # noqa: BLE001
            fb = []
        for r in fb:
            lbl = _classify_gene(r["g"])
            if lbl != "Other":
                buckets[lbl] = buckets.get(lbl, 0) + (r["n"] or 0)
        function_breakdown = sorted(({"label": k, "n": v} for k, v in buckets.items()), key=lambda x: -x["n"])

        return {"hero": {"total_protein_groups": cc["total"], "n_biology": cc["n_bio"], "n_contaminants": cc["n_contam"],
                         "n_multi_species": cc["n_multi"], "n_organisms": int(n_org), "n_whimsical": len(whimsical),
                         "max_species_reach": cc["max_reach"] or 0},
                "well_traveled": well_traveled, "biology_conserved": biology_conserved, "whimsical": whimsical,
                "function_breakdown": function_breakdown, "contaminant_gallery": contaminant_gallery,
                "most_abundant": most_abundant, "most_peptides": most_peptides}
    return SLOW_CACHE.get_or_set("proteins_showcase", _p)


# ---------------------------------------------------------------------------
# Species Showcase — a cross-species "fun facts" tour of every organism in the
# corpus. Built entirely from cheap, indexed sources: species_distribution's
# orphan-excluded run counts (delimp_sample_metadata + search_raw_files) and the
# precomputed delimp_mv_species_proteins matview (indexed on organism_name) for
# per-species protein-group counts — NEVER a live GROUP BY over delimp_proteins.
# Every superlative names a real organism and is clickable -> species detail page.
# Cached (SLOW_CACHE). Each sub-query degrades to empty independently.
# ---------------------------------------------------------------------------
# Heuristic taxonomic grouping by GENUS (first token of organism_name). Explicit
# lookup only — we never infer taxonomy we can't justify from the genus name.
# Labeled "heuristic" in the UI; genera not listed fall into "Other / unclassified".
_TAXON_GROUPS: dict[str, str] = {
    # Bats (the vampire-bat & Neotropical bat project)
    "Desmodus": "Bats", "Diphylla": "Bats", "Artibeus": "Bats", "Tadarida": "Bats",
    "Pteronotus": "Bats", "Eptesicus": "Bats", "Molossus": "Bats", "Lasiurus": "Bats",
    "Noctilio": "Bats", "Bauerus": "Bats",
    # Birds
    "Haemorhous": "Birds", "Zonotrichia": "Birds", "Junco": "Birds", "Gallus": "Birds",
    # Other mammals
    "Homo": "Mammals", "Mus": "Mammals", "Rattus": "Mammals", "Canis": "Mammals",
    "Bos": "Mammals", "Sus": "Mammals", "Ovis": "Mammals", "Oryctolagus": "Mammals",
    "Macaca": "Mammals", "Equus": "Mammals", "Felis": "Mammals", "Capra": "Mammals",
    "Trichechus": "Mammals",  # Florida manatee
    # Fish
    "Oncorhynchus": "Fish", "Danio": "Fish", "Salmo": "Fish",
    "Oreochromis": "Fish",  # Nile tilapia
    "Thunnus": "Fish",      # tuna (yellowfin / bluefin)
    "Gadus": "Fish", "Cyprinus": "Fish",
    # Bacteria
    "Escherichia": "Bacteria", "Xylella": "Bacteria", "Bacillus": "Bacteria",
    "Pseudomonas": "Bacteria", "Staphylococcus": "Bacteria", "Salmonella": "Bacteria",
    # Fungi / yeast
    "Saccharomyces": "Fungi / yeast", "Aspergillus": "Fungi / yeast",
    "Candida": "Fungi / yeast", "Pichia": "Fungi / yeast",
    "Komagataella": "Fungi / yeast",  # = Pichia pastoris (expression host)
    "Hypocrea": "Fungi / yeast",      # = Trichoderma reesei
    "Trichoderma": "Fungi / yeast", "Neurospora": "Fungi / yeast",
    # Plants
    "Arabidopsis": "Plants", "Oryza": "Plants", "Zea": "Plants",
    "Triticum": "Plants", "Glycine": "Plants",
    "Cannabis": "Plants", "Cicer": "Plants", "Cucurbita": "Plants",
    "Digitaria": "Plants", "Gossypium": "Plants", "Helianthus": "Plants",
    "Lupinus": "Plants", "Nicotiana": "Plants", "Pisum": "Plants",
    "Solanum": "Plants", "Vigna": "Plants", "Hordeum": "Plants",
    "Brassica": "Plants", "Medicago": "Plants", "Phaseolus": "Plants",
    # Invertebrates (mollusks, tardigrades, nematodes, insects)
    "Octopus": "Invertebrates", "Hypsibius": "Invertebrates",
    "Meloidogyne": "Invertebrates", "Caenorhabditis": "Invertebrates",
    "Drosophila": "Invertebrates", "Apis": "Invertebrates", "Daphnia": "Invertebrates",
    "Arachis": "Plants", "Vicia": "Plants",  # peanut, fava bean
    # more bacteria
    "Acinetobacter": "Bacteria", "Akkermansia": "Bacteria", "Edwardsiella": "Bacteria",
    "Flavobacterium": "Bacteria", "Lactococcus": "Bacteria", "Mesomycoplasma": "Bacteria",
    "Vibrio": "Bacteria",
    # more fish + insects(invertebrates)
    "Acipenser": "Fish",  # sterlet sturgeon
    "Aedes": "Invertebrates", "Galleria": "Invertebrates", "Spodoptera": "Invertebrates",  # insects
    # Algae / diatoms (stramenopiles — not true plants)
    "Ectocarpus": "Algae", "Saccharina": "Algae", "Thalassiosira": "Algae",
    # Amphibians
    "Xenopus": "Amphibians",  # African clawed frog
    # Protozoa / parasites
    "Toxoplasma": "Protozoa / parasites", "Plasmodium": "Protozoa / parasites",
    "Trypanosoma": "Protozoa / parasites", "Leishmania": "Protozoa / parasites",
    "Naegleria": "Protozoa / parasites",      # brain-eating amoeba
    "Spironucleus": "Protozoa / parasites",   # diplomonad (Giardia relative)
    "Tetrahymena": "Protozoa / parasites",    # ciliate
    "Stentor": "Protozoa / parasites",        # ciliate (Stentor coeruleus)
    # genera added as the corpus grew (were falling into "Other / unclassified")
    "Vitis": "Plants", "Sorghum": "Plants", "Populus": "Plants", "Olea": "Plants",
    "Juglans": "Plants", "Trifolium": "Plants", "Spirodela": "Plants", "Avena": "Plants",
    "Ananas": "Plants",  # pineapple
    "Neisseria": "Bacteria", "Enhygromyxa": "Bacteria", "Fusobacterium": "Bacteria",
    "Peromyscus": "Mammals", "Mesocricetus": "Mammals",  # deer mouse, golden hamster
    "Coccidioides": "Fungi / yeast",  # Valley-fever fungus
}


def _taxon_group(organism_name: str) -> str:
    """Map an organism to a broad group by its GENUS (explicit lookup, heuristic).
    Unlisted genera -> 'Other / unclassified'. Never guesses taxonomy."""
    genus = (organism_name or "").strip().split(" ")[0]
    return _TAXON_GROUPS.get(genus, "Other / unclassified")


def species_showcase() -> dict[str, Any]:
    """Cross-species fun facts: hero totals, most-sampled species (by run count),
    the rarest 'seen once' club, species with the most protein groups identified,
    a heuristic taxonomic-breadth breakdown, and a spotlight species. Every value
    is from real corpus data over cheap indexed sources. Cached (SLOW_CACHE)."""
    def _p() -> dict[str, Any]:
        # --- run counts per IDENTIFIED species (mirror species_distribution's orphan
        # exclusion + non-empty-name filter, but with NO LIMIT) ---
        try:
            runs = query(
                f"""
                SELECT organism_name AS organism,
                       MAX(organism_taxon_id) AS organism_taxon_id,
                       COUNT(*) AS n_runs
                FROM delimp_sample_metadata m
                WHERE EXISTS (SELECT 1 FROM search_raw_files srf WHERE srf.raw_path = m.raw_path)
                  AND {_REAL_ORG_PRED}
                GROUP BY organism_name
                ORDER BY n_runs DESC
                """,
                tables=["delimp_sample_metadata", "search_raw_files"],
            )
        except Exception:  # noqa: BLE001
            runs = []
        if not runs:
            return {"available": False}

        total_species = len({r["organism"] for r in runs})
        total_runs = sum(int(r["n_runs"] or 0) for r in runs)

        def _run_item(r):
            return {"organism": r["organism"], "organism_taxon_id": r.get("organism_taxon_id"),
                    "n_runs": int(r["n_runs"] or 0)}

        most_sampled = [_run_item(r) for r in runs[:8]]
        # rarest = fewest runs (n_runs >= 1); the "only seen once/twice" club.
        rarest = sorted((r for r in runs if (r["n_runs"] or 0) >= 1),
                        key=lambda r: ((r["n_runs"] or 0), r["organism"]))
        rarest_items = [_run_item(r) for r in rarest[:8]]
        n_seen_once = sum(1 for r in runs if (r["n_runs"] or 0) == 1)

        # --- per-species protein-group counts from the indexed matview (cheap) ---
        try:
            # n_genes = honest "proteome depth". Two layers of over-count to strip:
            #  1. protein_group STRINGS triple-count (same protein in many group combos across FASTAs).
            #  2. the raw `gene` field is dirty — DIA-NN puts ACCESSIONS, isoform suffixes, and
            #     semicolon-joined MULTI-gene strings in it, so raw COUNT(DISTINCT gene) for human was
            #     ~29k (impossible; the proteome is ~20k). Clean = first ';' token, upper-cased,
            #     excluding pure UniProt accessions → human ~19k (defensible). See _N_GENES_SQL.
            prot = query(
                f"""SELECT organism_name AS organism,
                          {_N_GENES_SQL} AS n_genes,
                          COUNT(DISTINCT protein_group) AS n_protein_groups
                   FROM delimp_mv_species_proteins
                   WHERE {_REAL_ORG_PRED}
                   GROUP BY organism_name
                   ORDER BY n_genes DESC""",
                tables=["delimp_mv_species_proteins"])
        except Exception:  # noqa: BLE001 - matview not built yet
            prot = []
        pref = _proteome_reference()
        tid_by_org = {r["organism"]: r.get("organism_taxon_id") for r in runs}
        most_proteins = [{"organism": r["organism"], "n_genes": int(r["n_genes"] or 0),
                          "n_protein_groups": int(r["n_protein_groups"] or 0),
                          "pct_proteome": _pct_proteome(int(r["n_genes"] or 0), tid_by_org.get(r["organism"]), pref)}
                         for r in prot[:8]]

        # --- heuristic taxonomic breadth (by genus) ---
        gbuckets: dict[str, dict[str, int]] = {}
        for r in runs:
            g = _taxon_group(r["organism"])
            b = gbuckets.setdefault(g, {"n_species": 0, "n_runs": 0})
            b["n_species"] += 1
            b["n_runs"] += int(r["n_runs"] or 0)
        taxonomic_breadth = sorted(
            ({"group": k, "n_species": v["n_species"], "n_runs": v["n_runs"]}
             for k, v in gbuckets.items()),
            key=lambda x: (-x["n_species"], -x["n_runs"]))

        # --- spotlight: the species with the most protein groups identified (deepest
        # proteome in the corpus), enriched with its run count if we have it ---
        spotlight = None
        if most_proteins:
            top = most_proteins[0]
            runs_by_org = {r["organism"]: int(r["n_runs"] or 0) for r in runs}
            spotlight = {"organism": top["organism"],
                         "n_genes": top.get("n_genes"),
                         "n_protein_groups": top["n_protein_groups"],
                         "pct_proteome": top.get("pct_proteome"),
                         "n_runs": runs_by_org.get(top["organism"]),
                         "organism_taxon_id": tid_by_org.get(top["organism"]),
                         "taxon_group": _taxon_group(top["organism"])}

        # --- the FULL clickable directory: every identified species, with run + protein
        # counts, common name and group. This is the "table of all species" — sorted by runs. ---
        prot_by_org = {r["organism"]: int(r["n_protein_groups"] or 0) for r in prot}
        genes_by_org = {r["organism"]: int(r["n_genes"] or 0) for r in prot}
        all_species = [{
            "organism": r["organism"],
            "organism_taxon_id": r.get("organism_taxon_id"),
            "n_runs": int(r["n_runs"] or 0),
            "n_genes": genes_by_org.get(r["organism"]),
            "n_protein_groups": prot_by_org.get(r["organism"]),
            "pct_proteome": _pct_proteome(genes_by_org.get(r["organism"]), r.get("organism_taxon_id"), pref),
            "taxon_group": _taxon_group(r["organism"]),
        } for r in runs]

        return {
            "available": True,
            "hero": {"total_species": total_species, "total_runs": total_runs,
                     "n_seen_once": n_seen_once,
                     "n_with_proteome": len(prot)},
            "most_sampled": most_sampled,
            "rarest": rarest_items,
            "most_proteins": most_proteins,
            "taxonomic_breadth": taxonomic_breadth,
            "spotlight": spotlight,
            "all_species": all_species,
        }
    return SLOW_CACHE.get_or_set("species_showcase", _p)


# ---------------------------------------------------------------------------
# Corpus leaderboards ("most common ...") — heavy GROUP BYs, long-cached.
# "Common" = most reproducibly OBSERVED (across runs/searches), which is the
# meaningful signal and gets richer as the corpus diversifies. Failures raise
# (so they are NOT cached); the endpoint wraps each in a safe fallback.
# ---------------------------------------------------------------------------
def top_peptides(limit: int = 40) -> list[dict[str, Any]]:
    def _p():
        try:  # fast path: precomputed materialized view (refreshed offline)
            return query(
                "SELECT stripped_seq, n_obs, n_runs, n_searches, n_charges, has_im "
                "FROM delimp_mv_top_peptides ORDER BY n_runs DESC, n_obs DESC LIMIT %s",
                (int(limit),), tables=["delimp_mv_top_peptides"],
            )
        except Exception:  # noqa: BLE001 - mv not built -> instant pg_stats approximation
            mcv = estimate_value_distribution("delimp_precursors", "stripped_seq") or []
            mcv.sort(key=lambda m: -m["n"])
            return [{"stripped_seq": m["value"], "n_obs": m["n"], "n_runs": None,
                     "n_searches": None, "n_charges": None, "has_im": None, "approximate": True}
                    for m in mcv[:int(limit)]]
    return SLOW_CACHE.get_or_set(f"top_pep_{limit}", _p)


def peptide_flyability(stripped_seq: str) -> dict[str, Any] | None:
    """Precomputed Koina PFly flyability for one peptide (0 = poor flyer, 1 = strong flyer)
    plus the 4 class probabilities. Returns None if not yet scored."""
    try:
        rows = query(
            "SELECT stripped_seq, flyability, c1, c2, c3, c4, n_obs, mean_log2_intensity, model "
            "FROM delimp_peptide_flyability WHERE stripped_seq = %s",
            (stripped_seq.strip().upper(),), tables=["delimp_peptide_flyability"])
    except Exception:  # noqa: BLE001 - table not built yet
        return None
    return rows[0] if rows else None


def flyability_scatter(sample_n: int = 8000) -> list[dict[str, Any]]:
    """Predicted flyability vs observed mean intensity, one point per peptide — the
    Highlights scatter. The table is small (~tens of thousands of rows) so a direct read
    is fast; cached. Empty list until flyability_ingest.py has run."""
    def _p():
        try:
            # also return the argmax PFly class (1..4) so the scatter can color points by the
            # SAME most-likely-class metric the breakdown bar uses (the collapsed 0-1 score bins
            # don't line up with argmax, which made the two views look inconsistent).
            return query(
                """SELECT stripped_seq, flyability, mean_log2_intensity, n_obs,
                          CASE WHEN c4 >= c1 AND c4 >= c2 AND c4 >= c3 THEN 4
                               WHEN c3 >= c1 AND c3 >= c2 AND c3 >= c4 THEN 3
                               WHEN c2 >= c1 AND c2 >= c3 AND c2 >= c4 THEN 2
                               ELSE 1 END AS klass
                   FROM delimp_peptide_flyability
                   WHERE flyability IS NOT NULL AND mean_log2_intensity IS NOT NULL
                   ORDER BY n_obs DESC LIMIT %s""",
                (int(sample_n),), tables=["delimp_peptide_flyability"])
        except Exception:  # noqa: BLE001
            return []
    return SLOW_CACHE.get_or_set(f"fly_scatter_{sample_n}", _p)


def flyability_summary() -> dict[str, Any]:
    """Corpus-wide flyability category breakdown: % of distinct peptides whose MOST-LIKELY
    PFly class (argmax of the 4 softmax probs c1..c4) is each of the four ordinal classes —
    non-flyer / weak / intermediate / strong (PFly, J.Proteome Res. 2024). This is the
    discrete classification, distinct from the continuous 0-1 score the scatter plots."""
    def _p():
        try:
            rows = query(
                """WITH cat AS (
                       SELECT CASE
                                WHEN c4 >= c1 AND c4 >= c2 AND c4 >= c3 THEN 4
                                WHEN c3 >= c1 AND c3 >= c2 AND c3 >= c4 THEN 3
                                WHEN c2 >= c1 AND c2 >= c3 AND c2 >= c4 THEN 2
                                ELSE 1 END AS klass
                       FROM delimp_peptide_flyability WHERE c1 IS NOT NULL)
                   SELECT klass, COUNT(*) AS n FROM cat GROUP BY klass""",
                tables=["delimp_peptide_flyability"])
        except Exception:  # noqa: BLE001 - table not built yet
            return {"total": 0, "categories": []}
        counts = {int(r["klass"]): int(r["n"]) for r in rows}
        total = sum(counts.values())
        labels = {1: "Non-flyer", 2: "Weak flyer", 3: "Intermediate", 4: "Strong flyer"}
        cats = [{"klass": k, "label": labels[k], "n": counts.get(k, 0),
                 "pct": round(100 * counts.get(k, 0) / total, 1) if total else 0.0}
                for k in (4, 3, 2, 1)]  # strong -> non-flyer, for display
        return {"total": total, "categories": cats}
    return SLOW_CACHE.get_or_set("fly_summary", _p)


def top_proteins(limit: int = 40) -> list[dict[str, Any]]:
    def _p():
        try:
            return query(
                "SELECT protein_group, gene, n_searches, n_runs, sum_precursors, any_contaminant "
                "FROM delimp_mv_top_proteins ORDER BY n_runs DESC, sum_precursors DESC NULLS LAST LIMIT %s",
                (int(limit),), tables=["delimp_mv_top_proteins"],
            )
        except Exception:  # noqa: BLE001 - mv not built -> instant pg_stats approximation
            mcv = estimate_value_distribution("delimp_proteins", "protein_group") or []
            mcv.sort(key=lambda m: -m["n"])
            return [{"protein_group": m["value"], "gene": None, "n_searches": None,
                     "n_runs": None, "sum_precursors": m["n"], "any_contaminant": None,
                     "approximate": True} for m in mcv[:int(limit)]]
    return SLOW_CACHE.get_or_set(f"top_prot_{limit}", _p)


def top_genes(limit: int = 40) -> list[dict[str, Any]]:
    def _p():
        try:
            return query(
                "SELECT gene, n_groups, n_searches, n_runs FROM delimp_mv_top_genes "
                "ORDER BY n_runs DESC, n_searches DESC LIMIT %s",
                (int(limit),), tables=["delimp_mv_top_genes"],
            )
        except Exception:  # noqa: BLE001 - mv not built -> instant pg_stats approximation
            mcv = estimate_value_distribution("delimp_proteins", "gene") or []
            mcv = [m for m in mcv if m["value"]]
            mcv.sort(key=lambda m: -m["n"])
            return [{"gene": m["value"], "n_groups": None, "n_searches": None,
                     "n_runs": m["n"], "approximate": True} for m in mcv[:int(limit)]]
    return SLOW_CACHE.get_or_set(f"top_gene_{limit}", _p)


def word_leaderboard(scan_n: int = 30000) -> list[dict[str, Any]]:
    """Fun: English words / names / spicy words hidden in the corpus peptides.

    Prefers the PRECOMPUTED leaderboard (delimp_word_leaderboard), which scripts/wordhunt_all.py
    builds by scanning EVERY distinct peptide (~2M) offline — so the hunt covers the whole corpus,
    not just the top-N. Falls back to a live scan of the top-peptides matview if that table isn't
    built yet."""
    def _p():
        from . import wordhunt
        try:  # precomputed over ALL peptides (cheap read — a few hundred word rows)
            rows = query(
                "SELECT word, category, n_peptides, n_obs, example FROM delimp_word_leaderboard "
                "ORDER BY n_obs DESC, n_peptides DESC, word LIMIT 2000",
                tables=["delimp_word_leaderboard"],
            )
            if rows:
                # scrub hidden (e.g. spicy/red) words still sitting in the precomputed table
                rows = [r for r in rows
                        if not wordhunt.is_hidden(r.get("word"), r.get("category"))]
                for r in rows:  # tag common vs uncommon (frequency-list membership) for the UI filter
                    r["common"] = wordhunt.is_common(r.get("word"), r.get("category"))
                return rows
        except Exception:  # noqa: BLE001 - table not built yet -> fall back below
            pass
        try:  # fallback: scan the precomputed top peptides (mv) instead of a live full GROUP BY
            peps = query(
                "SELECT stripped_seq, n_obs FROM delimp_mv_top_peptides ORDER BY n_obs DESC LIMIT %s",
                (int(scan_n),), tables=["delimp_mv_top_peptides"],
            )
        except Exception:  # noqa: BLE001 - mv not built -> scan the pg_stats MCV peptides (instant)
            mcv = estimate_value_distribution("delimp_precursors", "stripped_seq") or []
            peps = [{"stripped_seq": m["value"], "n_obs": m["n"]} for m in mcv]
        res = wordhunt.scan(peps)
        for r in res:
            r["common"] = wordhunt.is_common(r.get("word"), r.get("category"))
        return res
    return SLOW_CACHE.get_or_set("wordhunt_all", _p)


def weekly_poem() -> dict[str, Any] | None:
    """A 'Found Poem of the Week' assembled from the COMMON words actually hidden in the corpus'
    peptides — deterministic per ISO week (rotates weekly), so it's stable within a week and new
    each week. Pure procedural found-poetry (no LLM): rhyming couplets built from real hidden words,
    each word still clickable to its peptides. Returns {title, lines:[[{t,w}]], year, week, ...}."""
    import datetime
    y, w, _ = datetime.date.today().isocalendar()
    seed = y * 100 + w

    def _p() -> dict[str, Any] | None:
        import random
        from collections import defaultdict
        lb = word_leaderboard() or []
        # readable building blocks: common dictionary words + a few names, 4-7 letters
        pool = [x["word"].capitalize() for x in lb
                if x.get("common") and x.get("category") in ("word", "name")
                and x.get("word") and x["word"].isalpha() and 4 <= len(x["word"]) <= 7]
        # dedupe preserve order
        seen = set(); pool = [p for p in pool if not (p in seen or seen.add(p))]
        if len(pool) < 24:
            return None
        rng = random.Random(seed)
        rng.shuffle(pool)
        rhyme = defaultdict(list)
        for word in pool:
            rhyme[word[-2:].lower()].append(word)
        couplets = [grp for grp in rhyme.values() if len(grp) >= 2]
        rng.shuffle(couplets)
        glue_open = ["O", "And", "The", "In", "We", "All", "Here", "Now", "Soft", "Wild", "Through", "Of"]
        glue_mid = ["and", "the", "of", "in", "like", "with", "a", "or", "to", "beneath", "among"]
        used = set(); lines = []
        def _avail():
            return next((p for p in pool if p not in used), None)
        def _mkline(endword):
            toks = [{"t": rng.choice(glue_open), "w": False}]
            mid = _avail()
            if mid:
                used.add(mid); toks.append({"t": mid, "w": True})
            toks.append({"t": rng.choice(glue_mid), "w": False})
            used.add(endword)
            toks.append({"t": endword, "w": True})
            return toks
        for grp in couplets:
            if len(lines) >= 8:
                break
            g = [x for x in grp if x not in used]
            if len(g) < 2:
                continue
            rng.shuffle(g)
            lines.append(_mkline(g[0]))
            lines.append(_mkline(g[1]))
        if len(lines) < 4:
            return None
        titles = ["Ode to the Proteome", "A Peptide Lullaby", "Song of the Spectra",
                  "The Found Sequence", "Ballad of the Hidden Words", "Sonnet for the Singletons",
                  "Carol of the Corpus", "Hymn of the Hidden Letters"]
        return {"title": titles[seed % len(titles)], "lines": lines[:8],
                "year": y, "week": w, "n_source_words": len(pool)}

    return SLOW_CACHE.get_or_set(f"weekly_poem_{y}_{w}", _p)


# AI-curated "best of" — hand-picked from the full scan (real substrings; see scripts/proteome_code.py
# output). These are the coherent / funny / spicy / tech gems, shown as "decoded transmissions".
_CURATED_PHRASES = [
    {"text": "WILD SWAP", "peptide": "QPVLPDWILDSWAPLEK"},
    {"text": "DVDS EVIL", "peptide": "VHGSSEAFWILVEDVDSEVILHHEYFLLK"},
    {"text": "EAGLES SEES", "peptide": "FIIASEAGLESSEESWEVVDK"},
    {"text": "PIKE SLEEPS", "peptide": "VPIPIKESLEEPSAK"},
    {"text": "EVENT SMILE", "peptide": "KEVENTSMILELIIK"},
    {"text": "TIED ELVIS", "peptide": "TIAAVLHLGNVEFQTIEDELVISNK"},
    {"text": "WALK EAST", "peptide": "VLPGDYEILATHPTWALKEASTTVR"},
    {"text": "TIDE RAIN", "peptide": "MINLSVPDTIDERAINK"},
    {"text": "LADY DIED", "peptide": "DPNINLADYDIEDR"},
    {"text": "DEAD GIT", "peptide": "LASVFRDQGDEADGITAR"},
    {"text": "DEALT NASA", "peptide": "MDEALTNASAIGDQR"},
    {"text": "FATE NASA", "peptide": "TEEHVFATENASASIAK"},
    {"text": "IDLE MPEG", "peptide": "GSDLAISDPSIDLEMPEGK"},
    {"text": "HDTV DAN", "peptide": "AYALQDQGHDTVDANLLLNLPADAR"},
    {"text": "GREG ENDIF", "peptide": "VEIGAEIGSGREGENDIFEGI"},
    {"text": "KYLE DEAR", "peptide": "KYLEDEAR"},
    {"text": "EYES SAIL", "peptide": "VQNDEYESSAILQLNNIISGLK"},
    {"text": "DEER MEET", "peptide": "NYYAESYGVIFVVDSSDEERMEETK"},
    {"text": "GREW GAME", "peptide": "GREWGAMER"},
    {"text": "INDIA NICE", "peptide": "ISFINDIANICER"},
    {"text": "LEAN SEAL", "peptide": "KSDLEANSEALIQEIDFLR"},
    {"text": "VEGAS TEAM", "peptide": "QEVEGASTEAMR"},
    {"text": "FIND LISA", "peptide": "LNFINDLISAGLK"},
    {"text": "DAVE WAVE", "peptide": "FVGLDAVEWAVEAER"},
    {"text": "DICK EVAL", "peptide": "VDICKEVALLAAK"},
    {"text": "WANK MEAL", "peptide": "SWANKMEALTSK"},
    {"text": "TWAT YALE", "peptide": "NGQTWATYALETAAAANAK"},
    {"text": "REAL HELL", "peptide": "DIREALHELLCCTNVSTK"},
    {"text": "DEAD SMTP", "peptide": "IVILDEADSMTPGAQQALR"},
    {"text": "SKYPE APPS", "peptide": "VECGSKYPEAPPSVR"},
    {"text": "EVER KNEE", "peptide": "EVERKNEELSVLLK"},
    {"text": "KISS EDGE", "peptide": "ASKISSEDGEETR"},
    {"text": "MEDAL LYNN FIST", "peptide": "MEDALLYNNFISTPAYTGFLK"},
    {"text": "SEAS SANS DEAR", "peptide": "DSEASSANSDEAR"},
    {"text": "DENY FINE EDEN", "peptide": "NEDADENYFINEEDENLPHYDEK"},
    {"text": "SELL LADY FEED", "peptide": "SELLLADYFEEDPK"},
]
_CURATED_PROPHECIES = [
    # Each message is REORDERED from the content-words this protein literally hides in its peptides
    # (every word is a real substring) — arranged to actually READ, with the word-salad dropped.
    {"text": "Wise wind, lead well.", "protein_group": "P16546"},
    {"text": "Damn evil eyes. Neil, stay.", "protein_group": "A0A571BF58"},
    {"text": "Dean, dive less. Laser step.", "protein_group": "V6LX80"},
    {"text": "Mean peer, leave. Evil shade.", "protein_group": "Q04747"},
    {"text": "Five days. Angel died.", "protein_group": "V6LIK0"},
    {"text": "Save idea, Dave. Mind ends.", "protein_group": "A0A6A5BE73"},
    {"text": "Seal cell. Mass, flesh.", "protein_group": "A2AAJ9"},
    {"text": "Deaf Neil, sell deal.", "protein_group": "Q9QXS1"},
    {"text": "Alice gave Pete lean life.", "protein_group": "I7M9J2"},
    {"text": "Neal, stay. Miss past plan.", "protein_group": "P48415"},
    {"text": "Emma, tell. Task laden.", "protein_group": "Q92616"},
    {"text": "Deny reel. Wise wind, lead.", "protein_group": "A3KGU5"},
    # search-level prophecies — words pulled from across ALL proteins in one search, reordered to read.
    {"text": "Save teen, give life. Meet, feel, stay.",
     "search_id": "2258d7ad-f8d3-5043-96a0-b78bd70a2a71", "search_name": "PFC1-9"},
    {"text": "Same game. Save team. Keep self, tell.",
     "search_id": "4322178a-36f5-5b5d-b73c-19a2c9f7595d", "search_name": "Maribel-1-research"},
    {"text": "Want file. Send, keep, tell.",
     "search_id": "8316e5b3-f2bd-5613-bcf9-5debe72025af", "search_name": "JY_ReV_DB_REAL"},
]


def proteome_code() -> dict[str, Any]:
    """THE PROTEOME CODE 🔮 — AI-curated 'decoded transmissions' (the best hidden phrases + protein
    prophecies, hand-picked) plus the full auto-scan (delimp_proteome_code) as 'the raw signal', and
    a deterministic 'transmission of the week' rotating through the curated prophecies."""
    import datetime
    y, w, _ = datetime.date.today().isocalendar()

    key = f"proteome_code_{y}_{w}"
    hit = SLOW_CACHE.cached(key)
    if hit is not None:
        return hit

    def _p() -> tuple[dict[str, Any], bool]:
        # The CURATED set is unconditional (hardcoded) — it must always render, even if the dynamic
        # raw-signal scan errors on the live container. So build the curated payload first, then try
        # to enrich with the auto-scan; any failure there degrades to empty raw-signal, never blanks.
        # Returns (payload, raw_ok); we only CACHE when raw_ok, so a transient cold-start DB blip
        # returns the curated-only view but self-heals on the very next request (instead of caching
        # an empty raw signal for the whole 30-min TTL).
        out = {
            "available": True,
            "featured_phrases": _CURATED_PHRASES, "featured_prophecies": _CURATED_PROPHECIES,
            "transmission_of_week": _CURATED_PROPHECIES[(y * 100 + w) % len(_CURATED_PROPHECIES)],
            "phrases": [], "prophecies": [], "search_prophecies": [],
            "n_phrases": 0, "n_prophecies": 0, "n_search": 0, "week": w, "year": y,
        }
        raw_ok = False
        try:
            from . import wordhunt
            rows = query("SELECT kind, text, n_words, peptide, protein_group, search_id, words "
                         "FROM delimp_proteome_code", tables=["delimp_proteome_code"])

            # The machine 'prophecy' rows are a frequency-bag of content words from a whole
            # protein/search — they are inherently word-salad (no syntax), so we DON'T surface
            # them raw; the curated/hand-arranged transmissions above are the readable ones.
            # The raw signal we DO show is the genuinely-readable phrases only: a clean 2-word
            # English pair (both common words, >=4 letters) that actually reads, e.g. LIST DATA,
            # NAME SIGN, FEED NEED. This is the "filter the word salad" pass.
            def _reads(words):
                ws = [w for w in (words or []) if w]
                return (len(ws) == 2 and all(len(w) >= 4 for w in ws)
                        and all(wordhunt.is_common(w) for w in ws)
                        # drop phrases containing a hidden (spicy/red) word, e.g. DICK EVAL, WANK MEAL
                        and not any(wordhunt.is_hidden(w) for w in ws))

            phrases = [r for r in rows if r["kind"] == "phrase" and _reads(r.get("words"))]
            for r in phrases:
                r["sense"] = wordhunt.sense_score(r.get("words") or [])
            phrases.sort(key=lambda r: r["sense"])
            out.update({"phrases": phrases[:200], "prophecies": [], "search_prophecies": [],
                        "n_phrases": len(phrases), "n_prophecies": len(_CURATED_PROPHECIES),
                        "n_search": 0})
            raw_ok = True
        except Exception as e:  # noqa: BLE001 - raw-signal best-effort; curated already in `out`
            out["_raw_err"] = f"{type(e).__name__}: {e}"[:300]
        return out, raw_ok

    payload, raw_ok = _p()
    if raw_ok:
        SLOW_CACHE.put(key, payload)
    return payload


# ---------------------------------------------------------------------------
# Peptides showcase — a fact-filled tour of the corpus' peptides.
# PERFORMANCE: never a live GROUP BY over delimp_precursors (19M rows -> 30s
# timeout). Built entirely from the bounded precomputed matviews:
#   - delimp_mv_top_peptides (20k rows): superlatives + composition + words
#   - delimp_peptide_flyability (~383k, PK on stripped_seq, ORDER BY ... LIMIT
#     is index-fast): best/worst flyer extremes
# plus reuse of flyability_summary() and word_leaderboard(). The physico-chem
# stats are computed in-memory in Python over the bounded sequence list using
# peptide_physchem() — the same monoisotopic-mass / GRAVY math used on the
# peptide page. Long-cached (snapshot, not live).
# ---------------------------------------------------------------------------
def _peptides_showcase_pool(scan_n: int = 100000) -> list[dict[str, Any]]:
    """The bounded peptide pool the superlatives are computed over: the most-
    observed `scan_n` distinct peptides from delimp_mv_top_peptides (20k rows,
    fast). Each row already carries n_obs / n_runs / n_charges / has_im."""
    try:
        return query(
            "SELECT stripped_seq, n_obs, n_runs, n_charges, has_im "
            "FROM delimp_mv_top_peptides ORDER BY n_obs DESC LIMIT %s",
            (int(scan_n),), tables=["delimp_mv_top_peptides"],
        )
    except Exception:  # noqa: BLE001 - mv not built -> pg_stats MCV peptides (instant)
        mcv = estimate_value_distribution("delimp_precursors", "stripped_seq") or []
        mcv.sort(key=lambda m: -m["n"])
        return [{"stripped_seq": m["value"], "n_obs": m["n"], "n_runs": None,
                 "n_charges": None, "has_im": None} for m in mcv[:int(scan_n)]]


def _peptides_showcase_flyers() -> dict[str, Any]:
    """Best & worst predicted flyers from the precomputed flyability table.
    ORDER BY flyability ... LIMIT on a small (~383k) table is index/fast — never
    a full scan of delimp_precursors. Returns the continuous-score extremes."""
    cols = "stripped_seq, flyability, n_obs, mean_log2_intensity"
    try:
        best = query(
            f"SELECT {cols} FROM delimp_peptide_flyability "
            "WHERE flyability IS NOT NULL ORDER BY flyability DESC, n_obs DESC LIMIT 12",
            tables=["delimp_peptide_flyability"])
        worst = query(
            f"SELECT {cols} FROM delimp_peptide_flyability "
            "WHERE flyability IS NOT NULL ORDER BY flyability ASC, n_obs DESC LIMIT 12",
            tables=["delimp_peptide_flyability"])
    except Exception:  # noqa: BLE001 - table not built yet
        return {"best": [], "worst": []}
    return {"best": best or [], "worst": worst or []}


# ---------------------------------------------------------------------------
# INTERNAL (private deployment only) — collaborator / provenance browser.
# Reads delimp_search_provenance, which is ONLY in the allowlist when DELIMP_INTERNAL_MODE=1.
# Lets the core facility see "all searches for collaborator X" with real names + file locations.
# ---------------------------------------------------------------------------
def internal_collaborators() -> dict[str, Any]:
    """The private collaborator directory, keyed on the deterministic SERVICE-DIRECTORY customer
    folder (delimp_search_provenance.service_customer), merged to canonical + flagged via the curated
    map (collab/service_customer_aliases.json). This replaces the old grouping on `client`, which
    collapsed all 453 on-campus searches into a single generic "UC Davis" (the real lab lived in `pi`).

    Returns {collaborators: [...keep...], excluded: [...internal/standard...], n_internal_standard,
    n_unattributed_searches}. CoreOmics PI/institute is ADVISORY (confidence confirmed|suggested) and
    never overrides the folder name."""
    rows = query(
        """
        SELECT service_customer AS raw,
               COUNT(*) AS n_searches,
               COUNT(DISTINCT NULLIF(pi,'')) AS n_pis,
               COUNT(DISTINCT NULLIF(project,'')) AS n_projects,
               COUNT(*) FILTER (WHERE coreomics_submission_id IS NOT NULL
                                   OR sample_submission_id IS NOT NULL) AS n_lims_linked,
               MAX(service_campus) AS campus, MAX(service_source) AS source
        FROM delimp_search_provenance
        WHERE service_customer IS NOT NULL
        GROUP BY service_customer
        """,
        tables=["delimp_search_provenance"],
    )
    n_unattributed = query(
        "SELECT COUNT(*) FROM delimp_search_provenance WHERE service_customer IS NULL",
        tables=["delimp_search_provenance"], fetch="val") or 0

    merged: dict[str, dict[str, Any]] = {}
    for r in rows:
        info = collab.resolve(r["raw"])
        ck = collab._norm(info["canonical"])
        m = merged.get(ck)
        if m is None:
            m = merged[ck] = {"client": info["canonical"], "flag": info.get("flag", "keep"),
                              "campus": info.get("campus") or r["campus"], "scope": "customer",
                              "n_searches": 0, "n_pis": 0, "n_projects": 0, "n_lims_linked": 0,
                              "co_pi": None, "co_institute": None, "co_confidence": None,
                              "_sources": set()}
        m["n_searches"] += r["n_searches"] or 0
        m["n_pis"] += r["n_pis"] or 0
        m["n_projects"] += r["n_projects"] or 0
        m["n_lims_linked"] += r["n_lims_linked"] or 0
        if r["source"]:
            m["_sources"].add(r["source"])
        co = info.get("coreomics")
        if co and not m["co_pi"]:
            m["co_pi"], m["co_institute"], m["co_confidence"] = (
                co.get("pi"), co.get("institute"), co.get("confidence"))

    for m in merged.values():
        m["source"] = ",".join(sorted(m.pop("_sources")))
    keep = sorted((m for m in merged.values() if m["flag"] == "keep"),
                  key=lambda m: (-m["n_searches"], m["client"]))
    excluded = sorted((m for m in merged.values() if m["flag"] != "keep"),
                      key=lambda m: (-m["n_searches"], m["client"]))
    return {"collaborators": keep, "excluded": excluded,
            "n_internal_standard": len(excluded), "n_unattributed_searches": int(n_unattributed)}


def internal_labs_by_institution() -> dict[str, Any]:
    """PRIVATE: EVERY lab in the submission system that we have data for — analyzed in FRAN OR located
    on the share but not yet ingested (from the AI disk-match). Grouped by canonical institution (UC
    Davis spelling variants merged), deduped by PI, UC Davis labs annotated with college/department.
    Each lab carries a status (analyzed vs on-share/un-ingested) so core staff see the FULL customer
    base, not just what's in the corpus. Powers the 'Labs by institution' section; each lab clickable."""
    rows = query(
        """
        SELECT COALESCE(NULLIF(co.institute,''), '(institute not given)') AS institute,
               co.pi_first_name, co.pi_last_name,
               COUNT(DISTINCT co.submission_id) AS n_submissions,
               COUNT(DISTINCT p.search_id)      AS n_searches,
               COUNT(DISTINCT sd.submission_id) FILTER (WHERE sd.in_fran) AS n_analyzed_subs
        FROM coreomics_submissions_cache co
        LEFT JOIN delimp_search_provenance p ON p.coreomics_submission_id = co.submission_id
        LEFT JOIN delimp_submission_service_dir sd ON sd.submission_id = co.submission_id
        WHERE COALESCE(co.pi_last_name,'') <> ''
          AND (p.search_id IS NOT NULL OR sd.submission_id IS NOT NULL)
        GROUP BY 1, 2, 3
        """,
        tables=["coreomics_submissions_cache", "delimp_search_provenance", "delimp_submission_service_dir"],
    )
    overrides = _lab_institute_overrides()  # AI-researched institute for blank-institute labs
    insts: dict[str, dict[str, Any]] = {}
    for r in rows:
        pi = " ".join(x for x in (r.get("pi_first_name"), r.get("pi_last_name")) if x) or r.get("pi_last_name")
        raw_inst = r["institute"]
        # when CoreOmics gave no institute, fall back to the override map (keyed on the PI string) so
        # the lab lands under its real institution instead of "(institute not given)".
        if (not (raw_inst or "").strip()) or raw_inst == "(institute not given)":
            raw_inst = overrides.get((pi or "").strip().lower()) or raw_inst
        inst = collab.canonical_institute(raw_inst)
        bucket = insts.setdefault(inst, {"institute": inst, "labs": {}, "n_searches": 0})
        lab = bucket["labs"].setdefault(pi, {"pi": pi, "n_submissions": 0, "n_searches": 0,
                                             "n_analyzed": 0, "_raw": set()})
        lab["n_submissions"] += r["n_submissions"] or 0
        lab["n_searches"] += r["n_searches"] or 0
        lab["n_analyzed"] += r["n_analyzed_subs"] or 0
        lab["_raw"].add(r["institute"])
        bucket["n_searches"] += r["n_searches"] or 0
    out = []
    for b in insts.values():
        labs = []
        for lab in b["labs"].values():
            analyzed = lab["n_searches"] > 0 or lab["n_analyzed"] > 0
            entry = {"pi": lab["pi"], "n_submissions": lab["n_submissions"], "n_searches": lab["n_searches"],
                     "status": "analyzed" if analyzed else "on_share"}
            if b["institute"] == "UC Davis":
                college, dept = collab.uc_davis_affiliation(lab["_raw"])
                entry["college"], entry["department"] = college, dept
            labs.append(entry)
        # analyzed labs first (most searches), then un-ingested (most submissions)
        labs.sort(key=lambda l: (l["status"] != "analyzed", -l["n_searches"], -l["n_submissions"], l["pi"]))
        out.append({"institute": b["institute"], "labs": labs, "n_searches": b["n_searches"],
                    "n_labs": len(labs), "n_uningested": sum(1 for l in labs if l["status"] != "analyzed")})
    out.sort(key=lambda x: (-x["n_searches"], -x["n_labs"], x["institute"]))
    return {"n_institutions": len(out), "n_labs": sum(i["n_labs"] for i in out), "institutions": out}


def internal_collaborator_searches(client: str) -> dict[str, Any]:
    """All searches for one collaborator with REAL names, PI, project, report + raw-file paths,
    and LIMS links — for the private core-facility view."""
    c = (client or "").strip()
    # `c` is a CANONICAL collaborator name from the directory; map it back to the raw service_customer
    # folder value(s) the DB stores, and also match the legacy `client` column as a safety net.
    raws = collab.raws_for_canonical(c)
    cond = "(p.service_customer = ANY(%s) OR p.client = %s)"
    params = [raws, c]
    rows = query(
        f"""
        SELECT p.search_id, p.real_search_name, p.pi, p.project, p.scope, p.campus,
               p.report_path, p.n_raw_files, p.coreomics_submission_id, p.sample_submission_id,
               p.customer_contact, p.linkage_status, p.recorded_at,
               s.search_engine, s.n_precursors_total, s.n_proteins_total,
               -- CoreOmics submission identity (the authoritative LIMS link), when matched:
               co.pi_first_name, co.pi_last_name, co.submitter_first_name, co.submitter_last_name,
               co.institute AS co_institute, co.submitted_at::date AS co_submitted,
               co.num_samples AS co_num_samples, co.proteomics_type AS co_type, co.organism AS co_organism,
               (SELECT m.organism_name FROM search_raw_files srf
                  JOIN delimp_sample_metadata m ON m.raw_path = srf.raw_path
                 WHERE srf.search_id = p.search_id AND m.organism_name IS NOT NULL LIMIT 1) AS organism
        FROM delimp_search_provenance p
        LEFT JOIN delimp_searches s ON s.id = p.search_id
        LEFT JOIN coreomics_submissions_cache co ON co.submission_id = p.coreomics_submission_id
        WHERE {cond}
        ORDER BY p.recorded_at DESC NULLS LAST, p.real_search_name
        """,
        tuple(params),
        tables=["delimp_search_provenance", "delimp_searches", "search_raw_files",
                "delimp_sample_metadata", "coreomics_submissions_cache"],
    )
    return {"client": c, "n_searches": len(rows), "searches": rows}


def internal_people_search(q: str, limit: int = 80) -> dict[str, Any]:
    """PRIVATE: find searches by the PEOPLE / LIMS identity behind them — CoreOmics PI name,
    submitter name, submission number, institute — plus the provenance path-hint (client / pi /
    project / real search name). Powers the confidential search box's 'People / Submissions' tab.

    Matches against the linked CoreOmics submission when one exists, AND against the person
    resolved for unlinked searches (customer_contact, linkage_status='person:*'), so typing a PI's
    surname surfaces their data even where we couldn't pin an exact submission."""
    term = (q or "").strip()
    if len(term) < 2:
        return {"q": term, "total": 0, "rows": []}
    like = f"%{term}%"
    rows = query(
        """
        SELECT p.search_id, p.real_search_name, p.client, p.pi AS path_hint, p.project,
               p.coreomics_submission_id, p.customer_contact, p.linkage_status,
               co.pi_first_name, co.pi_last_name, co.submitter_first_name, co.submitter_last_name,
               co.institute AS co_institute, co.submitted_at::date AS co_submitted,
               co.num_samples AS co_num_samples,
               s.search_engine, s.n_precursors_total
        FROM delimp_search_provenance p
        LEFT JOIN coreomics_submissions_cache co ON co.submission_id = p.coreomics_submission_id
        LEFT JOIN delimp_searches s ON s.id = p.search_id
        WHERE co.pi_last_name ILIKE %(like)s OR co.pi_first_name ILIKE %(like)s
           OR co.submitter_last_name ILIKE %(like)s OR co.submitter_first_name ILIKE %(like)s
           OR co.institute ILIKE %(like)s OR p.coreomics_submission_id::text ILIKE %(like)s
           OR p.customer_contact ILIKE %(like)s
           OR p.client ILIKE %(like)s OR p.pi ILIKE %(like)s OR p.project ILIKE %(like)s
           OR p.real_search_name ILIKE %(like)s
        ORDER BY (p.coreomics_submission_id IS NOT NULL) DESC, p.recorded_at DESC NULLS LAST,
                 p.real_search_name
        LIMIT %(limit)s
        """,
        {"like": like, "limit": int(limit)},
        tables=["delimp_search_provenance", "coreomics_submissions_cache", "delimp_searches"],
    )
    return {"q": term, "total": len(rows), "rows": rows}


def internal_submission(submission_id: str) -> dict[str, Any]:
    """PRIVATE: one CoreOmics submission (PI / submitter / institute / date / samples) plus EVERY
    FRAN search linked to it. Powers the submission-ID page. Returns {submission, searches:[...]}."""
    sid = (submission_id or "").strip()
    sub_rows = query(
        """
        SELECT submission_id, pi_first_name, pi_last_name, submitter_first_name, submitter_last_name,
               institute, submitted_at::date AS submitted_at, num_samples, proteomics_type, organism
        FROM coreomics_submissions_cache WHERE submission_id::text = %s LIMIT 1
        """,
        (sid,), tables=["coreomics_submissions_cache"],
    )
    submission = sub_rows[0] if sub_rows else None
    samples = query(
        """SELECT unique_id, sample_name, condition_name
           FROM coreomics_samples_cache WHERE submission_id::text = %s ORDER BY sample_name""",
        (sid,), tables=["coreomics_samples_cache"],
    )
    searches = query(
        """
        SELECT p.search_id, p.real_search_name, p.client, p.pi AS path_hint, p.linkage_status,
               s.search_engine, s.n_precursors_total, s.n_proteins_total,
               (SELECT m.organism_name FROM search_raw_files srf
                  JOIN delimp_sample_metadata m ON m.raw_path = srf.raw_path
                 WHERE srf.search_id = p.search_id AND m.organism_name IS NOT NULL LIMIT 1) AS organism
        FROM delimp_search_provenance p
        LEFT JOIN delimp_searches s ON s.id = p.search_id
        WHERE p.coreomics_submission_id::text = %s
        ORDER BY p.recorded_at DESC NULLS LAST, p.real_search_name
        """,
        (sid,),
        tables=["delimp_search_provenance", "delimp_searches", "search_raw_files",
                "delimp_sample_metadata"],
    )
    # Service-directory location (from the AI disk-match) — where this submission's raw data lives on
    # the share, and whether it's analyzed in FRAN or still un-ingested.
    loc = query(
        """SELECT service_folder, service_folder_win, in_fran, run_count, match_confidence
           FROM delimp_submission_service_dir WHERE submission_id::text = %s LIMIT 1""",
        (sid,), tables=["delimp_submission_service_dir"])
    return {"submission_id": sid, "submission": submission,
            "samples": samples, "searches": searches, "n_searches": len(searches),
            "service_dir": (loc[0] if loc else None)}


_LAB_STOP = {"lab", "laboratory", "the", "dr", "prof", "mr", "ms", "mrs", "group", "core",
             "university", "california", "department", "institute", "center", "centre"}


def internal_lab(pi: str) -> dict[str, Any]:
    """PRIVATE: a lab page keyed on a PI name — every CoreOmics submission for that PI plus every
    FRAN search behind them (submission-linked OR person-resolved). Drill path: people-search ->
    lab -> submission -> search.

    CoreOmics pi_last_name is messy ("Dr. Palczewski Lab", full lab strings), so we match on the
    identity TOKENS of the passed name (alpha tokens >=4 chars, minus stopwords) as substrings of
    pi_last_name / submitter_last_name / customer_contact / provenance pi-hint — not exact equality."""
    name = (pi or "").strip()
    toks = [t for t in re.findall(r"[A-Za-z]{4,}", name.lower()) if t not in _LAB_STOP]
    if not toks:
        return {"pi": name, "surname": name, "institutes": [], "n_submissions": 0,
                "submissions": [], "n_searches": 0, "searches": []}
    # Match ALL name tokens (AND) against the person's FULL name (first + last) — NOT any-token OR on
    # last_name, which made "Maria Marco" match every PI/submitter named Maria. A submission belongs to
    # this lab if every token appears in the PI's full name OR in the submitter's full name.
    # WORD-BOUNDARY match on the PI's full name ONLY. Two prior bugs this fixes: (a) substring
    # matching made "Chen" hit "Muchena"/"Feschenko"; (b) OR-ing the submitter's full name pulled a
    # lab's SUBMITTER (who submits for several PIs) onto the wrong PI's page — rendering one PI's
    # CONFIDENTIAL data under another's name. \m..\M are Postgres regex word boundaries; toks are
    # alpha-only (>=4 chars) so they need no regex escaping.
    _pi_full = "lower(concat_ws(' ', pi_first_name, pi_last_name))"
    sub_or = " AND ".join(f"{_pi_full} ~ %(t{i})s" for i in range(len(toks)))
    params = {f"t{i}": r"\m" + t + r"\M" for i, t in enumerate(toks)}
    submissions = query(
        f"""
        SELECT submission_id, pi_first_name, pi_last_name, submitter_first_name, submitter_last_name,
               institute, submitted_at::date AS submitted_at, num_samples, proteomics_type, organism,
               description, sample_prep, prot_or_pep, mass_spec_wanted, dia, tmt
        FROM coreomics_submissions_cache
        WHERE {sub_or}
        ORDER BY submitted_at DESC NULLS LAST
        """,
        params, tables=["coreomics_submissions_cache"],
    )
    _hint = "lower(concat_ws(' ', p.customer_contact, p.pi))"
    s_or = "(" + " AND ".join(f"{_hint} ~ %(t{i})s" for i in range(len(toks))) + ")"
    searches = query(
        f"""
        SELECT p.search_id, p.real_search_name, p.client, p.pi AS path_hint,
               p.coreomics_submission_id, p.customer_contact, p.linkage_status,
               s.search_engine, s.n_precursors_total, s.n_proteins_total,
               (SELECT m.organism_name FROM search_raw_files srf
                  JOIN delimp_sample_metadata m ON m.raw_path = srf.raw_path
                 WHERE srf.search_id = p.search_id AND m.organism_name IS NOT NULL LIMIT 1) AS organism
        FROM delimp_search_provenance p
        LEFT JOIN delimp_searches s ON s.id = p.search_id
        WHERE p.coreomics_submission_id IN (
                SELECT submission_id FROM coreomics_submissions_cache WHERE {sub_or})
           OR {s_or}
        ORDER BY p.recorded_at DESC NULLS LAST, p.real_search_name
        """,
        params,
        tables=["delimp_search_provenance", "delimp_searches", "coreomics_submissions_cache",
                "search_raw_files", "delimp_sample_metadata"],
    )
    # Attach per-submission DATA-LOCATION status (the AI disk-match): is the data analyzed in FRAN,
    # sitting un-ingested on the share, or not located? Makes this a complete customer page.
    sub_ids = [s["submission_id"] for s in submissions]
    if sub_ids:
        sd = {r["submission_id"]: r for r in query(
            """SELECT submission_id, service_folder, service_folder_win, in_fran, run_count, match_confidence
               FROM delimp_submission_service_dir WHERE submission_id = ANY(%(ids)s)""",
            {"ids": sub_ids}, tables=["delimp_submission_service_dir"])}
        # SINGLE definition of "analyzed": the data is in FRAN. A linked provenance search PROVES that
        # regardless of the disk-match's in_fran flag (which can lag — it's folder-matched, not
        # search-derived). This matches the institution page (n_searches>0 OR in_fran) so the same
        # submission can't read "analyzed" there and "on_share" here. (bug-logic #6)
        linked = {str(x.get("coreomics_submission_id")) for x in searches if x.get("coreomics_submission_id")}
        for s in submissions:
            d = sd.get(s["submission_id"])
            if d:
                s["service_folder"] = d["service_folder"]
                s["service_folder_win"] = d["service_folder_win"]
                s["run_count"] = d["run_count"]
                s["match_confidence"] = d["match_confidence"]
            if str(s["submission_id"]) in linked or (d and d["in_fran"]):
                s["data_status"] = "analyzed"
            elif d:
                s["data_status"] = "on_share"
            else:
                s["data_status"] = "not_located"
    n_analyzed = sum(1 for s in submissions if s.get("data_status") == "analyzed")
    n_on_share = sum(1 for s in submissions if s.get("data_status") == "on_share")
    institutes = sorted({s["institute"] for s in submissions if s.get("institute")})
    pi_full = name
    for s in submissions:
        if s.get("pi_last_name"):
            pi_full = " ".join(x for x in (s.get("pi_first_name"), s.get("pi_last_name")) if x)
            break

    # ── Lab SEARCH STATS (for core-facility staff): most-identified proteins across this lab's
    # searches, organisms studied, engines used, totals, date span. ───────────────────────────────
    search_ids = [s["search_id"] for s in searches]
    top_proteins = []
    if search_ids:
        try:
            top_proteins = query(
                # cast the PARAM array to uuid[] (NOT the column to text) so the search_id index is used
                # — the ::text form forced a full scan of delimp_proteins (~33s vs ~0.2s).
                """SELECT COALESCE(NULLIF(gene,''), protein_group) AS protein,
                          COUNT(DISTINCT search_id) AS n_searches
                   FROM delimp_proteins
                   WHERE search_id = ANY(%(sids)s::uuid[]) AND is_contaminant IS NOT TRUE
                     AND COALESCE(NULLIF(gene,''), protein_group) IS NOT NULL
                   GROUP BY 1 ORDER BY n_searches DESC, protein LIMIT 15""",
                {"sids": search_ids}, tables=["delimp_proteins"])
        except Exception:  # noqa: BLE001 — stats are best-effort; never break the page
            top_proteins = []
    dates = [s["submitted_at"] for s in submissions if s.get("submitted_at")]
    stats = {
        "top_proteins": top_proteins,
        "organisms": sorted({s["organism"] for s in searches if s.get("organism")}),
        "engines": sorted({s["search_engine"] for s in searches if s.get("search_engine")}),
        "total_precursors": sum(s.get("n_precursors_total") or 0 for s in searches),
        "total_proteins": sum(s.get("n_proteins_total") or 0 for s in searches),
        "first_submission": str(min(dates)) if dates else None,
        "last_submission": str(max(dates)) if dates else None,
    }
    # ── External PI PROFILE (grants / lab website / photo), pre-fetched into delimp_pi_profile by the
    # enrichment pipeline. Shown when present; absent until enrichment runs (page never blocks on it).
    profile = None
    try:
        prof = query(
            """SELECT pi_name, institute, lab_url, photo_url, research_blurb, grants_json, updated_at
               FROM delimp_pi_profile WHERE lower(pi_name) = lower(%(pi)s) LIMIT 1""",
            {"pi": pi_full}, tables=["delimp_pi_profile"])
        if prof:
            profile = prof[0]
    except Exception:  # noqa: BLE001
        profile = None

    return {"pi": pi_full, "surname": toks[-1], "institutes": institutes,
            "n_submissions": len(submissions), "submissions": submissions,
            "n_searches": len(searches), "searches": searches,
            "n_analyzed": n_analyzed, "n_on_share": n_on_share,
            "stats": stats, "profile": profile}


# ── Tiered portal: lab-user (PI / submitter) scoping ──────────────────────────────────────────────
def submissions_for_email(email: str) -> list[str]:
    """The CoreOmics submission_ids a logged-in user is entitled to as a LAB user — where their login
    email is the submission's pi_email OR submitter_email (case-insensitive). [] = not a lab user.
    Runs `elevated` (a FIXED, parameterized query) because it's called at AUTH time, before the
    request's own scope is set. Briefly cached; only matches (truthy) cache, so it stays fresh as new
    submissions arrive."""
    em = (email or "").strip().lower()
    if "@" not in em:
        return []
    key = f"subs_for_email::{em}"
    hit = CACHE.cached(key)
    if hit is not None:
        return hit
    with elevated():
        rows = query(
            """SELECT submission_id FROM coreomics_submissions_cache
               WHERE lower(pi_email) = %(e)s OR lower(submitter_email) = %(e)s""",
            {"e": em}, tables=["coreomics_submissions_cache"],
        )
    subs = sorted({r["submission_id"] for r in rows if r.get("submission_id")})
    CACHE.put(key, subs)
    return subs


def search_submission_id(search_id):
    """The CoreOmics submission_id a search belongs to (for the lab-user export ownership check), or
    None. Reads delimp_search_provenance (internal table — only reachable on an internal request)."""
    rows = query("SELECT coreomics_submission_id FROM delimp_search_provenance WHERE search_id = %s LIMIT 1",
                 (search_id,), tables=["delimp_search_provenance"])
    return rows[0]["coreomics_submission_id"] if rows else None


def my_data(submission_ids) -> dict[str, Any]:
    """A LAB user's OWN data: their CoreOmics submissions + every FRAN search linked to them, with REAL
    names/paths (their own data — authorized). STRICTLY scoped to submission_ids; reads nothing else.
    The caller (main) must only pass the scope's own submission_ids."""
    ids = sorted({s for s in (submission_ids or []) if s})
    if not ids:
        return {"n_submissions": 0, "submissions": [], "n_searches": 0, "searches": []}
    submissions = query(
        """SELECT submission_id, pi_first_name, pi_last_name, submitter_first_name, submitter_last_name,
                  pi_email, submitter_email, institute, submitted_at::date AS submitted_at,
                  num_samples, proteomics_type, organism, status, description
           FROM coreomics_submissions_cache WHERE submission_id = ANY(%(ids)s)
           ORDER BY submitted_at DESC NULLS LAST""",
        {"ids": ids}, tables=["coreomics_submissions_cache"],
    )
    searches = query(
        """SELECT p.search_id, p.real_search_name, p.project, p.coreomics_submission_id,
                  p.report_path, p.n_raw_files, p.recorded_at,
                  s.search_engine, s.n_precursors_total, s.n_proteins_total
           FROM delimp_search_provenance p
           LEFT JOIN delimp_searches s ON s.id = p.search_id
           WHERE p.coreomics_submission_id = ANY(%(ids)s)
           ORDER BY p.recorded_at DESC NULLS LAST, p.real_search_name""",
        {"ids": ids},
        tables=["delimp_search_provenance", "delimp_searches"],
    )
    return {"n_submissions": len(submissions), "submissions": submissions,
            "n_searches": len(searches), "searches": searches}


def _peptides_showcase_distributions() -> dict[str, Any]:
    """Precursor m/z + ion-mobility (1/K0) distributions for the peptides page, binned server-side
    over the precomputed 20k-row delimp_mv_im_scatter sample (rt/irt/im/charge/precursor_mz) — cheap,
    never a live scan of delimp_precursors. Returns histograms [{x: bin_center, n}]. IM is only
    populated for timsTOF runs, so the IM chart reflects the ion-mobility subset (labeled as such)."""
    def _hist(col, lo, hi, nbins, where=""):
        try:
            rows = query(
                f"""SELECT width_bucket({col}, %s, %s, %s) AS b, COUNT(*) AS n
                    FROM delimp_mv_im_scatter
                    WHERE {col} IS NOT NULL {where}
                    GROUP BY b ORDER BY b""",
                (lo, hi, nbins), tables=["delimp_mv_im_scatter"])
        except Exception:  # noqa: BLE001 - mv not built
            return []
        w = (hi - lo) / nbins
        # bucket 0 = below lo, nbins+1 = above hi; clamp into the visible range
        out = []
        for r in rows:
            b = int(r["b"])
            if b < 1 or b > nbins:
                continue
            out.append({"x": round(lo + (b - 0.5) * w, 3), "n": int(r["n"])})
        return out
    return {
        "mz": _hist("precursor_mz", 300.0, 1300.0, 50),
        "im": _hist("im", 0.6, 1.7, 50, where="AND im > 0.3"),
    }


def _is_palindrome(seq: str) -> bool:
    return len(seq) >= 4 and seq == seq[::-1]


def _superlatives_from_pool(pool: list[dict[str, Any]]) -> dict[str, Any]:
    """The POOL-dependent half of the peptides showcase: physico-chemical / composition / fun
    superlatives + hero aggregates over whatever peptide pool is passed. Pure compute (the only DB
    call is via the pool the caller supplies). The app runs this over the top-100k matview live;
    scripts/peptides_survey_all.py runs it over EVERY peptide seen >=2x and stores the result as a
    snapshot (delimp_peptide_superlatives_snapshot) so the page can survey the whole corpus cheaply."""
    if True:
        # enrich each pooled peptide with computed physico-chem (no DB, pure fn)
        enriched: list[dict[str, Any]] = []
        for r in pool:
            seq = (r.get("stripped_seq") or "").strip().upper()
            if not seq or not seq.isalpha():
                continue
            pc = peptide_physchem(seq)
            if not pc.get("valid"):
                continue
            enriched.append({
                "stripped_seq": seq,
                "n_obs": r.get("n_obs"), "n_runs": r.get("n_runs"),
                "n_charges": r.get("n_charges"), "has_im": r.get("has_im"),
                "length": pc["length"], "mass": pc["monoisotopic_mass"],
                "gravy": pc["gravy"], "counts": pc["counts"],
                "c_terminus": pc["c_terminus"], "tryptic": pc["tryptic"],
            })

        if not enriched:
            return {"available": False, "pool_size": 0}

        n = len(enriched)
        # --- hero aggregates over the pool ---
        lengths = [p["length"] for p in enriched]
        masses = [p["mass"] for p in enriched]
        gravies = [p["gravy"] for p in enriched]
        n_tryptic = sum(1 for p in enriched if p["tryptic"])
        n_palindrome = sum(1 for p in enriched if _is_palindrome(p["stripped_seq"]))

        def _slim(p: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
            base = {"stripped_seq": p["stripped_seq"], "length": p["length"],
                    "mass": p["mass"], "n_obs": p["n_obs"], "n_runs": p["n_runs"]}
            return {**base, **(extra or {})}

        def _top(key, n_top=8, reverse=True):
            return [_slim(p, {"value": key(p)}) for p in
                    sorted(enriched, key=key, reverse=reverse)[:n_top]]

        # composition fraction helper: count of one residue / length
        def _frac(res):
            return lambda p: (p["counts"].get(res, 0) / p["length"]) if p["length"] else 0.0

        # superlatives (each entry: list of peptides + the metric "value")
        superlatives = {
            "longest": _top(lambda p: p["length"]),
            "heaviest": _top(lambda p: p["mass"]),
            "most_observed": _top(lambda p: (p["n_obs"] or 0)),
            "most_runs": _top(lambda p: (p["n_runs"] or 0)),
            "most_charges": _top(lambda p: (p["n_charges"] or 0)),
            "most_hydrophobic": _top(lambda p: p["gravy"]),
            "most_hydrophilic": _top(lambda p: p["gravy"], reverse=False),
            # composition oddities — fraction of the sequence that is one residue
            "most_cys": _top(_frac("C")),
            "most_trp": _top(_frac("W")),
            "most_pro": _top(_frac("P")),
            "most_his": _top(_frac("H")),
            "most_met": _top(_frac("M")),
        }
        # composition cards only make sense for peptides that contain the residue
        for key in ("most_cys", "most_trp", "most_pro", "most_his", "most_met"):
            superlatives[key] = [s for s in superlatives[key] if s["value"] > 0]

        # palindromes (rare + delightful) — shortest first so they're readable
        palindromes = sorted(
            [_slim(p) for p in enriched if _is_palindrome(p["stripped_seq"])],
            key=lambda p: (p["length"], -(p["n_obs"] or 0)))[:16]

        # "low-alphabet" peptides — built from very few distinct amino acids
        low_alpha = sorted(
            [_slim(p, {"value": len(set(p["stripped_seq"]))})
             for p in enriched if p["length"] >= 6 and len(set(p["stripped_seq"])) <= 4],
            key=lambda p: (p["value"], -p["length"]))[:12]

        # length histogram (over the pool, for a Chart.js bar)
        from collections import Counter
        lc = Counter(lengths)
        length_hist = [{"length": L, "n": lc[L]} for L in sorted(lc)]

        return {
            "available": True,
            "pool_size": n,
            "hero": {
                "n_peptides": n,
                "mean_length": round(sum(lengths) / n, 1),
                "min_length": min(lengths), "max_length": max(lengths),
                "mean_mass": round(sum(masses) / n, 1),
                "mean_gravy": round(sum(gravies) / n, 3),
                "pct_tryptic": round(100 * n_tryptic / n, 1),
                "n_palindrome": n_palindrome,
            },
            "superlatives": superlatives,
            "palindromes": palindromes,
            "low_alphabet": low_alpha,
            "length_hist": length_hist,
            # detection-frequency (long-tail) histogram over THIS pool's n_obs (the batch overrides
            # this with the all-peptides version so singletons show). log2 buckets: 1, 2-3, 4-7, ...
            "obs_histogram": obs_histogram([p["n_obs"] for p in enriched]),
        }


def obs_histogram(n_obs_values: list) -> list[dict[str, Any]]:
    """Long-tail histogram of 'how many times a peptide is seen': log2 buckets [1],[2-3],[4-7],...
    Each entry {label, lo, hi, n_peptides}. Showing the steep drop-off from 1-hit-wonders is the point."""
    from collections import Counter
    buckets: Counter = Counter()
    for v in n_obs_values:
        v = int(v or 0)
        if v < 1:
            continue
        b = v.bit_length() - 1            # floor(log2(v)): 1->0, 2..3->1, 4..7->2, ...
        buckets[b] += 1
    out = []
    for b in sorted(buckets):
        lo, hi = 2 ** b, 2 ** (b + 1) - 1
        out.append({"label": (str(lo) if lo == hi else f"{lo}–{hi}"),
                    "lo": lo, "hi": hi, "n_peptides": buckets[b]})
    return out


def _peptide_superlatives_snapshot() -> dict[str, Any] | None:
    """Read the precomputed all-peptides (>=2x) superlatives snapshot, if scripts/peptides_survey_all.py
    has built it. Returns the stored payload dict (jsonb) or None."""
    try:
        row = query("SELECT payload FROM delimp_peptide_superlatives_snapshot ORDER BY computed_at DESC "
                    "LIMIT 1", tables=["delimp_peptide_superlatives_snapshot"], fetch="one")
        return row["payload"] if row and row.get("payload") else None
    except Exception:  # noqa: BLE001 - table not built yet
        return None


def peptides_showcase() -> dict[str, Any]:
    """Everything the Peptides showcase page renders, in one cached payload. The pool-dependent half
    (hero + superlatives + histograms) comes from the all-peptides snapshot when available (surveys
    the WHOLE corpus), else a live scan of the top-100k matview. The corpus-wide parts (m/z & IM
    distributions, flyability extremes, hidden-words hunt) are added live (cheap, their own sources)."""
    def _p() -> dict[str, Any]:
        snap = _peptide_superlatives_snapshot()
        core = snap or _superlatives_from_pool(_peptides_showcase_pool())
        if not core.get("available"):
            return {"available": False, "pool_size": 0}
        # describe what was surveyed: the snapshot = every peptide seen >=2x; the live fallback = top-100k
        if not core.get("survey_scope"):
            core["survey_scope"] = ("every peptide seen ≥2× across the corpus" if snap
                                    else "the 100,000 most-observed peptides (the full ≥2× survey is still building)")
        return {
            **core,
            "distributions": _peptides_showcase_distributions(),
            "flyers": _peptides_showcase_flyers(),
            "flyability_summary": flyability_summary(),
            "words": (word_leaderboard() or [])[:1000],  # full dictionary hunt — buttons page through these
        }

    return SLOW_CACHE.get_or_set("peptides_showcase", _p)


def protein_coverage_peptides(protein_group: str, limit: int = 4000) -> dict[str, Any]:
    """Candidate observed peptides for a protein group (coverage map). CACHED: api_protein and
    api_protein_coverage BOTH call this for the same page, and a re-load re-calls it — caching means
    one successful scan serves all of them (consistent: no more 'map shows 83 but table shows 0' when
    one of the parallel calls times out). A failed/empty scan returns falsy -> NOT cached (get_or_set
    skips falsy) -> retried next time, so a transient timeout doesn't stick."""
    pg = (protein_group or "").strip()
    cached = CACHE.get_or_set(f"covpep_{pg}", lambda: _protein_coverage_peptides(pg, limit))
    return cached or {"gene": None, "peptides": []}


def _protein_coverage_peptides(pg: str, limit: int) -> dict[str, Any] | None:
    try:  # live table under ingestion load -> tight timeout, degrade to no gene rather than 503
        gene = query(
            "SELECT MAX(gene) AS gene FROM delimp_proteins WHERE protein_group = %s",
            (pg,), tables=["delimp_proteins"], fetch="val", timeout_ms=6000,
        )
    except Exception:  # noqa: BLE001
        gene = None
    # There is NO precursor->protein link in the schema (delimp_precursors has no protein_group),
    # so we approximate the protein's peptides by ALL stripped sequences co-observed in the runs
    # where this PG was reported, then api_protein keeps only those that map onto the canonical
    # sequence (the real coverage set). IMPORTANT: do NOT sample precursors per-run (a per-run
    # LIMIT grabs arbitrary co-eluting peptides and almost none belong to THIS protein -> 0 map ->
    # "observed peptides = 0"). We must scan the full run-set so the protein's own peptides are
    # included. CAP the number of runs + tight timeout + retry (db.query) + degrade to [] so a
    # huge protein degrades to "coverage unavailable" (honest) instead of 503 or wrong data.
    # THE REAL FIX is to store protein_group on delimp_precursors and filter WHERE protein_group=%s
    # (exact + fast) — see FRAN_REINGEST_AUDIT.md; that lands with the re-ingest.
    try:
        peps = query(
            """
            WITH pg_runs AS (
                SELECT DISTINCT search_id, raw_path
                FROM delimp_proteins WHERE protein_group = %s
                LIMIT 8
            )
            SELECT pr.stripped_seq,
                   COUNT(*)                    AS n_precursors,
                   COUNT(DISTINCT pr.charge)   AS n_charges,
                   COUNT(DISTINCT pr.raw_path) AS n_runs,
                   MIN(pr.q_value)             AS best_q_value,
                   bool_or(pr.im IS NOT NULL)  AS has_im
            FROM delimp_precursors pr
            JOIN pg_runs r ON r.search_id = pr.search_id AND r.raw_path = pr.raw_path
            GROUP BY pr.stripped_seq
            ORDER BY n_precursors DESC
            LIMIT %s
            """,
            (pg, min(int(limit), 8000)),
            tables=["delimp_proteins", "delimp_precursors"],
            timeout_ms=10000,
        )
    except Exception:  # noqa: BLE001 - high-run-count protein under load -> coverage unavailable, page still loads
        peps = []
    # empty peps == the scan timed out (a corpus protein always has co-observed precursors) ->
    # return None so it is NOT cached and gets retried, rather than sticking a 0 for the cache TTL.
    return {"gene": gene, "peptides": peps} if peps else None


def peptide_detail(stripped_seq: str) -> dict[str, Any]:
    seq = (stripped_seq or "").strip().upper()
    summary = query(
        """
        SELECT stripped_seq,
               COUNT(*) AS n_precursors,
               COUNT(DISTINCT modified_seq_proforma) AS n_modforms,
               COUNT(DISTINCT charge) AS n_charges,
               COUNT(DISTINCT raw_path) AS n_runs,
               COUNT(DISTINCT search_id) AS n_searches,
               MIN(q_value) AS best_q_value,
               AVG(rt) AS avg_rt,
               AVG(im) AS avg_im,
               bool_or(im IS NOT NULL) AS has_im,
               MAX(n_engines_confirming) AS max_engines
        FROM delimp_precursors
        WHERE stripped_seq = %s
        GROUP BY stripped_seq
        """,
        (seq,),
        tables=["delimp_precursors"],
        fetch="one",
    )
    # One row per (modified form, charge) with aggregate coordinates.
    forms = query(
        """
        SELECT modified_seq_proforma, charge,
               COUNT(*) AS n_obs,
               AVG(precursor_mz) AS avg_mz,
               AVG(rt) AS avg_rt,
               AVG(im) AS avg_im,
               MIN(q_value) AS best_q_value,
               AVG(intensity_log2) AS avg_log2_int,
               MAX(n_engines_confirming) AS max_engines
        FROM delimp_precursors
        WHERE stripped_seq = %s
        GROUP BY modified_seq_proforma, charge
        ORDER BY n_obs DESC
        LIMIT %s
        """,
        (seq, MAX_PAGE),
        tables=["delimp_precursors"],
    )
    # Individual observations across runs (bounded).
    observations = query(
        """
        SELECT pr.search_id, s.search_name, s.search_engine,
               pr.raw_path, pr.modified_seq_proforma, pr.charge,
               pr.precursor_mz, pr.rt, pr.im, pr.q_value,
               pr.intensity, pr.n_engines_confirming
        FROM delimp_precursors pr
        JOIN delimp_searches s ON s.id = pr.search_id
        WHERE pr.stripped_seq = %s
        ORDER BY pr.intensity DESC NULLS LAST
        LIMIT %s
        """,
        (seq, MAX_PAGE),
        tables=["delimp_precursors", "delimp_searches"],
    )
    # Cross-engine consensus, if recorded.
    consensus = query(
        """
        SELECT modified_seq_proforma, charge, engines, n_engines, best_q_value, raw_path
        FROM delimp_consensus_ids
        WHERE stripped_seq = %s
        ORDER BY n_engines DESC
        LIMIT %s
        """,
        (seq, MAX_PAGE),
        tables=["delimp_consensus_ids"],
    )
    return {
        "summary": summary,
        "forms": forms,
        "observations": observations,
        "consensus": consensus,
    }


def peptide_search_library(stripped_seq: str, charge: int | None = None) -> dict[str, Any] | None:
    """The search's OWN predicted/library fragment intensities (from the ingested DIA-NN
    report-lib) for the predicted-spectrum panel — labeled with the engine + version that
    produced them. Returns None until XIC/library is ingested."""
    import json as _json
    seq = (stripped_seq or "").strip().upper()
    try:
        rows = query(
            "SELECT charge, engine, engine_version, ms1_apex, fragments FROM delimp_precursor_xic WHERE stripped_seq = %s",
            (seq,), tables=["delimp_precursor_xic"], timeout_ms=6000,
        )
    except Exception:  # noqa: BLE001 - not ingested yet / unindexed scan timed out
        return None
    if not rows:
        return None

    def _j(v):
        return _json.loads(v) if isinstance(v, str) else v

    cand = [r for r in rows if charge and r["charge"] == charge] or rows
    row = max(cand, key=lambda r: r.get("ms1_apex") or 0)
    peaks = [{"ion": (f.get("type", "") + str(f.get("series", ""))), "label": f.get("label"),
              "mz": f.get("mz"), "rel_intensity": f.get("rel_intensity")}
             for f in (_j(row["fragments"]) or []) if f.get("mz") and f.get("rel_intensity")]
    peaks.sort(key=lambda p: -(p["rel_intensity"] or 0))
    return {"engine": row.get("engine"), "version": row.get("engine_version"),
            "charge": row["charge"], "peaks": peaks}


def peptide_interference(stripped_seq: str, mz_tol: float = 0.01,
                         rt_window: float = 0.5, max_partners: int = 40) -> dict[str, Any]:
    """Shared-transition / interference signal: for this peptide's quant fragments, find
    OTHER peptides in the corpus whose fragments share the same m/z (±tol). Ones that also
    co-elute (|ΔRT(apex)| ≤ window) are potential interference; RT-resolved ones share the
    transition but are separated in time. A novel DIA specificity/QC datapoint."""
    import json as _json

    seq = (stripped_seq or "").strip().upper()
    try:
        meas = query(
            "SELECT precursor_id, charge, rt_apex, fragments FROM delimp_precursor_xic WHERE stripped_seq = %s",
            (seq,), tables=["delimp_precursor_xic"], timeout_ms=6000,
        )
    except Exception:  # noqa: BLE001 - no XIC ingested yet / unindexed scan timed out
        return {"available": False}
    if not meas:
        return {"available": False}

    def _j(v):
        return _json.loads(v) if isinstance(v, str) else v

    # this peptide's quant transitions (label, mz, apex RT, charge)
    targets = []
    for m in meas:
        rt0 = m["rt_apex"]
        for f in _j(m["fragments"]) or []:
            if (f.get("exclude_from_quant", 0) == 0) and f.get("mz"):
                targets.append((f["label"], float(f["mz"]), rt0, m["charge"]))
    if not targets:
        return {"available": True, "stripped_seq": seq, "transitions": [], "partners": []}

    def _producer():
        transitions, partners = [], {}
        for label, mz, rt0, ch in targets[:12]:
            try:  # each transition is an unindexed jsonb-exploding scan — bound it; skip on timeout
                rows = query(
                    """SELECT x.stripped_seq, x.charge, x.rt_apex
                       FROM delimp_precursor_xic x, jsonb_array_elements(x.fragments) f
                       WHERE x.stripped_seq <> %s AND (f->>'mz')::float BETWEEN %s AND %s
                       LIMIT 3000""",
                    (seq, mz - mz_tol, mz + mz_tol), tables=["delimp_precursor_xic"], timeout_ms=5000,
                )
            except Exception:  # noqa: BLE001 — one transition's scan exceeded the bound; keep the rest
                continue
            seen, co = set(), 0
            for r in rows:
                p = r["stripped_seq"]
                if p in seen:
                    continue
                seen.add(p)
                dRT = (abs((r["rt_apex"] or 0) - (rt0 or 0))
                       if (rt0 is not None and r["rt_apex"] is not None) else None)
                coel = dRT is not None and dRT <= rt_window
                if coel:
                    co += 1
                pe = partners.setdefault(p, {"peptide": p, "shared": 0, "co_eluting": 0, "min_dRT": None})
                pe["shared"] += 1
                if coel:
                    pe["co_eluting"] += 1
                if dRT is not None:
                    pe["min_dRT"] = dRT if pe["min_dRT"] is None else min(pe["min_dRT"], dRT)
            transitions.append({"label": label, "mz": round(mz, 4), "charge": ch,
                                "n_sharing": len(seen), "n_co_eluting": co})
        for pe in partners.values():
            if pe["min_dRT"] is not None:
                pe["min_dRT"] = round(pe["min_dRT"], 3)
        plist = sorted(partners.values(), key=lambda x: (-x["co_eluting"], -x["shared"]))[:max_partners]
        return {"available": True, "stripped_seq": seq, "rt_window": rt_window, "mz_tol": mz_tol,
                "transitions": transitions, "partners": plist}

    return SLOW_CACHE.get_or_set(f"interf_{seq}", _producer)


def gene_detail(gene: str) -> dict[str, Any]:
    """Everything the corpus knows about a GENE: all its protein groups (each links to
    the protein page), how widely it's seen, which organisms/sample types, and which
    search pipelines found it (flagging proteogenomics / custom-FASTA searches)."""
    g = (gene or "").strip()
    proteins = query(
        """
        SELECT protein_group,
               COUNT(DISTINCT search_id) AS n_searches,
               COUNT(DISTINCT raw_path)  AS n_runs,
               SUM(n_precursors)         AS sum_precursors,
               SUM(n_unique_peptides)    AS sum_unique_peptides,
               bool_or(is_contaminant)   AS any_contaminant
        FROM delimp_proteins
        WHERE gene = %s
        GROUP BY protein_group
        ORDER BY sum_precursors DESC NULLS LAST
        LIMIT %s
        """,
        (g, MAX_PAGE), tables=["delimp_proteins"],
    )
    totals = query(
        """SELECT COUNT(DISTINCT protein_group) AS n_groups,
                  COUNT(DISTINCT search_id) AS n_searches,
                  COUNT(DISTINCT raw_path)  AS n_runs
           FROM delimp_proteins WHERE gene = %s""",
        (g,), tables=["delimp_proteins"], fetch="one",
    )
    organisms = query(
        """
        SELECT COALESCE(sm.organism_name, 'Unknown') AS organism, COUNT(DISTINCT p.raw_path) AS n_runs
        FROM delimp_proteins p
        JOIN delimp_sample_metadata sm ON sm.raw_path = p.raw_path
        WHERE p.gene = %s
        GROUP BY sm.organism_name ORDER BY n_runs DESC LIMIT 12
        """,
        (g,), tables=["delimp_proteins", "delimp_sample_metadata"],
    )
    # which search pipelines detected this gene — proteogenomics/custom-FASTA flagged
    pipelines = query(
        """
        SELECT s.pipeline_id, s.search_engine, s.fasta_path,
               COUNT(DISTINCT s.id) AS n_searches
        FROM delimp_proteins p
        JOIN delimp_searches s ON s.id = p.search_id
        WHERE p.gene = %s
        GROUP BY s.pipeline_id, s.search_engine, s.fasta_path
        ORDER BY n_searches DESC LIMIT 20
        """,
        (g,), tables=["delimp_proteins", "delimp_searches"],
    )
    for p in pipelines:
        fp = (p.get("fasta_path") or "").lower()
        pid = (p.get("pipeline_id") or "").lower()
        p["proteogenomics"] = any(k in fp or k in pid for k in
                                  ("proteogenom", "rnaseq", "rna-seq", "transcript", "custom", "novel", "denovo", "de-novo"))
    return {"gene": g, "proteins": proteins, "totals": totals,
            "organisms": organisms, "pipelines": pipelines}


def peptide_xic(stripped_seq: str, top_n: int = 6) -> dict[str, Any]:
    """Dual-pane XIC for a peptide — PER PRECURSOR (each charge state is its own
    precursor with its own chromatogram). Returns one entry per precursor (MS1 +
    top-N quant fragments with usage %), plus the corpus charge-state distribution
    ('do we see the +2 or +3 more?'). Traces are the apex-aligned average of real
    DIA-NN --xic runs. {available: False} until XIC is ingested."""
    import json as _json

    seq = (stripped_seq or "").strip().upper()
    try:
        meas = query(
            """SELECT precursor_id, charge, raw_path, rt_apex, ms1_apex, ms1, fragments
               FROM delimp_precursor_xic WHERE stripped_seq = %s ORDER BY charge""",
            (seq,), tables=["delimp_precursor_xic"], timeout_ms=6000,
        )
    except Exception:  # noqa: BLE001 - table not created until first XIC ingest / unindexed scan timed out
        return {"available": False}
    if not meas:
        return {"available": False}
    try:
        qrows = query(
            """SELECT search_id, precursor_id, quant_labels
               FROM delimp_xic_quant WHERE stripped_seq = %s""",
            (seq,), tables=["delimp_xic_quant"],
        )
    except Exception:  # noqa: BLE001
        qrows = []

    def _j(v):
        return _json.loads(v) if isinstance(v, str) else v

    # per-precursor quant usage: precursor_id -> {label: set(search_id)}
    usage_by_pid: dict[str, dict[str, set]] = {}
    searches_by_pid: dict[str, set] = {}
    for r in qrows:
        pid = r["precursor_id"]
        searches_by_pid.setdefault(pid, set()).add(r["search_id"])
        d = usage_by_pid.setdefault(pid, {})
        for lab in _j(r["quant_labels"]) or []:
            d.setdefault(lab, set()).add(r["search_id"])

    # corpus charge-state distribution (how often each charge is observed) — honest,
    # from all precursor observations, not just XIC-bearing ones.
    charge_dist = []
    try:
        cd = query(
            "SELECT charge, COUNT(*) AS n FROM delimp_precursors WHERE stripped_seq = %s GROUP BY charge",
            (seq,), tables=["delimp_precursors"],
        )
        tot = sum(c["n"] for c in cd) or 1
        charge_dist = sorted(({"charge": c["charge"], "n_obs": c["n"],
                               "pct": round(100 * c["n"] / tot)} for c in cd),
                             key=lambda x: -x["n_obs"])
    except Exception:  # noqa: BLE001
        pass

    precursors = []
    for m in meas:
        pid = m["precursor_id"]
        frags = _j(m["fragments"]) or []
        rep_frags = {f["label"]: f for f in frags}
        rel = {lab: (f.get("rel_intensity") or 0) for lab, f in rep_frags.items()}
        u = usage_by_pid.get(pid, {})
        ns = max(len(searches_by_pid.get(pid, set())), 1)
        usage_list = sorted(
            ({"label": lab, "n_searches": len(sids), "pct": round(100 * len(sids) / ns),
              "rel_intensity": round(rel.get(lab, 0), 4)} for lab, sids in u.items()),
            key=lambda x: (-x["n_searches"], -x["rel_intensity"], x["label"]),
        )
        if not usage_list:  # no quant overlay -> rank by library intensity
            usage_list = sorted(
                ({"label": lab, "n_searches": 0, "pct": 0, "rel_intensity": round(ri, 4)}
                 for lab, ri in rel.items()), key=lambda x: -x["rel_intensity"])
        top = [x["label"] for x in usage_list[:top_n]]
        bottom = [{"label": lab, "ion": rep_frags[lab].get("type", "") + str(rep_frags[lab].get("series", "")),
                   "mz": rep_frags[lab].get("mz"), "charge": rep_frags[lab].get("charge"),
                   "rel_intensity": rep_frags[lab].get("rel_intensity"),
                   "trace": rep_frags[lab].get("trace") or []}
                  for lab in top if lab in rep_frags]
        has_real = any(b["trace"] for b in bottom)
        precursors.append({
            "precursor_id": pid, "charge": m["charge"], "rt_apex": m["rt_apex"],
            "representative_run": m["raw_path"], "n_searches": len(searches_by_pid.get(pid, set())),
            "ms1": _j(m["ms1"]) or [], "fragments": bottom, "fragment_usage": usage_list,
            "has_real_trace": has_real})
    precursors.sort(key=lambda p: -(p.get("rt_apex") is not None), )  # stable; keep charge order from SQL
    return {"available": True, "stripped_seq": seq,
            "rt_axis": "RT − apex (min)",  # traces are apex-aligned averages
            "charge_distribution": charge_dist, "precursors": precursors}


def list_searches(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    lim, off = _page(limit, offset)
    rows = query(
        """
        SELECT id, search_name, search_engine, search_engine_version,
               pipeline_id, pipeline_version, status, sharing_status,
               n_raw_files, n_precursors_total, n_proteins_total,
               fasta_n_proteins, completed_at, submitted_at, ingested_at,
               delimp_version, doi, pride_accession
        FROM delimp_searches
        ORDER BY COALESCE(ingested_at, submitted_at) DESC
        LIMIT %s OFFSET %s
        """,
        (lim, off),
        tables=["delimp_searches"],
    )
    total = query(
        "SELECT COUNT(*) FROM delimp_searches",
        tables=["delimp_searches"],
        fetch="val",
    )
    return {"rows": rows, "total": total or 0, "limit": lim, "offset": off}


def search_detail(search_id: str) -> dict[str, Any]:
    sid = (search_id or "").strip()
    summary = query(
        """
        SELECT id, search_name, search_engine, search_engine_version,
               pipeline_id, pipeline_version, status, sharing_status,
               n_raw_files, n_precursors_total, n_proteins_total,
               fasta_path, fasta_n_proteins, contaminant_lib,
               completed_at, submitted_at, ingested_at, delimp_version,
               doi, pride_accession, citation
        FROM delimp_searches
        WHERE id = %s
        """,
        (sid,),
        tables=["delimp_searches"],
        fetch="one",
    )
    # Per-run protein count: stored srf.n_proteins, else computed live (distinct protein groups in the
    # run). That COUNT(DISTINCT) over delimp_precursors is unbounded — for a big DIA search it can blow
    # past the timeout and 503 the whole page. Bound it (short timeout); on timeout DROP the computed
    # column and fall back to srf.n_proteins alone, so the page always renders. (bug-sql #3)
    _COLS = ("srf.raw_path, srf.n_precursors, rf.raw_basename, rf.platform, rf.acquisition_method, "
             "rf.instrument_model, rf.gradient_minutes, "
             "COALESCE(sm.organism_name, sm.predicted_organism_name) AS organism_name, "
             "sm.sample_type, sm.organism_taxon_id")
    _BASE = ("FROM search_raw_files srf JOIN raw_files rf ON rf.raw_path = srf.raw_path "
             "LEFT JOIN delimp_sample_metadata sm ON sm.raw_path = srf.raw_path")
    try:
        runs = query(
            f"""SELECT {_COLS}, COALESCE(srf.n_proteins, pc.n_prot_computed) AS n_proteins
                {_BASE}
                LEFT JOIN (SELECT raw_path, COUNT(DISTINCT protein_group) AS n_prot_computed
                           FROM delimp_precursors WHERE search_id = %s GROUP BY raw_path) pc
                  ON pc.raw_path = srf.raw_path
                WHERE srf.search_id = %s ORDER BY srf.n_precursors DESC NULLS LAST LIMIT %s""",
            (sid, sid, MAX_PAGE),
            tables=["search_raw_files", "raw_files", "delimp_sample_metadata", "delimp_precursors"],
            timeout_ms=8000)
    except Exception:  # noqa: BLE001 — big search: skip the live per-run count, use the stored value
        runs = query(
            f"""SELECT {_COLS}, srf.n_proteins AS n_proteins
                {_BASE}
                WHERE srf.search_id = %s ORDER BY srf.n_precursors DESC NULLS LAST LIMIT %s""",
            (sid, MAX_PAGE),
            tables=["search_raw_files", "raw_files", "delimp_sample_metadata"])
    return {"summary": summary, "runs": runs}
