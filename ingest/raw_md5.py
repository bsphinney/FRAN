"""raw_md5.py - fill raw_files.md5 by reading each located raw file once (read-only).

The design doc (section 2) lists md5 at 0%: "no way to prove a raw is the one searched". This is the
proof, and the training split's eventual run identity.

WHAT IS HASHED:
  .raw   the file's bytes.
  .d     analysis.tdf's bytes, then analysis.tdf_bin's bytes, as one MD5 stream. Not the other files
         in the folder (method copies, lock files and exports come and go). Because analysis.tdf is
         included, a .d that something opens non-immutably later (the WAL truncation seen in this
         corpus, memory bruker-tdf-wal-truncation) no longer matches -- which is the point.
Files are opened plain 'rb'; nothing is ever opened as SQLite here, so nothing can write into a .d.

One hash per distinct hive_path, written to every raw_files row with that hive_path where md5 is
NULL. Each shard also appends to its own TSV (one writer per file on Quobyte).

    python raw_md5.py [--shard I --shards N] [--max-bytes 2e12] [--apply] [--tsv-dir DIR]
"""
import argparse
import hashlib
import os
import sys
import time

CHUNK = 16 * 1024 * 1024


def md5_of(path):
    h = hashlib.md5()
    n = 0
    parts = ([os.path.join(path, "analysis.tdf"), os.path.join(path, "analysis.tdf_bin")]
             if path.lower().rstrip("/").endswith(".d") else [path])
    for p in parts:
        with open(p, "rb") as fh:
            while True:
                b = fh.read(CHUNK)
                if not b:
                    break
                h.update(b)
                n += len(b)
    return h.hexdigest(), n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--max-bytes", type=float, default=0, help="stop after reading this many bytes")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--tsv-dir", default="/quobyte/proteomics-grp/brett/raw_census/md5")
    a = ap.parse_args()

    from coreomics_import import _conn
    conn = _conn()
    with conn.cursor() as c:
        c.execute("SET statement_timeout = '10min'")
        c.execute("""SELECT hive_path, max(file_size_bytes) FROM raw_files
                      WHERE nullif(btrim(hive_path), '') IS NOT NULL
                      GROUP BY hive_path
                     HAVING bool_or(nullif(btrim(md5), '') IS NULL)
                      ORDER BY hive_path""")
        todo = [(p, s) for i, (p, s) in enumerate(c.fetchall()) if i % a.shards == a.shard]
    total = sum(s or 0 for _, s in todo)
    print(f"shard {a.shard}/{a.shards}: {len(todo):,} file(s), ~{total / 1e12:.2f} TB; apply={a.apply}",
          flush=True)
    os.makedirs(a.tsv_dir, exist_ok=True)
    tsv = open(os.path.join(a.tsv_dir, f"md5_shard{a.shard}of{a.shards}_{os.environ.get('SLURM_JOB_ID', 'local')}.tsv"), "a")

    t0, done_b, pend, n_ok, n_err = time.time(), 0, [], 0, 0

    def flush():
        if not pend or not a.apply:
            pend.clear()
            return
        from psycopg2.extras import execute_values
        with conn.cursor() as c:
            c.execute("SET lock_timeout = '5s'")
            execute_values(c, """UPDATE raw_files r SET md5 = v.md5
                                   FROM (VALUES %s) AS v(hive_path, md5)
                                  WHERE r.hive_path = v.hive_path AND nullif(btrim(r.md5), '') IS NULL""",
                           pend, page_size=200)
        conn.commit()
        pend.clear()

    for path, _ in todo:
        if a.max_bytes and done_b >= a.max_bytes:
            print(f"reached --max-bytes after {n_ok:,} file(s)")
            break
        try:
            digest, nb = md5_of(path)
        except OSError as e:
            n_err += 1
            tsv.write(f"{path}\t\tERROR {type(e).__name__}: {str(e)[:120]}\n")
            continue
        done_b += nb
        n_ok += 1
        tsv.write(f"{path}\t{digest}\t{nb}\n")
        pend.append((path, digest))
        if len(pend) >= 50:
            tsv.flush()
            flush()
        if n_ok % 200 == 0:
            el = time.time() - t0
            print(f"  {n_ok:,} files, {done_b / 1e12:.2f} TB, {done_b / max(el, 1) / 1e6:.0f} MB/s", flush=True)
    tsv.close()
    flush()
    conn.close()
    el = time.time() - t0
    print(f"DONE shard {a.shard}: {n_ok:,} hashed, {n_err:,} unreadable, {done_b / 1e12:.2f} TB in "
          f"{el / 3600:.1f} h ({done_b / max(el, 1) / 1e6:.0f} MB/s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
