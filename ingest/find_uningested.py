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
from datetime import datetime

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

# The drop box. The proteomics skill's fran_deposit.py stages each finished search here as a
# DIRECTORY holding fran_manifest.json plus symlinks to the real report files (read_manifest below
# is the contract). Earlier entries were bare symlinks to the output dir, which is why the walk
# sets followlinks=True -- os.walk does NOT descend into a symlinked directory by default.
DROPBOX_ROOT = "/quobyte/proteomics-grp/fran/incoming"

DEFAULT_ROOTS = [
    DROPBOX_ROOT,
    "/nfs/lssc0/flinders/proteomics/Data/FRAN_reports",
]
# /quobyte/proteomics-grp/brett WAS a root until 2026-09-08. It is a personal working directory,
# and scanning it returns 72 candidates that are overwhelmingly experiments rather than customer
# searches: 13 parameter-sweep variants of ONE dataset under siegel_glp1_2026-08-12/diag/
# (ms2_40_ms1_7, ms2_50_ms1_10, ...), engine-tuning runs under radiant_orbitrap/, teaching data
# under short_course_data/, plus engine_comparison/ and fran_species_proof/. Ingesting those as
# distinct searches would corrupt every corpus count the same way STAN's QC runs would.
#
# Nothing there was ever actually ingested, because a single stray file --
# brett/20250910_120054_KG-human-2_Report.tsv -- tripped detect_engine's loose _Report test and
# made scan() classify the whole root as one search and prune the descent. That accident was the
# only thing holding the line. Fixing the shadowing without removing this root would have started
# ingesting the sweeps on the next cron tick.
#
# Work in that directory now reaches the corpus by EXPLICIT registration (fran_queue.py add),
# which is one row per real search and cannot be tripped by a stray file.

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
_PRUNE_NAME = {".snapshot", ".git", "__pycache__", ".Trash", "lost+found", ".ipynb_checkpoints",
               ".excluded"}   # incoming/.excluded/: drop-box entries a human set aside (DEPLOY_auto_ingest.md)

# Whole subtrees that produce engine output which is NOT a corpus search. Without these a full-tree
# sweep returns 4,493 "candidates", of which 4,427 are these: STAN writes a DIA-NN report.parquet
# for EVERY QC run (3,147 under STAN/processing alone, plus per-instrument trees), and smoke-test
# and scratch directories look identical to a real search from the outside. Ingesting QC runs as
# customer searches would corrupt every corpus count.
DEFAULT_EXCLUDES = [
    "/quobyte/proteomics-grp/STAN/",           # STAN QC — a separate system with its own database
    "/quobyte/proteomics-grp/hela_qcs/",       # QC watcher output
    "/quobyte/proteomics-grp/brett/v1_smoke",  # smoke tests
    "/quobyte/proteomics-grp/brett/glendon/",  # our scratch/working dir
    "/Data/lab/ToFEvoQC/",                     # instrument QC series, not customer searches
]


def excluded(path: str, patterns) -> bool:
    return any(pat in path for pat in patterns)


# QC runs are not customer searches -- the reason DEFAULT_EXCLUDES exists. That list recognises QC
# by WHERE it lives, which says nothing about a drop-box entry: every entry lives in incoming/,
# whatever it is. So a staged search is also recognised by what it is CALLED: "QC" as a standalone
# token ("… Lumos QC", "QC_run_01", "hela_qc_2", "Exploris QC2"), never inside a word ("aqc…",
# "QCM…" -- the lookahead refuses a following letter, not a digit).
#
# Deliberately narrower than the lab's raw-FILE classifier, STAN's DEFAULT_QC_PATTERN
# (stan/watcher/qc_filter.py), whose HeLa branch would also drop customer searches OF HeLa samples,
# which the corpus holds. The skill's stage step uses this IDENTICAL regex and writes its verdict
# into the manifest as "qc" / "qc_rule", so the two repos agree; tests pin the same vectors.
QC_NAME_RE = re.compile(r"(?i)(?<![a-z0-9])qc(?![a-z])")


