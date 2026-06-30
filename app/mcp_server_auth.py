"""
AUTHENTICATED / CONFIDENTIAL MCP server for the DE-LIMP corpus browser, mounted at /mcp-auth.

This is the private sibling of the PUBLIC server in app/mcp_server.py (mounted at /mcp). It exposes the
SAME 7 public read-only tools PLUS a set of INTERNAL tools that wrap the EXISTING confidential query
functions used by the REST `/api/internal/*` routes (queries.internal_*). No new SQL and no new allowlist
are written here — the internal tools reuse the exact functions and the exact per-request gating the web
app already uses (app/auth.py + app/db.py + app/privacy.py).

HOW GATING WORKS (must mirror /api/internal/*):
  1. A FastAPI middleware (app/main.py::_mcp_auth_mw) runs for every /mcp-auth request BEFORE the mounted
     MCP ASGI app handles it. It base64-decodes the Easy-Auth X-MS-CLIENT-PRINCIPAL header, decides
     authorization with the SAME helper the site uses (auth.principal_is_authorized → ALLOWED_USERS /
     REQUIRED_GROUP), and records the decision in a ContextVar (_auth_ctx) defined here.
  2. Because the server runs stateless_http=True, a ContextVar set in that middleware PROPAGATES into the
     tool's execution context (verified locally). So each INTERNAL tool reads _auth_ctx and:
       - if no principal at all   → the middleware already returned HTTP 401 + WWW-Authenticate (Claude
                                     Code then starts its OAuth flow); the tool never runs.
       - if principal NOT allowlisted → the tool returns a clear {"error": "not authorized", ...} result
                                     (no DB touched, nothing leaked). PUBLIC tools still work.
       - if allowlisted ("full")  → the tool sets the SAME confidential scope the REST layer uses
                                     (db.set_internal(True) → is_full()/is_internal() True) and reveals
                                     real names (privacy.set_reveal(True)), runs the existing
                                     queries.internal_* function, and returns the confidential rows.

  Read-only + parameterized + the db allowlist still apply unchanged: the confidential tables become
  reachable ONLY because the request is internal (db._assert_allowlisted folds them in when is_internal()),
  exactly as for /api/internal/*.
"""

from __future__ import annotations

import contextvars
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import db, privacy, queries
from .mcp_server import _safe  # reuse the exact JSON-safe conversion the public server uses

# Per-request authorization decision, set by the FastAPI middleware in app/main.py just before the
# mounted /mcp-auth app handles the request. Shape:
#   {"authorized": bool, "has_principal": bool, "email": str | None}
# Default = fail-closed (no principal, not authorized) so a tool can never accidentally run internal.
_auth_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "fran_mcp_auth", default={"authorized": False, "has_principal": False, "email": None}
)


def set_auth(decision: dict | None) -> None:
    """Record THIS request's authorization decision (called by the FastAPI middleware)."""
    _auth_ctx.set(decision or {"authorized": False, "has_principal": False, "email": None})


def get_auth() -> dict:
    return _auth_ctx.get()


# Returned (and the DB left untouched) when a present-but-unauthorized principal calls an internal tool.
_NOT_AUTHORIZED = {
    "error": "not authorized",
    "detail": (
        "This MCP tool exposes confidential UC Davis Proteomics Core data (real PI / submitter / "
        "collaborator identities) and is restricted to allowlisted core-facility staff. Your identity is "
        "authenticated but not on the access list (FRAN_ALLOWED_USERS / FRAN_REQUIRED_GROUP). The PUBLIC "
        "corpus tools remain available. Contact the Proteomics Core (bsphinney@ucdavis.edu) for access."
    ),
}


def _require_internal() -> dict | None:
    """Gate for an internal tool. Returns an error dict to return verbatim if the caller is NOT
    authorized, or None (and sets the confidential per-request scope) if they ARE. Mirrors the
    db.is_full() gate the REST /api/internal/* handlers apply."""
    decision = _auth_ctx.get()
    if not decision.get("authorized"):
        return dict(_NOT_AUTHORIZED)
    # Authorized core-facility caller → set the SAME confidential scope the web app sets per request:
    # full tier (is_full()/is_internal() True → db allowlist admits the private tables) + name reveal
    # (so privacy.redact does not hash real PI / file / project names).
    db.set_internal(True)        # equivalent to db.set_scope({"tier": "full"})
    privacy.set_reveal(True)
    return None


