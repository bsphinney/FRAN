"""
DE-LIMP Corpus Browser — FastAPI backend.

A modern, read-only window onto the live PG Farm `delimp` proteomics corpus.
Connects to the LIVE DB so newly-ingested searches/precursors appear on refresh.
All DB access goes through app.db (public-layer allowlist, read-only,
parameterized). No raw user SQL ever reaches the database.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db, queries

BASE = Path(__file__).parent
APP_VERSION = "0.5.9"  # shown in the site header so you can confirm you're on the latest deploy
app = FastAPI(title="DE-LIMP Corpus Browser", version=APP_VERSION)

app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


@app.exception_handler(db.GovernanceError)
async def _gov_handler(request: Request, exc: db.GovernanceError):
    # Missing credential / governance refusal -> clean 503 the UI can render.
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def _db_handler(request: Request, exc: Exception):
    # Surface DB connectivity errors (psycopg2 OperationalError etc.) as 503
    # with a readable message rather than a bare 500, so the frontend's
    # "Database unavailable" card shows the real reason.
    import psycopg2

    if isinstance(exc, psycopg2.Error):
        return JSONResponse(
            status_code=503,
            content={"detail": f"{type(exc).__name__}: {str(exc).strip()[:300]}"},
        )
    raise exc


def _json_safe(obj):
    """psycopg2 returns Decimal / datetime; make them JSON-serializable."""
    from datetime import date, datetime
    from decimal import Decimal
    from uuid import UUID

    if isinstance(obj, list):
        return [_json_safe(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        f = float(obj)
        return int(f) if f.is_integer() else f
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, UUID):
        return str(obj)
    return obj


@app.middleware("http")
async def _internal_key_mw(request: Request, call_next):
    """Reveal real filenames only when a valid core-facility internal key is
    presented (X-Internal-Key header or ?internal_key=); otherwise sanitize."""
    from . import privacy
    key = request.headers.get("x-internal-key") or request.query_params.get("internal_key")
    privacy.set_reveal(privacy.key_ok(key))
    return await call_next(request)


def ok(data):
    from . import privacy
    return JSONResponse(privacy.redact(_json_safe(data), privacy.get_reveal()))


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    # inject the version into the app.js URL so every deploy busts the browser cache
    return (BASE / "templates" / "index.html").read_text().replace("__APP_VERSION__", APP_VERSION)


@app.get("/health")
def health():
    h = db.healthcheck()
    if isinstance(h, dict):
        h = {**h, "version": APP_VERSION}
    return ok(h)


@app.get("/version")
def version():
    return ok({"version": APP_VERSION})


# ---------------------------------------------------------------------------
# API — overview / dashboard
# ---------------------------------------------------------------------------
def _safe(producer, fallback):
    """Run a dashboard section; on any DB error (e.g. a slow GROUP BY hitting the
    statement timeout) return a fallback so one slow section never blanks or 503s
    the whole overview. The counts already degrade per-card inside the producer."""
    try:
        return producer()
    except Exception:  # noqa: BLE001
        return fallback


@app.get("/api/overview")
def api_overview():
    return ok(
        {
            "counts": _safe(queries.overview_counts, {}),
            "species": _safe(queries.species_distribution, []),
            "platforms": _safe(queries.platform_distribution, []),
            "engines": _safe(queries.engine_distribution, []),
            "charges": _safe(queries.charge_distribution, []),
            "recent_searches": _safe(lambda: queries.recent_searches(8), []),
        }
    )


@app.get("/api/leaderboards")
def api_leaderboards(limit: int = 40):
    """Corpus 'most common' leaderboards — heavy GROUP BYs, long-cached, each
    degrades to [] independently if its query times out."""
    lim = max(5, min(int(limit), 100))
    return ok({
        "peptides": _safe(lambda: queries.top_peptides(lim), []),
        "proteins": _safe(lambda: queries.top_proteins(lim), []),
        "genes": _safe(lambda: queries.top_genes(lim), []),
    })


@app.get("/api/wordhunt")
def api_wordhunt():
    """Fun: English words / names / spicy words hidden in the corpus peptides."""
    return ok({"words": _safe(queries.word_leaderboard, [])})


@app.get("/api/counts")
def api_counts():
    """Cheap counts only — for the live auto-refresh ticker."""
    return ok(queries.overview_counts())


@app.get("/api/im_density")
def api_im_density(
    search_id: str | None = None,
    n: int = Query(6000, ge=500, le=20000),
):
    return ok(queries.im_rt_density_sample(search_id, n))  # {points, x_axis}


@app.get("/api/flyability_scatter")
def api_flyability_scatter(n: int = Query(8000, ge=500, le=30000)):
    """Predicted peptide flyability (Koina PFly) vs observed mean log2 intensity, one point
    per peptide — the Highlights 'flyability vs intensity' plot."""
    return ok({"points": _safe(lambda: queries.flyability_scatter(n), [])})


# ---------------------------------------------------------------------------
# API — search
# ---------------------------------------------------------------------------
@app.get("/api/search/peptides")
def api_search_peptides(
    q: str,
    exact: bool = False,
    limit: int = 50,
    offset: int = 0,
):
    if len(q.strip()) < 2:
        raise HTTPException(400, "Query must be at least 2 characters.")
    return ok(queries.search_peptides(q, exact, limit, offset))


@app.get("/api/search/proteins")
def api_search_proteins(q: str, limit: int = 50, offset: int = 0):
    if len(q.strip()) < 2:
        raise HTTPException(400, "Query must be at least 2 characters.")
    return ok(queries.search_proteins(q, limit, offset))


# ---------------------------------------------------------------------------
# API — detail
# ---------------------------------------------------------------------------
@app.get("/api/protein/{protein_group:path}/summary")
def api_protein_summary(protein_group: str):
    """Rich human-readable protein summary (function/location/interactions/links/trivia)."""
    from . import annotation
    acc = protein_group.split(";")[0].strip()
    return ok({"summary": annotation.protein_summary(acc)})


@app.get("/api/protein/{protein_group:path}/coverage")
def api_protein_coverage(protein_group: str):
    """Sequence coverage map: UniProt sequence + observed corpus peptides mapped
    onto it. Registered before the catch-all so the /coverage suffix isn't eaten."""
    from . import coverage as cov
    data = queries.protein_coverage_peptides(protein_group)
    acc = protein_group.split(";")[0].strip()
    seq = cov.fetch_uniprot_sequence(acc)
    mapped = cov.map_coverage(seq, data.get("peptides") or [])
    return ok({"accession": acc, "protein_group": protein_group, "gene": data.get("gene"),
               "sequence": seq, "sequence_available": bool(seq), **mapped})


