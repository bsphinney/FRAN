"""find_missed_sne.py — list the Spectronaut .sne experiments on THIS Windows computer's drives
that FRAN has never ingested.

READ-ONLY: it reads the disks and reads delimp_searches. It writes two files and nothing else.

  py find_missed_sne.py                         # every local fixed drive
  py find_missed_sne.py B:\\ S:\\sne_storage      # only these folders
  py find_missed_sne.py --include-network       # also mapped network drives (R:, K:)

Output, in <share>\\Data\\FRAN_SNE_export\\missed_sne\\ (or --out-dir):

  missed_sne_<HOST>_<YYYYMMDD>.tsv   every .sne found, one row each, with a status
  missed_sne_<HOST>_<YYYYMMDD>.cmd   one `py sne_one.py ...` line per experiment to ingest

STATUS
  MISSED         no search in FRAN under this path OR this name. Ingest it.
  COPY           another copy of a MISSED experiment (same name and same size on another drive or
                 folder). Listed so nothing is hidden; only the first copy gets a command.
  SAME_NAME      FRAN holds a search with this name, recorded from a different path. Almost always
                 the same experiment already ingested from another drive. NOT put in the .cmd file;
                 review these by hand (a re-search saved under an old name would look like this).
  INGESTED       FRAN holds a search recorded from exactly this path.
  TOO_NEW        modified in the last --min-age-hours (default 2): Spectronaut may still be writing.
  QC_OR_TEST     never ingested, but its name or folder says QC or test (find_uningested's QC rule,
                 plus the words test / testing / demo / tutorial). Listed, NOT put in the .cmd file.
                 To ingest one anyway, add its path to missed_sne\\include.txt (one per line).
  EXCLUDED       its path or name is in missed_sne\\exclude.txt (one per line). Never ingested.

WHY NAMES, NOT PATHS. The same .sne sits on several drives (B: project folders, S:\\sne_storage,
the R:/K: share), and FRAN records whichever path it was ingested from. Matching on the path alone
would call every other copy "missed" and ingest it a second time. The matching rule is the one
find_uningested.py uses on Hive (name_keys / norm_path), imported, not re-written.
"""
from __future__ import annotations

import argparse
import csv
import os
import socket
import string
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from find_uningested import name_keys, norm_path, known_keys, prune, name_qc_signal  # noqa: E402 -- the ONE matching rule

# Never worth walking on a Windows system drive, and never a place an experiment is saved.
SKIP_DIRS = {"$recycle.bin", "system volume information", "windows", "program files",
             "program files (x86)", "programdata", "appdata", "$windows.~bt", "$windows.~ws",
             "recovery", "perflogs", "msocache", "config.msi",
             # this pipeline's own export scratch: reports, not experiments
             "fran_sne_one", "fran_sne_export", "fran_winforge_export"}
# Preferred copy when the same experiment is on several drives: the project path (B:) is canonical
# (Brett, 2026-08-21); S:\sne_storage is the mirror.
DRIVE_ORDER = "BADECFGHIJLMNOPQTUVWXYZSRK"


def local_drives(include_network=False):
    """Drive roots to scan. Fixed disks only unless include_network (DRIVE_FIXED=3, DRIVE_REMOTE=4)."""
    if os.name != "nt":
        return []
    import ctypes
    out = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        kind = ctypes.windll.kernel32.GetDriveTypeW(root)
        if kind == 3 or (include_network and kind == 4):
            out.append(root)
    return out


def _depth(path, root):
    rel = os.path.relpath(path, root)
    return 0 if rel in (".", "") else rel.replace("\\", "/").count("/") + 1


def find_sne(roots, max_depth=14, walk=os.walk, progress=None):
    """Every .sne under the roots, as (path, stem). A FILE named <x>.sne, a DIRECTORY named <x>.sne,
    or a directory literally named ".sne" (Spectronaut's store inside an experiment folder, whose
    PARENT is the experiment). Never descends into a .sne directory, a .d or a .raw."""
    found, seen = [], 0
    for root in roots:
        for dirpath, dirnames, filenames in walk(root):
            seen += 1
            if progress and seen % 20000 == 0:
                progress(seen, len(found), dirpath)
            sne_dirs = [d for d in dirnames if d.lower().endswith(".sne")]
            for entry in list(filenames) + sne_dirs:
                if not entry.lower().endswith(".sne"):
                    continue
                stem = entry[:-4]
                full = os.path.join(dirpath, entry)
                if not stem:                       # a directory literally named ".sne"
                    stem = os.path.basename(dirpath.rstrip("\\/"))
                    full = dirpath
                if stem:
                    found.append((full, stem))
            prune(dirnames)
            dirnames[:] = [d for d in dirnames
                           if d.lower() not in SKIP_DIRS and not d.lower().endswith(".sne")]
            if _depth(dirpath, root) >= max_depth:
                dirnames[:] = []
    return found, seen


