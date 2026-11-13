"""fill_hive_paths_from_census.py - locate every raw_files row on Hive, verified, nightly.

WHY NOT resolve_raw_hive_paths. It matches by basename alone. The 2026-11-13 dry run over 13,139
unlocated rows showed what that costs: where the search's own input list says which file was read,
the basename match picked a DIFFERENT copy 264 times (a session's raw_data copy instead of the
archive file), and 1,541 rows recorded as .d exist on disk only as a Thermo .raw. This script
replaced it as fran_fill's first step on 2026-11-13; every later filler needs hive_path.

ORDER OF EVIDENCE (only for rows with no hive_path; a non-empty hive_path is never overwritten):
  1 exact      the search's own file_list.txt, next to the synthetic raw_path corpus_ingest stores:
               the file DIA-NN actually read. Checked ON DISK (not via the census), so a run
               acquired yesterday is located tonight. Must be complete (.raw: size > 0;
               .d: analysis.tdf and analysis.tdf_bin both present and non-empty).
  2 basename   the latest raw census (raw_census.py, refreshed weekly). Accepted ONLY when every
               complete copy of that run on disk has the same size, i.e. they are the same data
               and which copy we point at does not matter. Different sizes = ambiguous = left
               empty and listed. The copy chosen: a non-symlink under the Flinders archive first,
               then the shortest path (deterministic, so reruns agree).
Rows found nowhere are listed, not guessed.

Also fills file_size_bytes (.raw = file size, .d = analysis.tdf + analysis.tdf_bin) for every row
with a hive_path, from the census or, failing that, a direct stat.

Writes <out>/decisions.tsv (every unlocated row and why) and <out>/backup.tsv (the rows it changes,
before), then with --apply updates raw_files.

    python fill_hive_paths_from_census.py [--census DIR] [--exact TSV] [--dump CSV] [--out DIR] [--apply]

  --census  default: the newest YYYY-MM-DD folder under raw_census/
  --exact   a precomputed (raw_path, real_path) TSV; default: read the file lists in-process
  --dump    resolve_raw_hive_paths' --dump CSV; if given, its choice is preferred among same-size copies
"""
import argparse
import csv
import glob
import os
import re
import sys
import time
from collections import defaultdict

csv.field_size_limit(10 ** 8)

CENSUS_ROOT = "/quobyte/proteomics-grp/brett/raw_census"
ARCHIVE = "/nfs/lssc0/flinders/proteomics/Data/"
CENSUS_STALE_DAYS = 10


CENSUS_PARTS = ("flinders", "quobyte")


def latest_census(root=CENSUS_ROOT):
    """Newest dated census whose every part finished (raw_census.py writes _COMPLETE last)."""
    dirs = sorted(d for d in glob.glob(os.path.join(root, "*"))
                  if re.fullmatch(r"\d{4}-\d{2}-\d{2}", os.path.basename(d))
                  and all(os.path.isfile(os.path.join(d, p, "census.tsv"))
                          and os.path.isfile(os.path.join(d, p, "_COMPLETE")) for p in CENSUS_PARTS))
    return dirs[-1] if dirs else None


def load_census(root):
    by_path, by_base = {}, defaultdict(list)
    for f in glob.glob(os.path.join(root, "*", "census.tsv")):
        with open(f, newline="") as fh:
            for r in csv.DictReader(fh, delimiter="\t"):
                rec = {"path": r["path"], "realpath": r["realpath"], "ext": r["ext"],
                       "size": int(r["size_bytes"]) if (r.get("size_bytes") or "").isdigit() else None,
                       "complete": r.get("complete") == "1",
                       "symlink": r.get("is_symlink") == "1"}
                by_path[r["path"]] = rec
                by_path.setdefault(r["realpath"], rec)
                by_base[r["basename"].lower()].append(rec)
    return by_path, by_base


def disk_size(p):
    """(complete, size) read from disk, the census's definition. Never opens a .d as SQLite."""
    try:
        if p.lower().rstrip("/").endswith(".d"):
            a = os.path.getsize(os.path.join(p, "analysis.tdf"))
            b = os.path.getsize(os.path.join(p, "analysis.tdf_bin"))
            return (a > 0 and b > 0), a + b
        s = os.path.getsize(p)
        return s > 0, s
    except OSError:
        return False, None


class FileLists:
    """file_list.txt next to each synthetic raw_path, read once per directory."""

    def __init__(self):
        self.cache = {}

    def real_path(self, raw_path):
        if not raw_path or not raw_path.startswith("/"):
            return None
        d = os.path.dirname(raw_path)
        if d not in self.cache:
            m = {}
            f = os.path.join(d, "file_list.txt")
            try:
                with open(f) as fh:
                    for line in fh:
                        p = line.strip()
                        if p:
                            m[os.path.basename(p.rstrip("/"))] = p
            except OSError:
                pass
            self.cache[d] = m
        return self.cache[d].get(os.path.basename(raw_path))


