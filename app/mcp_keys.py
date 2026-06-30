"""Per-user API keys for the confidential MCP endpoint (/mcp-auth).

WHY THIS EXISTS
  The confidential MCP server (app/mcp_server_auth.py) was originally gated by Entra EasyAuth + an
  OAuth 401/Protected-Resource-Metadata handshake so Claude Code would run the Microsoft OAuth flow.
  That path is INCOMPATIBLE with Claude Code: Claude Code requires the discovery metadata `resource`
  to equal the MCP server URL, and Entra refuses to issue a token whose resource/audience is that URL
  (only a registered App ID URI is accepted). So we drop OAuth entirely and use a simple, robust
  PER-USER API KEY instead.

THE SCHEME (deterministic, stateless — no key store)
  A user's key is an HMAC of their (normalized) email under a single server secret:

      key = "fran_" + base64url( HMAC_SHA256( SECRET, email.strip().lower() ) ).rstrip("=")

  where SECRET = os.environ["FRAN_MCP_KEY_SECRET"].encode().

  * Deterministic: the same email always derives the same key, so we can show a user their key on the
    website (/mcp-key) and verify the same key on every request — with NO database/table of keys.
  * Bound to the allowlist: a presented key is accepted ONLY if it matches derive_key(e) for some e in
    FRAN_ALLOWED_USERS. Removing an email from the allowlist instantly revokes that key. Rotating
    FRAN_MCP_KEY_SECRET revokes ALL keys at once.
  * Fail-closed: if FRAN_MCP_KEY_SECRET is unset, email_for_key() always returns None (no key can ever
    authorize) and logs a warning, so a misconfigured deploy can never leak confidential data.

  The key is sent in the custom header `X-FRAN-Key` (NOT Authorization), so it sails past Azure App
  Service EasyAuth untouched (EasyAuth only intercepts the standard auth headers / its /.auth/* routes).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os

log = logging.getLogger("fran.mcp_keys")

KEY_PREFIX = "fran_"


def _secret() -> bytes | None:
    """The server-side HMAC secret, or None if unconfigured (→ fail closed)."""
    s = os.environ.get("FRAN_MCP_KEY_SECRET", "")
    return s.encode() if s else None


def _allowed_users() -> list[str]:
    """The allowlisted emails (normalized), read FRESH each call so it tracks the env at request time
    (mirrors app/auth.py's FRAN_ALLOWED_USERS, but re-read so tests/rotations don't need a reimport)."""
    return [u.strip().lower() for u in os.environ.get("FRAN_ALLOWED_USERS", "").split(",") if u.strip()]


def derive_key(email: str) -> str:
    """The deterministic per-user MCP key for `email`. Requires FRAN_MCP_KEY_SECRET to be set
    (raises KeyError if not — only ever called for display when the secret is present)."""
    secret = os.environ["FRAN_MCP_KEY_SECRET"].encode()
    mac = hmac.new(secret, email.strip().lower().encode(), hashlib.sha256).digest()
    return KEY_PREFIX + base64.urlsafe_b64encode(mac).decode().rstrip("=")


def email_for_key(presented_key: str | None) -> str | None:
    """Resolve a presented X-FRAN-Key to the allowlisted email it authorizes, or None.

    Iterates FRAN_ALLOWED_USERS, computes derive_key(e) for each, and constant-time-compares it to the
    presented key. Returns the matching email (the per-request confidential identity) or None if no
    allowlisted user's key matches. Fail-closed: if FRAN_MCP_KEY_SECRET is unset, ALWAYS returns None
    (and logs a warning) so no key can authorize on a misconfigured deployment."""
    if not presented_key:
        return None
    if _secret() is None:
        log.warning("FRAN_MCP_KEY_SECRET is not set — confidential MCP keys disabled (fail closed).")
        return None
    presented = presented_key.strip()
    for email in _allowed_users():
        if hmac.compare_digest(derive_key(email), presented):
            return email
    return None
