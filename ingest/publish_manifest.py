#!/usr/bin/env python3
"""publish_manifest.py — record what the CURRENT ingest code is, so a node can tell it is stale.

WHY CONTENT-ADDRESSED AND NOT A VERSION CONSTANT. On 2026-09-16 both the repo and the Windows-node
share declared CORPUS_INGEST_VERSION = "1.3.0" while the files differed: spectronaut_to_corpus.py
had gained the whole PTM localization block with no version bump, because there is no adapter
version constant at all. versions.py's own docstring warns that "a version that lags the code is
worse than no version, because it is trusted". A constant-based gate would have passed that.

WHY THE MANIFEST LIVES IN PG FARM AND NOT ON THE SHARE. A manifest file shipped alongside the code
is circular: a node that did not sync holds an OLD manifest that matches its OLD files, and nothing
notices. The database is the one thing both sides see, and a node holds no copy of it.
"""
from __future__ import annotations
import hashlib, os, socket, subprocess, sys

INGEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, INGEST_DIR)

# Scripts whose staleness silently corrupts the corpus. A stale adapter writes rows that look fine
# and need re-ingesting later, which is strictly worse than not running.
REFUSE_FILES = {"corpus_ingest.py", "spectronaut_to_corpus.py", "diann_to_corpus.py", "versions.py"}


def file_md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def git_sha() -> str:
    return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                   cwd=INGEST_DIR, text=True).strip()


def assert_clean_tree() -> None:
    """Refuse to publish from a dirty tree.

    Publishing a hash for uncommitted code records an expectation no other machine can reach: the
    node is then told it is stale against a git_sha that does not exist anywhere but this laptop.
    """
    # cwd is already INGEST_DIR, so the pathspec is "." (not "ingest" -- that would resolve to the
    # nonexistent ingest/ingest and silently report clean no matter what is actually dirty).
    out = subprocess.check_output(["git", "status", "--porcelain", "--", "."],
                                  cwd=INGEST_DIR, text=True).strip()
    if out:
        raise SystemExit("refusing to publish: ingest/ has uncommitted changes:\n" + out)


def manifest_rows():
    sha, host = git_sha(), socket.gethostname()
    for name in sorted(os.listdir(INGEST_DIR)):
        if not name.endswith(".py"):
            continue
        yield (name, file_md5(os.path.join(INGEST_DIR, name)), sha, host,
               "refuse" if name in REFUSE_FILES else "warn")


def main() -> int:
    assert_clean_tree()
    from coreomics_import import _conn
    rows = list(manifest_rows())
    with _conn() as cn, cn.cursor() as cur:
        for f, md5, sha, host, gate in rows:
            cur.execute("""INSERT INTO delimp_ingest_manifest
                             (file, md5, git_sha, published_by, gate, published_at)
                           VALUES (%s,%s,%s,%s,%s, now())
                           ON CONFLICT (file) DO UPDATE SET
                             md5=EXCLUDED.md5, git_sha=EXCLUDED.git_sha,
                             published_by=EXCLUDED.published_by, gate=EXCLUDED.gate,
                             published_at=EXCLUDED.published_at""",
                        (f, md5, sha, host, gate))
        cn.commit()
    print(f"published {len(rows)} ingest files at {rows[0][2]}")
    print(f"  refuse-gated: {sorted(f for f,_,_,_,g in rows if g=='refuse')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
