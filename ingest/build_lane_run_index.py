r"""build_lane_run_index.py — the missing join between the Lance lane and the run dimension.

THE PROBLEM (IMPLEMENTATION_PLAN.md §1.3). `STORAGE_DESIGN.md`'s core requirement is "select
comparable runs, then aggregate". That cannot be expressed today at any level:

  * Lance datasets are one per SEARCH, not per run. Run identity survives only as `run` / `raw_path`
    string columns INSIDE the 48-column schema.
  * `delimp_spectrum_lane` (1,553 rows) has no `run` and no `raw_path` column — only `search_id`.
  * Searches are overwhelmingly multi-raw: only 288 of 1,963 are single-raw; many carry 6, 12, 15+.

So you cannot tell which runs a `.lance` holds without opening it, and adding run metadata to
Postgres buys nothing on its own. This script materialises that join once: one row per
(lance_path, run), which is what makes `WHERE run IN (...)` a query rather than a corpus scan.

THE JOIN KEY IS `run` → `raw_files.raw_basename`, not raw_path. Measured on a smoke test, because
the obvious choice does not work:

  * The Lance `raw_path` column exists in the 48-column schema but is **NULL** in the datasets — the
    fragment backfill never populated it. So it cannot be joined on, and it is stored here only for
    the datasets that do carry it.
  * `raw_files.raw_path` is a *synthetic Windows* path (`R:\Data\...\<x>.sne\<run>.d`), which would
    never match a Spectronaut run name even if Lance had one.
  * `raw_files.raw_basename` = the run name, and it matches. On the smoke test, **100 of 100 runs
    reached `gradient_minutes`** through it.

⚠️ That join is **1:many** — 100 runs matched 183 `raw_files` rows (~1.8×), because the same basename
recurs across searches/resubmits. Cohort queries must `SELECT DISTINCT` on the run, or pick a
canonical raw_file, or aggregates will double-count runs.

OPERATIONAL RULES (STORAGE_DESIGN.md §8), which this script exists to honour:
  * **Checkpointed.** Commits after each dataset and resumes by skipping lance_paths already
    indexed. The prior corpus-scale job in this codebase wrote one pickle at the very end, ran
    6:00:14 against a 6-hour wall, and produced nothing.
  * **Flushed.** Progress goes out unbuffered, so a running job is distinguishable from a hung one.

Run it on a COMPUTE NODE (see build_lane_run_index.sbatch) — never the Hive login node.

    python ingest/build_lane_run_index.py --dry-run
    python ingest/build_lane_run_index.py --apply
    python ingest/build_lane_run_index.py --apply --rebuild     # ignore checkpoints, redo all
"""
import argparse
import functools
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

print = functools.partial(print, flush=True)  # noqa: A001 — §8.2, never look hung

DDL = """
CREATE TABLE IF NOT EXISTS delimp_spectrum_lane_runs (
    lance_path    TEXT NOT NULL,
    run           TEXT NOT NULL,
    raw_path      TEXT,
    n_precursors  BIGINT,
    search_id     UUID,
    indexed_at    TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (lance_path, run)
);
CREATE INDEX IF NOT EXISTS idx_lane_runs_run      ON delimp_spectrum_lane_runs (run);
CREATE INDEX IF NOT EXISTS idx_lane_runs_rawpath  ON delimp_spectrum_lane_runs (raw_path);
CREATE INDEX IF NOT EXISTS idx_lane_runs_search   ON delimp_spectrum_lane_runs (search_id);
"""


def _conn():
    import psycopg2
    from refresh_leaderboards import _token
    return psycopg2.connect(
        host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
        dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
        user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
        password=_token(), sslmode="require", connect_timeout=30,
        keepalives=1, keepalives_idle=20, keepalives_interval=10, keepalives_count=6,
        options="-c statement_timeout=600000")


