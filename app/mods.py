"""Canonicalisation of modification notation in FRAN's modified_seq_proforma.

WHY: delimp_precursors.modified_seq_proforma records the same chemistry several ways depending on
the engine / export (see the memory notes fran-mod-notation-trap / -variants):

  * UniMod accession          C[UNIMOD:4]            (DIA-NN; Spectronaut after _to_proforma)
  * Spectronaut name          M[Oxidation (M)]       (older ingests -- name left as literal text)
                              NM[Dioxidation (MW)]QD[Cation:Na (DE)]M
  * elemental composition     C[+C2+H3+N+O]          (= Carbamidomethyl)
  * delta mass                C[+57.0215]
  * custom / lab labels       R[SILAC][SILAC], K[DEL-gg_Cys137L]   (no UniMod entry)

Grouping on the raw string therefore splits one chemical form into several rows and shows opaque
tokens. This module maps each bracketed token to a UniMod accession + name where that is
UNAMBIGUOUS, and leaves everything else exactly as written, flagged. It never guesses: an
unknown token is passed through unchanged and reported in `unmapped`.

SOURCE of the table below: UniMod (https://www.unimod.org, unimod.xml fetched 2026-11-16 and
checked offline; record_id, title and mono_mass are the official values; composition is UniMod's).
Not fetched at runtime. Which entries are here: every modification token that occurs in the
corpus (token census of a 0.15% page-sample of delimp_precursors, 2026-11-16, ~354k bracket
tokens) that has a UniMod entry. Add an entry here (and in tests) when a new token shows up.

Pure functions, no DB access, safe to import anywhere.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

# accession -> (display name, monoisotopic delta mass, UniMod composition)
# Names are UniMod `title`s, except 121 where UniMod's title is "GG"; FRAN already calls it
# "GlyGly" everywhere (proforma.VARIABLE_MODS, the ingest), so that wins for display.
UNIMOD: dict[int, tuple[str, float, str]] = {
    1: ("Acetyl", 42.010565, "H(2) C(2) O"),
    3: ("Biotin", 226.077598, "H(14) C(10) N(2) O(2) S"),
    4: ("Carbamidomethyl", 57.021464, "H(3) C(2) N O"),
    5: ("Carbamyl", 43.005814, "H C N O"),
    6: ("Carboxymethyl", 58.005479, "H(2) C(2) O(2)"),
    7: ("Deamidated", 0.984016, "H(-1) N(-1) O"),
    21: ("Phospho", 79.966331, "H O(3) P"),
    23: ("Dehydrated", -18.010565, "H(-2) O(-1)"),
    24: ("Propionamide", 71.037114, "H(5) C(3) N O"),
    27: ("Glu->pyro-Glu", -18.010565, "H(-2) O(-1)"),
    28: ("Gln->pyro-Glu", -17.026549, "H(-3) N(-1)"),
    30: ("Cation:Na", 21.981943, "H(-1) Na"),
    34: ("Methyl", 14.01565, "H(2) C"),
    35: ("Oxidation", 15.994915, "O"),
    36: ("Dimethyl", 28.0313, "H(4) C(2)"),
    37: ("Trimethyl", 42.04695, "H(6) C(3)"),
    39: ("Methylthio", 45.987721, "H(2) C S"),
    40: ("Sulfo", 79.956815, "O(3) S"),
    41: ("Hex", 162.052824, "Hex"),
    43: ("HexNAc", 203.079373, "HexNAc"),
    45: ("Myristoyl", 210.198366, "H(26) C(14) O"),
    64: ("Succinyl", 100.016044, "H(4) C(4) O(3)"),
    121: ("GlyGly", 114.042927, "H(6) C(4) N(2) O(2)"),
    122: ("Formyl", 27.994915, "C O"),
    275: ("Nitrosyl", 28.990164, "H(-1) N O"),
    299: ("Carboxy", 43.989829, "C O(2)"),
    312: ("Cysteinyl", 119.004099, "H(5) C(3) N O(2) S"),
    345: ("Trioxidation", 47.984744, "O(3)"),
    352: ("Lys->Allysine", -1.031634, "H(-3) N(-1) O"),
    354: ("Nitro", 44.985078, "H(-1) N O(2)"),
    378: ("Carboxyethyl", 72.021129, "H(4) C(3) O(2)"),
    401: ("Didehydro", -2.01565, "H(-2)"),
    405: ("Phosphoadenosine", 329.05252, "H(12) C(10) N(5) O(6) P"),
    414: ("Hydroxymethyl", 30.010565, "H(2) C O"),
    425: ("Dioxidation", 31.989829, "O(2)"),
    739: ("TMT", 224.152478, "H(20) C(12) N(2) O(2)"),
    956: ("Cation:Mg[II]", 21.969392, "H(-2) Mg"),
    1363: ("Crotonyl", 68.026215, "H(4) C(4) O"),
}

# Name tokens (lower-cased, residue suffix and localisation probability already removed) -> accession.
# Built from the UniMod titles, plus the aliases Spectronaut / users actually write.
_NAME_ALIASES: dict[str, int] = {
    "deamidation": 7,        # UniMod full_name of Deamidated; Spectronaut's name
    "carbamylation": 5,      # full_name of Carbamyl ("Carbamylation (KR)")
    "mmts": 39,              # S-methylthio: Methylthio, composition H(2) C S
    "glygly": 121,           # ubiquitin remnant, FRAN's own name
    "gg": 121,
    "cam": 4,                # abbreviation seen on a handful of rows
    "oxi": 35,               # abbreviation seen on a handful of rows
}
_NAME_TO_ID: dict[str, int] = {v[0].lower(): k for k, v in UNIMOD.items()}
_NAME_TO_ID.update(_NAME_ALIASES)

# Monoisotopic element masses for the elemental ("+C2+H3+N+O") notation. An element outside this
# set makes the token unmappable rather than guessed.
_ELEMENT_MASS = {
    "H": 1.00782503207, "C": 12.0, "N": 14.0030740048, "O": 15.99491461956,
    "S": 31.97207100, "P": 30.97376163, "Na": 22.9897692809, "Mg": 23.9850417,
    "Se": 79.9165213, "Fe": 55.9349375, "Zn": 63.9291422, "K": 38.96370668,
    "Ca": 39.96259098, "Cu": 62.9295975,
}

_FORMULA = re.compile(r"^(?:[+-][A-Z][a-z]?\d*)+$")
_FORMULA_PART = re.compile(r"([+-])([A-Z][a-z]?)(\d*)")
_DELTA = re.compile(r"^([+-])(\d+)(?:\.(\d+))?$")
_UNIMOD_TOKEN = re.compile(r"^UNIMOD:(\d+)$", re.I)
_RESIDUE_SUFFIX = re.compile(r"\s*\([^()]*\)\s*$")          # "Oxidation (M)", "Methyl (KR)"
_LOCALISATION = re.compile(r"\s*:\s*[\d.]+\s*%\s*$")        # "GlyGly (K): 95.4%"

# A mass given to d decimals is matched within half a unit of the last digit (plus a hair).
_FORMULA_TOL = 0.0015


def _formula_mass(token: str) -> float | None:
    if not _FORMULA.match(token):
        return None
    total = 0.0
    for sign, el, n in _FORMULA_PART.findall(token):
        m = _ELEMENT_MASS.get(el)
        if m is None:
            return None
        total += (1 if sign == "+" else -1) * m * (int(n) if n else 1)
    return total


def _unique_by_mass(mass: float, tol: float) -> int | None:
    """The single UniMod table entry within `tol` of `mass`, else None (no match OR ambiguous).

    Ambiguity is real inside this table (Dehydrated 23 and Glu->pyro-Glu 27 share -18.0106), and
    a mass alone cannot choose between them, so that is reported as unmapped instead of guessed.
    """
    hits = [k for k, (_, m, _c) in UNIMOD.items() if abs(m - mass) <= tol]
    return hits[0] if len(hits) == 1 else None


def resolve_token(token: str) -> dict[str, Any]:
    """Map ONE bracket body (text between the outer [ ]) to a UniMod entry if unambiguous.

    Returns {"raw", "accession": int|None, "name": str|None, "mass": float|None, "how": str}.
    `how` is one of accession | name | formula | delta | unmapped.
    """
    raw = token
    t = token.strip()
    out: dict[str, Any] = {"raw": raw, "accession": None, "name": None, "mass": None, "how": "unmapped"}

    m = _UNIMOD_TOKEN.match(t)
    if m:
        acc = int(m.group(1))
        if acc in UNIMOD:
            return _hit(out, acc, "accession")
        return out  # a UNIMOD accession we have no name for: keep as written, flagged

    if t[:1] in "+-":
        mass = _formula_mass(t)
        if mass is not None:
            acc = _unique_by_mass(mass, _FORMULA_TOL)
            return _hit(out, acc, "formula") if acc else out
        d = _DELTA.match(t)
        if d:
            dec = len(d.group(3) or "")
            val = float(t)
            tol = 0.5 * 10 ** (-dec) + 0.0005
            acc = _unique_by_mass(val, tol)
            return _hit(out, acc, "delta") if acc else out
        return out

    name = _LOCALISATION.sub("", t)
    name = _RESIDUE_SUFFIX.sub("", name).strip().lower()
    acc = _NAME_TO_ID.get(name)
    if acc is None:
        # "Cation:Mg[II] (DE)" and friends: nothing else to try; also try the un-suffixed whole text
        acc = _NAME_TO_ID.get(t.lower())
    return _hit(out, acc, "name") if acc else out


def _hit(out: dict[str, Any], acc: int, how: str) -> dict[str, Any]:
    name, mass, _c = UNIMOD[acc]
    out.update(accession=acc, name=name, mass=mass, how=how)
    return out


def _split(pf: str) -> list[tuple[str, str]]:
    """Tokenise into ("seq", text) and ("mod", bracket body); handles nested brackets
    ("Cation:Mg[II]") by depth counting. An unbalanced '[' is kept as literal sequence text."""
    parts: list[tuple[str, str]] = []
    buf: list[str] = []
    i, n = 0, len(pf)
    while i < n:
        ch = pf[i]
        if ch == "[":
            depth, j = 0, i
            while j < n:
                if pf[j] == "[":
                    depth += 1
                elif pf[j] == "]":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if j < n:
                if buf:
                    parts.append(("seq", "".join(buf)))
                    buf = []
                parts.append(("mod", pf[i + 1:j]))
                i = j + 1
                continue
        buf.append(ch)
        i += 1
    if buf:
        parts.append(("seq", "".join(buf)))
    return parts


def canonicalize(pf: str | None) -> dict[str, Any]:
    """Canonicalise one ProForma-ish string.

    Returns:
      key       grouping key: mapped mods as [UNIMOD:n], unknown tokens verbatim; mods stacked on
                one residue are sorted so order of notation cannot split a form. None-safe.
      display   same string with readable names: NM[Oxidation]QDMVEDYR
      segments  [{"t": text, "acc": int, "name": str, "raw": str, "how": str}|{"t": text}] for
                rendering a tooltip per mod (accession in `acc`, original notation in `raw`)
      mods      [{"pos", "residue", **resolve_token()}] pos 0 = N-terminal
      unmapped  raw tokens that were passed through unchanged
    """
    if not pf or not isinstance(pf, str):
        return {"key": pf, "display": pf, "segments": [], "mods": [], "unmapped": []}
    parts = _split(pf)

    # group consecutive mod tokens (stacked on one residue) so they can be ordered canonically
    segments: list[dict[str, Any]] = []
    mods: list[dict[str, Any]] = []
    unmapped: list[str] = []
    key_parts: list[str] = []
    n_res = 0
    last_res: str | None = None
    i = 0
    while i < len(parts):
        kind, text = parts[i]
        if kind == "seq":
            segments.append({"t": text})
            key_parts.append(text)
            for ch in text:
                if ch.isalpha():
                    n_res += 1
                    last_res = ch
            i += 1
            continue
        stack = []
        while i < len(parts) and parts[i][0] == "mod":
            stack.append(resolve_token(parts[i][1]))
            i += 1
        # canonical order: mapped by accession, then unmapped by raw text
        stack.sort(key=lambda r: (r["accession"] is None, r["accession"] or 0, r["raw"]))
        for r in stack:
            mods.append({"pos": n_res, "residue": last_res if n_res else None, **r})
            if r["accession"] is None:
                unmapped.append(r["raw"])
                segments.append({"t": r["raw"], "raw": r["raw"], "how": "unmapped", "unmapped": True, "bracket": True})
                key_parts.append(f"[{r['raw']}]")
            else:
                segments.append({"t": r["name"], "acc": r["accession"], "name": r["name"],
                                 "raw": r["raw"], "how": r["how"], "mass": r["mass"], "bracket": True})
                key_parts.append(f"[UNIMOD:{r['accession']}]")
    display = "".join(f"[{s['t']}]" if s.get("bracket") else s["t"] for s in segments)
    return {"key": "".join(key_parts), "display": display, "segments": segments,
            "mods": mods, "unmapped": unmapped}


def _wavg(rows: list[dict[str, Any]], field: str) -> float | None:
    num = den = 0.0
    for r in rows:
        v, w = r.get(field), r.get("n_obs") or 0
        if v is not None and w:
            num += float(v) * w
            den += w
    return num / den if den else None


def merge_forms(forms: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge (modified_seq_proforma, charge) rows that are the same canonical form x charge.

    Input rows: modified_seq_proforma, charge, n_obs, avg_mz, avg_rt, avg_im, best_q_value,
    avg_log2_int, max_engines and optionally `engines` (list of engine names).
    n_obs is summed, averages are re-weighted by n_obs (over rows where the average exists),
    best_q is the minimum, engines the union. Each output row carries:
      modified_seq_proforma  the canonical key (accession notation), kept for API back-compat
      display / segments     readable rendering (see canonicalize)
      notations              the distinct raw strings that were merged (list)
      n_notations            len(notations); > 1 means the row merged several notations
      has_unmapped           True when an unrecognised token was passed through
    Order: n_obs descending (as the SQL did). Work is O(rows) on an already-bounded input.
    """
    groups: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    canon: dict[str, dict[str, Any]] = {}
    for f in forms:
        raw = f.get("modified_seq_proforma")
        c = canon.get(raw)
        if c is None:
            c = canon[raw] = canonicalize(raw)
        groups.setdefault((c["key"], f.get("charge")), []).append(f)

    out: list[dict[str, Any]] = []
    for (key, charge), rows in groups.items():
        raws = []
        for r in rows:
            raw = r.get("modified_seq_proforma")
            if raw not in raws:
                raws.append(raw)
        c = canon[raws[0]]
        engines = sorted({e for r in rows for e in (r.get("engines") or []) if e})
        qs = [r["best_q_value"] for r in rows if r.get("best_q_value") is not None]
        out.append({
            "modified_seq_proforma": key,
            "display": c["display"],
            "segments": c["segments"],
            "charge": charge,
            "n_obs": sum(int(r.get("n_obs") or 0) for r in rows),
            "avg_mz": _wavg(rows, "avg_mz"),
            "avg_rt": _wavg(rows, "avg_rt"),
            "avg_im": _wavg(rows, "avg_im"),
            "best_q_value": min(qs) if qs else None,
            "avg_log2_int": _wavg(rows, "avg_log2_int"),
            "max_engines": max((r.get("max_engines") or 0) for r in rows) or None,
            "engines": engines,
            "notations": raws,
            "n_notations": len(raws),
            "has_unmapped": bool(c["unmapped"]),
        })
    out.sort(key=lambda r: -r["n_obs"])
    return out
