"""Filename privacy for the PUBLIC corpus browser.

Raw acquisition filenames encode customer / sample / project info (PII for a core
facility). On the public site they are replaced server-side with a stable,
non-identifying hash label (run-a3f2c1.d) — the real names never reach a public
client. A core-facility INTERNAL KEY (env DELIMP_INTERNAL_KEY, sent as the
X-Internal-Key header or ?internal_key=) reveals the real names for customer
management + analysis. Default (no key configured / not provided) = always sanitized.
"""
import contextvars
import hashlib
import os

_reveal = contextvars.ContextVar("reveal", default=False)
INTERNAL_KEY = os.environ.get("DELIMP_INTERNAL_KEY", "")

# JSON keys whose string values are raw filenames/paths to sanitize on the public site.
_FILE_KEYS = {"raw_path", "raw_basename", "run", "file", "filename", "fasta_path"}
# Keys that are search/project NAMES (can encode customer/project) -> hashed label.
_NAME_KEYS = {"search_name", "project"}
_EXTS = (".d", ".raw", ".wiff", ".wiff2", ".mzml", ".mzxml", ".tsv", ".parquet", ".dia")


def set_reveal(v: bool) -> None:
    _reveal.set(bool(v))


def get_reveal() -> bool:
    return _reveal.get()


def key_ok(provided: str | None) -> bool:
    # constant-time-ish compare; only valid if a key is actually configured
    return bool(INTERNAL_KEY) and bool(provided) and \
        hashlib.sha256((provided or "").encode()).digest() == hashlib.sha256(INTERNAL_KEY.encode()).digest()


def sanitize_name(name: str) -> str:
    if not isinstance(name, str) or not name:
        return name
    base = name.replace("\\", "/").split("/")[-1]
    ext = next((e for e in _EXTS if base.lower().endswith(e)), "")
    h = hashlib.sha1(name.encode()).hexdigest()[:6]   # stable, non-identifying
    return f"run-{h}{ext}"


def sanitize_search_name(name: str) -> str:
    if not isinstance(name, str) or not name:
        return name
    return f"search-{hashlib.sha1(name.encode()).hexdigest()[:6]}"


def redact(obj, reveal: bool):
    """Recursively replace raw-filename + search/project-name string values with
    sanitized labels, unless reveal (internal key present)."""
    if reveal:
        return obj
    if isinstance(obj, list):
        return [redact(x, reveal) for x in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _FILE_KEYS and isinstance(v, str):
                out[k] = sanitize_name(v)
            elif k in _NAME_KEYS and isinstance(v, str):
                out[k] = sanitize_search_name(v)
            else:
                out[k] = redact(v, reveal)
        return out
    return obj
