"""The one safe way to open a Bruker ``analysis.tdf``.

A Bruker ``.d`` holds its frame index in ``analysis.tdf`` — a plain SQLite
database — beside the spectra in ``analysis.tdf_bin``. A copy taken off the
instrument can carry a **stale, mid-acquisition ``analysis.tdf-wal``** next to
an otherwise finished ``analysis.tdf``. That makes how we open the file a data
integrity question, not a style question:

  * A **read-write** open makes SQLite checkpoint that stale WAL into the
    finished database and **truncate it to the mid-acquisition size**. The
    frame index is destroyed permanently. The spectra in ``analysis.tdf_bin``
    survive but are no longer addressable, so the run is unrecoverable.
  * A plain **``mode=ro``** open does not truncate, but it reads *through* the
    stale WAL — so instrument metadata and frame counts are silently taken
    from mid-acquisition state — and it drops an ``analysis.tdf-shm`` inside
    the raw ``.d``.
  * **``?mode=ro&immutable=1``** is the only correct open: SQLite reads the
    bytes as they lie on disk, takes no locks and writes nothing. That is
    right for a tdf because nothing legitimately writes one after acquisition.

This already happened at scale: 350 ``.d`` on the cluster are damaged, the
largest single cluster being 63 files in 77 minutes on 2026-04-27.

Ingest walks tens of thousands of ``.d`` on shared storage, so it is exactly
the code that must never get this wrong. Never call ``sqlite3.connect`` on a
tdf directly — call :func:`connect_tdf`. ``tests/test_tdf_immutable_guard.py``
walks the repo and fails on any open that does not.
"""

import os
import sqlite3
from pathlib import Path

__all__ = ["tdf_read_uri", "connect_tdf", "synthetic_tdf_write_uri"]


def _file_uri(path):
    """Absolute ``file:`` URI for ``path``, with URI metacharacters escaped.

    ``Path.as_uri()`` percent-encodes ``?``, ``#`` and ``%``; an f-string does
    not. That is why hand-built URIs are banned here. FRAN ingests real
    submission directories with names operators chose, and a ``.d`` named
    ``Sample#3 50%.d`` turns an f-string URI into a path SQLite reads
    differently — at best an error, at worst a silently-created empty database
    whose GlobalMetadata table does not exist, which ingest would record as a
    raw file with no instrument metadata.
    """
    return Path(os.path.abspath(os.fspath(path))).as_uri()


def tdf_read_uri(path):
    """SQLite URI that reads a Bruker ``analysis.tdf`` without writing anything.

    Args:
        path: Path to the ``analysis.tdf`` file (not the ``.d`` directory).

    Returns:
        A ``file:...?mode=ro&immutable=1`` URI for
        ``sqlite3.connect(..., uri=True)``.
    """
    return _file_uri(path) + "?mode=ro&immutable=1"


def connect_tdf(path, timeout=30.0):
    """Open a Bruker ``analysis.tdf`` read-only and immutable.

    The only sanctioned way to read a tdf. Writes nothing into the ``.d`` — no
    checkpoint, no WAL, no shm — so it is safe against raw data on shared
    storage, including a ``.d`` left carrying a stale WAL by an interrupted
    acquisition.

    Args:
        path: Path to the ``analysis.tdf`` file.
        timeout: Kept for call-site compatibility. An immutable open takes no
            locks, so it never comes into play.

    Returns:
        An open read-only :class:`sqlite3.Connection`.

    Raises:
        sqlite3.Error: If the file is missing or is not a SQLite database.
    """
    return sqlite3.connect(tdf_read_uri(path), uri=True, timeout=timeout)


def synthetic_tdf_write_uri(path):
    """Writable URI for building a **synthetic** tdf in a test fixture.

    A fixture that fabricates a tdf has to write one, so it genuinely needs a
    read-write handle. Routing it through this named constructor is how it
    declares that intent at the call site: the repo guard recognises the name
    and lets that one open through. The exemption is therefore per call site,
    not per file — a real tdf open sneaking into an exempted test file is
    still caught.

    Never call this on a real ``.d``. It is read-write, which is precisely the
    operation that destroys an acquisition's frame index.

    Args:
        path: Path to the synthetic ``analysis.tdf`` to create, under a
            temporary directory.

    Returns:
        A plain ``file:`` URI with no mode parameter (read-write, creating).
    """
    return _file_uri(path)