def qc_reason(path: str, name: str | None = None, qc: bool | None = None,
              exclude: bool | None = None) -> str | None:
    """Why FRAN policy keeps this DROP-BOX search out of the corpus, or None.

    The producer's word wins, in this order:
      1. manifest "qc": true or "exclude": true  -> excluded
      2. manifest "qc": false                    -> NOT excluded, whatever the name (the producer
                                                    overrode a false positive)
      3. no flag: DEFAULT_EXCLUDES on `path` (the substring test scan() applies to every directory
         it walks), then QC_NAME_RE on `name` and on the last three components of `path`.
    Scope: drop-box candidates only (auto_ingest.select). The FRAN_reports scan is NOT name-filtered.
    A hit is never a failure: nothing is attempted, charged or quarantined."""
    if qc is True:
        return "manifest says qc: true"
    if exclude is True:
        return "manifest says exclude: true"
    if qc is False:
        return None
    for pat in DEFAULT_EXCLUDES:
        if pat in (path or ""):
            return f"output_dir is under {pat} (DEFAULT_EXCLUDES)"
    parts = [p for p in str(path or "").replace("\\", "/").split("/") if p][-3:]
    for field, text in [("search_name", name or "")] + [("output_dir", p) for p in parts]:
        if QC_NAME_RE.search(text):
            return f"{field} {text!r} matches QC_NAME_RE"
    return None


MANIFEST = "fran_manifest.json"
MANIFEST_VERSIONS = (1,)
ENGINES = ("diann", "spectronaut", "fragpipe", "radiant")


