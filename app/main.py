"""
DE-LIMP Corpus Browser — FastAPI backend.

A modern, read-only window onto the live PG Farm `delimp` proteomics corpus.
Connects to the LIVE DB so newly-ingested searches/precursors appear on refresh.
All DB access goes through app.db (public-layer allowlist, read-only,
parameterized). No raw user SQL ever reaches the database.
"""

from __future__ import annotations

import contextlib
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db, queries
from .mcp_server import build_mcp_app, mcp_lifespan
from .mcp_server_auth import build_mcp_auth_app, mcp_auth_lifespan

BASE = Path(__file__).parent
APP_VERSION = "0.14.10"  # 0.14.8: honest proteome stat — "% of genes (cumulative)" + isoform-level %;
                        #         +Taxonomic-breadth-by-run-count chart on the species overview.
                        # 0.13.0: rich lab/customer page — search stats (most-identified proteins,
                        # organisms, totals), submission topics (description/prep), + PI profile banner
                        # (grants/website/photo from delimp_pi_profile enrichment).
                        # 0.12.0: Collaborators re-keyed on service_customer; customer/lab pages show
                        # submission data-status (analyzed/on-share/not-ingested) from the AI disk-match;
                        # institutions merged (UC Davis variants) + college/department on UC Davis labs
APP_VERSION_NOTE = "0.11.0: /mcp-auth gated by per-user API key (X-FRAN-Key). " \
                   "0.10.1: OAuth metadata resource=App ID URI + scope (fix AADSTS9010010)"
                        # 0.9.3: MCP host-header fix (allow public host). 0.9.2: +/mcp, protein-search fix

# Entra (Azure AD) tenant that issues the tokens Easy Auth validates — its OIDC issuer is the
# authorization server Claude Code discovers via the Protected Resource Metadata document below.
ENTRA_TENANT_ID = os.environ.get("FRAN_ENTRA_TENANT_ID", "a8046f64-66c0-4f00-9046-c8daf92ff62b")
# Public origin of this deployment (the resource identifier Claude Code authenticates against).
PUBLIC_ORIGIN = os.environ.get("FRAN_PUBLIC_ORIGIN", "https://fran.stan-proteomics.org").rstrip("/")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """App lifespan. EACH mounted MCP StreamableHTTP session manager (public /mcp + authenticated
    /mcp-auth) REQUIRES its task group to be running for the lifetime of the app (else requests raise
    "Task group is not initialized"). Run both here via an AsyncExitStack."""
    async with contextlib.AsyncExitStack() as stack:
        await stack.enter_async_context(mcp_lifespan(app))
        await stack.enter_async_context(mcp_auth_lifespan(app))
        yield


app = FastAPI(title="DE-LIMP Corpus Browser", version=APP_VERSION, lifespan=_lifespan)

app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

# Read-only MCP server (Streamable HTTP) at /mcp — wraps the PUBLIC queries.py functions as tools so
# an MCP client (e.g. Claude Code) can query the corpus. PUBLIC-only: MCP requests don't set an
# internal scope, so the db allowlist keeps them on the public layer (see app/mcp_server.py).
app.mount("/mcp", build_mcp_app())


