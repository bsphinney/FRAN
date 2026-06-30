"""collab.py — apply the curated collaborator map to the raw `service_customer` values stored in
delimp_search_provenance. The DB holds the DETERMINISTIC raw folder name; this layer applies the
EDITABLE curation (canonical display, spelling merges, internal/standard flags, advisory CoreOmics
enrichment) from service_customer_aliases.json. Editing that JSON re-curates with no DB re-backfill.

The collaborator page groups DB rows by raw service_customer, then merges them to canonical here.
"""
from __future__ import annotations
import json, os, re

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "service_customer_aliases.json")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _display(raw: str) -> str:
    return re.sub(r"\s+", " ", (raw or "").replace("_", " ").replace("-", " ").strip())


def _load():
    by_rawvar: dict[str, dict] = {}   # norm(raw variant) -> curated entry
    canon_raws: dict[str, set] = {}   # norm(canonical)   -> {raw variant strings}
    try:
        with open(_PATH) as f:
            data = json.load(f)
        for c in data.get("customers", []):
            entry = {"canonical": c.get("canonical") or "", "campus": c.get("campus"),
                     "flag": c.get("flag", "keep"), "coreomics": c.get("coreomics")}
            ck = _norm(entry["canonical"])
            for rv in (c.get("raw_variants") or [entry["canonical"]]):
                by_rawvar[_norm(rv)] = entry
                canon_raws.setdefault(ck, set()).add(rv)
            by_rawvar.setdefault(c.get("norm_key", ck), entry)
    except FileNotFoundError:
        pass
    return by_rawvar, canon_raws


_BY_RAWVAR, _CANON_RAWS = _load()


def resolve(raw_service_customer: str) -> dict:
    """Raw DB service_customer -> {canonical, flag, campus, coreomics}. Unknown raw values fall back
    to a cleaned display name with flag 'keep' (so a search ingested after the last map build still
    shows up sensibly)."""
    e = _BY_RAWVAR.get(_norm(raw_service_customer))
    if e:
        return e
    return {"canonical": _display(raw_service_customer), "campus": None, "flag": "keep", "coreomics": None}


def raws_for_canonical(canonical: str) -> list[str]:
    """All raw service_customer DB values that belong to a canonical collaborator (for drill-down).
    Falls back to the name itself if the map doesn't know it."""
    raws = _CANON_RAWS.get(_norm(canonical))
    return sorted(raws) if raws else [canonical]


# ── Institute canonicalization (the CoreOmics `institute` field is free-text and wildly inconsistent:
# "UC Davis" / "University of California, Davis" / "UCDavis" / "MCB UC Davis" / "UC Davis Health" all
# mean one place). Used to merge the "Labs by institution" cards. ──────────────────────────────────
_UC_ABBR = {"ucsf": "UCSF", "ucsc": "UC Santa Cruz", "ucsb": "UC Santa Barbara", "ucla": "UCLA",
            "ucsd": "UC San Diego", "ucb": "UC Berkeley", "ucr": "UC Riverside", "ucm": "UC Merced",
            "uci": "UC Irvine"}
_UC_CITIES = [("davis", "UC Davis"), ("berkeley", "UC Berkeley"), ("san francisco", "UCSF"),
              ("santa cruz", "UC Santa Cruz"), ("santa barbara", "UC Santa Barbara"),
              ("san diego", "UC San Diego"), ("los angeles", "UCLA"), ("irvine", "UC Irvine"),
              ("merced", "UC Merced"), ("riverside", "UC Riverside")]


def canonical_institute(name: str) -> str:
    """Collapse the many spellings of an institution to one display name. Conservative: only
    rewrites institutions we recognize (UC campuses, Gladstone, Stanford, Mayo, CSU Long Beach);
    otherwise returns the original string untouched so we never mis-merge distinct places."""
    s = (name or "").strip()
    if not s:
        return "(institute not given)"
    n = re.sub(r"\s+", " ", re.sub(r"[.,\-/]", " ", s.lower())).strip()
    for ab, disp in _UC_ABBR.items():
        if re.search(r"\b" + ab + r"\b", n):
            return disp
    if re.search(r"\bucdavis\b", n) or re.search(r"\bucd\b", n):
        return "UC Davis"
    has_uc = ("university of california" in n) or bool(re.search(r"\buc\b", n))
    if has_uc:
        for city, disp in _UC_CITIES:
            if city in n:
                return disp
    if "gladstone" in n:
        return "Gladstone Institutes"
    if "stanford" in n:
        return "Stanford University"
    if "mayo clinic" in n:
        return "Mayo Clinic"
    if "long beach" in n and ("state" in n or "csu" in n):
        return "CSU Long Beach"
    return s


def uc_davis_affiliation(raw_institutes) -> tuple:
    """For a UC Davis lab, best-effort (college, department) from its raw CoreOmics institute
    string(s). Department is parsed out of the string; college is mapped from department keywords
    (verifiable UC Davis org structure). Returns (None, None)/(college, None) when not derivable —
    never guesses."""
    cands = [r for r in (raw_institutes or []) if r and r.strip()]
    if not cands:
        return (None, None)
    def info(r):  # prefer strings that actually name a unit
        return (1 if re.search(r"(?i)\b(department|dept|school|college|center|division|program)\b", r) else 0, len(r))
    best = max(cands, key=info)
    n = best.lower()
    # department display: strip the UC-Davis identity tokens, keep the rest
    dept = best
    for pat in (r"university of california", r"\buc\b", r"\bucdavis\b", r"\bucd\b", r"davis",
                r"medical center", r"\bhealth\b", r"sch(ool)? of medicine", r"\bsom\b", r"\bsvm\b"):
        dept = re.sub(r"(?i)" + pat, " ", dept)
    dept = re.sub(r"[\s,.\-:]+", " ", dept).strip(" ,.-:")
    dept = dept if len(dept) >= 3 else None
    college = None
    if any(k in n for k in ("veterinar", "svm", "vm:", "aquatic animal", "apc")):
        college = "School of Veterinary Medicine"
    elif any(k in n for k in ("school of medicine", "som", "medical center", "neurolog", "surgery",
                              "dermatolog", "physiology", "membrane biology", "molecular medicine",
                              "pathology", "pharmacolog", "internal medicine", "psychiatr", "health")):
        college = "School of Medicine"
    elif any(k in n for k in ("biomedical engineering", "engineering")):
        college = "College of Engineering"
    elif any(k in n for k in ("food science", "plant sciences", "entomolog", "nematolog",
                              "animal science", "viticulture", "enology", "agricultural",
                              "environmental science", "land air water", "soil science")):
        college = "College of Agricultural & Environmental Sciences"
    elif any(k in n for k in ("molecular & cellular", "molecular and cellular", "mcb", "microbiolog",
                              "molecular genetics", "plant biology", "neurobiolog",
                              "biological sciences", "evolution and ecology")):
        college = "College of Biological Sciences"
    elif any(k in n for k in ("psycholog", "chemistry", "letters and science", "physics", "statistics")):
        college = "College of Letters & Science"
    return (college, dept)