def scan_dataset(path):
    """[(run, raw_path, n_rows)] for one dataset.

    pyarrow's group_by does the counting in C++ — a Python loop over 354M corpus rows would not
    finish. Only two columns are read; Lance is columnar, so this touches a tiny fraction of the
    137 GB on disk."""
    import lance
    ds = lance.dataset(path)
    have = {f.name for f in ds.schema}
    cols = [c for c in ("run", "raw_path") if c in have]
    if "run" not in cols:
        return []
    t = ds.scanner(columns=cols).to_table()
    if "raw_path" not in cols:
        import pyarrow as pa
        t = t.append_column("raw_path", pa.nulls(t.num_rows, pa.string()))
    agg = t.group_by(["run", "raw_path"]).aggregate([([], "count_all")])
    d = agg.to_pydict()
    ccol = "count_all" if "count_all" in d else [k for k in d if k.startswith("count")][0]
    return [(d["run"][i], d["raw_path"][i], int(d[ccol][i])) for i in range(agg.num_rows)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rebuild", action="store_true", help="ignore checkpoints; re-index every dataset")
    ap.add_argument("--limit", type=int, default=0, help="only process N datasets (smoke test)")
    a = ap.parse_args()

    conn = _conn(); conn.autocommit = False
    cur = conn.cursor()
    if a.apply:
        for stmt in filter(str.strip, DDL.split(";")):
            cur.execute(stmt)
        conn.commit()

    cur.execute("""SELECT lance_path, search_id FROM delimp_spectrum_lane
                   WHERE lance_path IS NOT NULL ORDER BY lance_path""")
    rows = cur.fetchall()
    print(f"registry datasets: {len(rows):,}")

    done = set()
    if a.apply and not a.rebuild:
        cur.execute("SELECT DISTINCT lance_path FROM delimp_spectrum_lane_runs")
        done = {r[0] for r in cur.fetchall()}
        print(f"already indexed (checkpoint): {len(done):,}")

    todo = [(p, s) for p, s in rows if p not in done]
    if a.limit:
        todo = todo[:a.limit]
    print(f"to process: {len(todo):,}")
    if not a.apply:
        print("\nDRY RUN — re-run with --apply.")
        conn.close(); return

    t0 = time.time()
    n_ds = n_pairs = n_missing = 0
    for i, (path, sid) in enumerate(todo, 1):
        if not os.path.exists(path):
            n_missing += 1
            if n_missing <= 5:
                print(f"  MISSING on disk, skipped: {os.path.basename(path)}")
            continue
        try:
            pairs = scan_dataset(path)
        except Exception as e:  # noqa: BLE001 — one bad dataset must not kill a 1,553-dataset job
            print(f"  ERR {os.path.basename(path)[:56]}: {str(e)[:70]}")
            continue
        if not pairs:
            continue
        import psycopg2.extras
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO delimp_spectrum_lane_runs (lance_path, run, raw_path, n_precursors, search_id)
               VALUES %s
               ON CONFLICT (lance_path, run) DO UPDATE SET
                 raw_path=EXCLUDED.raw_path, n_precursors=EXCLUDED.n_precursors,
                 search_id=EXCLUDED.search_id, indexed_at=now()""",
            [(path, r, rp, n, str(sid) if sid else None) for r, rp, n in pairs], page_size=500)
        conn.commit()  # §8.1: checkpoint per dataset, so a wall-clock kill keeps everything so far
        n_ds += 1; n_pairs += len(pairs)
        if i % 25 == 0 or i == len(todo):
            el = time.time() - t0
            rate = i / el if el else 0
            print(f"  [{i:,}/{len(todo):,}] {n_ds:,} datasets, {n_pairs:,} (dataset,run) pairs, "
                  f"{el/60:.1f} min, {rate:.1f} ds/s, eta {((len(todo)-i)/rate)/60 if rate else 0:.0f} min")

    import versions as V
    V.record_run(cur, "lane_run_index", "1.0.0", notes=f"{n_ds} datasets, {n_pairs} pairs")
    conn.commit()

    cur.execute("SELECT count(*), count(DISTINCT run), count(DISTINCT lance_path) FROM delimp_spectrum_lane_runs")
    tot, nruns, nds = cur.fetchone()
    print(f"\nDONE  rows={tot:,}  distinct runs={nruns:,}  datasets={nds:,}  missing-on-disk={n_missing}")

    # The whole point: can we now select an LC-comparable cohort in SQL? DISTINCT is required —
    # raw_basename is 1:many against raw_files (resubmits reuse the name).
    cur.execute("""
        SELECT count(DISTINCT lr.run)
        FROM delimp_spectrum_lane_runs lr
        JOIN raw_files rf ON rf.raw_basename = lr.run
        WHERE rf.gradient_minutes IS NOT NULL""")
    print(f"runs reachable to gradient_minutes : {cur.fetchone()[0]:,}")
    cur.execute("""
        SELECT count(DISTINCT lr.run)
        FROM delimp_spectrum_lane_runs lr
        JOIN raw_files rf ON rf.raw_basename = lr.run
        WHERE rf.gradient_minutes BETWEEN 18 AND 22""")
    print(f"runs in the 18-22 min gradient band: {cur.fetchone()[0]:,}   <- the Phase 1 gate cohort")
    conn.close()


if __name__ == "__main__":
    main()