# ---------------------------------------------------------------------------
# AUTHENTICATED / CONFIDENTIAL MCP server at /mcp-auth (see app/mcp_server_auth.py).
# The mounted MCP ASGI app is wrapped by an ASGI middleware that runs PER REQUEST, BEFORE the MCP app:
# it decides whether THIS caller may see the confidential layer and records that decision in the MCP
# server's _auth_ctx ContextVar (which, under stateless_http=True, propagates into tool execution).
#
# AUTH IS A SIMPLE PER-USER API KEY (the primary path):
#   * header `X-FRAN-Key: fran_…` → mcp_keys.email_for_key() matches it against derive_key(e) for every
#     e in FRAN_ALLOWED_USERS. A match grants the confidential ("full") scope for this request.
#   * The key is a CUSTOM header, so it rides straight past Azure EasyAuth (which only touches the
#     standard Authorization header / its /.auth/* routes) — no Entra token, no OAuth flow.
# We ALSO keep the legacy EasyAuth-principal path working: if a verified, allowlisted
# X-MS-CLIENT-PRINCIPAL is present (a logged-in core-staff browser session reaching /mcp-auth), grant
# confidential too. The key is the primary path; the principal is the fallback.
#
# NO OAUTH, NO 401. The Entra-OAuth-via-Claude-Code flow is abandoned (Claude Code needs the discovery
# `resource` to equal the server URL, which Entra won't accept as a token resource). A missing/invalid
# key is NOT an error: the request simply runs at the PUBLIC tier (the internal tools self-return a
# clean "not authorized" result; the 7 public tools keep working). We never return 401 here.
class _McpAuthGate:
    """ASGI wrapper around the /mcp-auth MCP app: per-request key/principal check + ContextVar.
    Never 401s — no key just means public tier."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        from . import auth, db, mcp_keys, privacy
        from .mcp_server_auth import set_auth

        request = Request(scope, receive)
        # SECURITY (bug-sec #1): mounted ASGI sub-apps skip the parent http middleware that resets the
        # DB scope per request, and the MCP session manager's task group is app-lifetime — so a prior
        # authorized request's db.set_internal(True)/reveal could bleed into this one. Reset to the
        # PUBLIC baseline before dispatch in EVERY branch; authorized tools re-elevate via _require_internal().
        db.set_scope(None)
        privacy.set_reveal(False)
        # (1) PRIMARY: per-user API key in the custom X-FRAN-Key header (rides past EasyAuth untouched).
        key_email = mcp_keys.email_for_key(request.headers.get("x-fran-key"))
        if key_email:
            set_auth({"authorized": True, "has_principal": True, "email": key_email})
            await self.app(scope, receive, send)
            return
        # (2) FALLBACK: a verified, allowlisted EasyAuth principal (logged-in core-staff browser session).
        principal = auth._decode_principal(request)
        if principal is not None and auth.principal_is_authorized(request):
            set_auth({"authorized": True, "has_principal": True,
                      "email": auth.caller_email(request, principal)})
            await self.app(scope, receive, send)
            return
        # (3) Otherwise PUBLIC tier — NOT a 401. Internal tools return "not authorized"; public ones work.
        set_auth({"authorized": False, "has_principal": principal is not None,
                  "email": auth.caller_email(request, principal) if principal is not None else None})
        await self.app(scope, receive, send)


app.mount("/mcp-auth", _McpAuthGate(build_mcp_auth_app()))


@app.get("/.well-known/oauth-protected-resource")
def oauth_protected_resource():
    """OAuth 2.0 Protected Resource Metadata (RFC 9728). NO LONGER USED by the auth flow: /mcp-auth is
    now gated by a per-user API key (X-FRAN-Key), not OAuth, and never returns 401, so nothing triggers
    a metadata fetch. We keep this endpoint only so it isn't actively wrong — `resource` is now the
    standards-correct server URL ({PUBLIC_ORIGIN}/mcp-auth), not the previous broken `resource=api://…`
    (the Entra App ID URI, which Claude Code rejected because it didn't equal the server URL). Public."""
    return _no_store(JSONResponse({
        "resource": f"{PUBLIC_ORIGIN}/mcp-auth",
        "authorization_servers": [
            f"https://login.microsoftonline.com/{ENTRA_TENANT_ID}/v2.0"
        ],
        "bearer_methods_supported": ["header"],
    }))


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
async def _auth_mw(request: Request, call_next):
    """Per-request access control for the single merged deployment. Decides — once, here — whether
    THIS caller gets the confidential layer, then propagates that to the DB allowlist (db.set_internal)
    and the name-reveal (privacy.set_reveal) for the rest of the request.

    Internal iff: deployment-wide env force (db.INTERNAL_MODE) OR an authenticated, group-authorized
    SSO principal (auth.principal_is_authorized). A legacy core-facility internal key still reveals
    real names (but does NOT admit the private tables — that needs a real internal grant)."""
    from . import auth, privacy
    db.set_scope(auth.resolve_access(request))   # tier: public | lab | full (lab carries submission_ids)
    key = request.headers.get("x-internal-key") or request.query_params.get("internal_key")
    # GLOBAL name-reveal only for the unrestricted (full/core-staff) view. Lab users see SANITIZED
    # global views — their own real data is served, scoped, by /api/my — so no cross-lab leak.
    privacy.set_reveal(db.is_full() or privacy.key_ok(key))
    return await call_next(request)


def ok(data):
    from . import privacy
    return JSONResponse(privacy.redact(_json_safe(data), privacy.get_reveal()))


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    # inject the version into the app.js URL so every deploy busts the browser cache.
    # __INTERNAL_MODE__ / __INTERNAL_USER__ are PER-REQUEST (the auth middleware ran first), so the
    # same instance renders the confidential UI only for an authorized, logged-in caller.
    from . import auth
    tier = db.access_tier()
    user = (auth.principal_name(request) or "") if tier != "public" else ""
    html = (BASE / "templates" / "index.html").read_text() \
        .replace("__APP_VERSION__", APP_VERSION) \
        .replace("__INTERNAL_MODE__", "true" if db.is_full() else "false") \
        .replace("__ACCESS_TIER__", tier) \
        .replace("__INTERNAL_USER__", user.replace('"', ""))
    # NEVER cache this page: its logged-in/out state is baked in per-request, so a cached copy would
    # show the wrong auth state (e.g. still "Log in" after signing in). Always revalidate with the server.
    return HTMLResponse(html, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                                       "Pragma": "no-cache", "Expires": "0"})


