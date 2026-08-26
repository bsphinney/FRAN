"""backfill_engines_confirming.py — populate delimp_precursors.n_engines_confirming.

The column existed and nothing ever wrote it: it is 1 on every row in the corpus. The peptide card
read MAX(n_engines_confirming) and therefore reported "1 engine" for IPSHAVVAR, which 213
Spectronaut, 17 DIA-NN and 1 Radiant search all report.

DEFINITION, and it is a choice worth stating. n_engines_confirming = how many DISTINCT ENGINES
reported this precursor IN THE SAME ACQUISITION. Confirmation across different runs is not
confirmation -- two engines agreeing on a peptide in two different samples says nothing about
either measurement. Same raw file means sample, instrument and gradient are held constant and the
software is the only variable, which is exactly what makes agreement evidence.

KEYED ON raw_basename, NOT raw_path. This is the whole reason the column looks impossible to fill:
each engine records the same acquisition under its OWN path string (a Windows drive path, a Hive
path, a different directory), so grouping by raw_path finds ZERO multi-engine runs across 21,059
acquisitions. Grouped by basename the same corpus has hundreds. Anyone re-deriving this from
raw_path will conclude, wrongly, that no acquisition was ever searched twice.

Precursor identity is (stripped_seq, charge): the same unit the cross-engine page counts, so the
column and the page cannot disagree. Modified forms are deliberately NOT part of the key -- engines
spell modifications differently, and requiring an exact ProForma match would count a spelling
difference as a disagreement.

Batched one acquisition at a time and committed per batch: this table has 434M rows, and a single
UPDATE would hold locks on a shared production table for the whole run.

    python backfill_engines_confirming.py            # measure only
    python backfill_engines_confirming.py --apply
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MULTI_ENGINE_RUNS = """
SELECT rf.raw_basename
  FROM search_raw_files f
  JOIN delimp_searches s ON s.id = f.search_id
  JOIN raw_files rf ON rf.raw_path = f.raw_path
 GROUP BY 1
HAVING count(DISTINCT s.search_engine) > 1
"""

# One acquisition. The UPDATE touches only rows whose value actually changes, so a re-run is cheap
# and idempotent rather than rewriting every row again.
UPDATE_ONE = """
WITH counts AS (
  SELECT p.stripped_seq, p.charge, count(DISTINCT s.search_engine) AS n_eng
    FROM delimp_precursors p
    JOIN raw_files rf ON rf.raw_path = p.raw_path
    JOIN delimp_searches s ON s.id = p.search_id
   WHERE rf.raw_basename = %s
   GROUP BY 1, 2
)
UPDATE delimp_precursors p
   SET n_engines_confirming = c.n_eng
  FROM counts c, raw_files rf
 WHERE rf.raw_path = p.raw_path
   AND rf.raw_basename = %s
   AND p.stripped_seq = c.stripped_seq
   AND p.charge = c.charge
   AND p.n_engines_confirming IS DISTINCT FROM c.n_eng
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="only this many acquisitions")
    ap.add_argument("--statement-timeout", default="600s")
    a = ap.parse_args()

    import corpus_ingest as ci
    conn = ci._conn()
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute(f"SET statement_timeout='{a.statement_timeout}'")

    t0 = time.time()
    cur.execute(MULTI_ENGINE_RUNS)
    runs = [r[0] for r in cur.fetchall()]
    conn.commit()
    print(f"{len(runs):,} multi-engine acquisitions (keyed on raw_basename) "
          f"in {time.time()-t0:.0f}s", flush=True)
    if not runs:
        print("nothing to do"); conn.close(); return
    if a.limit:
        runs = runs[:a.limit]
        print(f"  --limit: processing {len(runs)}", flush=True)

    if not a.apply:
        print("\nDRY RUN — re-run with --apply. Counting affected rows without writing:", flush=True)
        cur.execute("""
            WITH counts AS (
              SELECT rf.raw_basename, p.stripped_seq, p.charge,
                     count(DISTINCT s.search_engine) AS n_eng
                FROM delimp_precursors p
                JOIN raw_files rf ON rf.raw_path = p.raw_path
                JOIN delimp_searches s ON s.id = p.search_id
               WHERE rf.raw_basename = ANY(%s)
               GROUP BY 1, 2, 3)
            SELECT count(*) FILTER (WHERE n_eng > 1), count(*)
              FROM counts""", (runs,))
        multi, total = cur.fetchone()
        conn.rollback()
        print(f"  {total:,} distinct (run, peptide, charge) in these acquisitions")
        print(f"  {multi:,} of them were reported by MORE THAN ONE engine")
        conn.close(); return

    changed = done = 0
    t0 = time.time()
    for i, rb in enumerate(runs, 1):
        try:
            cur.execute(UPDATE_ONE, (rb, rb))
            changed += cur.rowcount
            conn.commit()
        except Exception as e:  # noqa: BLE001 - one bad acquisition must not abort the sweep
            conn.rollback()
            print(f"  [warn] {rb[:52]}: {type(e).__name__}: {str(e)[:90]}", flush=True)
            continue
        done += 1
        if i % 25 == 0 or i == len(runs):
            print(f"  {i}/{len(runs)} acquisitions, {changed:,} rows updated, "
                  f"{time.time()-t0:.0f}s", flush=True)

    cur.execute("""SELECT n_engines_confirming, count(*)
                     FROM delimp_precursors
                    WHERE n_engines_confirming > 1
                    GROUP BY 1 ORDER BY 1""")
    print("\nverify — rows now carrying multi-engine confirmation:")
    for v, n in cur.fetchall():
        print(f"  n_engines_confirming={v}: {n:,} rows")
    try:
        import versions as V
        V.record_run(cur, "backfill_engines_confirming", "1.0.0",
                     notes=f"{done} acquisitions, {changed} rows")
        conn.commit()
    except Exception as e:  # noqa: BLE001
        print(f"[warn] could not record run: {type(e).__name__}")
    conn.close()
    print(f"\nDONE: {done} acquisitions, {changed:,} rows updated")


if __name__ == "__main__":
    main()