def _staged_epoch(v):
    """A manifest's staged_at as epoch seconds, or None. Accepts epoch numbers and ISO 8601 (a naive
    ISO time is read as local time; for oldest-first ordering an hour either way does not matter)."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    try:
        return float(str(v).strip())
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(str(v).strip().replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def read_manifest(d: str, detected_engine: str | None = None):
    """(manifest, None) for a drop-box entry's VALID fran_manifest.json, else (None, why-not).

    THE DROP-BOX CONTRACT, read in this one place: scan() uses it to recognise an already-ingested
    entry, auto_ingest to take the entry's identity. fran_deposit.py stages a real directory, so
    realpath() of the entry is the drop box itself and cannot say where the search lives; the
    manifest's output_dir is the search's identity (search_id = uuid5(namespace, output_dir)).
    Validation is strict for that reason: a manifest that cannot name its output_dir, or names an
    engine the directory does not look like, is SKIPPED with this reason -- never guessed at, and
    never charged as an ingest failure.

    Returns output_dir (no trailing slash), engine, search_name, organism, taxon (digits, as str),
    qc / exclude (True/False, or None when the producer did not say -- see qc_reason), fasta_path and
    staged_by (or None), and staged_at (epoch seconds, or None -- callers fall back to the
    manifest's mtime)."""
    try:
        with open(os.path.join(d, MANIFEST), encoding="utf-8") as fh:
            m = json.load(fh)
    except FileNotFoundError:
        return None, f"no {MANIFEST}"
    except (OSError, ValueError) as e:
        return None, f"{MANIFEST} unreadable ({type(e).__name__})"
    if not isinstance(m, dict):
        return None, f"{MANIFEST} is not a JSON object"
    ver = m.get("fran_manifest_version", 1)
    if ver not in MANIFEST_VERSIONS:
        return None, f"{MANIFEST} version {ver!r} is not one this reader understands"
    od = m.get("output_dir")
    if not isinstance(od, str) or not od.strip() or not os.path.isabs(od.strip()):
        return None, f"{MANIFEST} has no absolute output_dir"
    eng = m.get("engine")
    if eng is not None:
        if not isinstance(eng, str) or eng.strip().lower() not in ENGINES:
            return None, f"{MANIFEST} names an unknown engine {eng!r}"
        eng = eng.strip().lower()
        if detected_engine and eng != detected_engine:
            return None, (f"{MANIFEST} says engine {eng!r}, the directory looks like "
                          f"{detected_engine!r}")
    out = {"output_dir": od.strip().rstrip("/"), "engine": eng or detected_engine}
    for k in ("search_name", "organism"):
        v = m.get(k)
        if v is not None and (not isinstance(v, str) or not v.strip()):
            return None, f"{MANIFEST} field {k!r} is not a non-empty string"
        out[k] = v.strip() if v is not None else None
    tx = m.get("taxon")
    if tx is None or tx == "":
        out["taxon"] = None
    elif isinstance(tx, bool) or not str(tx).strip().isdigit():
        return None, f"{MANIFEST} taxon {tx!r} is not a numeric NCBI taxon id"
    else:
        out["taxon"] = str(tx).strip()
    for k in ("qc", "exclude"):
        v = m.get(k)
        if v is not None and not isinstance(v, bool):
            return None, f"{MANIFEST} {k} {v!r} is not true/false"
        out[k] = v
    for k in ("fasta_path", "staged_by"):
        v = m.get(k)
        out[k] = v.strip() if isinstance(v, str) and v.strip() else None
    out["staged_at"] = _staged_epoch(m.get("staged_at"))
    return out, None


def prune(dirnames: list[str]) -> None:
    """Drop directories os.walk should not descend into. Mutates in place, as os.walk requires."""
    dirnames[:] = [d for d in dirnames
                   if d not in _PRUNE_NAME and not d.lower().endswith(_PRUNE_SUFFIX)]


def detect_engine(d: str, dirnames=None, filenames=None, loose: bool = True):
    """Which engine's output this directory is, or None.

    `loose=False` disables ONLY the trailing _SN_REPORT filename heuristic, keeping the explicit
    per-engine marker tests. scan() passes it for a configured ROOT, because a root that gets
    classified is never descended into (`dirnames[:] = []`) and therefore hides its whole subtree.
    A single stray file, /quobyte/proteomics-grp/brett/20250910_120054_KG-human-2_Report.tsv, was
    enough to make that root report as one Spectronaut search and skip all 246 entries beneath it
    -- among them PROT_0793, two finished DIA-NN searches that had never been ingested.

    The explicit markers stay live at a root on purpose: pointing --roots straight at one search
    directory is a normal way to use this tool, and that still resolves.

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
    if loose and any(_SN_REPORT.search(e) for e in files):
        return "spectronaut"
    return None


def scan(roots, paths, names, bases, max_depth=3, limit=0, engines=None, excludes=None):
    found, seen = [], 0
    excludes = DEFAULT_EXCLUDES if excludes is None else excludes
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
            if excluded(dirpath, excludes):
                dirnames[:] = []
                continue
            # A root is only classifiable by an EXPLICIT marker. See detect_engine's `loose`.
            at_root = os.path.normpath(dirpath) == os.path.normpath(root)
            engine = detect_engine(dirpath, dirnames, filenames, loose=not at_root)
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
            # A drop-box entry lives at incoming/<name>__<hash>/, which no corpus row records; the
            # row records the manifest's output_dir. Without this, a search that was staged AND
            # ingested another way (GallPlasCer/GallPlasStrap: queue rows Q6/Q7, 2026-09-08) stays
            # a candidate forever -- and corpus_ingest does not refuse an existing output_dir, it
            # deletes and re-inserts the whole search.
            if MANIFEST in filenames:
                man, _ = read_manifest(dirpath)
                if man and norm_path(man["output_dir"]) in paths:
                    print(f"  [drop box] {dirpath}: already in the corpus as "
                          f"{man['output_dir']}; skipped", flush=True)
                    continue
            found.append({"dir": dirpath, "engine": engine,
                          "real": real if real != dirpath else None})
            if limit and len(found) >= limit:
                return found, seen
    return found, seen


def scan_sne(roots, paths, names, bases, max_depth=12, limit=0, excludes=None):
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
            if excluded(dirpath, excludes or []):
                dirnames[:] = []
                continue
            # .sne appears as a FILE in most layouts and as a DIRECTORY in some, so check both.
            for entry in list(filenames) + [d for d in dirnames if d.lower().endswith(".sne")]:
                if not entry.lower().endswith(".sne"):
                    continue
                full = os.path.join(dirpath, entry)
                stem = entry[:-4]
                if not stem:
                    # The entry is a directory literally named ".sne" -- Spectronaut's internal
                    # store INSIDE an experiment folder, not an experiment archive called
                    # "<name>.sne". The experiment is the PARENT directory, and its name is what the
                    # corpus knows. Matching on the empty stem compared "" against every search name
                    # and reported 41 already-ingested experiments as missing.
                    stem = os.path.basename(dirpath.rstrip("/"))
                    full = dirpath
                if not stem:
                    continue
                if name_keys(stem) & (names | bases) or norm_path(full) in paths:
                    continue
                try:
                    st = os.stat(full)
                    size = st.st_size if os.path.isfile(full) else sum(
                        os.path.getsize(os.path.join(dp, f))
                        for dp, _, fs in os.walk(full) for f in fs)
                except OSError:
                    size = -1
                rep = _find_report(dirpath, stem, full)
                found.append({"sne": full, "name": stem, "bytes": size,
                              "has_report": bool(rep), "report": rep})
                if limit and len(found) >= limit:
                    return found, seen
    return found, seen


def _find_report(d: str, stem: str, sne_path: str):
    """Path to an existing Spectronaut report for this .sne, or None.

    Worth being thorough: a hit here means the search can be ingested ON HIVE right now, and a miss
    means shipping tens of GB to a Windows box to re-export it. Four places, most to least specific:

      1. INSIDE the .sne, when it is a directory (Spectronaut writes exports there)
      2. beside the .sne, filename carrying the stem  -- unambiguous when several .sne share a dir
      3. FRAN_reports/<stem>/<timestamp>/            -- where the archive pull puts them
      4. beside the .sne, any report at all          -- only when this is the ONLY .sne in the
                                                        directory, so it cannot be misattributed
    """
    # An empty stem is catastrophic here, not merely useless: os.path.join(REPORTS_ROOT, "")
    # collapses to REPORTS_ROOT itself, so the archive-root probe returns an ARBITRARY report, and
    # `stem in filename` is true for every file. A pre-fix scan produced 41 entries with an empty
    # stem and 21 of them were assigned the SAME unrelated Mouse_Brains report -- which would have
    # ingested one report 21 times under 21 different search identities. Refuse outright.
    if not str(stem).strip():
        return None

    def _reports_in(path, depth=1):
        try:
            entries = os.listdir(path)
        except OSError:
            return None
        for e in entries:
            if _SN_REPORT.search(e):
                full = os.path.join(path, e)
                try:
                    if os.path.getsize(full) > 1024:
                        return full
                except OSError:
                    continue
        if depth > 0:
            for e in entries:
                sub = os.path.join(path, e)
                if os.path.isdir(sub):
                    got = _reports_in(sub, depth - 1)
                    if got:
                        return got
        return None

    if os.path.isdir(sne_path):
        got = _reports_in(sne_path)
        if got:
            return got
    try:
        siblings = os.listdir(d)
    except OSError:
        siblings = []
    for e in siblings:
        if _SN_REPORT.search(e) and stem.lower() in e.lower():
            return os.path.join(d, e)
    got = _reports_in(os.path.join(REPORTS_ROOT, stem))
    if got:
        return got
    n_sne = sum(1 for e in siblings if e.lower().endswith(".sne"))
    if n_sne == 1:
        for e in siblings:
            if _SN_REPORT.search(e):
                return os.path.join(d, e)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--roots", nargs="*", default=DEFAULT_ROOTS)
    ap.add_argument("--max-depth", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json-out", help="write candidates as JSON (the cron's input)")
    ap.add_argument("--engines", default="", help="comma-separated engines to keep (default: all)")
    ap.add_argument("--exclude", nargs="*", default=None,
                    help="path substrings to skip (default: STAN QC, smoke and scratch trees)")
    ap.add_argument("--find-sne", action="store_true",
                    help="find .sne experiments with no search in the corpus (needs a Windows "
                         "export before it can be ingested)")
    a = ap.parse_args()

    import corpus_ingest as ci
    conn = ci._conn()
    paths, names, bases = known_keys(conn)
    print(f"corpus knows {len(paths):,} paths, {len(names):,} names, {len(bases):,} dir basenames")

    if a.find_sne:
        found, seen = scan_sne(a.roots, paths, names, bases, a.max_depth, a.limit,
                               DEFAULT_EXCLUDES if a.exclude is None else a.exclude)
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
            print(f"  {'INGESTABLE' if f['has_report'] else 'NEEDS-WIN':<11} {gb:>8.1f} GB  {f['sne']}")
            if f.get("report"):
                print(f"  {'':>11} {'':>8}     report: {f['report']}")
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
    ex = DEFAULT_EXCLUDES if a.exclude is None else a.exclude
    print(f"excluding {len(ex)} subtree(s): {ex}")
    found, seen = scan(a.roots, paths, names, bases, a.max_depth, a.limit, want, ex)
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