def pick_copy(copies, preferred=None):
    if preferred and any(c["path"] == preferred for c in copies):
        return preferred
    return sorted(copies, key=lambda c: (c["symlink"], not c["path"].startswith(ARCHIVE),
                                         len(c["path"]), c["path"]))[0]["path"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--census", default=None)
    ap.add_argument("--exact", default=None)
    ap.add_argument("--dump", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    out = a.out or os.path.join(CENSUS_ROOT, "fill", time.strftime("%Y%m%d_%H%M%S") + "_" +
                                os.environ.get("SLURM_JOB_ID", "local"))
    os.makedirs(out, exist_ok=True)

    t0 = time.time()
    census = a.census or latest_census()
    by_path, by_base = ({}, {}) if not census else load_census(census)
    age = None
    if census and re.fullmatch(r"\d{4}-\d{2}-\d{2}", os.path.basename(census.rstrip("/"))):
        age = (time.time() - time.mktime(time.strptime(os.path.basename(census.rstrip("/")),
                                                       "%Y-%m-%d"))) / 86400
    print(f"census: {census or 'NONE'} (age {age:.1f} d), {len(by_path):,} path keys, "
          f"{len(by_base):,} basenames ({time.time() - t0:.0f}s)" if age is not None else
          f"census: {census or 'NONE'}, {len(by_path):,} path keys")
    if age is not None and age > CENSUS_STALE_DAYS:
        print(f"WARN: census is {age:.0f} days old (> {CENSUS_STALE_DAYS}); basename matches may "
              f"miss new copies. raw_census.sbatch should run weekly.", file=sys.stderr)

    exact_tsv = {}
    if a.exact:
        with open(a.exact, newline="") as fh:
            for r in csv.DictReader(fh, delimiter="\t"):
                exact_tsv[r["raw_path"]] = r["real_path"]
    resolved = {}
    if a.dump:
        with open(a.dump, newline="") as fh:
            for r in csv.DictReader(fh):
                if r.get("resolved_path"):
                    resolved[r["raw_path_db"]] = r["resolved_path"]
    lists = FileLists()

    from coreomics_import import _conn
    conn = _conn()
    with conn.cursor() as c:
        c.execute("SET statement_timeout = '10min'")
        c.execute("""SELECT raw_path, raw_basename, nullif(btrim(hive_path), ''), file_size_bytes
                       FROM raw_files
                      WHERE nullif(btrim(hive_path), '') IS NULL OR file_size_bytes IS NULL""")
        rows = c.fetchall()

    decisions, updates = [], []
    tally = defaultdict(int)
    for raw_path, base, hive, size in rows:
        new_hive, why, new_size = None, None, None
        if not hive:
            ex = exact_tsv.get(raw_path) or lists.real_path(raw_path)
            if ex:
                ok, sz = disk_size(ex)
                if ok:
                    new_hive, why, new_size = ex, "exact", sz
                else:
                    why = "exact_listed_but_missing_or_incomplete"
            if not new_hive:
                copies = [r for r in by_base.get((base or "").lower(), []) if r["complete"]]
                sizes = {r["size"] for r in copies}
                if not copies:
                    why = why or ("not_found" if census else "no_census")
                elif len(sizes) == 1:
                    new_hive, why = pick_copy(copies, resolved.get(raw_path)), "basename_same_size_copies"
                    new_size = next(iter(sizes))
                else:
                    why = why or f"ambiguous: {len(copies)} copies, {len(sizes)} sizes"
        path_for_size = hive or new_hive
        if path_for_size and size is None and new_size is None:
            rec = by_path.get(path_for_size) or by_path.get(os.path.realpath(path_for_size))
            if rec and rec["size"] and rec["complete"]:
                new_size = rec["size"]
            else:
                ok, sz = disk_size(path_for_size)
                new_size = sz if ok else None
        if size is not None:
            new_size = None
        tally["hive_path:" + (why or "already_set")] += 1
        if new_size:
            tally["size_filled"] += 1
        if not hive:
            decisions.append((raw_path, base, new_hive or "", why, new_size or ""))
        if new_hive or new_size:
            updates.append((raw_path, hive, size, new_hive, new_size))

    with open(os.path.join(out, "decisions.tsv"), "w", newline="") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(["raw_path", "raw_basename", "hive_path_new", "reason", "size_new"])
        w.writerows(decisions)
    with open(os.path.join(out, "backup.tsv"), "w", newline="") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(["raw_path", "hive_path_before", "file_size_bytes_before"])
        w.writerows((u[0], u[1] or "", "" if u[2] is None else u[2]) for u in updates)
    for k, v in sorted(tally.items()):
        print(f"  {k:48s} {v:8,}")
    print(f"{len(rows):,} row(s) missing hive_path or size; {len(updates):,} to update; "
          f"decisions + backup in {out}")
    if not a.apply:
        print("dry run: nothing written (--apply to write)")
        return 0
    if not updates:
        conn.close()
        return 0

    from psycopg2.extras import execute_values
    with conn.cursor() as c:
        c.execute("SET lock_timeout = '5s'")
        c.execute("SET statement_timeout = '20min'")
        execute_values(c, """
            UPDATE raw_files r
               SET hive_path = COALESCE(nullif(btrim(r.hive_path), ''), v.hive_path),
                   hive_verified_at = CASE WHEN nullif(btrim(r.hive_path), '') IS NULL
                                            AND v.hive_path IS NOT NULL THEN now()
                                           ELSE r.hive_verified_at END,
                   file_size_bytes = COALESCE(r.file_size_bytes, v.size)
              FROM (VALUES %s) AS v(raw_path, hive_path, size)
             WHERE r.raw_path = v.raw_path""",
                       [(u[0], u[3], u[4]) for u in updates],
                       template="(%s, %s::text, %s::bigint)", page_size=1000)
        n = c.rowcount
    conn.commit()
    with conn.cursor() as c:
        c.execute("""SELECT count(*), count(nullif(btrim(hive_path), '')), count(file_size_bytes)
                       FROM raw_files""")
        tot, hp, fs = c.fetchone()
    conn.close()
    print(f"updated {n:,} row(s). raw_files now: hive_path {hp:,}/{tot:,} ({hp / tot:.1%}), "
          f"file_size_bytes {fs:,}/{tot:,} ({fs / tot:.1%})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
