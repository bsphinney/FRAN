"""Shared plumbing for the data-audit repair scripts in this directory.

Every script here is a DRY RUN unless it is given --apply. The dry run opens its connection
READ-ONLY at the session level, so even a bug in a script cannot write. Statements are scoped by
search_id batches (or, for raw_files, by raw_path batches) so none needs a full scan of
delimp_precursors (1.09 billion rows) or delimp_proteins (97 million).
"""
from __future__ import annotations

import argparse
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))      # ingest/


def base_parser(desc: str) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=desc)
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="report what would change; this is the DEFAULT and writes nothing")
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Off by default; the owner runs this after reviewing a dry run.")
    ap.add_argument("--batch", type=int, default=25, help="searches per statement (default 25)")
    return ap


def connect(apply: bool):
    from fran_queue import _conn
    conn = _conn()
    if apply:
        conn.autocommit = False
    else:
        conn.set_session(readonly=True, autocommit=True)     # a dry run CANNOT write
    cur = conn.cursor()
    cur.execute("SET statement_timeout = '240s'")
    return conn, cur


def all_search_ids(cur) -> list[str]:
    cur.execute("SELECT id FROM delimp_searches ORDER BY ingested_at")
    return [r[0] for r in cur.fetchall()]


def pick(ids: list, sample: int, seed: int = 20261124) -> tuple[list, bool]:
    """(ids to examine, is_estimate). sample=0 or >= len(ids) means every search (exact)."""
    if not sample or sample >= len(ids):
        return list(ids), False
    return random.Random(seed).sample(ids, sample), True


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def show_examples(title: str, examples: list, n: int = 3) -> None:
    print(f"  examples ({title}):")
    for e in examples[:n]:
        print("    ", e)
    if not examples:
        print("     (none)")
