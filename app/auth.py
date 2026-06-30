"""Per-request authorization: may THIS caller see the confidential (internal) layer?

The single merged FRAN deployment is public by default. The confidential layer (real PI/customer
names, raw paths, the People/Submissions tab, the private provenance + CoreOmics tables) turns on
**per request** only for an authenticated, group-authorized caller. Everyone else gets the public,
sanitized site.

PRODUCTION — Azure App Service "Easy Auth" with Microsoft Entra ID (OpenID Connect) in
"allow-unauthenticated" mode. The platform performs the login and injects the verified principal as a
base64-encoded JSON blob in the `X-MS-CLIENT-PRINCIPAL` header (and `X-MS-CLIENT-PRINCIPAL-NAME`).
We authorize iff that principal is present AND carries the required Entra security-group object id
(env FRAN_REQUIRED_GROUP). Membership in that one group is the entire access list — managed in Entra,
no redeploy.

  Why trust the header? With Easy Auth enabled, App Service strips any client-supplied
  X-MS-CLIENT-PRINCIPAL* headers and sets them itself only after validating the login — the app never
  sees a spoofed one. (If you front the app a different way, terminate auth at that proxy the same way.)

FAIL-CLOSED. No principal, or no FRAN_REQUIRED_GROUP configured, or the group is absent → public.
Confidential data is never served without an explicit, satisfied group gate.

LOCAL / DEV — set FRAN_DEV_AUTH=1 to enable two non-production shortcuts:
  * env FRAN_DEV_INTERNAL=1            → every request is internal (mimics the legacy private Space)
  * header  X-Dev-Internal: 1         → that single request is internal (used by the test script)
Both are ignored unless FRAN_DEV_AUTH=1, so they can't accidentally open prod.
"""
from __future__ import annotations

import base64
import json
import os

# EITHER of these grants the confidential layer (configure one — or both):
#   FRAN_REQUIRED_GROUP : Entra security-group OBJECT ID whose members are authorized (best long-term;
#                         membership managed in Entra, no redeploy).
#   FRAN_ALLOWED_USERS  : comma-separated UC Davis emails / UPNs (e.g. "bsphinney@ucdavis.edu,jdoe@ucdavis.edu").
#                         Zero tickets — deploy with just yourself today; switch to a group anytime.
# A caller is authorized if they satisfy EITHER. If NEITHER is configured → deny everyone (fail-closed).
REQUIRED_GROUP = os.environ.get("FRAN_REQUIRED_GROUP", "").strip()
ALLOWED_USERS = frozenset(
    u.strip().lower() for u in os.environ.get("FRAN_ALLOWED_USERS", "").split(",") if u.strip()
)

_DEV_AUTH = os.environ.get("FRAN_DEV_AUTH") == "1"
_DEV_FORCE_INTERNAL = os.environ.get("FRAN_DEV_INTERNAL") == "1"

# Entra/Easy-Auth emit group membership under one of these claim types depending on token shape.
_GROUP_CLAIMS = (
    "groups",
    "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups",
    "http://schemas.microsoft.com/identity/claims/objectidentifier",  # not a group, but tolerated below
)
_NAME_CLAIMS = (
    "name",
    "preferred_username",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
)


def _decode_principal(request) -> dict | None:
    """Decode the Easy Auth principal header into a dict, or None if absent/malformed."""
    raw = request.headers.get("x-ms-client-principal")
    if not raw:
        return None
    try:
        return json.loads(base64.b64decode(raw).decode("utf-8"))
    except Exception:  # noqa: BLE001 - any decode failure = no usable principal = fail closed
        return None