@app.get("/login")
def login(post: str = "/"):
    """Send the user through UC Davis SSO. Azure App Service 'Easy Auth' intercepts /.auth/* at the
    platform edge (the app never sees it) and returns here authenticated. Swap this single redirect
    target for the CAS login URL if campus IAM requires CAS instead."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"/.auth/login/aad?post_login_redirect_uri={post}", status_code=302)


@app.get("/logout")
def logout():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/.auth/logout?post_logout_redirect_uri=/", status_code=302)


def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


_MCP_KEY_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>FRAN — Your Confidential MCP Key</title>
<style>
  body{{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:760px;margin:3rem auto;padding:0 1.25rem;color:#1a1a2e;line-height:1.55}}
  h1{{font-size:1.5rem}} code,pre{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}}
  .key{{font-size:1.05rem;background:#0f1535;color:#7ee787;padding:.6rem .8rem;border-radius:8px;display:inline-block;word-break:break-all;user-select:all}}
  pre{{background:#0f1535;color:#e6edf3;padding:1rem;border-radius:8px;overflow-x:auto;white-space:pre-wrap;word-break:break-all}}
  .warn{{background:#fff4e5;border:1px solid #f0b429;border-radius:8px;padding:.85rem 1rem;margin:1.25rem 0}}
  .muted{{color:#666}} a{{color:#3b5bdb}}
</style></head><body>
<h1>Your FRAN confidential MCP key</h1>
<p class="muted">Signed in as <b>{email}</b> (UC Davis Proteomics Core).</p>
<p>This key lets Claude Code reach the <b>confidential</b> FRAN corpus tools (real PI / submitter /
collaborator identities and LIMS submission linkage). Your personal key:</p>
<p><span class="key">{key}</span></p>
<h2 style="font-size:1.15rem">Add it to Claude Code</h2>
<p>Run this once — it registers the FRAN MCP server with your key as a custom header:</p>
<pre>{add_cmd}</pre>
<div class="warn"><b>Keep this key secret.</b> It grants access to confidential lab data. Anyone with it
can read real PI/customer identities. Do not paste it into shared docs, tickets, or chats. If it leaks,
contact the Proteomics Core to have it rotated (rotating the server secret invalidates all keys).</div>
<p class="muted">The public, sanitized corpus needs no key — only the confidential tools do.</p>
</body></html>"""