@app.get("/api/protein/{protein_group:path}")
def api_protein(protein_group: str):
    from . import coverage as cov

    res = queries.protein_detail(protein_group)
    if not res["summary"]:
        raise HTTPException(404, "Protein group not found.")
    # The corpus has no explicit peptide->protein link, so protein_detail's peptide
    # list is everything co-observed in runs where this PG was reported -- which
    # wrongly includes co-eluting OTHER proteins' peptides (the KNG1-shows-FGG bug,
    # and the inflated "31k unique peptides"). Make the canonical UniProt sequence
    # the single source of truth: keep only peptides that actually map onto it
    # (I/L-equated substring), exactly the set the coverage map draws.
    acc = protein_group.split(";")[0].strip()
    seq = cov.fetch_uniprot_sequence(acc)
    if seq:
        cand = queries.protein_coverage_peptides(protein_group).get("peptides") or []
        mapped = cov.map_coverage(seq, cand)
        peps = sorted(mapped["peptides"], key=lambda p: p.get("n_precursors") or 0, reverse=True)
        res["peptides"] = peps
        res["n_mapped_peptides"] = mapped["n_mapped"]
        res["coverage_pct"] = mapped["coverage_pct"]
        res["peptides_sequence_mapped"] = True
    else:
        # No canonical sequence (unknown accession / UniProt down) -> we cannot
        # verify membership; fall back to co-observed, flagged so the UI says so.
        res["peptides_sequence_mapped"] = False
        res["n_mapped_peptides"] = None
    return ok(res)


@app.get("/api/peptide/{stripped_seq}/lca")
def api_peptide_lca(stripped_seq: str):
    """Taxonomic LCA of the peptide across UniProt (Unipept pept2lca)."""
    from . import lca as lca_mod
    return ok({"lca": lca_mod.peptide_lca(stripped_seq)})


@app.get("/api/peptide/{stripped_seq}/proteins")
def api_peptide_proteins(stripped_seq: str):
    """All UniProt proteins containing this peptide across all taxa (homologues),
    grouped by organism — Unipept pept2prot."""
    from . import lca as lca_mod
    return ok({"proteins": lca_mod.peptide_proteins(stripped_seq)})


