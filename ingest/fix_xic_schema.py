"""fix_xic_schema.py — make delimp_precursor_xic honest about what a row is.

BACKGROUND. An engine-side consumer joined this table on (stripped_seq, charge), extracted at
`rt_apex`, and got a median RT offset of 388 seconds against precursors their engine identifies at
q=0.0009. The number was absurd enough to catch; a subtler one would have shipped. Their brief
(FRAN_XIC_SCHEMA_FIX_BRIEF.md, 2026-08-10) is correct on every verified claim.

FOUR THINGS A CONSUMER CANNOT CURRENTLY KNOW, all fixed or documented here:

1. EVERY ROW IS A CROSS-RUN AVERAGE, and the only signal is the string 'avg of N runs' sitting in a
   column named `raw_path`. Verified: 264,275 of 264,275 rows. Nobody should have to parse English
   to learn the grain of a table. -> is_consensus + n_runs_averaged columns, populated from the
   string.

2. `rt_apex` IS NOT AN AVERAGE. Reading xic_ingest.py `_records()`: rt_apex is `_apex_rt(t["ms1"])`
   taken from whichever single run had the highest MS1 apex intensity. So it is a real absolute RT
   from ONE arbitrary run of the set -- and if that run used a different gradient than yours, the
   offset is unbounded. This, not the averaging itself, is the direct cause of the 388 s.

3. `trace[].rt` IS RELATIVE, in minutes from the apex, spanning [-0.5, +0.5] on a 41-point grid
   (AVG_W/AVG_K). It is NOT an absolute retention time and must never be compared to one. Verified
   live: trace[0].rt = -0.5 and trace[40].rt = +0.5 on every row sampled.

4. MULTIPLE ROWS PER (stripped_seq, charge) ARE DIFFERENT MOLECULES, not competing aggregates. The
   brief guessed these were averages over different chromatographic methods. Measured, the 9 rows for
   EEKDPGMGAMGGMGGGMGGGMF z=2 are eight methionine-oxidation states (UNIMOD:35) from ONE search plus
   one unmodified form from another:

       EEKDPGMGAMGGM[UNIMOD:35]GGGMGGGM[UNIMOD:35]F2   rt  6.577
       EEKDPGM[UNIMOD:35]GAMGGMGGGMGGGMF2              rt  7.712
       EEKDPGMGAMGGMGGGMGGGMF2                         rt 24.993   (different search)

   Oxidised methionine is more polar and elutes earlier; these are chemically distinct species that
   SHOULD have different RTs. Joining on (stripped_seq, charge) silently mixes them and picks one at
   random. The modified sequence was always present, buried inside precursor_id -> promote it to its
   own column so the correct join key is visible.

The averaging method, for the record (xic_ingest.py `_avg_on_grid`): each run's trace is APEX-ALIGNED
(its own apex RT subtracted), resampled onto the shared relative grid by linear interpolation with
zero fill outside, then averaged with an unweighted arithmetic MEAN across runs. Because alignment is
per-trace, mixing gradients does NOT smear the consensus SHAPE -- the widths are real. `rel_intensity`
comes from the averaged trace, not an average of per-run ratios.

    python ingest/fix_xic_schema.py            # dry run
    python ingest/fix_xic_schema.py --apply
"""
import argparse
import functools
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
print = functools.partial(print, flush=True)   # noqa: A001

COLS = [
    ("is_consensus", "boolean"),
    ("n_runs_averaged", "integer"),
    ("modified_seq", "text"),
    ("run", "text"),
]