def _size_mtime(path):
    try:
        if os.path.isfile(path):
            st = os.stat(path)
            return st.st_size, st.st_mtime
        size, mt = 0, 0.0
        for dp, _, fs in os.walk(path):
            for f in fs:
                try:
                    st = os.stat(os.path.join(dp, f))
                    size += st.st_size; mt = max(mt, st.st_mtime)
                except OSError:
                    pass
        return size, mt
    except OSError:
        return -1, 0.0


def _drive_rank(path):
    d = path[:1].upper() if len(path) > 1 and path[1] == ":" else "?"
    i = DRIVE_ORDER.find(d)
    return (i if i >= 0 else len(DRIVE_ORDER), path.lower())


def has_xic_dbs(sne_path, listdir=os.listdir, isdir=os.path.isdir):
    """True when an <anything>_XIC-DBs folder sits beside the .sne (sne_one.py ingests it)."""
    p = sne_path.rstrip("\\/")
    parent = p[:max(p.rfind("\\"), p.rfind("/"))]      # either separator: tests run on POSIX
    try:
        return any(e.lower().endswith("_xic-dbs") and isdir(os.path.join(parent, e))
                   for e in listdir(parent))
    except OSError:
        return False


_TEST_RE = __import__("re").compile(r"(?i)(?<![a-z0-9])(test|testing|tests|demo|tutorial|delete ?me|junk)(?![a-z])")


def qc_or_test(path, stem):
    """Why this experiment looks like QC or a test, or None: the name, or one of the two folders
    above it. find_uningested.name_qc_signal is the corpus's own QC rule; the test words are extra."""
    parts = [p for p in str(path).replace("\\", "/").split("/") if p]
    for text in [stem] + parts[-3:-1]:
        sig = name_qc_signal(text)
        if sig:
            return f"{text!r} {sig}"
        m = _TEST_RE.search(text or "")
        if m:
            return f"{text!r} contains {m.group(1)!r}"
    return None


