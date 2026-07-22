"""resolve_raw_hive_paths.py — fill raw_files.hive_path from the physical raws on disk, and (with
--fix-meta) correct the unreliable platform metadata from the ACTUAL file format.

The DB's raw_path is the original Windows/Spectronaut path (drive letters, .d nested in a .sne),
which doesn't match Flinders' instrument layout — so we match by BASENAME across the raw roots
(Flinders /Data raw_data + service dirs, Quobyte /to-hive archives).

EXTENSION-AWARE: raw_files.platform is not trustworthy (a 'timstof'/diaPASEF row can resolve to a
Thermo .raw). So we prefer the physical file whose extension matches the DB path's extension; if only
a different-extension file exists, we still resolve it but FLAG the conflict, because the physical
file's format (.d = Bruker, .raw = Thermo) is ground truth. The two-copy practice and symlinked
datasets (bigDOG) are collapsed by realpath so they don't look ambiguous.

    python resolve_raw_hive_paths.py --dry-run [--root <dir> ...]      # report only
    python resolve_raw_hive_paths.py [--root ...]                      # fill hive_path
    python resolve_raw_hive_paths.py --fix-meta [--root ...]           # + correct platform from format
Heavy filesystem walk -> run on a COMPUTE node, never the login node.
"""
from __future__ import annotations
import argparse, os, sys
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plan_spectrum_backfill as P

DEFAULT_ROOTS = ["/nfs/lssc0/flinders/proteomics/Data",
                 "/quobyte/proteomics-grp/to-hive"]


def _ext(name):
    n = name.lower().rstrip("/")
    if n.endswith(".d"):
        return ".d"
    if n.endswith(".raw"):
        return ".raw"
    return ""


def _base(name):
    e = _ext(name)
    return name.rstrip("/")[: -len(e)] if e else None


def _platform_of(ext):
    return {".d": "bruker_timstof", ".raw": "thermo_orbitrap"}.get(ext)


def index_raws(roots):
    idx = defaultdict(set)
    for root in roots:
        if not os.path.isdir(root):
            print(f"  (root missing: {root})"); continue
        for dp, dirnames, filenames in os.walk(root):
            keep = []
            for d in dirnames:
                if _ext(d):            # Bruker .d dir — record, don't descend
                    idx[_base(d)].add(os.path.join(dp, d))
                else:
                    keep.append(d)
            dirnames[:] = keep
            for f in filenames:
                if _ext(f):            # Thermo .raw file
                    idx[_base(f)].add(os.path.join(dp, f))
    return idx


def _dedupe_realpath(paths):
    """Collapse symlinks / duplicate copies (two-copy practice, bigDOG) to one path per real file;
    prefer a raw_data copy as the display path."""
    by_real = {}
    for p in sorted(paths):
        try:
            rp = os.path.realpath(p)
        except OSError:
            rp = p
        by_real.setdefault(rp, [])
        by_real[rp].append(p)
    out = []
    for rp, ps in by_real.items():
        out.append(next((p for p in ps if "/raw_data/" in p), ps[0]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fix-meta", action="store_true", help="also correct raw_files.platform from the resolved file's format")
    ap.add_argument("--root", action="append", default=[])
    a = ap.parse_args()
    roots = a.root or DEFAULT_ROOTS
    print(f"indexing raws under: {roots}")
    idx = index_raws(roots)
    print(f"  {len(idx):,} distinct basenames physically present on disk")

    conn = P._conn(); cur = conn.cursor()
    cur.execute("SELECT raw_path, raw_basename, platform FROM raw_files WHERE hive_path IS NULL OR hive_path=''")
    rows = cur.fetchall()
    hive_ups, plat_ups = [], []
    n_match = n_conflict = ambiguous = nomatch = 0
    conflict_samples = []
    for raw_path, base, db_platform in rows:
        cands = idx.get(base) if base else None
        if not cands:
            nomatch += 1; continue
        reals = _dedupe_realpath(cands)
        want = _ext(raw_path)
        same = [p for p in reals if _ext(p) == want]
        other = [p for p in reals if _ext(p) != want]
        if len(same) == 1:
            chosen, conflict = same[0], False
        elif len(same) > 1:
            ambiguous += 1; continue           # >1 distinct file, same ext -> genuinely ambiguous
        elif len(other) == 1:
            chosen, conflict = other[0], True   # only a different-format file exists -> resolve + flag
        else:
            ambiguous += 1; continue
        hive_ups.append((chosen, raw_path))
        true_plat = _platform_of(_ext(chosen))
        if true_plat and true_plat != (db_platform or ""):
            plat_ups.append((true_plat, raw_path))
        if conflict:
            n_conflict += 1
            if len(conflict_samples) < 8:
                conflict_samples.append((base, want or "?", _ext(chosen)))
        else:
            n_match += 1

    print(f"\n{len(rows):,} rows with empty hive_path:")
    print(f"  resolved (extension matches DB) : {n_match:,}")
    print(f"  resolved but FORMAT CONFLICT    : {n_conflict:,}  (DB ext != physical file ext)")
    print(f"  ambiguous (>1 distinct file)    : {ambiguous:,}")
    print(f"  not found on disk               : {nomatch:,}")
    print(f"  => platform metadata that disagrees with the real file: {len(plat_ups):,}")
    for b, dbe, phe in conflict_samples:
        print(f"     CONFLICT  {b}  DB={dbe}  physical={phe}")

    if not a.dry_run and hive_ups:
        import psycopg2.extras
        psycopg2.extras.execute_batch(
            cur, "UPDATE raw_files SET hive_path=%s, hive_verified_at=now() WHERE raw_path=%s",
            hive_ups, page_size=500)
        conn.commit()
        print(f"\n  set hive_path on {len(hive_ups):,} rows")
        if a.fix_meta and plat_ups:
            psycopg2.extras.execute_batch(
                cur, "UPDATE raw_files SET platform=%s WHERE raw_path=%s", plat_ups, page_size=500)
            conn.commit()
            print(f"  corrected platform on {len(plat_ups):,} rows (from the physical file format)")
    elif a.dry_run:
        print("\n  (dry-run — no writes)")

    cur.execute("SELECT count(*) FILTER (WHERE hive_path IS NOT NULL AND hive_path<>''), count(*) FROM raw_files")
    t = cur.fetchone()
    print(f"raw_files now: {t[0]:,}/{t[1]:,} have a hive_path")
    conn.close()


if __name__ == "__main__":
    main()