_MCP_KEY_DENIED = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>FRAN — MCP Key</title>
<style>body{{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:640px;margin:4rem auto;padding:0 1.25rem;color:#1a1a2e;line-height:1.55}}a{{color:#3b5bdb}}</style>
</head><body><h1>Confidential MCP key</h1><p>{message}</p></body></html>"""


@app.get("/mcp-key", response_class=HTMLResponse)
def mcp_key_page(request: Request):
    """Show the logged-in, allowlisted core-staff user their personal confidential-MCP key
    (derive_key(their_email)) plus a ready-to-copy `claude mcp add …` command. Gated exactly like
    /api/internal/* — only an allowlisted EasyAuth principal sees a key; everyone else gets a short
    message. The key itself is deterministic from the email + FRAN_MCP_KEY_SECRET (no key store)."""
    from . import auth, mcp_keys
    # Not a logged-in allowlisted core-staff user → no key.
    if not auth.principal_is_authorized(request):
        msg = ('Please <a href="/login?post=/mcp-key">log in with your UC Davis account</a> to get your '
               "confidential MCP key.") if not auth._decode_principal(request) else \
              ("Your account is not authorized for the confidential FRAN MCP tools. Contact the "
               "Proteomics Core (bsphinney@ucdavis.edu) for access.")
        return _no_store(HTMLResponse(_MCP_KEY_DENIED.format(message=msg), status_code=403))
    email = auth.caller_email(request)
    if not email:
        return _no_store(HTMLResponse(_MCP_KEY_DENIED.format(
            message="Could not determine your account email from the login session."), status_code=403))
    if os.environ.get("FRAN_MCP_KEY_SECRET", "") == "":
        return _no_store(HTMLResponse(_MCP_KEY_DENIED.format(
            message="MCP keys are not configured on this deployment (FRAN_MCP_KEY_SECRET unset). "
                    "Contact the Proteomics Core."), status_code=503))
    key = mcp_keys.derive_key(email)
    add_cmd = (f'claude mcp add --transport http --header "X-FRAN-Key: {key}" '
               f'fran {PUBLIC_ORIGIN}/mcp-auth/')
    return _no_store(HTMLResponse(_MCP_KEY_PAGE.format(email=email, key=key, add_cmd=add_cmd)))


@app.get("/api/me")
def api_me(request: Request):
    """LIVE login state for the frontend — fetched on every page load so the UI never shows a stale
    logged-in/out state from a cached page. Never cached. Returns only the caller's OWN identity/tier."""
    from . import auth
    tier = db.access_tier()
    return _no_store(ok({
        "authenticated": bool(request.headers.get("x-ms-client-principal")) or tier != "public",
        "tier": tier,                       # public | lab | full
        "is_full": db.is_full(),            # core staff (drives the full confidential UI)
        "name": auth.principal_name(request) if tier != "public" else None,
        "email": db.get_scope().get("email"),
        "n_submissions": len(db.scoped_submission_ids() or []) if tier == "lab" else None,
    }))


@app.get("/api/my")
def api_my(request: Request):
    """LAB (and full) users' OWN data — their CoreOmics submissions + linked FRAN searches, real
    names/paths. STRICTLY scoped to the caller's own submission_ids; returns unredacted because it's
    the caller's authorized data. Public callers get 404."""
    tier = db.access_tier()
    if tier == "public":
        raise HTTPException(404, "Not found.")
    ids = db.scoped_submission_ids()   # None for full (sees everything elsewhere); list for lab
    if tier == "full" and not ids:
        # core staff use the full directory (Collaborators); /api/my is the lab-scoped view.
        return _no_store(JSONResponse(_json_safe({"tier": "full", "n_submissions": 0, "submissions": [],
                                                  "n_searches": 0, "searches": []})))
    data = _safe(lambda: queries.my_data(ids), {"n_submissions": 0, "submissions": [],
                                                "n_searches": 0, "searches": []})
    # own data -> return UNREDACTED (bypass the global sanitizer); it's strictly the caller's submissions
    return _no_store(JSONResponse(_json_safe({"tier": tier, **data})))


@app.get("/api/export/diann_report/{search_id}")
def api_export_diann_report(search_id: str):
    """Download a DIA-NN-style report.parquet for ONE search, generated from FRAN's data, ready to
    upload to DE-LIMP (HF or local) for LIMPA. FULL staff can export any search; a LAB user only their
    OWN (search must belong to one of their submissions); public = 404. Contains real run/file names,
    so it's gated to the owner."""
    from fastapi.responses import Response
    from . import export_report
    tier = db.access_tier()
    if tier == "public":
        raise HTTPException(404, "Not found.")
    if not db.is_full():
        ids = {str(s) for s in (db.scoped_submission_ids() or [])}
        sub = _safe(lambda: queries.search_submission_id(search_id), None)
        if not sub or str(sub) not in ids:
            raise HTTPException(404, "Not found.")
    try:
        data, fname, meta = export_report.build_report_parquet(search_id)
    except export_report.ExportError as e:
        raise HTTPException(422, str(e))
    return Response(content=data, media_type="application/vnd.apache.parquet",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"',
                             "Cache-Control": "no-store",
                             "X-Report-Rows": str(meta.get("rows", 0)),
                             "X-Report-Runs": str(meta.get("runs", 0))})


@app.get("/api/export/research_brief/{search_id}")
def api_export_research_brief(search_id: str):
    """Download a markdown research brief for ONE search — a pre-filled input packet for a HIVE-connected
    Claude (the proteomics-pipeline skill) to re-search with DIA-NN + analyze with LIMPA. Same ownership
    gate as the parquet export (contains real file paths)."""
    from fastapi.responses import Response
    from . import export_report
    tier = db.access_tier()
    if tier == "public":
        raise HTTPException(404, "Not found.")
    if not db.is_full():
        ids = {str(s) for s in (db.scoped_submission_ids() or [])}
        sub = _safe(lambda: queries.search_submission_id(search_id), None)
        if not sub or str(sub) not in ids:
            raise HTTPException(404, "Not found.")
    try:
        data, fname, meta = export_report.build_research_brief(search_id)
    except export_report.ExportError as e:
        raise HTTPException(422, str(e))
    return Response(content=data, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"',
                             "Cache-Control": "no-store"})


@app.get("/api/export/resubmit_brief/{submission_id}")
def api_export_resubmit_brief(submission_id: str):
    """'Re-search this data' markdown packet for an UN-INGESTED CoreOmics submission — raw data on the
    service directory but not yet in FRAN. Pre-fills file locations (HIVE/Flinders) + full submission
    info for a HIVE-connected Claude to re-search with DIA-NN. FULL: any submission; LAB: own only; public: 404."""
    from fastapi.responses import Response
    from . import export_report
    tier = db.access_tier()
    if tier == "public":
        raise HTTPException(404, "Not found.")
    if not db.is_full():
        ids = {str(s) for s in (db.scoped_submission_ids() or [])}
        if str(submission_id) not in ids:
            raise HTTPException(404, "Not found.")
    try:
        data, fname, meta = export_report.build_resubmit_brief(submission_id)
    except export_report.ExportError as e:
        raise HTTPException(422, str(e))
    return Response(content=data, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"',
                             "Cache-Control": "no-store"})


@app.get("/api/_authcheck")
def authcheck(request: Request):
    """Diagnostic (safe — reports only the CALLER's own identity + booleans, no other data)."""
    from . import auth
    principal = auth._decode_principal(request)
    return _no_store(ok({
        "has_principal_header": bool(request.headers.get("x-ms-client-principal")),
        "principal_name_header": request.headers.get("x-ms-client-principal-name"),
        "decoded_name": auth.principal_name(request),
        "principal_decoded": principal is not None,
        "tier": db.access_tier(),
        "is_full": db.is_full(),
        "is_internal": db.is_internal(),
        "n_scoped_submissions": len(db.scoped_submission_ids() or []),
        # COUNT only — never the roster: this endpoint is unauthenticated, and the per-user MCP key is
        # HMAC(secret, email), so exposing the allowlisted emails hands an attacker the exact identities
        # to forge keys for. (bug-sec #2)
        "n_allowed_users_configured": len(auth.ALLOWED_USERS),
        "group_configured": bool(auth.REQUIRED_GROUP),
    }))


@app.get("/health")
@app.get("/api/health")  # /api/health alias: the custom-domain edge layer hijacks the bare /health
                         # path (returns its own "OK"), so the frontend reads status from /api/health.
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


@app.get("/api/proteins_showcase")
def api_proteins_showcase():
    """Proteins Showcase — fun, data-grounded tour: conservation hall of fame, biology-only
    conservation, whimsically-named gallery, function breakdown, contaminant gallery, superlatives."""
    return ok({"showcase": _safe(queries.proteins_showcase, {})})


@app.get("/api/species_showcase")
def api_species_showcase():
    """Species Showcase — cross-species fun facts: most-sampled species, the rarest
    'seen once' club, species with the most protein groups identified, a heuristic
    taxonomic-breadth breakdown, and a spotlight species. All from real corpus data
    over cheap indexed sources (run counts + delimp_mv_species_proteins)."""
    return ok({"showcase": _safe(queries.species_showcase, {})})


# FULL (core staff) ONLY — the cross-lab directory. Lab users are NOT admitted here (they'd see other
# labs' people); their own data is served, scoped, by /api/my. Hence is_full(), not is_internal().
@app.get("/api/internal/collaborators")
def api_internal_collaborators():
    """FULL only: the collaborator directory from provenance."""
    if not db.is_full():
        raise HTTPException(404, "Not found.")
    return ok(_safe(queries.internal_collaborators, {"collaborators": []}))


@app.get("/api/internal/collaborator/{name:path}")
def api_internal_collaborator(name: str):
    """FULL only: all searches for one collaborator, real names + paths."""
    if not db.is_full():
        raise HTTPException(404, "Not found.")
    return ok(_safe(lambda: queries.internal_collaborator_searches(name), {"client": name, "searches": []}))


@app.get("/api/internal/labs")
def api_internal_labs():
    """FULL only: real PI labs from CoreOmics, grouped by institution."""
    if not db.is_full():
        raise HTTPException(404, "Not found.")
    return ok(_safe(queries.internal_labs_by_institution, {"institutions": []}))


@app.get("/api/internal/people_search")
def api_internal_people_search(q: str, limit: int = 80):
    """FULL only: find searches by PI / submitter / submission number / institute."""
    if not db.is_full():
        raise HTTPException(404, "Not found.")
    return ok(_safe(lambda: queries.internal_people_search(q, limit), {"q": q, "total": 0, "rows": []}))


@app.get("/api/internal/submission/{submission_id}")
def api_internal_submission(submission_id: str):
    """One CoreOmics submission + all FRAN searches under it. FULL sees any; a LAB user only their OWN
    submission (else 404 — never another lab's submission)."""
    if not db.is_internal():
        raise HTTPException(404, "Not found.")
    if not db.is_full():
        ids = db.scoped_submission_ids() or []
        if str(submission_id) not in {str(s) for s in ids}:
            raise HTTPException(404, "Not found.")
    return ok(_safe(lambda: queries.internal_submission(submission_id),
                    {"submission_id": submission_id, "submission": None, "searches": []}))


@app.get("/api/internal/lab/{pi:path}")
def api_internal_lab(pi: str):
    """FULL only: a lab/PI page — all submissions + searches for a PI."""
    if not db.is_full():
        raise HTTPException(404, "Not found.")
    return ok(_safe(lambda: queries.internal_lab(pi), {"pi": pi, "submissions": [], "searches": []}))


@app.get("/api/weekly_poem")
def api_weekly_poem():
    """A 'Found Poem of the Week' built from the words hidden in the corpus peptides — rotates weekly."""
    return ok({"poem": _safe(queries.weekly_poem, None)})


@app.get("/api/proteome_code")
def api_proteome_code():
    """THE PROTEOME CODE 🔮 — hidden phrases + protein prophecies found in the peptides."""
    return ok(_safe(queries.proteome_code, {"available": False, "phrases": [], "prophecies": []}))


@app.get("/api/wordhunt")
def api_wordhunt():
    """Fun: English words / names / spicy words hidden in the corpus peptides."""
    return ok({"words": _safe(queries.word_leaderboard, [])})


@app.get("/api/peptides_showcase")
def api_peptides_showcase():
    """Fun, fact-filled tour of the corpus' peptides: hero stats, physico-chemical
    & composition superlatives (longest/heaviest/most-Cys/most-hydrophobic/...),
    palindromes, flyability extremes + the 4-class breakdown, and the hidden-words
    hunt. All computed over bounded precomputed matviews (never a live precursor
    GROUP BY), so it's fast and never trips the 30s timeout."""
    return ok(_safe(queries.peptides_showcase, {"available": False, "pool_size": 0}))


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


@app.get("/api/flyability_summary")
def api_flyability_summary():
    """Corpus-wide flyability category breakdown — % of peptides in each of PFly's four
    most-likely classes (strong / intermediate / weak / non-flyer)."""
    return ok(_safe(lambda: queries.flyability_summary(), {"total": 0, "categories": []}))


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


@app.get("/api/search/species")
def api_search_species(q: str, limit: int = 60):
    """Search identified species by scientific OR common name (e.g. 'dog', 'yeast', 'bat')."""
    if len(q.strip()) < 2:
        raise HTTPException(400, "Query must be at least 2 characters.")
    return ok(queries.search_species(q, limit))


# ---------------------------------------------------------------------------
# API — detail
# ---------------------------------------------------------------------------
@app.get("/api/protein/{protein_group:path}/summary")
def api_protein_summary(protein_group: str):
    """Rich human-readable protein summary (function/location/interactions/links/trivia)."""
    from . import annotation
    acc = protein_group.split(";")[0].strip()
    return ok({"summary": annotation.protein_summary(acc)})


@app.get("/api/protein/{protein_group:path}/card")
def api_protein_card(protein_group: str):
    """Fun 'trading card' stats for the protein pane: cross-species conservation, abundance
    percentile vs the corpus, peak run/organism, detection breadth, best confidence, contaminant
    flair, + Wikipedia blurb/image and UniProt/AlphaFold links. Before the catch-all so /card isn't eaten."""
    import re as _re
    from . import annotation
    stats = queries.protein_card_stats(protein_group)
    # strip contaminant prefixes (cRAP-/Cont_) so UniProt/AlphaFold links resolve, not 404
    acc = annotation.clean_accession(protein_group.split(";")[0].strip())
    gene = (stats or {}).get("gene") or ""
    clean_gene = _re.sub(r"^(crap-|cont[_-])", "", gene, flags=_re.I).strip() or None
    # The trivia blurb used the bare GENE SYMBOL — for short/ambiguous symbols that hits unrelated
    # pages (gene "spa" -> the wellness "Spa", not Staph Protein A). So: prefer the protein NAME,
    # and ACCEPT a blurb only if it reads as biology (else drop it rather than show nonsense).
    _BIO = ("protein", "gene", "enzyme", "peptide", "amino acid", "receptor", "kinase", "antibody",
            "immunoglobulin", "chromosome", "encoded", "expressed", "cellular", "membrane",
            "molecular", "domain", "residue", "proteome", "genome", "bacteri", "virus", "hormone",
            "signaling", "signalling", "metaboli", "transcription", "ribosom", "mitochond", "secreted")
    def _bio_ok(w):
        # only show trivia if the blurb reads as biology — drops "spa"->wellness, "ALB"->Albania,
        # etc. We deliberately do NOT fall back to the protein NAME: that finds plausible-but-WRONG
        # pages (gene 'spa'/Protein A matched human IGBP1). Better to show no trivia than wrong trivia.
        return bool(w) and any(t in ((w.get("extract") or "") + " " + (w.get("title") or "")).lower() for t in _BIO)
    wiki = None
    for title in (clean_gene, gene or None):
        if title and title not in ("NaN", "nan"):
            w = _safe(lambda t=title: annotation.fetch_wikipedia(t), None)
            if _bio_ok(w):
                wiki = w
                break
    links = {"uniprot": f"https://www.uniprot.org/uniprotkb/{acc}",
             "alphafold": f"https://alphafold.ebi.ac.uk/entry/{acc}"}
    return ok({"accession": acc, "card": stats, "wikipedia": wiki, "links": links})


@app.get("/api/protein/{protein_group:path}/coverage")
def api_protein_coverage(protein_group: str):
    """Sequence coverage map: UniProt sequence + observed corpus peptides mapped
    onto it. Registered before the catch-all so the /coverage suffix isn't eaten."""
    from . import coverage as cov
    data = queries.protein_coverage_peptides(protein_group)
    acc = protein_group.split(";")[0].strip()
    custom = queries.is_custom_accession(protein_group, data.get("gene"))
    seq = "" if custom else cov.fetch_uniprot_sequence(acc)
    mapped = cov.map_coverage(seq, data.get("peptides") or [])
    return ok({"accession": acc, "protein_group": protein_group, "gene": data.get("gene"),
               "custom_construct": custom,
               "sequence": seq, "sequence_available": bool(seq), **mapped})


@app.get("/api/protein/{protein_group:path}")
def api_protein(protein_group: str):
    from . import coverage as cov

    res = queries.protein_detail(protein_group)
    if not res["summary"]:
        raise HTTPException(404, "Protein group not found.")
    # Observed peptides are computed ONCE here (protein_detail no longer does it) via the
    # bounded+timed coverage query. The corpus has no explicit peptide->protein link, so this
    # candidate list is everything co-observed in runs where this PG was reported -- which
    # wrongly includes co-eluting OTHER proteins' peptides (the KNG1-shows-FGG bug, and the
    # inflated "31k unique peptides"). When we have the canonical UniProt/NCBI sequence we make
    # it the source of truth: keep only peptides that actually map onto it (I/L-equated
    # substring), exactly the set the coverage map draws. Else fall back to co-observed (flagged).
    acc = protein_group.split(";")[0].strip()
    cand = queries.protein_coverage_peptides(protein_group).get("peptides") or []
    # Custom-FASTA placeholder (recombinant construct, e.g. P0000 / gene 'whoKnows') -> no public
    # entry exists, so don't bother fetching a sequence; flag it so the UI shows a friendly note
    # instead of a "sequence not available" error + dead UniProt link.
    res["custom_construct"] = queries.is_custom_accession(protein_group, (res["summary"] or {}).get("gene"))
    seq = "" if res["custom_construct"] else cov.fetch_uniprot_sequence(acc)
    if seq:
        mapped = cov.map_coverage(seq, cand)
        res["peptides"] = sorted(mapped["peptides"], key=lambda p: p.get("n_precursors") or 0, reverse=True)
        res["n_mapped_peptides"] = mapped["n_mapped"]
        res["coverage_pct"] = mapped["coverage_pct"]
        res["peptides_sequence_mapped"] = True
    else:
        # No canonical sequence (custom construct / unknown accession / UniProt+NCBI down) -> we
        # cannot verify membership; fall back to co-observed, flagged so the UI says so.
        res["peptides"] = cand
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


@app.get("/api/species/{name:path}")
def api_species(name: str):
    """Species detail 'trading card': protein cool-stats (counts, most/least abundant, function
    breakdown, whimsical protein) for one organism."""
    res = _safe(lambda: queries.species_detail(name), {"organism": name, "n_proteins": 0})
    if not res or not res.get("n_proteins"):
        raise HTTPException(404, "Species not found in corpus.")
    return ok(res)


@app.get("/api/wiki")
def api_wiki(title: str = Query(...)):
    """Cached Wikipedia summary (extract + image + url) for fun facts — used by the species page
    (pass common or scientific name). Returns {wiki: null} when there's no clean page."""
    from . import annotation
    return ok({"wiki": _safe(lambda: annotation.fetch_wikipedia(title), None)})


@app.get("/api/peptide/{stripped_seq}/fragments")
def api_peptide_fragments(stripped_seq: str, carbamidomethyl: bool = True, max_charge: int = 2):
    """Theoretical b/y fragment ions (computed from sequence). Layer-1 of the spectrum view."""
    from . import fragments as frag
    return ok({"fragments": frag.fragments(stripped_seq, carbamidomethyl, max(1, min(max_charge, 3)))})


@app.get("/api/peptide/{stripped_seq}/observed")
def api_peptide_observed(request: Request, stripped_seq: str, charge: int | None = None):
    """Layer-0: the REAL measured MS2 spectrum for this peptide, read on demand from the
    observed-spectrum blob (identity-decoupled). Rate-limited per client; one peptide per request,
    no bulk path — so the corpus can't be scraped out through it."""
    from . import ratelimit, observed_spectrum as obs
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        request.client.host if request.client else "?")
    if not ratelimit.allow(f"observed:{ip}", limit=30, window_s=60):
        raise HTTPException(429, "Too many spectrum requests — please slow down.")
    z = max(1, min(charge, 6)) if charge else None
    return ok({"observed": _safe(lambda: obs.observed_spectrum(stripped_seq, z), None)})


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


@app.get("/api/peptide/{stripped_seq}/funfacts")
def api_peptide_funfacts(stripped_seq: str):
    """Peptide 'trading card': sequence-intrinsic physico-chem (computed) + live corpus stats —
    cross-species breadth, observation/charge/engine breadth, ion mobility & RT/iRT, peak abundance."""
    return ok({"funfacts": _safe(lambda: queries.peptide_fun_facts(stripped_seq), {"found": False})})


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
