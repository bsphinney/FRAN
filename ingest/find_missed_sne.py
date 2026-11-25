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
from find_uningested import name_keys, norm_path, known_keys, prune  # noqa: E402 -- the ONE matching rule

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
    parent = os.path.dirname(sne_path.rstrip("\\/"))
    try:
        return any(e.lower().endswith("_xic-dbs") and isdir(os.path.join(parent, e))
                   for e in listdir(parent))
    except OSError:
        return False


def classify(items, paths, names, bases, now=None, min_age_hours=2.0):
    """items: dicts with path, stem, bytes, mtime. Adds status, matched_by, n_copies, in place, and
    returns them sorted (MISSED first, largest first)."""
    now = time.time() if now is None else now
    known_names = names | bases
    groups = {}
    for it in items:
        if norm_path(it["path"]) in paths:
            it["status"], it["matched_by"] = "INGESTED", "path"
        elif name_keys(it["stem"]) & known_names:
            it["status"], it["matched_by"] = "SAME_NAME", "name"
        elif it["mtime"] and now - it["mtime"] < min_age_hours * 3600:
            it["status"], it["matched_by"] = "TOO_NEW", ""
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
    order = {"MISSED": 0, "COPY": 1, "TOO_NEW": 2, "SAME_NAME": 3, "INGESTED": 4}
    return sorted(items, key=lambda x: (order[x["status"]], -x["bytes"], x["path"].lower()))


def command_for(it, script="sne_one.py"):
    """The sne_one.py line for one experiment. Double quotes inside a name cannot occur on Windows."""
    return f'py "{script}" --sne "{it["path"]}" --name "{it["stem"]}"'


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
        fh.write("@echo off\nrem One line per experiment FRAN has never ingested. Review, then run.\n")
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


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="List .sne experiments on this computer that FRAN has never ingested.")
    ap.add_argument("roots", nargs="*", help="folders or drives to scan (default: every local fixed drive)")
    ap.add_argument("--include-network", action="store_true", help="also scan mapped network drives")
    ap.add_argument("--max-depth", type=int, default=14)
    ap.add_argument("--min-age-hours", type=float, default=2.0)
    ap.add_argument("--out-dir", default=os.path.join(here, "missed_sne"))
    a = ap.parse_args()

    roots = a.roots or local_drives(a.include_network)
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
    items = classify(items, paths, names, bases, min_age_hours=a.min_age_hours)

    host = socket.gethostname().split(".")[0]
    tsv, cmd = write_outputs(items, a.out_dir, host, time.strftime("%Y%m%d"),
                             os.path.join(here, "sne_one.py"))
    n = {s: sum(1 for it in items if it["status"] == s) for s in
         ("MISSED", "COPY", "TOO_NEW", "SAME_NAME", "INGESTED")}
    gb = sum(it["bytes"] for it in items if it["status"] == "MISSED" and it["bytes"] > 0) / 1e9
    print(f"\nwalked {seen:,} folders; {len(items)} .sne found")
    print(f"  MISSED     {n['MISSED']:>5}   ({gb:,.1f} GB) -> in the .cmd file")
    print(f"  COPY       {n['COPY']:>5}   further copies of a MISSED experiment")
    print(f"  TOO_NEW    {n['TOO_NEW']:>5}   modified in the last {a.min_age_hours:g} h")
    print(f"  SAME_NAME  {n['SAME_NAME']:>5}   FRAN has this name from another path (review by hand)")
    print(f"  INGESTED   {n['INGESTED']:>5}")
    print(f"\nlist:     {tsv}\ncommands: {cmd}")


if __name__ == "__main__":
    main()
