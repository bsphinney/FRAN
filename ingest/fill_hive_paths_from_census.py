"""fill_hive_paths_from_census.py - locate every raw_files row on Hive, verified against the raw census.

WHY NOT resolve_raw_hive_paths ALONE. It matches by basename. The 2026-11-13 dry run over 13,139
unlocated rows showed what that costs: where the search's own input list says which file was read,
the basename match picked a DIFFERENT copy 264 times (a session's raw_data copy instead of the
archive file), and 1,541 rows recorded as .d exist on disk only as a Thermo .raw.

ORDER OF EVIDENCE:
  1 exact      the search's own file_list.txt (exact_hive_paths.tsv from exact_from_filelists.py):
               the file DIA-NN actually read. Accepted when the census has it and it is complete.
  2 basename   resolve_raw_hive_paths' choice (its --dump CSV), accepted ONLY when every complete
               copy of that run on disk has the same size -- i.e. they are the same data and which
               copy we point at does not matter. Different sizes = ambiguous = left empty, listed.
Rows found nowhere are listed, not guessed.

Also fills file_size_bytes (from the census: .raw = file size, .d = analysis.tdf + analysis.tdf_bin)
for EVERY row with a hive_path, including the ones located before today. Never overwrites a non-empty
hive_path or file_size_bytes. hive_verified_at = now() for what it writes.

Writes <out>/decisions.tsv (every row, every reason) and <out>/backup.tsv (the rows it changes,
before), then with --apply updates raw_files.

    python fill_hive_paths_from_census.py --census DIR --exact TSV --dump CSV --out DIR [--apply]
"""
import argparse
import csv
import glob
import os
import sys
import time
from collections import defaultdict

csv.field_size_limit(10 ** 8)


def load_census(root):
    by_path, by_base = {}, defaultdict(list)
    for f in glob.glob(os.path.join(root, "*", "census.tsv")):
        with open(f, newline="") as fh:
            for r in csv.DictReader(fh, delimiter="\t"):
                rec = {"path": r["path"], "realpath": r["realpath"], "ext": r["ext"],
                       "size": int(r["size_bytes"]) if (r.get("size_bytes") or "").isdigit() else None,
                       "complete": r.get("complete") == "1"}
                by_path[r["path"]] = rec
                by_path.setdefault(r["realpath"], rec)
                by_base[r["basename"].lower()].append(rec)
    return by_path, by_base


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--census", required=True)
    ap.add_argument("--exact", required=True)
    ap.add_argument("--dump", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    t0 = time.time()
    by_path, by_base = load_census(a.census)
    print(f"census: {len(by_path):,} path keys, {len(by_base):,} distinct basenames ({time.time() - t0:.0f}s)")
    exact = {}
    with open(a.exact, newline="") as fh:
        for r in csv.DictReader(fh, delimiter="\t"):
            exact[r["raw_path"]] = r["real_path"]
    resolved = {}
    with open(a.dump, newline="") as fh:
        for r in csv.DictReader(fh):
            if r.get("resolved_path"):
                resolved[r["raw_path_db"]] = r["resolved_path"]

    from coreomics_import import _conn
    conn = _conn()
    with conn.cursor() as c:
        c.execute("SET statement_timeout = '10min'")
        c.execute("""SELECT raw_path, raw_basename, nullif(btrim(hive_path), ''), file_size_bytes
                       FROM raw_files""")
        rows = c.fetchall()

    decisions, updates = [], []
    tally = defaultdict(int)
    for raw_path, base, hive, size in rows:
        new_hive, why = None, None
        if not hive:
            ex = exact.get(raw_path)
            if ex:
                rec = by_path.get(ex) or by_path.get(os.path.realpath(ex))
                if rec and rec["complete"]:
                    new_hive, why = ex, "exact"
                elif rec:
                    why = "exact_listed_but_incomplete"
                else:
                    why = "exact_listed_not_in_census"
            if not new_hive and raw_path in resolved:
                cand = resolved[raw_path]
                copies = [r for r in by_base.get((base or "").lower(), []) if r["complete"]]
                sizes = {r["size"] for r in copies}
                if not copies:
                    why = why or "resolver_path_not_complete_in_census"
                elif len(sizes) == 1 and (by_path.get(cand) or {}).get("complete"):
                    new_hive, why = cand, "basename_same_size_copies"
                elif not why:
                    why = f"ambiguous: {len(copies)} copies, {len(sizes)} sizes"
            if not new_hive and not why:
                why = "not_found"
        path_for_size = hive or new_hive
        new_size = None
        if path_for_size and size is None:
            rec = by_path.get(path_for_size) or by_path.get(os.path.realpath(path_for_size))
            if rec and rec["size"] and rec["complete"]:
                new_size = rec["size"]
        tally["hive_path:" + (why or "already_set")] += 1
        if new_size:
            tally["size_filled"] += 1
        decisions.append((raw_path, base, hive or "", new_hive or "", why or "already_set", new_size or ""))
        if new_hive or new_size:
            updates.append((raw_path, hive, size, new_hive, new_size))

    with open(os.path.join(a.out, "decisions.tsv"), "w", newline="") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(["raw_path", "raw_basename", "hive_path_before", "hive_path_new", "reason", "size_new"])
        w.writerows(decisions)
    with open(os.path.join(a.out, "backup.tsv"), "w", newline="") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(["raw_path", "hive_path_before", "file_size_bytes_before"])
        w.writerows((u[0], u[1] or "", "" if u[2] is None else u[2]) for u in updates)
    for k, v in sorted(tally.items()):
        print(f"  {k:48s} {v:8,}")
    print(f"{len(updates):,} row(s) to update; decisions + backup in {a.out}")
    if not a.apply:
        print("dry run: nothing written (--apply to write)")
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
