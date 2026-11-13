"""raw_census.py - one list of EVERY raw MS file on Flinders + Quobyte (Bruker .d and Thermo .raw).

READ-ONLY on the raws. Every analysis.tdf is opened `?mode=ro&immutable=1` (a plain sqlite open of a
.d can truncate it - memory bruker-tdf-wal-truncation). Never descends into a .d.

Output (one writer per file - Quobyte rule): <out>/census.tsv with, per physical copy:
  basename (extension stripped), ext, path, realpath, is_symlink, size_bytes (.raw: file;
  .d: analysis.tdf + analysis.tdf_bin), mtime, complete (.raw: size>0; .d: both tdf files present
  and non-empty), has_wal, acq_datetime + instrument + serial (.d, from GlobalMetadata), error.
Thermo .raw acquisition time is not parsed here (no reader on Hive); mtime + size stand in.

A finished census writes <out>/_COMPLETE last. fill_hive_paths_from_census.py only reads a dated
census whose every part has it: a half-written census hides copies, and a run whose only listed
copies all match in size would be accepted when a differently sized copy exists in the unread part.
Weekly from raw_census_cron.sh (raw_census.sbatch).

    python raw_census.py --out DIR [--threads 16] ROOT [ROOT ...]
"""
import argparse
import csv
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial

print = partial(print, flush=True)  # noqa: A001

SKIP_DIRS = {"conda_envs", "conda_pkgs", "envs", "apptainers", "bioinformatics_programs",
             "fragpipe24", "qwen-local", "tools", "de-limp-github", ".git", "__pycache__",
             "node_modules", ".snapshot", "skill_release", "skill_runs", "skill_issues",
             "skill_notes", "slurm-scripts", "claude"}
COLS = ["basename", "ext", "path", "realpath", "is_symlink", "size_bytes", "mtime", "complete",
        "has_wal", "acq_datetime", "instrument", "serial", "error"]


def ext_of(name):
    n = name.lower().rstrip("/")
    return ".d" if n.endswith(".d") else ".raw" if n.endswith(".raw") else ""


def probe_d(p):
    r = {"ext": ".d", "error": ""}
    tdf, binf = os.path.join(p, "analysis.tdf"), os.path.join(p, "analysis.tdf_bin")
    try:
        st_t = os.stat(tdf) if os.path.exists(tdf) else None
        st_b = os.stat(binf) if os.path.exists(binf) else None
        r["size_bytes"] = (st_t.st_size if st_t else 0) + (st_b.st_size if st_b else 0)
        r["mtime"] = int(max(st_t.st_mtime if st_t else 0, st_b.st_mtime if st_b else 0))
        r["complete"] = int(bool(st_t and st_b and st_t.st_size > 0 and st_b.st_size > 0))
        r["has_wal"] = int(os.path.exists(tdf + "-wal"))
        if st_t and st_t.st_size > 0:
            con = sqlite3.connect(f"file:{tdf}?mode=ro&immutable=1", uri=True, timeout=5)
            try:
                kv = dict(con.execute("SELECT Key, Value FROM GlobalMetadata WHERE Key IN "
                                      "('AcquisitionDateTime','InstrumentName',"
                                      "'InstrumentSerialNumber')").fetchall())
            finally:
                con.close()
            r["acq_datetime"] = kv.get("AcquisitionDateTime", "")
            r["instrument"] = kv.get("InstrumentName", "")
            r["serial"] = kv.get("InstrumentSerialNumber", "")
    except Exception as e:  # noqa: BLE001 - record, never stop the census
        r["error"] = f"{type(e).__name__}: {e}"[:200]
    return r


def probe_raw(p):
    r = {"ext": ".raw", "error": "", "has_wal": 0}
    try:
        st = os.stat(p)
        r["size_bytes"], r["mtime"] = st.st_size, int(st.st_mtime)
        r["complete"] = int(st.st_size > 0)
    except Exception as e:  # noqa: BLE001
        r["error"] = f"{type(e).__name__}: {e}"[:200]
    return r


def walk(root):
    """Yield raw paths under root; never descend into a .d; follow no directory symlinks
    (a symlinked .d itself IS recorded, as a copy, with is_symlink=1)."""
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            it = list(os.scandir(d))
        except OSError:
            continue
        for e in it:
            name = e.name
            try:
                is_dir = e.is_dir(follow_symlinks=True)
            except OSError:
                continue
            x = ext_of(name)
            if is_dir:
                if x == ".d":
                    yield e.path
                elif not e.is_symlink() and name not in SKIP_DIRS:
                    stack.append(e.path)
            elif x == ".raw":
                yield e.path


def one(p):
    r = probe_d(p) if p.lower().rstrip("/").endswith(".d") else probe_raw(p)
    name = os.path.basename(p.rstrip("/"))
    r["basename"] = name[: -len(r["ext"])]
    r["path"] = p
    try:
        r["realpath"] = os.path.realpath(p)
    except OSError:
        r["realpath"] = p
    r["is_symlink"] = int(os.path.islink(p))
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--threads", type=int, default=int(os.environ.get("SLURM_CPUS_PER_TASK", 8)) * 2)
    ap.add_argument("roots", nargs="+")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    t0, n = time.time(), 0
    with open(os.path.join(a.out, "census.tsv"), "w", newline="") as fh, \
            ThreadPoolExecutor(a.threads) as ex:
        w = csv.DictWriter(fh, COLS, delimiter="\t", extrasaction="ignore")
        w.writeheader()
        for root in a.roots:
            print(f"walking {root}")
            paths = list(walk(root))
            print(f"  {len(paths):,} raws found in {time.time() - t0:.0f}s; probing")
            for r in ex.map(one, paths, chunksize=64):
                w.writerow(r)
                n += 1
                if n % 5000 == 0:
                    print(f"  {n:,} probed, {time.time() - t0:.0f}s")
    with open(os.path.join(a.out, "_COMPLETE"), "w") as fh:
        fh.write(f"{n}\t{time.strftime('%FT%T')}\n")
    print(f"DONE {n:,} raw copies in {time.time() - t0:.0f}s -> {a.out}/census.tsv")


if __name__ == "__main__":
    sys.exit(main())
