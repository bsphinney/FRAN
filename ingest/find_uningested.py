"""find_uningested.py — find search output dirs on Hive/Flinders that are NOT in delimp_searches.

READ-ONLY. It proposes; it never ingests. The auto-ingest cron consumes its output.

WHY THIS IS NOT "compare output_dir against the filesystem". Measured 2026-08-25 over 2,011
searches, delimp_searches.output_dir roots are:

    R:\\  1,084   K:\\  574   B:\\  104   /Volumes  66   D:\\  63   A:\\  51
    /quobyte  28  E:\\  20    S:\\  13   /nfs/lssc0/flinders  5    C:\\  3

The corpus was almost entirely ingested FROM WINDOWS, so it records Windows drive paths. A scanner
walking /nfs/lssc0/flinders from Hive and matching on the path string would find essentially every
directory "missing" and propose re-ingesting ~1,700 searches -- each one a delete+copy against a
434M-row table. That is the failure mode this file exists to avoid.

So a candidate is only reported when it misses on EVERY key we can match on:

  1. normalized path   drive letters mapped to their POSIX mount, backslashes and case folded
  2. terminal dir name the search folder's own name
  3. search_name       what corpus_ingest stored as the human name

Key 3 is what catches the FRAN_reports tree. /FRAN_reports/<name>/<timestamp>/ holds RE-EXPORTS of
searches whose corpus row records the ORIGINAL .sne path, so no path mapping can ever match them --
only the name can. win-1 confirmed this the hard way: all 1,871 regen-queue searches were already in
delimp_searches, 0 truly un-ingested.

Being wrong in the safe direction: a false NEGATIVE (missing a genuinely un-ingested search) costs a
search that stays un-ingested until someone notices. A false POSITIVE costs a re-ingest of live
data. They are not symmetric, so this matches loosely on purpose.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Only R:\Data is an established equivalence (verified via engine-version backfill: the same search
# is spelled R:\Data\... on Windows and /nfs/lssc0/flinders/proteomics/Data/... on Hive). The other
# drive letters are win-1/win-2 local disks that Hive cannot see at all; they are folded to a bare
# basename match rather than given a fake mount point.
DRIVE_MAP = {
    # R:\Data maps to <mount>/Data, so the value must NOT itself end in /data or the
    # remainder doubles it (R:\Data\lab -> .../proteomics/data/data/lab).
    "r:": "/nfs/lssc0/flinders/proteomics",
}

DEFAULT_ROOTS = [
    "/nfs/lssc0/flinders/proteomics/Data/FRAN_reports",
    "/quobyte/proteomics-grp/brett",
]

# Marker files that identify an engine's output directory. Ordered most- to least-specific:
# FragPipe's tree also contains a DIA-NN report, so it must be tested first or every FragPipe dir
# would be ingested as DIA-NN and silently lose the diaTracer/MSFragger provenance.
ENGINE_MARKERS = [
    ("fragpipe", ["dia-quant-output/report.tsv", "fragpipe.fp-manifest"]),
    ("radiant", ["search_provenance.json", "radiant_results/fulcrum-results",
                 "fulcrum-results/_SUCCESS"]),
    ("diann", ["report.parquet", "report.tsv"]),
    ("spectronaut", ["RunSummaries"]),
]
_SN_REPORT = re.compile(r"_Report.*\.(tsv|parquet)$", re.I)

# A leading Spectronaut export timestamp, "20260402_103129_". The same search is spelled with and
# without it depending on who wrote the string: delimp_searches stores
# "20260402_103129_SpN_WeimbsUCSB-ZiruiZeng-musMIPs_mar26" while the FRAN_reports folder is just
# "SpN_WeimbsUCSB-ZiruiZeng-musMIPs_mar26", and a re-export nests BOTH
# ("20260625_082325_20260622_143758_SpN_Michaelides-..."). Matching only the literal string reported
# an already-ingested search as missing, so every name is indexed under all of its stripped forms.
_TS_PREFIX = re.compile(r"^\d{8}_\d{4,6}_")


def name_keys(s: str) -> set[str]:
    """A name plus each form with leading export timestamps peeled off."""
    out, cur_ = set(), str(s or "").strip().lower()
    cur_ = re.sub(r"\.sne$", "", cur_)
    for _ in range(3):                      # at most two nested timestamps observed; 3 is slack
        if not cur_:
            break
        out.add(cur_)
        nxt = _TS_PREFIX.sub("", cur_)
        if nxt == cur_:
            break
        cur_ = nxt
    out.discard("")
    return out


def norm_path(p: str) -> str:
    s = str(p or "").replace("\\", "/").strip().rstrip("/").lower()
    m = re.match(r"^([a-z]:)(/.*)?$", s)
    if m:
        mapped = DRIVE_MAP.get(m.group(1))
        s = (mapped + (m.group(2) or "")) if mapped else s
    return s


def known_keys(conn):
    """Every string the corpus already knows a search by."""
    cur = conn.cursor()
    cur.execute("SELECT output_dir, search_name FROM delimp_searches")
    paths, names, bases = set(), set(), set()
    for od, name in cur.fetchall():
        if od:
            n = norm_path(od)
            paths.add(n)
            bases.add(n.rsplit("/", 1)[-1])
        if name:
            names |= name_keys(name)
        if od:
            names |= name_keys(norm_path(od).rsplit("/", 1)[-1])
    return paths, names, bases


def detect_engine(d: str):
    try:
        entries = set(os.listdir(d))
    except OSError:
        return None
    for engine, markers in ENGINE_MARKERS:
        for mk in markers:
            if os.path.exists(os.path.join(d, mk)):
                return engine
    if any(_SN_REPORT.search(e) for e in entries):
        return "spectronaut"
    return None


def scan(roots, paths, names, bases, max_depth=3, limit=0):
    found, seen = [], 0
    for root in roots:
        if not os.path.isdir(root):
            print(f"  [skip] no such root: {root}", flush=True)
            continue
        base_depth = root.rstrip("/").count("/")
        for dirpath, dirnames, _ in os.walk(root):
            if dirpath.count("/") - base_depth >= max_depth:
                dirnames[:] = []
            seen += 1
            engine = detect_engine(dirpath)
            if not engine:
                continue
            dirnames[:] = []                      # a search dir's children are its own outputs
            n = norm_path(dirpath)
            leaf = n.rsplit("/", 1)[-1]
            parent = n.rsplit("/", 2)[-2] if n.count("/") >= 2 else ""
            lk, pk = name_keys(leaf), name_keys(parent)
            hit = ("path" if n in paths else
                   "leaf-name" if lk & (bases | names) else
                   "parent-name" if pk & (bases | names) else None)
            if hit:
                continue
            found.append({"dir": dirpath, "engine": engine})
            if limit and len(found) >= limit:
                return found, seen
    return found, seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--roots", nargs="*", default=DEFAULT_ROOTS)
    ap.add_argument("--max-depth", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json-out", help="write candidates as JSON (the cron's input)")
    a = ap.parse_args()

    import corpus_ingest as ci
    conn = ci._conn()
    paths, names, bases = known_keys(conn)
    print(f"corpus knows {len(paths):,} paths, {len(names):,} names, {len(bases):,} dir basenames")

    found, seen = scan(a.roots, paths, names, bases, a.max_depth, a.limit)
    print(f"walked {seen:,} directories under {len(a.roots)} root(s)")
    print(f"\n=== {len(found)} candidate(s) not matched by path OR name ===")
    by_engine = {}
    for f in found:
        by_engine[f["engine"]] = by_engine.get(f["engine"], 0) + 1
    for e, n in sorted(by_engine.items(), key=lambda x: -x[1]):
        print(f"  {e:<12} {n}")
    for f in found[:40]:
        print(f"  {f['engine']:<12} {f['dir']}")
    if len(found) > 40:
        print(f"  ... and {len(found)-40} more")

    if a.json_out:
        with open(a.json_out, "w") as fh:
            json.dump(found, fh, indent=1)
        print(f"\nwrote {a.json_out}")
    conn.close()


if __name__ == "__main__":
    main()
