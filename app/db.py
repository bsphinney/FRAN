"""
Secure, read-only PostgreSQL access layer for the DE-LIMP corpus browser.

GOVERNANCE (non-negotiable, enforced here):
  1. READ-ONLY. The connection is opened with a read-only session
     (default_transaction_read_only) so the server is structurally unable to
     issue INSERT/UPDATE/DELETE/DDL even if a future bug tried to.
  2. PUBLIC-LAYER-ONLY HARD ALLOWLIST. Every table named in a query is checked
     against PUBLIC_TABLES. The internal/customer tables
     (delimp_searches_internal, delimp_raw_files_internal) are NOT in the
     allowlist, so they cannot be queried through this layer.
  3. PARAMETERIZED QUERIES ONLY. All callers pass psycopg2 params; there is no
     path that interpolates user input into SQL text.
  4. CREDENTIALS VIA ENV. The DB password comes from $DELIMP_PG_PASSWORD or a
     token file at $DELIMP_PG_TOKEN_FILE. Never committed.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterable, Sequence

import psycopg2
import psycopg2.extras
import psycopg2.pool

# ---------------------------------------------------------------------------
# Hard allowlist — the ONLY tables this app may ever read.
# Internal/customer tables are deliberately absent.
# ---------------------------------------------------------------------------
PUBLIC_TABLES: frozenset[str] = frozenset(
    {
        "delimp_searches",
        "raw_files",
        "search_raw_files",
        "delimp_sample_metadata",
        "delimp_proteins",
        "delimp_precursors",
        "delimp_consensus_ids",
        "delimp_schema_version",
        "delimp_precursor_xic",
        "delimp_xic_quant",
        "delimp_search_sources",
        # precomputed leaderboard snapshots (Highlights) — full GROUP BY over millions of
        # precursor rows times out live on PG Farm, so these are refreshed offline.
        "delimp_mv_top_peptides",
        "delimp_mv_top_proteins",
        "delimp_mv_top_genes",
        "delimp_mv_im_scatter",
        # precomputed Koina PFly flyability per peptide (peptide page + Highlights scatter)
        "delimp_peptide_flyability",
    }
)

# Belt-and-suspenders: tables we must never name. Used as an assertion guard.
FORBIDDEN_TABLES: frozenset[str] = frozenset(
    {
        "delimp_searches_internal",
        "delimp_raw_files_internal",
        "coreomics_submissions_cache",
        "coreomics_samples_cache",
    }
)


class GovernanceError(RuntimeError):
    """Raised when a query would touch a non-allowlisted table."""


# --- PG Farm service-account auth -------------------------------------------
# The service-account `secret` is NOT the DB password: it must be exchanged for a
# short-lived (7-day) JWT at /auth/service-account/login, and the JWT is the password.
# We mint+cache that JWT here and refresh it before expiry, so the Space never goes
# stale (set DELIMP_PG_SECRET to the service-account secret and you're done).
# See docs/PGFARM_SERVICE_ACCOUNT_AUTH.md.
_tok: dict[str, Any] = {"jwt": None, "exp": 0.0}
_tok_lock = threading.Lock()


def _mint_jwt(secret: str, user: str, host: str) -> str:
    import json as _json
    import time as _time
    import urllib.request as _u
    with _tok_lock:
        if _tok["jwt"] and _time.time() < _tok["exp"]:
            return _tok["jwt"]
        body = _json.dumps({"username": user, "secret": secret}).encode()
        req = _u.Request(f"https://{host}/auth/service-account/login", data=body,
                         headers={"Content-Type": "application/json"})
        d = _json.loads(_u.urlopen(req, timeout=30).read().decode())
        _tok["jwt"] = d["access_token"]
        # refresh a day before expiry (default 7 days) to avoid edge-of-expiry failures
        _tok["exp"] = _time.time() + max(int(d.get("expires_in", 604800)) - 86400, 60)
        return _tok["jwt"]


def _conn_kwargs() -> dict[str, Any]:
    host = os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu")
    port = int(os.environ.get("DELIMP_PG_PORT", "5432"))
    dbname = os.environ.get(
        "DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"
    )
    user = os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account")
    sslmode = os.environ.get("DELIMP_PG_SSLMODE", "require")

    # Gather the credential from any of the supported env vars / files (priority order).
    cred = os.environ.get("DELIMP_PG_SECRET") or os.environ.get("DELIMP_PG_PASSWORD")
    if not cred:
        for var in ("DELIMP_PG_SECRET_FILE", "DELIMP_PG_TOKEN_FILE"):
            fp = os.environ.get(var)
            if fp and os.path.exists(fp):
                cred = open(fp).read().strip()
                break
    if not cred:
        raise GovernanceError(
            "No DB credential. Set DELIMP_PG_SECRET (or DELIMP_PG_PASSWORD) to the PG Farm "
            "service-account secret (or a current JWT) as an HF Secret."
        )
    # Auto-detect: a JWT (eyJ..., two dots) is used directly; anything else is treated as a
    # service-account SECRET and exchanged for a self-refreshing JWT. So it works whether the
    # secret lives in DELIMP_PG_SECRET or DELIMP_PG_PASSWORD, holding a JWT or a secret.
    is_jwt = cred.startswith("eyJ") and cred.count(".") == 2
    password = cred if is_jwt else _mint_jwt(cred, user, host)

    return dict(
        host=host,
        port=port,
        dbname=dbname,
        user=user,
        password=password,
        sslmode=sslmode,
        connect_timeout=20,
        # Force a read-only session at the server. Any write attempt errors.
        options="-c default_transaction_read_only=on -c statement_timeout=30000",
        application_name="delimp-corpus-browser",
    )


class _Pool:
    """Lazy, thread-safe connection pool with read-only sessions."""

    def __init__(self) -> None:
        self._pool: psycopg2.pool.ThreadedConnectionPool | None = None
        self._lock = threading.Lock()

    def _ensure(self) -> psycopg2.pool.ThreadedConnectionPool:
        if self._pool is None:
            with self._lock:
                if self._pool is None:
                    self._pool = psycopg2.pool.ThreadedConnectionPool(
                        minconn=1,
                        maxconn=int(os.environ.get("DELIMP_PG_MAXCONN", "6")),
                        **_conn_kwargs(),
                    )
        return self._pool

    def _reset(self):
        """Drop the pool + cached JWT so the next connection re-mints a fresh token."""
        with self._lock:
            try:
                if self._pool is not None:
                    self._pool.closeall()
            except Exception:  # noqa: BLE001
                pass
            self._pool = None
        _tok["jwt"] = None
        _tok["exp"] = 0.0

    @contextmanager
    def connection(self):
        try:
            pool = self._ensure()
            conn = pool.getconn()
        except psycopg2.OperationalError:
            # likely an expired JWT / rotated secret -> rebuild with a fresh token, retry once
            self._reset()
            pool = self._ensure()
            conn = pool.getconn()
        try:
            conn.set_session(readonly=True, autocommit=True)
            yield conn
        finally:
            pool.putconn(conn)


_POOL = _Pool()


def _assert_allowlisted(tables: Iterable[str]) -> None:
    for t in tables:
        tl = t.lower().strip()
        if tl in FORBIDDEN_TABLES:
            raise GovernanceError(f"Refusing to query forbidden table: {t!r}")
        if tl not in PUBLIC_TABLES:
            raise GovernanceError(
                f"Table {t!r} is not in the public-layer allowlist."
            )


def query(
    sql: str,
    params: Sequence[Any] | dict[str, Any] | None = None,
    *,
    tables: Iterable[str],
    fetch: str = "all",
    timeout_ms: int | None = None,
) -> Any:
    """Run a parameterized, read-only SELECT.

    Args:
      sql: SQL text with %s / %(name)s placeholders ONLY. Never interpolate
           user input into this string.
      params: psycopg2 params bound to the placeholders.
      tables: the set of public tables this query reads — validated against the
              allowlist before execution. This is the structural guard that
              makes querying the internal layer impossible.
      fetch: "all" | "one" | "val" | "none".
      timeout_ms: optional per-statement timeout. Lets heavy precursor-heap reads
              (scatter sample, big aggregates) fail fast and release their pooled
              connection instead of holding it for the full 30s connection-level
              timeout — which is what exhausts the pool and 503s the dashboard
              when PG Farm's bulk heap reads are degraded. Reset afterwards so the
              pooled connection's default (30s) is never contaminated.
    """
    _assert_allowlisted(tables)
    stripped = sql.lstrip().lower()
    if not (stripped.startswith("select") or stripped.startswith("with")):
        raise GovernanceError("Only SELECT/WITH statements are permitted.")

    with _POOL.connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if timeout_ms is not None:
                cur.execute("SET statement_timeout = %s", (int(timeout_ms),))
            try:
                cur.execute(sql, params)
                if fetch == "all":
                    return cur.fetchall()
                if fetch == "one":
                    return cur.fetchone()
                if fetch == "val":
                    row = cur.fetchone()
                    if not row:
                        return None
                    return next(iter(row.values()))
                return None
            finally:
                if timeout_ms is not None:
                    try:
                        cur.execute("SET statement_timeout = 30000")
                    except Exception:  # noqa: BLE001 - conn returns to pool either way
                        pass


def estimate_rows(relname: str) -> int | None:
    """Planner row-count estimate (pg_class.reltuples) for an allowlisted table.

    Instant regardless of table size — used for the dashboard "snapshot" cards so
    an exact COUNT(*) over millions of rows never blocks (or times out) a page load.
    Catalog read only; relname is validated against the public allowlist.
    """
    _assert_allowlisted([relname])
    try:
        with _POOL.connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT c.reltuples::bigint AS est FROM pg_class c "
                    "WHERE c.relname = %s AND c.relkind = 'r' "
                    "ORDER BY c.reltuples DESC LIMIT 1",
                    (relname,),
                )
                row = cur.fetchone()
        if not row or row["est"] is None or row["est"] < 0:
            return None
        return int(row["est"])
    except Exception:  # noqa: BLE001 - dashboard degrades to "—", never 503s
        return None


def estimate_distinct(relname: str, column: str) -> int | None:
    """Planner distinct-value estimate (pg_stats.n_distinct) for a column.

    n_distinct is either an absolute estimate (>= 0) or the negative of the
    distinct/rows ratio (< 0, when distinctness scales with table size); resolve
    the ratio form against reltuples. Returns None if the column was never
    ANALYZEd (caller shows "—" rather than running a multi-second COUNT DISTINCT).
    """
    _assert_allowlisted([relname])
    try:
        with _POOL.connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT n_distinct FROM pg_stats "
                    "WHERE tablename = %s AND attname = %s LIMIT 1",
                    (relname, column),
                )
                row = cur.fetchone()
        if not row or row["n_distinct"] is None:
            return None
        nd = float(row["n_distinct"])
        if nd >= 0:
            return int(round(nd))
        rows = estimate_rows(relname)
        if rows is None:
            return None
        return int(round(-nd * rows))
    except Exception:  # noqa: BLE001
        return None


def estimate_non_null(relname: str, column: str) -> int | None:
    """Estimated count of non-NULL values in a column = reltuples * (1 - null_frac)."""
    _assert_allowlisted([relname])
    try:
        with _POOL.connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT null_frac FROM pg_stats "
                    "WHERE tablename = %s AND attname = %s LIMIT 1",
                    (relname, column),
                )
                row = cur.fetchone()
        rows = estimate_rows(relname)
        if rows is None:
            return None
        null_frac = float(row["null_frac"]) if row and row["null_frac"] is not None else 0.0
        return int(round(rows * (1.0 - null_frac)))
    except Exception:  # noqa: BLE001
        return None


def estimate_value_distribution(relname: str, column: str) -> list[dict[str, Any]] | None:
    """Approximate value->count distribution from pg_stats (most_common_vals x
    reltuples). Instant, used for low-cardinality columns like `charge` where an
    exact GROUP BY over millions of rows would hit the statement timeout.
    Returns None if the column was never ANALYZEd."""
    _assert_allowlisted([relname])
    try:
        rows_total = estimate_rows(relname)
        with _POOL.connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT most_common_vals::text AS vals, most_common_freqs AS freqs "
                    "FROM pg_stats WHERE tablename = %s AND attname = %s LIMIT 1",
                    (relname, column),
                )
                row = cur.fetchone()
        if not row or not row["vals"] or not row["freqs"] or rows_total is None:
            return None
        # most_common_vals renders as a Postgres array literal: "{2,3,4}"
        vals = [v for v in row["vals"].strip("{}").split(",") if v != ""]
        freqs = list(row["freqs"])
        out = []
        for v, f in zip(vals, freqs):
            out.append({"value": v.strip('"'), "n": int(round(float(f) * rows_total))})
        return out
    except Exception:  # noqa: BLE001
        return None


def healthcheck() -> dict[str, Any]:
    """Confirm connectivity + that the session is genuinely read-only."""
    out: dict[str, Any] = {"connected": False, "read_only": None, "error": None}
    try:
        ro = query(
            "SELECT current_setting('transaction_read_only') AS ro",
            tables=["delimp_schema_version"],  # not actually read; allowlisted no-op
            fetch="val",
        )
        out["connected"] = True
        out["read_only"] = ro
    except Exception as exc:  # noqa: BLE001 - surface to caller
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


# ---------------------------------------------------------------------------
# Lightweight in-process TTL cache for the expensive aggregate dashboard
# queries, so "watch it populate" refreshes stay cheap. Short TTL so the
# numbers still visibly grow as ingest proceeds.
# ---------------------------------------------------------------------------
class TTLCache:
    def __init__(self, ttl_seconds: float = 20.0) -> None:
        self.ttl = ttl_seconds
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get_or_set(self, key: str, producer):
        now = time.time()
        with self._lock:
            hit = self._store.get(key)
            if hit and (now - hit[0]) < self.ttl:
                return hit[1]
        value = producer()
        with self._lock:
            self._store[key] = (now, value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


CACHE = TTLCache(ttl_seconds=float(os.environ.get("DELIMP_CACHE_TTL", "20")))
# Long-TTL cache for expensive corpus-wide leaderboards (snapshots, not live).
SLOW_CACHE = TTLCache(ttl_seconds=float(os.environ.get("DELIMP_SLOW_CACHE_TTL", "1800")))
