"""FRAN-QC data layer (staff only): the Hive health-scan history + LIVE Postgres health.

Two very different read paths live here:

  1. qc_overview() reads the two scan tables (delimp_qc_scan / delimp_qc_event) through the normal
     db.query() path. Both are in db._INTERNAL_TABLES, so only an internal request can reach them.

  2. db_health() reads ONLY pg_catalog / pg_stat_* views and server functions (no corpus rows). Those
     are not tables the allowlist can name, so they go through _pg_cursor(), a separate helper that
     takes a connection from db._POOL. This deliberately bypasses db._assert_allowlisted. That is safe
     because (a) every statement below is a FIXED string constant in this file, no user input is ever
     interpolated or even accepted, (b) the pooled session is read-only, and (c) the caller (main.py)
     gates the route on db.is_full(). Query text of other sessions is never selected.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from . import db

GIB = 1024 ** 3
OS_OVERHEAD = 5 * GIB
PER_METRIC_TIMEOUT_MS = 8000
WRAPAROUND_LIMIT = 2 ** 31


def disk_gb() -> float:
    try:
        return float(os.environ.get("FRAN_PG_DISK_GB", "1600"))
    except ValueError:
        return 1600.0


def est_free_bytes(size_bytes, disk=None):
    if size_bytes is None:
        return None
    return (disk if disk is not None else disk_gb()) * GIB - float(size_bytes) - OS_OVERHEAD


# ---------------------------------------------------------------------------
# Scan history
# ---------------------------------------------------------------------------
def _num(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def _ts(x):
    if isinstance(x, datetime):
        return x if x.tzinfo else x.replace(tzinfo=timezone.utc)
    if isinstance(x, str):
        try:
            d = datetime.fromisoformat(x.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def compute_growth(history: list[dict], now: datetime | None = None, disk: float | None = None,
                   window_days: int = 14) -> dict:
    """Least-squares GB/day of DB size over the last `window_days` of scan history, and the days until
    the estimated free space hits 0. history items: {"scanned_at", "size_bytes"} (oldest first)."""
    out = {"gb_per_day": None, "days_to_full": None, "est_free_bytes": None, "n_points": 0,
           "window_days": window_days}
    pts = [(_ts(h.get("scanned_at")), _num(h.get("size_bytes"))) for h in history or []]
    pts = [(t, s) for t, s in pts if t is not None and s is not None]
    if not pts:
        return out
    pts.sort(key=lambda p: p[0])
    out["est_free_bytes"] = est_free_bytes(pts[-1][1], disk)
    ref = now or pts[-1][0]
    pts = [p for p in pts if p[0] >= ref - timedelta(days=window_days)]
    out["n_points"] = len(pts)
    if len(pts) < 2:
        return out
    t0 = pts[0][0]
    xs = [(t - t0).total_seconds() / 86400.0 for t, _ in pts]
    ys = [s for _, s in pts]
    if max(xs) < 0.5:
        return out
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return out
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den   # bytes/day
    out["gb_per_day"] = slope / GIB
    if slope > 0 and out["est_free_bytes"] is not None:
        out["days_to_full"] = max(out["est_free_bytes"], 0) / slope
    return out


def _recent_vals(group) -> dict:
    out = {}
    if isinstance(group, dict):
        for k, v in group.items():
            if isinstance(v, dict):
                r = _num(v.get("recent"))
                if r is not None:
                    out[str(k)] = r
    return out


def qc_overview() -> dict:
    empty = {"latest": None, "history": [], "events": [], "growth": compute_growth([]),
             "note": "no scans yet"}
    try:
        latest = db.query(
            "SELECT id, scanned_at, kind, n_red, n_amber, findings, metrics, job_id, host "
            "FROM delimp_qc_scan ORDER BY scanned_at DESC LIMIT 1",
            tables=["delimp_qc_scan"], fetch="one")
    except Exception as e:  # noqa: BLE001  table missing / DB down -> empty state, never a 500
        empty["note"] = f"no scans yet ({type(e).__name__})"
        return empty
    try:
        rows = db.query(
            "SELECT scanned_at, kind, n_red, n_amber, metrics->'db'->>'size_bytes' AS size_bytes, "
            "metrics->'fill' AS fill, metrics->'coverage' AS coverage "
            "FROM delimp_qc_scan ORDER BY scanned_at DESC LIMIT 120",
            tables=["delimp_qc_scan"]) or []
    except Exception:  # noqa: BLE001
        rows = []
    history = [{"scanned_at": r["scanned_at"], "kind": r["kind"], "n_red": r["n_red"],
                "n_amber": r["n_amber"], "size_bytes": _num(r.get("size_bytes")),
                "fill": _recent_vals(r.get("fill")), "coverage": _recent_vals(r.get("coverage"))}
               for r in reversed(rows)]
    try:
        events = db.query(
            "SELECT id, at, source, kind, summary FROM delimp_qc_event ORDER BY at DESC LIMIT 200",
            tables=["delimp_qc_event"]) or []
    except Exception:  # noqa: BLE001
        events = []
    disk = None
    try:
        disk = _num(((latest or {}).get("metrics") or {}).get("db", {}).get("disk_gb"))
    except AttributeError:
        pass
    return {"latest": latest, "history": history, "events": events,
            "growth": compute_growth(history, disk=disk), "note": None if latest else "no scans yet"}


# ---------------------------------------------------------------------------
# Live DB health (fixed SQL, pg_catalog only — see module docstring)
# ---------------------------------------------------------------------------
@contextmanager
def _pg_cursor():
    with db._POOL.connection() as conn:   # read-only, autocommit
        cur = conn.cursor()
        try:
            yield cur
        finally:
            try:
                cur.execute("SET statement_timeout = 30000")
            except Exception:  # noqa: BLE001
                pass
            cur.close()


def _rows(cur, sql: str) -> list[dict]:
    cur.execute("SET statement_timeout = %s", (PER_METRIC_TIMEOUT_MS,))
    cur.execute(sql)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


_SQL = {
    "size": "SELECT pg_database_size(current_database()) AS size_bytes, current_database() AS name",
    "relations": """
        SELECT c.relname AS name, pg_total_relation_size(c.oid) AS total_bytes,
               pg_relation_size(c.oid) AS heap_bytes, pg_indexes_size(c.oid) AS index_bytes,
               COALESCE(pg_total_relation_size(c.reltoastrelid), 0) AS toast_bytes,
               c.reltuples::bigint AS row_estimate
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r','m') AND n.nspname NOT IN ('pg_catalog','information_schema','pg_toast')
        ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 15""",
    "vacuum": """
        SELECT s.relname AS name, s.n_live_tup, s.n_dead_tup,
               CASE WHEN s.n_live_tup + s.n_dead_tup > 0
                    THEN round(100.0 * s.n_dead_tup / (s.n_live_tup + s.n_dead_tup), 2) END AS dead_pct,
               s.last_vacuum, s.last_autovacuum, s.last_analyze, s.last_autoanalyze
        FROM pg_stat_user_tables s ORDER BY pg_total_relation_size(s.relid) DESC LIMIT 15""",
    "wrap_db": "SELECT age(datfrozenxid) AS db_age FROM pg_database WHERE datname = current_database()",
    "wrap_tables": """
        SELECT c.relname AS name, age(c.relfrozenxid) AS xid_age
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r','m') AND n.nspname NOT IN ('pg_catalog','information_schema')
        ORDER BY age(c.relfrozenxid) DESC LIMIT 10""",
    "freeze_max": "SELECT current_setting('autovacuum_freeze_max_age')::bigint AS freeze_max_age",
    "conn_states": """
        SELECT COALESCE(state, 'unknown') AS state, count(*) AS n
        FROM pg_stat_activity WHERE datname = current_database() GROUP BY 1 ORDER BY 2 DESC""",
    "conn_age": """
        SELECT extract(epoch FROM now() - min(xact_start)) AS oldest_xact_s,
               extract(epoch FROM now() - min(query_start) FILTER (WHERE state = 'active'
                       AND pid <> pg_backend_pid())) AS longest_active_s,
               count(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_txn
        FROM pg_stat_activity WHERE datname = current_database()""",
    "conn_apps": """
        SELECT COALESCE(NULLIF(application_name, ''), '(none)') AS application_name,
               COALESCE(state, 'unknown') AS state, count(*) AS n
        FROM pg_stat_activity WHERE datname = current_database()
        GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 15""",
    "locks": "SELECT count(*) AS waiting FROM pg_locks WHERE NOT granted",
    "dbstat": """
        SELECT blks_hit, blks_read,
               CASE WHEN blks_hit + blks_read > 0
                    THEN round(100.0 * blks_hit / (blks_hit + blks_read), 3) END AS cache_hit_pct,
               xact_commit, xact_rollback, deadlocks, temp_files, temp_bytes, stats_reset
        FROM pg_stat_database WHERE datname = current_database()""",
    "idx_unused": """
        SELECT s.relname AS table_name, s.indexrelname AS index_name,
               pg_relation_size(s.indexrelid) AS index_bytes, s.idx_scan
        FROM pg_stat_user_indexes s JOIN pg_index i ON i.indexrelid = s.indexrelid
        WHERE s.idx_scan = 0 AND NOT i.indisunique AND NOT i.indisprimary
        ORDER BY pg_relation_size(s.indexrelid) DESC LIMIT 15""",
    "server": """
        SELECT version() AS version, pg_postmaster_start_time() AS started_at,
               extract(epoch FROM now() - pg_postmaster_start_time()) AS uptime_s""",
    "bgwriter17": "SELECT * FROM pg_stat_checkpointer",
    "bgwriter": "SELECT * FROM pg_stat_bgwriter",
}


def db_health() -> dict:
    """Live health metrics. Each metric is isolated: failure -> None + an entry in out['unavailable']."""
    out: dict[str, Any] = {"unavailable": {}}
    try:
        cm = _pg_cursor()
        cur = cm.__enter__()
    except Exception as e:  # noqa: BLE001  can't even connect
        out["unavailable"]["connection"] = f"{type(e).__name__}: {str(e).strip()[:160]}"
        return out
    try:
        def m(key, sql_key, shape="rows"):
            try:
                rows = _rows(cur, _SQL[sql_key])
                return rows if shape == "rows" else (rows[0] if rows else None)
            except Exception as e:  # noqa: BLE001
                out["unavailable"][key] = f"{type(e).__name__}: {str(e).strip()[:160]}"
                return None

        size = m("size", "size", "one")
        out["size"] = size
        if size and size.get("size_bytes") is not None:
            d = disk_gb()
            sb = float(size["size_bytes"])
            free = est_free_bytes(sb, d)
            out["disk"] = {"disk_gb": d, "os_overhead_bytes": OS_OVERHEAD, "est_free_bytes": free,
                           "pct_used": round(100.0 * (sb + OS_OVERHEAD) / (d * GIB), 2) if d else None,
                           "note": "disk size is configured (FRAN_PG_DISK_GB), not measured"}
        else:
            out["disk"] = None
        out["relations"] = m("relations", "relations")
        out["vacuum"] = m("vacuum", "vacuum")
        wdb = m("wraparound_db", "wrap_db", "one")
        wtb = m("wraparound_tables", "wrap_tables")
        fz = m("autovacuum_freeze_max_age", "freeze_max", "one")
        out["wraparound"] = {
            "limit": WRAPAROUND_LIMIT,
            "db_age": wdb.get("db_age") if wdb else None,
            "db_pct": round(100.0 * wdb["db_age"] / WRAPAROUND_LIMIT, 2) if wdb and wdb.get("db_age") is not None else None,
            "freeze_max_age": fz.get("freeze_max_age") if fz else None,
            "tables": wtb,
        } if (wdb or wtb) else None
        states = m("connection_states", "conn_states")
        age = m("connection_ages", "conn_age", "one")
        out["connections"] = {"by_state": states, "by_app": m("connection_apps", "conn_apps"),
                              **(age or {})} if (states or age) else None
        lk = m("locks", "locks", "one")
        out["waiting_locks"] = lk.get("waiting") if lk else None
        out["dbstat"] = m("dbstat", "dbstat", "one")
        iu = m("unused_indexes", "idx_unused")
        out["unused_indexes"] = {"indexes": iu,
                                 "stats_reset": (out["dbstat"] or {}).get("stats_reset")} if iu is not None else None
        out["server"] = m("server", "server", "one")
        bg = None
        try:
            bg = _rows(cur, _SQL["bgwriter17"])
        except Exception:  # noqa: BLE001  PG < 17 has no pg_stat_checkpointer
            bg = m("bgwriter", "bgwriter")
        out["checkpoints"] = bg[0] if bg else None
    finally:
        try:
            cm.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass
    return out
