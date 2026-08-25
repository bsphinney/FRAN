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

REPORTS_ROOT = "/nfs/lssc0/flinders/proteomics/Data/FRAN_reports"

DEFAULT_ROOTS = [
    # The drop box: the proteomics skill SYMLINKS finished search results here. Entries are links
    # to the real output dirs, which is why the walk below sets followlinks=True -- os.walk does NOT
    # descend into a symlinked directory by default, so without it every dropped result would be
    # listed and never looked inside.
    "/quobyte/proteomics-grp/fran/incoming",
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


# Directory names never worth descending into. Bruker .d and Thermo .raw "files" are DIRECTORIES
# holding hundreds of entries each, and there are ~21,000 of them: walking inside is pure cost, and
# a search output never lives within one. Without this a full-tree sweep spends most of its time in
# raw data.
# NOT pruned: ".sne". A Spectronaut experiment folder IS a legitimate search location -- the corpus
# records output_dirs like S:\sne_storage\<name>.sne -- and pruning it would remove it from
# dirnames, so os.walk would never yield it and detect_engine would never see it. Silently losing a
# whole engine's layout is a worse cost than descending into a few of them.
_PRUNE_SUFFIX = (".d", ".raw", ".wiff", ".wiff2", ".mzml", ".mzxml", ".lance")
_PRUNE_NAME = {".snapshot", ".git", "__pycache__", ".Trash", "lost+found", ".ipynb_checkpoints"}


def prune(dirnames: list[str]) -> None:
    """Drop directories os.walk should not descend into. Mutates in place, as os.walk requires."""
    dirnames[:] = [d for d in dirnames
                   if d not in _PRUNE_NAME and not d.lower().endswith(_PRUNE_SUFFIX)]


def detect_engine(d: str, dirnames=None, filenames=None):
    """Which engine's output this directory is, or None.

    Takes os.walk's own dirnames/filenames when available. That matters at full-tree scale: calling
    os.listdir() again per directory doubles the metadata traffic over a network filesystem holding
    millions of entries, for information the walk already has."""
    if filenames is None or dirnames is None:
        try:
            entries = os.listdir(d)
        except OSError:
            return None
        filenames = entries
        dirnames = entries
    files, dirs = set(filenames), set(dirnames)
    for engine, markers in ENGINE_MARKERS:
        for mk in markers:
            if "/" in mk:                       # nested marker, e.g. dia-quant-output/report.tsv
                head = mk.split("/", 1)[0]
                if head in dirs and os.path.exists(os.path.join(d, mk)):
                    return engine
            elif mk in files or mk in dirs:
                return engine
    if any(_SN_REPORT.search(e) for e in files):
        return "spectronaut"
    return None


def scan(roots, paths, names, bases, max_depth=3, limit=0, engines=None):
    found, seen = [], 0
    for root in roots:
        if not os.path.isdir(root):
            print(f"  [skip] no such root: {root}", flush=True)
            continue
        base_depth = root.rstrip("/").count("/")
        # followlinks=True is required for the incoming/ drop box (see DEFAULT_ROOTS). Safe here
        # only because max_depth bounds the walk -- following links without a depth cap can loop
        # forever on a link that points at an ancestor.
        for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
            prune(dirnames)
            if dirpath.count("/") - base_depth >= max_depth:
                dirnames[:] = []
            seen += 1
            if seen % 200000 == 0:
                print(f"  ...{seen:,} dirs walked, {len(found)} candidates", flush=True)
            engine = detect_engine(dirpath, dirnames, filenames)
            if not engine or (engines and engine not in engines):
                continue
            dirnames[:] = []                      # a search dir's children are its own outputs
            n = norm_path(dirpath)
            # A dropped result is reachable by two names: the symlink in incoming/ and the real
            # directory. Match on BOTH, and record the real one as the identity -- otherwise the
            # same search ingested via the link would look un-ingested when the scan later reaches
            # its real location, and would be ingested a second time under a different output_dir.
            real = os.path.realpath(dirpath)
            rn = norm_path(real)
            leaf = n.rsplit("/", 1)[-1]
            parent = n.rsplit("/", 2)[-2] if n.count("/") >= 2 else ""
            lk, pk = name_keys(leaf) | name_keys(rn.rsplit("/", 1)[-1]), name_keys(parent)
            hit = ("path" if n in paths or rn in paths else
                   "leaf-name" if lk & (bases | names) else
                   "parent-name" if pk & (bases | names) else None)
            if hit:
                continue
            found.append({"dir": dirpath, "engine": engine,
                          "real": real if real != dirpath else None})
            if limit and len(found) >= limit:
                return found, seen
    return found, seen


def scan_sne(roots, paths, names, bases, max_depth=12, limit=0):
    """Find Spectronaut .sne EXPERIMENTS with no corresponding search in the corpus.

    A different problem from scan(): an .sne is the experiment archive itself, not an output
    directory, so no engine-marker test will ever see one. It is also not directly ingestable --
    corpus_ingest needs a REPORT, and only Spectronaut on Windows can export one
    (`manageSNE -rs FRAN.rs`). So this reports candidates to SHIP to a Windows node, and checks
    whether a report already exists before recommending that.

    Matching is by name, because that is what survives the round trip: the corpus records a search
    by its .sne basename (e.g. "20260824_123640_sn1 Taha entrapment.sne" -> search_name
    "20260824_123640_sn1 Taha entrapment"), while the .sne itself may sit on a different mount
    entirely from where it was ingested."""
    found, seen = [], 0
    for root in roots:
        if not os.path.isdir(root):
            print(f"  [skip] no such root: {root}", flush=True)
            continue
        base_depth = root.rstrip("/").count("/")
        for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
            prune(dirnames)
            if dirpath.count("/") - base_depth >= max_depth:
                dirnames[:] = []
            seen += 1
            # .sne appears as a FILE in most layouts and as a DIRECTORY in some, so check both.
            for entry in list(filenames) + [d for d in dirnames if d.lower().endswith(".sne")]:
                if not entry.lower().endswith(".sne"):
                    continue
                full = os.path.join(dirpath, entry)
                stem = entry[:-4]
                if name_keys(stem) & (names | bases) or norm_path(full) in paths:
                    continue
                try:
                    st = os.stat(full)
                    size = st.st_size if os.path.isfile(full) else sum(
                        os.path.getsize(os.path.join(dp, f))
                        for dp, _, fs in os.walk(full) for f in fs)
                except OSError:
                    size = -1
                found.append({"sne": full, "name": stem, "bytes": size,
                              "has_report": _report_nearby(dirpath, stem)})
                if limit and len(found) >= limit:
                    return found, seen
    return found, seen


def _report_nearby(d: str, stem: str) -> bool:
    """Is a Spectronaut report already sitting next to this .sne (or in FRAN_reports under its
    name)? If so it does not need a Windows round trip -- it needs ingesting."""
    try:
        for f in os.listdir(d):
            if _SN_REPORT.search(f) and stem.lower() in f.lower():
                return True
    except OSError:
        pass
    for cand in (os.path.join(REPORTS_ROOT, stem), os.path.join(d, stem)):
        if os.path.isdir(cand):
            try:
                for sub in os.listdir(cand):
                    if _SN_REPORT.search(sub):
                        return True
                    if os.path.isdir(os.path.join(cand, sub)) and any(
                            _SN_REPORT.search(x) for x in os.listdir(os.path.join(cand, sub))):
                        return True
            except OSError:
                pass
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--roots", nargs="*", default=DEFAULT_ROOTS)
    ap.add_argument("--max-depth", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json-out", help="write candidates as JSON (the cron's input)")
    ap.add_argument("--engines", default="", help="comma-separated engines to keep (default: all)")
    ap.add_argument("--find-sne", action="store_true",
                    help="find .sne experiments with no search in the corpus (needs a Windows "
                         "export before it can be ingested)")
    a = ap.parse_args()

    import corpus_ingest as ci
    conn = ci._conn()
    paths, names, bases = known_keys(conn)
    print(f"corpus knows {len(paths):,} paths, {len(names):,} names, {len(bases):,} dir basenames")

    if a.find_sne:
        found, seen = scan_sne(a.roots, paths, names, bases, a.max_depth, a.limit)
        print(f"walked {seen:,} directories under {len(a.roots)} root(s)")
        need_win = [f for f in found if not f["has_report"]]
        have_rep = [f for f in found if f["has_report"]]
        tot = sum(f["bytes"] for f in found if f["bytes"] > 0)
        print(f"\n=== {len(found)} .sne experiment(s) with no search in the corpus "
              f"({tot/1e12:.2f} TB) ===")
        print(f"  {len(have_rep)} already have a report on disk -> INGEST, no Windows trip needed")
        print(f"  {len(need_win)} have no report -> ship to a Windows box for manageSNE -rs FRAN.rs")
        for f in sorted(found, key=lambda x: -x["bytes"])[:40]:
            gb = f["bytes"] / 1e9
            print(f"  {'REPORT' if f['has_report'] else 'NEEDS-WIN':<10} {gb:>8.1f} GB  {f['sne']}")
        if len(found) > 40:
            print(f"  ... and {len(found)-40} more")
        if a.json_out:
            with open(a.json_out, "w") as fh:
                json.dump(found, fh, indent=1)
            print(f"\nwrote {a.json_out}")
        conn.close()
        return

    want = {e.strip() for e in a.engines.split(",") if e.strip()} or None
    if want:
        print(f"engines: {sorted(want)}")
    found, seen = scan(a.roots, paths, names, bases, a.max_depth, a.limit, want)
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
