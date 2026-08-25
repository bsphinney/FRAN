"""federation_guard.py — bound what a peer can extract, not just how fast they can ask.

THE THREAT this addresses is not a burst; it is patience. `app/ratelimit.py` is per-worker,
in-process and windowed in seconds, so it stops a tight loop. It does not stop a peer that asks for
one peptide per second and walks the corpus over a month, because every individual request is
reasonable. Nothing in a per-request check can see that pattern.

So there are four independent controls here, and they fail in different ways on purpose:

  1. SHAPE     — a query must be specific. No wildcards, no empty selectors, no "list everything".
                 Blocks enumeration by API design rather than by measurement.
  2. RESPONSE  — a hard row cap per response. Bounds the blast radius of any single call.
  3. BUDGET    — a cumulative, DURABLE per-peer row allowance per day and per month. This is the
                 control that actually bounds total exfiltration, and the only one that survives a
                 restart, multiple workers, and a slow attacker.
  4. NOVELTY   — enumeration looks different from research. Research repeats and clusters
                 (the same peptides, proteins and organisms come up again); enumeration is a stream
                 of never-before-seen keys. A peer whose queries are almost all novel across a large
                 volume is walking the corpus, whatever its rate.

Every decision is written to delimp_federation_access_log, which is also where spend is measured
from — so the budget can never disagree with the audit trail.

Defaults are deliberately generous for research and hostile to bulk transfer. Tune per peer in
delimp_federation_quota rather than by raising the global default, which is how a limit gets
loosened once and never tightened.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

# ── policy (env-tunable; per-peer overrides live in delimp_federation_quota) ─────────────────────
MAX_ROWS_PER_RESPONSE = int(os.environ.get("FRAN_FED_MAX_ROWS", "1000"))
ROWS_PER_DAY = int(os.environ.get("FRAN_FED_ROWS_PER_DAY", "200000"))
ROWS_PER_MONTH = int(os.environ.get("FRAN_FED_ROWS_PER_MONTH", "2000000"))
# A peptide lookup must be a real peptide, not a probe that matches a large slice of the corpus.
MIN_SEQ_LEN = int(os.environ.get("FRAN_FED_MIN_SEQ_LEN", "6"))
# Novelty: below this many requests in the window the ratio is statistical noise.
NOVELTY_MIN_REQUESTS = int(os.environ.get("FRAN_FED_NOVELTY_MIN_REQUESTS", "500"))
NOVELTY_MAX_RATIO = float(os.environ.get("FRAN_FED_NOVELTY_MAX_RATIO", "0.98"))

_AA = re.compile(r"^[ACDEFGHIKLMNPQRSTVWY]+$")

# Versioned so "does this node have working extraction limits?" is answerable from the node
# descriptor rather than by reading its source. Every denial is also written to
# delimp_federation_access_log, so the enforcing version is recoverable after the fact.
try:
    from ingest.versions import FEDERATION_GUARD_VERSION as __version__
except Exception:                                                        # noqa: BLE001
    __version__ = "0.1.0"


class Denied(Exception):
    """Refusal, with a machine-readable reason so an honest peer can adapt instead of retrying."""
    def __init__(self, reason: str, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.reason, self.message, self.retry_after = reason, message, retry_after


# ── 1. shape ─────────────────────────────────────────────────────────────────────────────────────
def check_shape(endpoint: str, q: dict[str, Any]) -> None:
    """Reject queries whose SHAPE is enumeration. This is the cheapest and most important control:
    if the API cannot express "give me everything", no budget has to catch it later."""
    if endpoint in ("peptide", "presence"):
        seq = (q.get("stripped_seq") or q.get("seq") or "").strip().upper()
        if not seq:
            raise Denied("deny_shape", "a peptide sequence is required; there is no 'list all' form")
        if len(seq) < MIN_SEQ_LEN:
            raise Denied("deny_shape",
                         f"sequence too short ({len(seq)}<{MIN_SEQ_LEN}): a short prefix matches a "
                         f"large slice of the corpus and is enumeration, not a lookup")
        if not _AA.match(seq):
            # Wildcards/regex/SQL metacharacters are how a "lookup" becomes a scan.
            raise Denied("deny_shape", "sequence must be plain amino acids (no wildcards or patterns)")
    elif endpoint == "protein":
        pg = (q.get("protein_group") or q.get("gene") or "").strip()
        if not pg or len(pg) < 3:
            raise Denied("deny_shape", "a protein group or gene of >=3 characters is required")
        if any(c in pg for c in "%*?"):
            raise Denied("deny_shape", "wildcards are not accepted on federated protein lookups")
    else:
        raise Denied("deny_shape", f"endpoint {endpoint!r} is not federated")

    # Offset paging is corpus-walking with extra steps: a peer can page a specific lookup, but not
    # use offset to sweep past what the lookup itself returned.
    if int(q.get("offset") or 0) > 10_000:
        raise Denied("deny_shape", "offset beyond 10000; refine the query instead of paging the corpus")


# ── 2. response cap ──────────────────────────────────────────────────────────────────────────────
def cap_rows(requested: int | None, peer_quota: dict[str, Any] | None = None) -> int:
    hard = (peer_quota or {}).get("max_rows_per_response") or MAX_ROWS_PER_RESPONSE
    want = int(requested or hard)
    return max(1, min(want, hard))


# ── 3. cumulative budget + 4. novelty (both measured from the log) ───────────────────────────────
def query_hash(endpoint: str, q: dict[str, Any]) -> str:
    """Stable hash of the NORMALIZED query, so 'the same question again' is recognisable. Paging
    parameters are excluded: page 2 of a question is the same question, and counting it as novel
    would make honest paging look like enumeration."""
    norm = {k: str(v).strip().upper() for k, v in sorted(q.items())
            if k not in ("limit", "offset") and v not in (None, "")}
    return hashlib.sha256(f"{endpoint}\x00{json.dumps(norm, sort_keys=True)}".encode()).hexdigest()[:32]


def _quota_for(conn, peer: str) -> dict[str, Any]:
    cur = conn.cursor()
    cur.execute("""select rows_per_day, rows_per_month, max_rows_per_response
                   from delimp_federation_quota where peer=%s""", (peer,))
    r = cur.fetchone()
    if not r:
        return {}
    return {"rows_per_day": r[0], "rows_per_month": r[1], "max_rows_per_response": r[2]}


def check_budget(conn, peer: str) -> dict[str, Any]:
    """Enforce the durable per-peer allowance. Raises Denied when spent.

    Spend is SUMmed from the access log rather than kept in a counter: one source of truth, and a
    number an auditor can recompute. Denials are recorded too, so a peer hammering a closed door is
    visible.
    """
    quota = _quota_for(conn, peer)
    day_cap = quota.get("rows_per_day") or ROWS_PER_DAY
    month_cap = quota.get("rows_per_month") or ROWS_PER_MONTH
    cur = conn.cursor()
    cur.execute("""select coalesce(sum(n_rows) filter (where at > now() - interval '1 day'), 0),
                          coalesce(sum(n_rows) filter (where at > now() - interval '30 days'), 0),
                          count(*)          filter (where at > now() - interval '1 day'),
                          count(distinct query_hash) filter (where at > now() - interval '1 day')
                     from delimp_federation_access_log
                    where peer=%s and decision='allow'""", (peer,))
    day_rows, month_rows, day_reqs, day_distinct = cur.fetchone()

    if day_rows >= day_cap:
        raise Denied("deny_budget",
                     f"daily row budget exhausted ({day_rows:,}/{day_cap:,}); resets on a rolling "
                     f"24h window", retry_after=3600)
    if month_rows >= month_cap:
        raise Denied("deny_budget",
                     f"30-day row budget exhausted ({month_rows:,}/{month_cap:,})", retry_after=86400)

    # 4. NOVELTY. A high volume of never-repeated questions is a walk, not a study.
    if day_reqs >= NOVELTY_MIN_REQUESTS:
        ratio = day_distinct / max(1, day_reqs)
        if ratio > NOVELTY_MAX_RATIO:
            raise Denied("deny_shape",
                         f"query pattern looks like enumeration: {day_distinct:,} distinct of "
                         f"{day_reqs:,} requests in 24h ({ratio:.1%} novel). Federation is for "
                         f"lookups, not corpus transfer — contact the node operator for bulk access",
                         retry_after=86400)
    return {"day_rows": day_rows, "day_cap": day_cap,
            "month_rows": month_rows, "month_cap": month_cap,
            "remaining_today": max(0, day_cap - day_rows)}


def record(conn, peer: str, endpoint: str, q: dict[str, Any], n_rows: int,
           decision: str = "allow") -> None:
    """Append to the ledger. Commits on its own connection-level transaction because a denial that
    is not recorded is a denial nobody can investigate."""
    cur = conn.cursor()
    cur.execute("""insert into delimp_federation_access_log (peer, endpoint, query_hash, query,
                                                             n_rows, decision)
                   values (%s,%s,%s,%s,%s,%s)""",
                (peer or "anonymous", endpoint, query_hash(endpoint, q),
                 json.dumps({k: v for k, v in q.items() if k != "limit"}), int(n_rows), decision))
    conn.commit()


def guard(conn, peer: str, endpoint: str, q: dict[str, Any]) -> dict[str, Any]:
    """Full pre-flight: shape, then budget/novelty. Records every refusal before raising."""
    try:
        check_shape(endpoint, q)
        return check_budget(conn, peer)
    except Denied as d:
        try:
            record(conn, peer, endpoint, q, 0, d.reason)
        except Exception:      # noqa: BLE001 - never let logging convert a refusal into a 500
            pass
        raise
