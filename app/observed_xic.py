"""Measured XIC for a peptide, read on demand from the per-search XIC Lance lanes in Azure Blob
(franfragments/xic). The fallback behind /api/peptide/{seq}/xic when the curated table
delimp_precursor_xic has no row for the peptide -- which, since that table is a hand-picked display
set, is most peptides, including every newly ingested DIA-NN search.

What it returns, and why it is labelled differently from the curated rows:
  * ONE acquisition per charge state -- the most confident (lowest q-value) target row of that
    precursor in one lane -- apex-aligned on the engine's own apex RT. Curated rows are an
    apex-aligned AVERAGE over runs; this is a single measured chromatogram, and the response says
    so (trace_basis = "single_run") so the panel never calls it an average.
  * Fragment m/z are THEORETICAL (from the sequence, Cys carbamidomethyl assumed): the lane stores
    trace labels (y7^1), not m/z. Only computed when the chosen row carries no modification other
    than carbamidomethyl; otherwise m/z is left out rather than drawn wrong.
  * No library intensities: the lane has none, so the mirror's predicted half is not drawn.

Governance (same as observed_spectrum): the blob container is private, read server-side with the
App Service managed identity; the response carries no search, run, raw path or customer identity;
one peptide per request, rate-limited at the route; decoy rows are never served.
"""
from __future__ import annotations

import os
import re

from . import db
from .fragments import fragments as _theoretical
from .observed_spectrum import _clean_seq, _dataset

_CONTAINER = os.environ.get("FRAN_XIC_CONTAINER", "xic")
_TOP_N = 6
_ION = re.compile(r"^([abcxyz])(\d+)(?:-([A-Za-z0-9]+))?\^(\d+)$")
_CAM = re.compile(r"\(UniMod:4\)|\[UNIMOD:4\]|\[\+57\.0\d*\]|\(Carbamidomethyl[^)]*\)")


_LANE_CACHE: dict = {"at": 0.0, "rows": {}}


def _lane_map() -> dict:
    """search_id -> blob basename for every DIA-NN XIC lane (~1k rows), cached 10 min. Spectronaut
    lanes are left out: their trace labels follow another convention, and the two that exist (the
    dog set) are already in the curated table."""
    import time
    if time.time() - _LANE_CACHE["at"] > 600:
        rows = db.query(
            """SELECT search_id::text AS sid, lance_path FROM delimp_xic_lane
                WHERE lance_path LIKE '%%.diann.xic.lance'""",
            tables=["delimp_xic_lane"], timeout_ms=5000) or []
        _LANE_CACHE.update(at=time.time(), rows={r["sid"]: os.path.basename(str(r["lance_path"]).rstrip("/"))
                                                 for r in rows})
    return _LANE_CACHE["rows"]


def _lanes(seq: str, limit: int = 3) -> list[str]:
    """Up to `limit` lane datasets holding this peptide. Deliberately UNORDERED with a LIMIT: ranking
    every occurrence by q-value fetched all of them, and a common peptide has ~19k rows scattered
    over a 763M-row table (124 s, then statement timeouts, 2026-11-16). The lane read itself picks
    the most confident run, so stopping at the first hits costs nothing. search_id only locates
    the blob and is never returned."""
    lanes = _lane_map()
    if not lanes:
        return []
    rows = db.query(
        """SELECT DISTINCT search_id::text AS sid FROM (
               SELECT search_id FROM delimp_precursors
                WHERE stripped_seq = %s AND search_id = ANY(%s::uuid[])
                LIMIT 50) x""",
        (seq, list(lanes)),
        tables=["delimp_precursors"], timeout_ms=8000) or []
    return [lanes[r["sid"]] for r in rows if r["sid"] in lanes][:limit]


def _charge_distribution(seq: str) -> list[dict]:
    try:
        cd = db.query(
            "SELECT charge, COUNT(*) AS n FROM delimp_precursors WHERE stripped_seq = %s GROUP BY charge",
            (seq,), tables=["delimp_precursors"], timeout_ms=6000,
        ) or []
    except Exception:  # noqa: BLE001 - optional context, never fatal
        return []
    tot = sum(c["n"] for c in cd) or 1
    return sorted(({"charge": c["charge"], "n_obs": c["n"], "pct": round(100 * c["n"] / tot)} for c in cd),
                  key=lambda x: -x["n_obs"])


