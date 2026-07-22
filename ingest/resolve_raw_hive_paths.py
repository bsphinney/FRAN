"""resolve_raw_hive_paths.py — fill raw_files.hive_path by finding each raw's physical location on
Flinders. The DB's raw_path is the ORIGINAL Windows/Spectronaut path (R:\\Data\\lab\\service\\... with
the .d nested in a .sne), which does NOT match Flinders' instrument-organized layout
(/nfs/.../raw_data/<Exploris480|Lumos1|tTOF_HT>/...). So we match by BASENAME: index every physical
*.d (Bruker dir) and *.raw (Thermo file) under the raw roots, then link each raw_files row.

Only sets hive_path where the basename resolves to exactly ONE physical file (never mislinks), and
only touches rows where hive_path IS NULL. Idempotent.

    python resolve_raw_hive_paths.py [--dry-run] [--root <dir> ...]
Heavy filesystem walk -> run on a COMPUTE node, never the login node.
"""
from __future__ import annotations
import argparse, os, sys
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plan_spectrum_backfill as P

DEFAULT_ROOTS = ["/nfs/lssc0/flinders/proteomics/Data/raw_data"]


def _base(name):
    for ext in (".d", ".raw"):
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return None


def index_raws(roots):
    """basename -> set(physical paths) for every *.d dir and *.raw file under roots."""
    idx = defaultdict(set)
    for root in roots:
        if not os.path.isdir(root):
            print(f"  (root missing: {root})"); continue
        for dp, dirnames, filenames in os.walk(root):
            # Bruker .d are directories — record and DON'T descend into them
            keep = []
            for d in dirnames:
                b = _base(d)
                if b:
                    idx[b].add(os.path.join(dp, d))
                else:
                    keep.append(d)
            dirnames[:] = keep
            for f in filenames:
                b = _base(f)
                if b:
                    idx[b].add(os.path.join(dp, f))
    return idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--root", action="append", default=[])
    a = ap.parse_args()
    roots = a.root or DEFAULT_ROOTS
    print(f"indexing raws under: {roots}")
    idx = index_raws(roots)
    print(f"  {len(idx):,} distinct basenames physically present on disk")

    conn = P._conn(); cur = conn.cursor()
    cur.execute("SELECT raw_path, raw_basename FROM raw_files WHERE hive_path IS NULL OR hive_path=''")
    rows = cur.fetchall()
    ups, ambiguous, nomatch = [], 0, 0
    for raw_path, base in rows:
        paths = idx.get(base) if base else None
        if not paths:
            nomatch += 1
            continue
        # The same physical raw often appears under >1 path — the two-copy practice (a copy in
        # raw_data AND in the service dir) and symlinked large datasets (e.g. bigDOG). Collapse by
        # realpath so those count as ONE file, not "ambiguous". Prefer the raw_data copy (canonical
        # store; some raws live there only). Only genuinely-distinct files => ambiguous.
        by_real = {}
        for p in sorted(paths):
            try:
                rp = os.path.realpath(p)
            except OSError:
                rp = p
            by_real.setdefault(rp, p)
        if len(by_real) == 1:
            chosen = next((p for p in sorted(paths) if "/raw_data/" in p), next(iter(by_real.values())))
            ups.append((chosen, raw_path))
        else:
            ambiguous += 1
    print(f"{len(rows):,} rows with empty hive_path: {len(ups):,} resolvable, "
          f"{ambiguous:,} ambiguous (basename on disk >1x), {nomatch:,} not found on disk")
    if ups and not a.dry_run:
        import psycopg2.extras
        psycopg2.extras.execute_batch(
            cur, "UPDATE raw_files SET hive_path=%s, hive_verified_at=now() WHERE raw_path=%s",
            ups, page_size=500)
        conn.commit()
        print(f"  set hive_path on {len(ups):,} rows")
    elif a.dry_run:
        print("  (dry-run — no writes)")
    cur.execute("SELECT count(*) FILTER (WHERE hive_path IS NOT NULL AND hive_path<>''), count(*) FROM raw_files")
    t = cur.fetchone()
    print(f"raw_files now: {t[0]:,}/{t[1]:,} have a hive_path")
    conn.close()


if __name__ == "__main__":
    main()