def _claims(principal: dict | None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for c in (principal or {}).get("claims", []) or []:
        typ, val = c.get("typ"), c.get("val")
        if typ is not None:
            out.setdefault(typ, []).append(val)
    return out


def principal_is_authorized(request) -> bool:
    """Per-request decision: is this caller allowed the confidential layer? Fail-closed."""
    if _DEV_AUTH and _DEV_FORCE_INTERNAL:
        return True
    if _DEV_AUTH and request.headers.get("x-dev-internal") == "1":
        return True

    principal = _decode_principal(request)
    if not principal:
        return False
    if not REQUIRED_GROUP and not ALLOWED_USERS:
        # Authenticated but no gate configured at all → refuse (never leak by misconfiguration).
        return False
    claims = _claims(principal)

    # (a) group membership
    if REQUIRED_GROUP:
        groups: list[str] = []
        for ct in ("groups", "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups"):
            groups += claims.get(ct, []) or []
        if REQUIRED_GROUP in groups:
            return True

    # (b) explicit user allow-list (UPN / email), case-insensitive
    if ALLOWED_USERS:
        ids = set()
        name_hdr = (request.headers.get("x-ms-client-principal-name") or "").strip().lower()
        if name_hdr:
            ids.add(name_hdr)
        for ct in ("preferred_username",
                   "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
                   "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn"):
            for v in claims.get(ct, []) or []:
                if v:
                    ids.add(v.strip().lower())
        ud = (principal.get("userDetails") or "").strip().lower()
        if ud:
            ids.add(ud)
        if ids & ALLOWED_USERS:
            return True

    return False


def caller_email(request, principal: dict | None = None) -> str | None:
    """The logged-in user's email/UPN (lowercased), used to match CoreOmics PI/submitter. None if absent."""
    principal = principal if principal is not None else _decode_principal(request)
    name_hdr = (request.headers.get("x-ms-client-principal-name") or "").strip().lower()
    if "@" in name_hdr:
        return name_hdr
    claims = _claims(principal)
    for ct in ("preferred_username", "email",
               "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
               "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn"):
        for v in claims.get(ct, []) or []:
            if v and "@" in v:
                return v.strip().lower()
    ud = ((principal or {}).get("userDetails") or "").strip().lower()
    return ud if "@" in ud else None


def resolve_access(request) -> dict:
    """Decide the CURRENT request's access scope (tiered portal). Returns a dict with 'tier' in
    {'public','lab','full'} plus 'email' and, for lab users, 'submission_ids'. Fail-closed to public.

      full   — Proteomics Core staff (FRAN_ALLOWED_USERS / FRAN_REQUIRED_GROUP): everything.
      lab    — a logged-in user whose email matches a CoreOmics pi_email/submitter_email: ONLY their
               own submissions (submission_ids).
      public — anonymous, or logged in but matched to no CoreOmics record.
    """
    # dev hooks (only when FRAN_DEV_AUTH=1) — used by the local test harness, inert in production
    if _DEV_AUTH and _DEV_FORCE_INTERNAL:
        return {"tier": "full", "email": None}
    if _DEV_AUTH and request.headers.get("x-dev-internal") == "1":
        return {"tier": "full", "email": "dev@ucdavis.edu"}
    if _DEV_AUTH and request.headers.get("x-dev-lab-email"):
        em = request.headers["x-dev-lab-email"].strip().lower()
        from . import queries
        subs = queries.submissions_for_email(em)
        return {"tier": "lab", "email": em, "submission_ids": subs} if subs else {"tier": "public", "email": em}

    principal = _decode_principal(request)
    if not principal:
        return {"tier": "public", "email": None}
    email = caller_email(request, principal)
    if principal_is_authorized(request):           # core staff (group or allow-list) -> full
        return {"tier": "full", "email": email}
    if email:                                      # lab user? match email to CoreOmics records
        from . import queries
        subs = queries.submissions_for_email(email)
        if subs:
            return {"tier": "lab", "email": email, "submission_ids": subs}
    return {"tier": "public", "email": email}      # logged in but no data linked to this account


def principal_name(request) -> str | None:
    """Best-effort display name of the logged-in user (for the 'logged in as …' UI), else None."""
    name = request.headers.get("x-ms-client-principal-name")
    if name:
        return name
    principal = _decode_principal(request)
    if not principal:
        return None
    claims = _claims(principal)
    for ct in _NAME_CLAIMS:
        if claims.get(ct):
            return claims[ct][0]
    return principal.get("userDetails")
