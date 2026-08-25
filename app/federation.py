"""federation.py — FRAN node identity, peer registry and the outbound de-identification boundary.

Implements FEDERATION_DESIGN.md phase 0 (node descriptor + registry) and the trust rules from §4
that phase 1 depends on. Three things live here and nowhere else:

  1. WHO THIS NODE IS         -- from config, per design §1 (node_id is not DB state).
  2. WHO WE WILL TALK TO      -- the git-repo registry, cached to disk, per design §2.
  3. WHAT MAY LEAVE THE NODE  -- the field allowlist + pseudonymisation, per design §4.2.

(3) is the security boundary, so it is written as an allowlist that a serializer cannot bypass:
`public_record()` builds its output by copying permitted keys OUT, never by deleting forbidden keys
from a full row. A denylist fails open the moment someone adds a column; an allowlist fails closed.

PSEUDONYMS ARE SALTED, and that is a deliberate departure from `app/privacy.py`. That module hashes a
raw path with unsalted SHA-1 to make `run-a3f2c1.d`, which is right for a human browsing a public
page. It is NOT right against a peer program: raw filenames are drawn from a small, guessable space
(instrument prefix, date, well position), so an unsalted digest can be confirmed by hashing guesses
at scale. With a per-node secret salt the same guess proves nothing. Design §4.2 requires "opaque run
ids"; unsalted SHA-1 is obfuscated, not opaque.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any, Iterable

# ── 1. node identity (config, not DB) ────────────────────────────────────────────────────────────
NODE_ID = os.environ.get("FRAN_NODE_ID", "").strip().lower()
NODE_DISPLAY_NAME = os.environ.get("FRAN_NODE_DISPLAY_NAME", "").strip()
NODE_BASE_URL = os.environ.get("FRAN_NODE_BASE_URL", "").strip().rstrip("/")
NODE_CONTACT = os.environ.get("FRAN_NODE_CONTACT", "").strip()
NODE_LICENSE = os.environ.get("FRAN_NODE_LICENSE", "CC-BY-4.0").strip()
NODE_CITATION = os.environ.get("FRAN_NODE_CITATION", "").strip()

# The JSON contract version this node speaks (design §5). Bump only when response SHAPES change.
CONTRACT_VERSION = "fran-public-1"

# Sharing is OPT-IN. A fresh install federates nothing until an operator sets a policy.
#   closed      — refuse every federation request (default)
#   counts-only — presence and additive aggregates, no per-observation detail
#   public-tier — the public tier as app/db.py already defines it
SHARE_POLICY = os.environ.get("FRAN_SHARE_POLICY", "closed").strip().lower()
_VALID_POLICIES = ("closed", "counts-only", "public-tier")

# Per-node secret. Salts every pseudonym that leaves this node; never transmitted, never logged.
# Absent => pseudonymisation is not safe => sharing is refused (see enabled()).
_SALT = os.environ.get("FRAN_FED_SALT", "")


def configured() -> tuple[bool, str]:
    """Is this node safely shareable? Returns (ok, reason-if-not). Checked before ANY outbound data."""
    if SHARE_POLICY not in _VALID_POLICIES:
        return False, f"FRAN_SHARE_POLICY={SHARE_POLICY!r} is not one of {_VALID_POLICIES}"
    if SHARE_POLICY == "closed":
        return False, "this node is closed to federation (set FRAN_SHARE_POLICY)"
    if not NODE_ID:
        return False, "FRAN_NODE_ID is not set"
    if not _SALT or len(_SALT) < 32:
        # Refuse rather than fall back to an unsalted hash: a weak pseudonym looks identical to a
        # strong one in every response, so the failure would be invisible.
        return False, "FRAN_FED_SALT is missing or too short (>=32 chars); refusing to pseudonymise"
    return True, ""


def enabled() -> bool:
    return configured()[0]


# ── 2. pseudonymisation ──────────────────────────────────────────────────────────────────────────
def pseudonym(kind: str, local_value: str) -> str:
    """Stable, namespaced, SALTED id for something local. `kind` separates the domains so a run and
    a search that happen to share a string do not collide.

    Stable within a node (so repeated queries agree and aggregates can be joined) and meaningless
    outside it. Rotating FRAN_FED_SALT breaks linkage to everything shared before, by design.
    """
    if not local_value:
        return ""
    mac = hmac.new(_SALT.encode(), f"{kind}\x00{local_value}".encode(), hashlib.sha256).hexdigest()
    return f"{NODE_ID}:{kind}-{mac[:16]}"        # namespaced per design §1


# ── 3. the outbound field allowlist ──────────────────────────────────────────────────────────────
# EXACTLY the keys that may cross the wire. Adding a column to delimp_precursors does NOT make it
# shareable; someone has to add it here, on purpose.
ALLOWED_FIELDS: frozenset[str] = frozenset({
    # peptide identity — the point of federating
    "stripped_seq", "modified_seq_proforma", "charge",
    # measurement
    "precursor_mz", "rt", "irt", "im", "iim", "q_value", "pg_q_value",
    "intensity", "normalized_intensity",
    # annotation
    "protein_group", "gene", "organism_name", "organism_taxon_id",
    # acquisition context — structured metadata only (design §4.2)
    "platform", "acquisition_method", "instrument_model", "gradient_minutes",
    "engine", "engine_version",
    # attribution / identity, all pseudonymous
    "node_id", "run", "search", "contract_version",
})

# Named so the guard test can assert on them, and so the reason is recorded next to the rule.
FORBIDDEN_FIELDS: frozenset[str] = frozenset({
    # free text carrying customer, project, PI, grant codes (design §4.2)
    "raw_path", "raw_basename", "hive_path", "search_name", "output_dir", "fasta_path",
    "report_path", "project", "client", "pi", "campus", "scope", "service_customer",
    "customer_contact", "raw_files_json", "notes",
    # a bare local id is worse than useless across nodes: two labs' run 4471 merge silently
    "id", "search_id", "raw_name_anonymized",
    # identifies the physical instrument, i.e. the lab
    "instrument_serial",
    # LIMS linkage
    "coreomics_submission_id", "sample_submission_id", "submission_id",
})


def public_record(row: dict[str, Any], *, run_local: str = "", search_local: str = "") -> dict[str, Any]:
    """Build ONE outbound record. Copies allowed keys out; never deletes forbidden ones from a row.

    That direction matters. A denylist over `dict(row)` ships every column somebody adds later; an
    allowlist ships only what was reviewed. This function is the entire egress boundary, so it is
    written to fail closed.
    """
    ok, why = configured()
    if not ok:
        raise PermissionError(f"refusing to build a federated record: {why}")
    out = {k: row[k] for k in ALLOWED_FIELDS if k in row and k not in ("node_id", "run", "search",
                                                                       "contract_version")}
    out["node_id"] = NODE_ID
    out["contract_version"] = CONTRACT_VERSION
    if run_local:
        out["run"] = pseudonym("run", run_local)
    if search_local:
        out["search"] = pseudonym("search", search_local)
    return out


def assert_clean(records: Iterable[dict[str, Any]]) -> None:
    """Belt-and-braces egress check. Cheap, and it turns a serializer mistake into an exception here
    rather than a disclosure at the far end."""
    for r in records:
        bad = set(r) & FORBIDDEN_FIELDS
        if bad:
            raise PermissionError(f"federated record carries forbidden field(s): {sorted(bad)}")
        extra = set(r) - ALLOWED_FIELDS
        if extra:
            raise PermissionError(f"federated record carries non-allowlisted field(s): {sorted(extra)}")


# ── 4. node descriptor (design §1) ───────────────────────────────────────────────────────────────
def node_descriptor(corpus: dict[str, Any] | None = None,
                    coverage: dict[str, Any] | None = None,
                    fran_version: str = "") -> dict[str, Any]:
    """The whole handshake, per design §1. Public, unauthenticated, cheap, cacheable.

    `capabilities` is what makes version skew survivable: a peer asks for what we SAY we can do and
    degrades cleanly, instead of probing endpoints and interpreting 404s.
    """
    caps = ["search.peptide", "search.protein"]
    if SHARE_POLICY in ("counts-only", "public-tier"):
        caps += ["presence.filter", "aggregate.additive"]
    return {
        "node_id": NODE_ID,
        "display_name": NODE_DISPLAY_NAME or NODE_ID,
        "base_url": NODE_BASE_URL,
        "contact": NODE_CONTACT,
        "fran_version": fran_version,
        "schema_version": CONTRACT_VERSION,
        "share_policy": SHARE_POLICY,
        "capabilities": caps,
        "corpus": corpus or {},
        "coverage": coverage or {},
        "license": NODE_LICENSE,
        "citation": NODE_CITATION,
        "updated_at": int(time.time()),
    }


# ── 5. registry: a git repo, cached to disk (design §2) ──────────────────────────────────────────
REGISTRY_URL = os.environ.get(
    "FRAN_REGISTRY_URL",
    "https://raw.githubusercontent.com/stan-proteomics/fran-registry/main/registry.json")
REGISTRY_CACHE = os.environ.get("FRAN_REGISTRY_CACHE", "/tmp/fran_registry.json")
REGISTRY_TTL = int(os.environ.get("FRAN_REGISTRY_TTL", "3600"))
_ALLOW = {x.strip().lower() for x in os.environ.get("FRAN_PEERS_ALLOW", "").split(",") if x.strip()}
_DENY = {x.strip().lower() for x in os.environ.get("FRAN_PEERS_DENY", "").split(",") if x.strip()}


def load_registry(fetch: bool = True) -> dict[str, Any]:
    """Registry nodes, from cache when fresh. Registry down != federation down (design §2 rule 3):
    a stale cache is always preferred to an empty list, and a fetch failure is never fatal."""
    cached, age = None, None
    try:
        st = os.stat(REGISTRY_CACHE)
        age = time.time() - st.st_mtime
        with open(REGISTRY_CACHE) as fh:
            cached = json.load(fh)
    except (OSError, ValueError):
        pass
    if cached is not None and age is not None and age < REGISTRY_TTL:
        return cached
    if fetch:
        try:
            import urllib.request
            with urllib.request.urlopen(REGISTRY_URL, timeout=10) as r:
                data = json.loads(r.read().decode())
            tmp = REGISTRY_CACHE + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(data, fh)
            os.replace(tmp, REGISTRY_CACHE)          # atomic: never leave a half-written cache
            return data
        except Exception:                            # noqa: BLE001 - never fatal
            pass
    return cached or {"registry_version": 1, "nodes": []}


def peers() -> list[dict[str, Any]]:
    """Peers we are willing to query. Listing yourself in the registry does not entitle you to be
    queried here (design §2 rule 4): the local admin still gates it, and we never query ourselves."""
    out = []
    for n in load_registry().get("nodes", []):
        nid = (n.get("node_id") or "").lower()
        if not nid or nid == NODE_ID:
            continue
        if nid in _DENY:
            continue
        if _ALLOW and nid not in _ALLOW:
            continue
        out.append(n)
    return out