@app.get("/api/gene/{gene}/summary")
def api_gene_summary(gene: str):
    """Plain-language gene identity + biologist resource links (UniProt/GeneCards/HPA/GTEx...)."""
    from . import annotation
    return ok({"summary": annotation.gene_summary(gene)})


@app.get("/api/gene/{gene}")
def api_gene(gene: str):
    """All corpus proteins for a gene + where it's seen + which pipelines found it."""
    res = queries.gene_detail(gene)
    if not res["proteins"]:
        raise HTTPException(404, "Gene not found in corpus.")
    return ok(res)


@app.get("/api/peptide/{stripped_seq}/fragments")
def api_peptide_fragments(stripped_seq: str, carbamidomethyl: bool = True, max_charge: int = 2):
    """Theoretical b/y fragment ions (computed from sequence). Layer-1 of the spectrum view."""
    from . import fragments as frag
    return ok({"fragments": frag.fragments(stripped_seq, carbamidomethyl, max(1, min(max_charge, 3)))})


@app.get("/api/peptide/{stripped_seq}/predicted")
def api_peptide_predicted(stripped_seq: str, charge: int = 2, ce: float = 28.0):
    """Koina model-predicted fragment intensities (Prosit/AlphaPeptDeep/ms2pip) + the
    across-model average — to compare independent predictors with the search library."""
    from . import koina
    z = max(1, min(charge, 6))
    return ok({"predicted": koina.predict(stripped_seq, z, ce=ce),
               "search_library": queries.peptide_search_library(stripped_seq, z)})


@app.get("/api/peptide/{stripped_seq}/xic")
def api_peptide_xic(stripped_seq: str):
    """Dual-pane XIC (MS1 + top-6 common quant fragments) with per-fragment usage %."""
    return ok({"xic": queries.peptide_xic(stripped_seq)})


@app.get("/api/peptide/{stripped_seq}/interference")
def api_peptide_interference(stripped_seq: str):
    """Shared-transition / interference: other peptides sharing this one's quant fragment
    m/z, flagged co-eluting (interference) vs RT-resolved."""
    return ok({"interference": queries.peptide_interference(stripped_seq)})


@app.get("/api/peptide/{stripped_seq}/summary")
def api_peptide_summary(stripped_seq: str):
    """Plain-language identity/function of the peptide's protein(s)."""
    from . import annotation
    return ok({"summary": annotation.peptide_summary(stripped_seq)})


@app.get("/api/peptide/{stripped_seq}/flyability")
def api_peptide_flyability(stripped_seq: str):
    """Predicted flyability (Koina PFly, pfly_2024_fine_tuned). Uses the precomputed corpus
    table when present, else scores this one peptide live. 0 = poor flyer, 1 = strong flyer;
    classes = the 4-class softmax (class 1 poor .. class 4 strong)."""
    rec = queries.peptide_flyability(stripped_seq)
    if rec and rec.get("flyability") is not None:
        return ok({"flyability": rec["flyability"],
                   "classes": [rec.get("c1"), rec.get("c2"), rec.get("c3"), rec.get("c4")],
                   "n_obs": rec.get("n_obs"), "mean_log2_intensity": rec.get("mean_log2_intensity"),
                   "model": rec.get("model"), "source": "precomputed"})
    from . import koina
    seq = (stripped_seq or "").strip().upper()
    live = koina.predict_flyability([seq]).get(seq) if seq.isalpha() else None
    if not live:
        return ok({"flyability": None})
    return ok({"flyability": live["score"], "classes": live["classes"],
               "model": koina._PFLY_MODEL, "source": "live"})


@app.get("/api/peptide/{stripped_seq}")
def api_peptide(stripped_seq: str):
    res = queries.peptide_detail(stripped_seq)
    if not res["summary"]:
        raise HTTPException(404, "Peptide not found.")
    return ok(res)


@app.get("/api/searches")
def api_searches(limit: int = 50, offset: int = 0):
    return ok(queries.list_searches(limit, offset))


@app.get("/api/search/{search_id}")
def api_search_detail(search_id: str):
    res = queries.search_detail(search_id)
    if not res["summary"]:
        raise HTTPException(404, "Search not found.")
    return ok(res)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "7860")),
        reload=False,
    )