def _reveal_safe(obj: Any) -> Any:
    """JSON-safe + apply the SAME privacy policy the REST `ok()` applies. For an authorized internal
    caller privacy.get_reveal() is True (set by _require_internal), so real names pass through — matching
    /api/internal/* which redacts with reveal = db.is_full()."""
    return privacy.redact(_safe(obj), privacy.get_reveal())


mcp = FastMCP(
    "FRAN DE-LIMP Corpus (Confidential)",
    instructions=(
        "Authenticated access to the UC Davis Proteomics Core 'PG Farm' DE-LIMP corpus. Includes the "
        "same PUBLIC corpus tools as the open /mcp server (proteins, peptides, genes, searches, overview) "
        "PLUS CONFIDENTIAL lab-support tools (people_search, lab, labs, collaborators, submission) that "
        "reveal real PI / submitter / collaborator identities and LIMS submission linkage. The "
        "confidential tools work ONLY for allowlisted UC Davis core-facility staff; everyone else gets a "
        "clear 'not authorized' result while the public tools keep working. Use the confidential tools to "
        "answer lab-support and collaborator-activity questions — e.g. summarizing a PI lab's submission "
        "history when drafting a support letter."
    ),
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


# ===========================================================================
# PUBLIC TOOLS — identical surface to app/mcp_server.py (re-registered on this
# instance). Each wraps ONE existing public query function; runs at default
# (public) scope, so the confidential tables stay structurally unreachable.
# ===========================================================================
@mcp.tool()
def corpus_overview() -> dict[str, Any]:
    """High-level snapshot of the whole corpus: counts of searches, raw files, identified organisms,
    precursors, proteins, distinct peptides and distinct protein groups (some exact, some fast planner
    estimates flagged by the "estimated" key). Good first call to size up the corpus. PUBLIC."""
    return _safe(queries.overview_counts())


@mcp.tool()
def search_proteins(q: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """Search protein groups by GENE symbol (exact, case-insensitive, e.g. "APOE") or by ACCESSION /
    protein_group prefix (e.g. "P02649"). Returns {rows, total, limit, offset}. PUBLIC.

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
    """Search peptides by stripped (unmodified) amino-acid sequence; exact=False is a substring match,
    exact=True the whole sequence. Returns {rows, total, limit, offset}. PUBLIC.

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
    """Detail for ONE protein group: corpus-wide summary + per-search breakdown. Pass the full
    protein_group id (may be semicolon-delimited). PUBLIC.

    Args:
        protein_group: the protein_group identifier to look up.
    """
    return _safe(queries.protein_detail(protein_group))


@mcp.tool()
def peptide_detail(stripped_seq: str) -> dict[str, Any]:
    """Detail for ONE peptide (by stripped sequence): summary, modforms x charges, observations, and
    cross-engine consensus. PUBLIC.

    Args:
        stripped_seq: the unmodified amino-acid sequence (case-insensitive).
    """
    return _safe(queries.peptide_detail(stripped_seq))


@mcp.tool()
def gene_detail(gene: str) -> dict[str, Any]:
    """Everything the corpus knows about a GENE symbol: protein groups, totals, organisms, pipelines.
    Exact gene-symbol match (case-sensitive as stored, e.g. "APOE" / "Apoe"). PUBLIC.

    Args:
        gene: the gene symbol to look up.
    """
    return _safe(queries.gene_detail(gene))


@mcp.tool()
def list_searches(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """Browse the corpus's searches (DIA proteomics analyses), newest first. Returns {rows, total,
    limit, offset}. PUBLIC.

    Args:
        limit: max rows (capped at 200).
        offset: pagination offset.
    """
    return _safe(queries.list_searches(limit, offset))


# ===========================================================================
# INTERNAL / CONFIDENTIAL TOOLS — allowlisted callers only. Each wraps the
# EXACT queries.internal_* function the matching /api/internal/* route uses
# and is gated by the SAME db.is_full() check (here via _require_internal()).
# ===========================================================================
@mcp.tool()
def people_search(q: str, limit: int = 80) -> dict[str, Any]:
    """CONFIDENTIAL (allowlisted core-facility staff only). Find the corpus searches behind a PERSON or
    LIMS identity: matches a CoreOmics PI name, submitter name, submission number, or institute, plus the
    provenance path-hints (client / PI / project / real search name). Returns {q, total, rows:[...]} with
    REAL identities, submission linkage, search engine and precursor counts.

    This is the primary lab-support / collaborator-activity tool: use it to find everything a given PI or
    lab submitted — e.g. when drafting a PI support letter, summarizing a collaborator's activity, or
    confirming who owns a submission. Non-allowlisted callers get a 'not authorized' result.

    Args:
        q: a PI / submitter surname, institute, submission number, or project fragment (>= 2 chars).
        limit: max rows (default 80).
    """
    err = _require_internal()
    if err is not None:
        return err
    return _reveal_safe(queries.internal_people_search(q, limit))


@mcp.tool()
def lab(pi: str) -> dict[str, Any]:
    """CONFIDENTIAL (allowlisted core-facility staff only). A LAB / PI page keyed on a PI name: every
    CoreOmics submission for that PI plus every FRAN search behind them (submission-linked or
    person-resolved), with real names, institutes, sample counts, dates, and engines. Matching is on the
    identity tokens of the name, so a surname is enough.

    Use this for lab-support / collaborator-activity questions — e.g. to assemble a PI's full submission
    and search history when building a support letter or summarizing what a lab has run through the core.
    Non-allowlisted callers get a 'not authorized' result.

    Args:
        pi: a PI name (surname is enough; full "First Last" also works).
    """
    err = _require_internal()
    if err is not None:
        return err
    return _reveal_safe(queries.internal_lab(pi))


@mcp.tool()
def labs() -> dict[str, Any]:
    """CONFIDENTIAL (allowlisted core-facility staff only). The directory of real PI LABS pulled from
    CoreOmics submissions, grouped by institution — limited to labs that actually have a FRAN-linked
    search in the corpus. Returns {n_institutions, n_labs, institutions:[{institute, labs:[...]}]} with
    per-lab submission + search counts. Good for an institution-level overview of core-facility
    collaborators. Non-allowlisted callers get a 'not authorized' result."""
    err = _require_internal()
    if err is not None:
        return err
    return _reveal_safe(queries.internal_labs_by_institution())


@mcp.tool()
def collaborators() -> dict[str, Any]:
    """CONFIDENTIAL (allowlisted core-facility staff only). The collaborator (client) directory from
    provenance: every client with search counts, distinct PIs/projects, and LIMS-linkage counts. Returns
    {collaborators:[...]}. Non-allowlisted callers get a 'not authorized' result."""
    err = _require_internal()
    if err is not None:
        return err
    return {"collaborators": _reveal_safe(queries.internal_collaborators())}


@mcp.tool()
def collaborator(name: str) -> dict[str, Any]:
    """CONFIDENTIAL (allowlisted core-facility staff only). All searches for ONE collaborator (client)
    with REAL names, PI, project, report + raw-file paths, and CoreOmics/LIMS identity. Returns
    {client, n_searches, searches:[...]}. Pass a client name from `collaborators`. Non-allowlisted
    callers get a 'not authorized' result.

    Args:
        name: the collaborator/client name (use "(unparsed)" for the unparsed bucket).
    """
    err = _require_internal()
    if err is not None:
        return err
    return _reveal_safe(queries.internal_collaborator_searches(name))


@mcp.tool()
def submission(submission_id: str) -> dict[str, Any]:
    """CONFIDENTIAL (allowlisted core-facility staff only). One CoreOmics SUBMISSION (PI / submitter /
    institute / date / samples / type) plus EVERY FRAN search linked to it, with real names. Returns
    {submission_id, submission, samples, searches}. Pass a submission_id surfaced by people_search / lab.
    Non-allowlisted callers get a 'not authorized' result.

    Args:
        submission_id: the CoreOmics submission id.
    """
    err = _require_internal()
    if err is not None:
        return err
    return _reveal_safe(queries.internal_submission(submission_id))


def build_mcp_auth_app():
    """The authenticated MCP Streamable-HTTP ASGI app to mount at /mcp-auth on the parent FastAPI app.
    Wrap it with the per-request auth middleware (app/main.py) so the principal is decoded and the
    _auth_ctx is set BEFORE the MCP app runs."""
    return mcp.streamable_http_app()


def mcp_auth_lifespan(app):
    """Lifespan for THIS server's StreamableHTTP session manager. Like the public server's, it must run
    for the lifetime of the parent app, so wire it into the parent FastAPI(lifespan=...)."""
    return mcp.session_manager.run()
