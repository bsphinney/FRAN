"""xic_engine_display_set.py — put a CURATED set of chromatograms behind the cross-engine page.

The XIC lane holds 3.49M precursors for the dog set, but delimp_precursor_xic -- the table the web
app can actually serve -- holds none of them. Bulk-loading the lane is not the answer: at ~6.1 KB a
row that is ~22 GB in PostgreSQL against ~13 GB as Lance, on a database already at 228 GB. The page
shows EXAMPLES, so only the examples need to be servable.

WHAT IT PICKS, and why those two groups:

  core    precursors every engine on the run reported. These are the ones where an overlay is
          honest: same peptide, same acquisition, each engine's own extraction.
  unique  precursors exactly ONE engine reported. The interesting case -- and the one that must be
          labelled carefully. Only the finder wrote a trace, because every engine's XIC export
          covers only what that engine REPORTED. A missing trace means "this engine did not report
          it here", NEVER "there was no signal there".

Ranked by how many of the run's acquisitions the precursor was seen in, then by intensity: a
one-off hit makes a poor example even when its peak is tall.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DOG_LANCE = ("/quobyte/proteomics-grp/brett/glendon/xic_lance/"
             "20260522_170136_08May2026_AmeerTaha_allDog.xic.lance")


def target_keys(conn, engine_of_lane: str, per_group: int):
    """(keys, runs, meta) — the precursors to extract, keyed (stripped_seq, charge, run)."""
    cur = conn.cursor()
    cur.execute("""SELECT s.id, s.search_engine FROM delimp_searches s
                   WHERE s.search_name IN ('diann261_Taha_dog_9file','fragpipe_taha_dog_9file')
                      OR s.search_name LIKE '20260522_170136%'""")
    by_eng = {e: str(i) for i, e in cur.fetchall()}
    if engine_of_lane not in by_eng:
        sys.exit(f"no search for lane engine {engine_of_lane}")
    cur.execute("""SELECT DISTINCT rf.raw_basename FROM delimp_precursors p
                   JOIN raw_files rf ON rf.raw_path = p.raw_path
                   WHERE p.search_id = %s::uuid""", (by_eng["diann"],))
    runs = sorted(r[0] for r in cur.fetchall())

    # Per (peptide,charge): which engines saw it, in how many runs, and its best run+intensity in
    # the lane's own engine. Done in SQL so 136k rows are never pulled to the client.
    cur.execute("""
        WITH p AS (
          SELECT pr.stripped_seq, pr.charge, rf.raw_basename AS run, s.search_engine AS eng,
                 max(pr.intensity) AS inten
            FROM delimp_precursors pr
            JOIN delimp_searches s ON s.id = pr.search_id
            JOIN raw_files rf ON rf.raw_path = pr.raw_path
           WHERE s.id = ANY(%s::uuid[]) AND rf.raw_basename = ANY(%s)
           GROUP BY 1,2,3,4),
        agg AS (
          SELECT stripped_seq, charge,
                 count(DISTINCT eng) AS n_eng,
                 count(DISTINCT run) FILTER (WHERE eng = %s) AS n_runs_lane,
                 max(inten) FILTER (WHERE eng = %s) AS inten_lane,
                 (array_agg(run ORDER BY inten DESC NULLS LAST)
                    FILTER (WHERE eng = %s))[1] AS best_run
            FROM p GROUP BY 1,2)
        SELECT stripped_seq, charge, n_eng, n_runs_lane, best_run
          FROM agg
         WHERE best_run IS NOT NULL AND (n_eng = %s OR n_eng = 1)
         ORDER BY (n_eng = %s) DESC, n_runs_lane DESC, inten_lane DESC NULLS LAST
    """, (list(by_eng.values()), runs, engine_of_lane, engine_of_lane, engine_of_lane,
          len(by_eng), len(by_eng)))

    core, uniq = [], []
    for seq, ch, n_eng, n_runs, run in cur.fetchall():
        bucket = core if n_eng == len(by_eng) else uniq
        if len(bucket) < per_group:
            bucket.append((seq, int(ch), run))
        if len(core) >= per_group and len(uniq) >= per_group:
            break
    keys = set(core) | set(uniq)
    return keys, {k[2] for k in keys}, {"core": len(core), "unique": len(uniq),
                                        "engines_on_run": len(by_eng)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=DOG_LANCE)
    ap.add_argument("--engine", default="spectronaut", help="which engine's lane this dataset is")
    ap.add_argument("--per-group", type=int, default=150)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    from ingest_perrun_xic import _conn, build_rows
    conn = _conn()
    keys, runs, meta = target_keys(conn, a.engine, a.per_group)
    print(f"target: {meta['core']} core (found by all {meta['engines_on_run']}) + "
          f"{meta['unique']} unique-to-one = {len(keys)} precursors across {len(runs)} runs",
          flush=True)

    rows = build_rows(a.dataset, keys=keys, runs=runs)
    print(f"extracted {len(rows):,} rows from the lane "
          f"({100*len(rows)/max(1,len(keys)):.0f}% of targets found)", flush=True)
    if not rows:
        sys.exit("nothing extracted — check the lane covers these runs")
    if not a.apply:
        print("DRY RUN — re-run with --apply"); return

    import psycopg2.extras
    cur = conn.cursor()
    psycopg2.extras.execute_values(cur, """
        INSERT INTO delimp_precursor_xic
          (precursor_id, stripped_seq, charge, precursor_mz, raw_path, search_id, engine,
           engine_version, rt_apex, ms1_apex, ms1, fragments, n_fragments_total,
           is_consensus, n_runs_averaged, modified_seq, run, trace_rt_basis)
        VALUES %s
        ON CONFLICT (precursor_id) DO UPDATE SET
          rt_apex=EXCLUDED.rt_apex, ms1_apex=EXCLUDED.ms1_apex, ms1=EXCLUDED.ms1,
          fragments=EXCLUDED.fragments, n_fragments_total=EXCLUDED.n_fragments_total,
          trace_rt_basis=EXCLUDED.trace_rt_basis""", rows, page_size=100)
    conn.commit()
    cur.execute("""SELECT count(*), count(DISTINCT run) FROM delimp_precursor_xic
                   WHERE run = ANY(%s)""", (sorted(runs),))
    n, nr = cur.fetchone()
    print(f"verify: {n:,} rows now servable across {nr} of the run(s)")
    conn.close()


if __name__ == "__main__":
    main()
