"""
MCP (Model Context Protocol) server for the DE-LIMP corpus browser.

Exposes the corpus as READ-ONLY tools over Streamable HTTP at /mcp, so a Claude Code
(or any MCP client) can query the public proteomics corpus directly. Every tool is a thin
wrapper over an EXISTING function in app.queries — no new SQL is written here, so all the
governance guarantees of app.db apply unchanged:

  * read-only sessions + parameterized queries only (app.db.query),
  * the PUBLIC-layer table allowlist (app.db.PUBLIC_TABLES).

SECURITY — PUBLIC TOOLS ONLY.
  MCP requests do NOT pass through the FastAPI auth middleware that sets a per-request access
  scope, so every MCP call runs at the DEFAULT scope = "public" (app.db.access_tier() -> "public",
  is_internal() -> False). The internal/confidential tables are therefore structurally unreachable
  through these tools even if a query named one — app.db._assert_allowlisted() would raise. We also
  deliberately do NOT register any internal/collaborator/people/submission tools here.
  TODO(internal MCP): if internal MCP tools are ever wanted, gate them behind the SAME auth check
  used for /api/internal/* (auth.principal_is_authorized / db.is_full) by reading the request's
  Easy-Auth principal inside the tool via the MCP request context — until then, omitted on purpose.

Output is kept JSON-serializable (Decimal/datetime/UUID -> primitives) via _safe(), reusing the
same conversion the REST layer uses, so an agent always gets clean JSON.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import queries

# stateless_http=True: each call is self-contained (no server-side session affinity needed for these
# pure read tools), which is the simplest, most robust shape behind Azure App Service. json_response
# lets simple clients (and curl) read a plain JSON body instead of an SSE stream. streamable_http_path
# is "/" so that mounting the sub-app at "/mcp" puts the endpoint at exactly /mcp (not /mcp/mcp).
mcp = FastMCP(
    "FRAN DE-LIMP Corpus",
    instructions=(
        "Read-only access to the UC Davis Proteomics Core 'PG Farm' DE-LIMP corpus: a cross-species "
        "library of DIA proteomics searches, their precursors, peptides, and protein groups. Use these "
        "tools to look up proteins/genes/peptides, browse searches, and get corpus-wide stats. All data "
        "is the PUBLIC, aggregated layer (no customer/PI identities)."
    ),
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
    # Behind Azure App Service this is a PUBLIC, TLS-terminated endpoint serving read-only public
    # data. The MCP transport's DNS-rebinding protection only trusts localhost and otherwise rejects
    # the real Host (fran.stan-proteomics.org) with "Invalid Host header" — it guards localhost dev
    # servers, not a hosted public server. Disable it so real MCP clients (Claude Code) can connect.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _safe(obj: Any) -> Any:
    """Make psycopg2 values (Decimal / datetime / UUID) JSON-serializable. Mirrors app.main._json_safe
    so MCP tool output matches the REST API's shapes."""
    from datetime import date, datetime
    from decimal import Decimal
    from uuid import UUID

    if isinstance(obj, list):
        return [_safe(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _safe(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        f = float(obj)
        return int(f) if f.is_integer() else f
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, UUID):
        return str(obj)
    return obj


# ---------------------------------------------------------------------------
# Tools — each wraps ONE existing public query function.
# ---------------------------------------------------------------------------
@mcp.tool()
def corpus_overview() -> dict[str, Any]:
    """High-level snapshot of the whole corpus: counts of searches, raw files, identified organisms,
    precursors, proteins, distinct peptides and distinct protein groups (some are exact, some are
    fast planner estimates flagged by the "estimated" key). Good first call to size up the corpus."""
    return _safe(queries.overview_counts())


@mcp.tool()
def search_proteins(q: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """Search protein groups by GENE symbol (exact, case-insensitive, e.g. "APOE") or by ACCESSION /
    protein_group prefix (e.g. "P02649" or "A0A077"). Returns {rows, total, limit, offset}; each row has
    protein_group, gene, n_searches, n_runs, sum_unique_peptides, sum_precursors, any_contaminant.
    The common case is index-fast. A pure substring match that the indexes can't serve may return
    {"degraded": true} with a hint instead of results — prefer an exact gene or an accession prefix.

    Args:
        q: gene symbol or accession/protein_group prefix (>= 2 chars).
        limit: max rows (capped at 200).
        offset: pagination offset.
    """
    if len((q or "").strip()) < 2:
        return {"rows": [], "total": 0, "limit": limit, "offset": offset,
                "error": "Query must be at least 2 characters."}
    return _safe(queries.search_proteins(q, limit, offset))


@mcp.tool()
def search_peptides(q: str, exact: bool = False, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """Search peptides by stripped (unmodified) amino-acid sequence. With exact=False (default) this is
    a substring match (e.g. "VLDSFSNGMK" or a fragment); exact=True requires the whole sequence.
    Returns {rows, total, limit, offset}; each row aggregates one stripped_seq with n_precursors,
    n_modforms, n_charges, n_runs, n_searches, best_q_value, has_im, max_engines.

    Args:
        q: amino-acid sequence or fragment (>= 2 chars), case-insensitive.
        exact: True for an exact full-sequence match, False for substring.
        limit: max rows (capped at 200).
        offset: pagination offset.
    """
    if len((q or "").strip()) < 2:
        return {"rows": [], "total": 0, "limit": limit, "offset": offset,
                "error": "Query must be at least 2 characters."}
    return _safe(queries.search_peptides(q, exact, limit, offset))


@mcp.tool()
def protein_detail(protein_group: str) -> dict[str, Any]:
    """Detail for ONE protein group: a corpus-wide summary (gene, n_searches, n_runs, peptide/precursor
    totals, mean intensity, best protein-group q-value, contaminant flag) plus a per-search breakdown of
    where it was seen and how intensely. Pass the full protein_group id (may be semicolon-delimited,
    e.g. "P02649" or "A0A077S9R2;P81708"); typically obtained from search_proteins.

    Args:
        protein_group: the protein_group identifier to look up.
    """
    return _safe(queries.protein_detail(protein_group))


@mcp.tool()
def peptide_detail(stripped_seq: str) -> dict[str, Any]:
    """Detail for ONE peptide (by stripped sequence): a summary (observation/charge/run/search breadth,
    best q-value, mean RT/IM, max confirming engines), the modified forms x charge states observed,
    a bounded list of individual observations across runs, and any cross-engine consensus records.

    Args:
        stripped_seq: the unmodified amino-acid sequence (case-insensitive).
    """
    return _safe(queries.peptide_detail(stripped_seq))


@mcp.tool()
def gene_detail(gene: str) -> dict[str, Any]:
    """Everything the corpus knows about a GENE symbol: all its protein groups (with breadth + precursor
    totals), aggregate totals (distinct protein groups / searches / runs), the organisms it's seen in,
    and which search pipelines detected it. Exact gene-symbol match (case-sensitive as stored, e.g.
    "APOE" for human, "Apoe" for mouse).

    Args:
        gene: the gene symbol to look up.
    """
    return _safe(queries.gene_detail(gene))


@mcp.tool()
def list_searches(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """Browse the corpus's searches (DIA proteomics analyses), newest first. Returns {rows, total,
    limit, offset}; each row has id, search_name, search_engine(+version), pipeline, status,
    sharing_status, n_raw_files, n_precursors_total, n_proteins_total, timestamps, DOI / PRIDE
    accession when known. Use the returned id with other corpus tools / the web UI.

    Args:
        limit: max rows (capped at 200).
        offset: pagination offset.
    """
    return _safe(queries.list_searches(limit, offset))


def build_mcp_app():
    """The MCP Streamable-HTTP ASGI app to mount at /mcp on the parent FastAPI app."""
    return mcp.streamable_http_app()


def mcp_lifespan(app):
    """Lifespan for the MCP StreamableHTTP session manager. MUST run for the duration of the parent
    app (the session manager's task group has to be live, or requests raise "Task group is not
    initialized"). Wire this into the parent FastAPI(lifespan=...). Returns an async context manager."""
    return mcp.session_manager.run()