def read_list(path):
    """Lines of a hand-kept include/exclude file, lower-cased; blank lines and # comments skipped."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return {l.strip().strip('"').lower() for l in fh if l.strip() and not l.lstrip().startswith("#")}
    except OSError:
        return set()


def classify(items, paths, names, bases, now=None, min_age_hours=2.0, include=(), exclude=()):
    """items: dicts with path, stem, bytes, mtime. Adds status, matched_by, n_copies, in place, and
    returns them sorted (MISSED first, largest first). include/exclude: lower-cased paths or names."""
    now = time.time() if now is None else now
    known_names = names | bases
    groups = {}
    for it in items:
        mine = {it["path"].lower(), it["stem"].lower()}
        if norm_path(it["path"]) in paths:
            it["status"], it["matched_by"] = "INGESTED", "path"
        elif name_keys(it["stem"]) & known_names:
            it["status"], it["matched_by"] = "SAME_NAME", "name"
        elif mine & set(exclude):
            it["status"], it["matched_by"] = "EXCLUDED", "exclude.txt"
        elif it["mtime"] and now - it["mtime"] < min_age_hours * 3600:
            it["status"], it["matched_by"] = "TOO_NEW", ""
        elif qc_or_test(it["path"], it["stem"]) and not (mine & set(include)):
            it["status"], it["matched_by"] = "QC_OR_TEST", qc_or_test(it["path"], it["stem"])
        else:
            it["status"], it["matched_by"] = "MISSED", ""
            groups.setdefault((it["stem"].lower(), it["bytes"]), []).append(it)
        it["n_copies"] = 1
    for grp in groups.values():
        grp.sort(key=lambda x: _drive_rank(x["path"]))
        for i, it in enumerate(grp):
            it["n_copies"] = len(grp)
            if i:
                it["status"] = "COPY"
                it["matched_by"] = grp[0]["path"]
    order = {"MISSED": 0, "COPY": 1, "QC_OR_TEST": 2, "TOO_NEW": 3, "EXCLUDED": 4, "SAME_NAME": 5, "INGESTED": 6}
    return sorted(items, key=lambda x: (order[x["status"]], -x["bytes"], x["path"].lower()))


def command_for(it, script="sne_one.py"):
    """The sne_one.py line for one experiment, for a .cmd file. A double quote cannot occur in a
    Windows file name; a percent sign can, and cmd expands it even inside quotes, so it is doubled.
    --name=... so a name that starts with a dash is not read as an option."""
    q = lambda s: str(s).replace("%", "%%")   # noqa: E731
    return f'py "{q(script)}" --sne "{q(it["path"])}" --name="{q(it["stem"])}"'


def write_outputs(items, out_dir, host, day, script="sne_one.py"):
    os.makedirs(out_dir, exist_ok=True)
    tsv = os.path.join(out_dir, f"missed_sne_{host}_{day}.tsv")
    cmd = os.path.join(out_dir, f"missed_sne_{host}_{day}.cmd")
    with open(tsv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(["status", "sne_path", "name", "gb", "modified", "n_copies", "has_xic_dbs", "matched_by"])
        for it in items:
            w.writerow([it["status"], it["path"], it["stem"],
                        f"{it['bytes'] / 1e9:.2f}" if it["bytes"] >= 0 else "",
                        time.strftime("%Y-%m-%d %H:%M", time.localtime(it["mtime"])) if it["mtime"] else "",
                        it["n_copies"], "yes" if it.get("xic") else "no", it["matched_by"]])
    with open(cmd, "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write("@echo off\nchcp 65001 >nul\nrem One line per experiment FRAN has never ingested. Review, then run.\n")
        for it in items:
            if it["status"] == "MISSED":
                fh.write(command_for(it, script) + "\n")
    return tsv, cmd


def _db():
    import psycopg2
    from refresh_leaderboards import _token
    conn = psycopg2.connect(host="pgfarm.library.ucdavis.edu", port=5432,
        dbname="uc-davis-genome-center-proteomics-core/delimp", user="genome-proteomics-service-account",
        password=_token(), sslmode="require", connect_timeout=30, options="-c statement_timeout=300000")
    conn.set_session(readonly=True, autocommit=True)
    return conn


HERE = os.path.dirname(os.path.abspath(__file__))
STATUSES = ("MISSED", "COPY", "QC_OR_TEST", "TOO_NEW", "EXCLUDED", "SAME_NAME", "INGESTED")


def scan(roots=None, include_network=False, max_depth=14, min_age_hours=2.0, out_dir=None):
    """The whole job: walk, match against FRAN, write the list and the .cmd file.
    Returns (items, tsv path, cmd path). Raises SystemExit when there is nothing to scan."""
    here = HERE
    out_dir = out_dir or os.path.join(here, "missed_sne")
    a = argparse.Namespace(max_depth=max_depth, min_age_hours=min_age_hours, out_dir=out_dir)
    roots = list(roots or []) or local_drives(include_network)
    roots = [r for r in roots if os.path.isdir(r)]
    if not roots:
        sys.exit("no folders to scan (give one or more paths)")
    conn = _db()
    paths, names, bases = known_keys(conn)
    conn.close()
    print(f"FRAN knows {len(paths):,} search paths and {len(names):,} names", flush=True)
    print(f"scanning: {', '.join(roots)}", flush=True)

    found, seen = find_sne(roots, a.max_depth, progress=lambda s, n, d: print(
        f"  ... {s:,} folders, {n} .sne so far ({d[:70]})", flush=True))
    items = []
    for path, stem in found:
        size, mt = _size_mtime(path)
        items.append({"path": path, "stem": stem, "bytes": size, "mtime": mt, "xic": has_xic_dbs(path)})
    items = classify(items, paths, names, bases, min_age_hours=a.min_age_hours,
                     include=read_list(os.path.join(a.out_dir, "include.txt")),
                     exclude=read_list(os.path.join(a.out_dir, "exclude.txt")))

    host = socket.gethostname().split(".")[0]
    tsv, cmd = write_outputs(items, a.out_dir, host, time.strftime("%Y%m%d"),
                             os.path.join(here, "sne_one.py"))
    n = {s: sum(1 for it in items if it["status"] == s) for s in STATUSES}
    gb = sum(it["bytes"] for it in items if it["status"] == "MISSED" and it["bytes"] > 0) / 1e9
    print(f"\nwalked {seen:,} folders; {len(items)} .sne found")
    print(f"  MISSED     {n['MISSED']:>5}   ({gb:,.1f} GB) -> in the .cmd file")
    print(f"  COPY       {n['COPY']:>5}   further copies of a MISSED experiment")
    print(f"  QC_OR_TEST {n['QC_OR_TEST']:>5}   name says QC or test: listed, not ingested (include.txt overrides)")
    print(f"  EXCLUDED   {n['EXCLUDED']:>5}   in exclude.txt")
    print(f"  TOO_NEW    {n['TOO_NEW']:>5}   modified in the last {a.min_age_hours:g} h")
    print(f"  SAME_NAME  {n['SAME_NAME']:>5}   FRAN has this name from another path (review by hand)")
    print(f"  INGESTED   {n['INGESTED']:>5}")
    print(f"\nlist:     {tsv}\ncommands: {cmd}")
    try:
        import node_report
        node_report.report("find_missed_sne", f"roots={', '.join(roots)} found={len(items)} "
                           + " ".join(f"{k}={v}" for k, v in n.items()) + f" missed_gb={gb:.1f} list={tsv}")
    except Exception:  # noqa: BLE001
        pass
    return items, tsv, cmd


def main():
    ap = argparse.ArgumentParser(description="List .sne experiments on this computer that FRAN has never ingested.")
    ap.add_argument("roots", nargs="*", help="folders or drives to scan (default: every local fixed drive)")
    ap.add_argument("--include-network", action="store_true", help="also scan mapped network drives")
    ap.add_argument("--max-depth", type=int, default=14)
    ap.add_argument("--min-age-hours", type=float, default=2.0)
    ap.add_argument("--out-dir", default=os.path.join(HERE, "missed_sne"))
    a = ap.parse_args()
    scan(a.roots, a.include_network, a.max_depth, a.min_age_hours, a.out_dir)


if __name__ == "__main__":
    main()