COMMENTS = [
    ("delimp_precursor_xic", None,
     "One row per PRECURSOR-PER-SEARCH. Every row is built by averaging across the runs a search "
     "covers, but 136,977 rows average exactly ONE run and carry that run's name in `run` -- those "
     "are genuine per-run XICs. For the rest, no single row corresponds to any single acquisition. "
     "Traces are apex-aligned before averaging, so consensus SHAPE and WIDTH stay meaningful. "
     "Correct join key is (modified_seq, charge), NOT stripped_seq."),
    ("delimp_precursor_xic", "raw_path",
     "MISNAMED, kept only for compatibility: it holds the string 'avg of N runs', never a path. Use "
     "is_consensus and n_runs_averaged instead; both are derived from this string."),
    ("delimp_precursor_xic", "is_consensus",
     "True on every row, because every row went through the averaging path. It is NOT the field that "
     "tells you whether a row describes one acquisition -- use n_runs_averaged = 1, or better, "
     "`run IS NOT NULL`. Retained because the brief asked for it and it beats parsing raw_path."),
    ("delimp_precursor_xic", "n_runs_averaged",
     "How many runs went into the average. Measured range 1-319 (the 319 is a real 319-run "
     "Desmodus rotundus search, not a parse artifact). n=1 on 138,709 rows -- those are single "
     "acquisitions, and 136,977 of them have the run named in `run`."),
    ("delimp_precursor_xic", "modified_seq",
     "The MODIFIED sequence, parsed out of precursor_id. THE CORRECT JOIN KEY, with charge. Joining "
     "on stripped_seq alone merges distinct molecules: EEKDPGMGAMGGMGGGMGGGMF z=2 has 9 rows that "
     "are 8 methionine-oxidation states plus the unmodified form, eluting 6.6-25.0 min. Oxidised "
     "Met is more polar and elutes earlier -- the RT spread is real chemistry, not noise."),
    ("delimp_precursor_xic", "rt_apex",
     "ABSOLUTE RT in minutes, taken from whichever SINGLE run had the highest MS1 apex -- it is NOT "
     "an average and NOT tied to any run you care about. If that run used a different gradient than "
     "yours the offset is unbounded; extracting at this RT against another acquisition produced a "
     "388-second median error for one consumer. Use it to identify the peak, never to seed an "
     "extraction window in a different run."),
    ("delimp_precursor_xic", "fragments",
     "Per-ion jsonb: mz, label ('y7^1'), type, series, charge, apex, rel_intensity, score, trace. "
     "trace is 41 points of {i, rt} where **rt is RELATIVE MINUTES FROM THE APEX**, spanning "
     "[-0.5, +0.5] -- NOT an absolute retention time. Add rt_apex to convert, bearing in mind the "
     "caveat on that column. rel_intensity is computed from the averaged trace."),
    ("delimp_precursor_xic", "ms1",
     "MS1 isotope trace, same shape and same relative-RT convention as fragments[].trace."),
    ("delimp_precursor_xic", "run",
     "The raw_basename this row's trace came from, populated ONLY where n_runs_averaged = 1 AND the "
     "search covers exactly one raw file -- 136,977 rows. Where it is non-NULL the row is a genuine "
     "PER-RUN XIC of a named acquisition, and rt_apex is that run's own absolute RT rather than an "
     "arbitrary pick, so it is safe to seed an extraction window with. NULL means the trace is an "
     "average over runs that cannot be individually named."),
    ("delimp_precursor_xic", "search_id",
     "TEXT, and 13 of 23 distinct values are name-slugs rather than UUIDs, so this does NOT reliably "
     "join to delimp_searches.id."),
]