def _only_cam(modified: str | None, stripped: str) -> bool:
    """True when the modified sequence differs from the stripped one only by carbamidomethyl-Cys."""
    if not modified:
        return True
    rest = _CAM.sub("", modified)
    return "".join(ch for ch in rest if "A" <= ch <= "Z") == stripped and not re.search(r"[\[(]", rest)


def _precursor(seq: str, r: dict, n_runs: int, engine: str | None) -> dict:
    apex = float(r.get("rt") or 0.0)
    labels = r.get("trace_label") or []
    levels = r.get("trace_ms_level") or []
    rts = r.get("trace_rt") or []
    ints = r.get("trace_intensity") or []
    theo = {}
    if _only_cam(r.get("modified_seq"), seq):
        theo = {(i["type"], i["series"], i["charge"]): i["mz"]
                for i in _theoretical(seq, carbamidomethyl=True, max_charge=3)["ions"]}

    def _trace(k):
        xs, ys = (rts[k] if k < len(rts) else None) or [], (ints[k] if k < len(ints) else None) or []
        return [{"rt": round(float(x) - apex, 4), "i": float(y)} for x, y in zip(xs, ys)]

    ms1, frags = [], []
    for k, lab in enumerate(labels):
        lv = levels[k] if k < len(levels) else None
        if lab == "ms1" or lv == 1:
            if not ms1:
                ms1 = _trace(k)
            continue
        m = _ION.match(lab or "")
        if not m:                       # e.g. DIA-NN's 'index' pseudo-trace: not an ion
            continue
        typ, series, loss, z = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
        tr = _trace(k)
        frags.append({"label": lab, "ion": f"{typ}{series}" + (f"-{loss}" if loss else ""),
                      "charge": z, "rel_intensity": None,
                      "mz": None if loss else theo.get((typ, series, z)),
                      "trace": tr, "_apex": max((t["i"] for t in tr), default=0.0)})
    frags.sort(key=lambda f: -f["_apex"])
    top = frags[:_TOP_N]
    for f in top:
        f.pop("_apex", None)
    usage = [{"label": f["label"], "n_searches": 0, "pct": 0, "rel_intensity": 0} for f in top]
    return {
        "precursor_id": None, "charge": int(r["charge"]), "rt_apex": round(apex, 3),
        "representative_run": None,             # identity-decoupled by design
        "n_searches": 1, "n_runs_in_search": n_runs,
        "ms1": ms1, "fragments": top, "fragment_usage": usage,
        "engine": engine,
        "engine_version": None,
        "has_real_trace": any(f["trace"] for f in top),
        "has_library_intensities": False,
        "fragment_mz_theoretical": any(f["mz"] is not None for f in top),
        "trace_basis": "single_run",
    }


def lane_xic(stripped_seq: str) -> dict:
    """{available: False} or the peptide_xic() shape, built from one lane. Never raises for a
    missing peptide; blob/auth errors propagate to the route's _safe()."""
    seq = _clean_seq(stripped_seq)
    if len(seq) < 2:
        return {"available": False}
    cols = ["stripped_seq", "modified_seq", "charge", "rt", "q_value", "is_decoy",
            "trace_label", "trace_ms_level", "trace_rt", "trace_intensity"]
    for basename in _lanes(seq):
        flt = f"stripped_seq = '{seq}' AND is_decoy = false"   # seq is [A-Z] only -> injection-safe
        rows = _dataset(basename, _CONTAINER).scanner(columns=cols, filter=flt).to_table().to_pylist()
        if not rows:
            continue
        by_z: dict[int, list[dict]] = {}
        for r in rows:
            by_z.setdefault(int(r["charge"]), []).append(r)
        precs = []
        for z in sorted(by_z):
            cand = by_z[z]
            # prefer a carbamidomethyl-only form (its fragment m/z can be computed), then confidence
            best = min(cand, key=lambda r: (not _only_cam(r.get("modified_seq"), seq),
                                            r.get("q_value") if r.get("q_value") is not None else 1.0))
            engine = "DIA-NN"                     # _lane_map() serves DIA-NN lanes only
            precs.append(_precursor(seq, best, len(cand), engine))
        return {"available": True, "stripped_seq": seq, "source": "lane", "trace_basis": "single_run",
                "rt_axis": "RT − apex (min)", "charge_distribution": _charge_distribution(seq),
                "precursors": precs}
    return {"available": False}