def _conn():
    import psycopg2
    from refresh_leaderboards import _token
    return psycopg2.connect(
        host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
        dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
        user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
        password=_token(), sslmode="require", connect_timeout=30,
        options="-c statement_timeout=600000")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    conn = _conn(); conn.autocommit = False
    cur = conn.cursor()
    # ADD COLUMN takes an ACCESS EXCLUSIVE lock even when it is a no-op, and this corpus has already
    # had readers blocked once by an ALTER queued behind a long job. Pre-check, then bound the wait.
    cur.execute("SET LOCAL lock_timeout = '10s'")
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_name='delimp_precursor_xic'""")
    have = {r[0] for r in cur.fetchall()}
    todo = [(c, t) for c, t in COLS if c not in have]
    print(f"columns to add: {[c for c, _ in todo] or 'none (already present)'}")

    cur.execute("""SELECT count(*), count(*) FILTER (WHERE raw_path LIKE 'avg of%')
                   FROM delimp_precursor_xic""")
    total, avg = cur.fetchone()
    print(f"rows: {total:,}; matching 'avg of N runs': {avg:,} ({100*avg/total:.1f}%)")

    if not a.apply:
        print("\nDRY RUN — re-run with --apply. Adds columns, backfills them from data already in "
              "the table, and attaches COMMENTs. No existing value is modified.")
        conn.rollback(); conn.close(); return

    for c, t in todo:
        cur.execute(f"ALTER TABLE delimp_precursor_xic ADD COLUMN IF NOT EXISTS {c} {t}")
    conn.commit()
    print(f"added {len(todo)} columns")

    # is_consensus / n_runs_averaged straight out of the string they were always encoded in
    cur.execute(r"""
        UPDATE delimp_precursor_xic
        SET is_consensus = (raw_path LIKE 'avg of%'),
            n_runs_averaged = NULLIF(substring(raw_path from 'avg of ([0-9]+) runs'), '')::int
        WHERE is_consensus IS NULL OR n_runs_averaged IS NULL""")
    print(f"populated is_consensus / n_runs_averaged on {cur.rowcount:,} rows")
    conn.commit()

    # modified_seq: precursor_id is '<modified sequence><charge>', e.g.
    # 'EEKDPGM[UNIMOD:35]GAMGGMGGGMGGGMF2'. Strip the trailing charge digits.
    cur.execute(r"""
        UPDATE delimp_precursor_xic
        SET modified_seq = regexp_replace(precursor_id, '[0-9]+$', '')
        WHERE modified_seq IS NULL AND precursor_id IS NOT NULL""")
    print(f"populated modified_seq on {cur.rowcount:,} rows")
    conn.commit()

    # A single-run row IS a per-run XIC -- the brief reported "FRAN holds no per-run XICs at all",
    # which is true of raw_path but not of the data: 138,709 rows have n_runs_averaged = 1, and
    # 136,977 of those belong to a search covering exactly one raw file, so the run is knowable.
    # Naming it costs one UPDATE and converts them from "an average of 1" into a usable reference.
    cur.execute("""
        WITH one AS (
          SELECT s.id::text AS sid, s.search_name, min(rf.raw_basename) AS run
          FROM delimp_searches s
          JOIN search_raw_files srf ON srf.search_id = s.id
          JOIN raw_files rf ON rf.raw_path = srf.raw_path
          GROUP BY 1, 2 HAVING count(DISTINCT rf.raw_basename) = 1)
        UPDATE delimp_precursor_xic x SET run = one.run
        FROM one
        WHERE x.n_runs_averaged = 1 AND x.run IS NULL
          AND (one.sid = x.search_id::text OR one.search_name = x.search_id::text)""")
    print(f"named the run on {cur.rowcount:,} single-run rows (now genuine per-run XICs)")
    conn.commit()

    cur.execute("""CREATE INDEX IF NOT EXISTS idx_xic_run ON delimp_precursor_xic (run)
                   WHERE run IS NOT NULL""")
    cur.execute("""CREATE INDEX IF NOT EXISTS idx_xic_modseq_charge
                   ON delimp_precursor_xic (modified_seq, charge)""")
    conn.commit()
    print("index idx_xic_modseq_charge ready")

    for tbl, col, txt in COMMENTS:
        target = f'"{tbl}"."{col}"' if col else f'"{tbl}"'
        cur.execute(f"COMMENT ON {'COLUMN' if col else 'TABLE'} {target} IS %s", (txt,))
    conn.commit()
    print(f"attached {len(COMMENTS)} comments")

    # ---- verify -------------------------------------------------------------------------
    cur.execute("""SELECT count(*), count(*) FILTER (WHERE is_consensus),
                          min(n_runs_averaged), max(n_runs_averaged),
                          count(*) FILTER (WHERE n_runs_averaged IS NULL),
                          count(*) FILTER (WHERE modified_seq IS NULL)
                   FROM delimp_precursor_xic""")
    t, ic, lo, hi, nn, nm = cur.fetchone()
    print(f"\nverify: {t:,} rows | is_consensus true {ic:,} | n_runs_averaged {lo}-{hi} "
          f"({nn:,} null) | modified_seq null {nm:,}")

    # the whole point: modified_seq must SPLIT what stripped_seq merged
    cur.execute("""SELECT count(DISTINCT (stripped_seq, charge)),
                          count(DISTINCT (modified_seq, charge)) FROM delimp_precursor_xic""")
    ss, ms = cur.fetchone()
    print(f"distinct (stripped_seq,charge)={ss:,} vs (modified_seq,charge)={ms:,} "
          f"-> the stripped key merges {ms-ss:,} distinct molecules")

    cur.execute("""SELECT count(*), count(DISTINCT run) FROM delimp_precursor_xic
                   WHERE run IS NOT NULL""")
    nr, ndr = cur.fetchone()
    print(f"per-run rows with a named run: {nr:,} across {ndr:,} distinct runs")

    if nm or nn:
        print("!! unexpected NULLs — investigate before consumers rely on these columns")

    import versions as V
    V.record_run(cur, "xic_schema_fix", "1.0.0",
                 notes=f"is_consensus/n_runs_averaged/modified_seq on {t} rows")
    conn.commit(); conn.close()
    print("DONE")


if __name__ == "__main__":
    main()
