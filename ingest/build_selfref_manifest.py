"""build_selfref_manifest.py — choose which runs to self-reference score, and write the array manifest.

Selection is Orbitrap-family runs that have BOTH halves of the comparison available: a Hive-reachable
raw file (raw_files.hive_path) and an entry in the spectrum lane (the engine's reported RT, fragment
areas and MS1 isotope intensities). 3,945 runs qualify corpus-wide.

THE 9 TRACE-VALIDATED RUNS ARE FORCED IN FIRST. They already carry a DIA-NN trace-level verdict
(6 pass / 3 fail), so scoring them again by the scalar route produces the only thing that can
actually justify the selfref thresholds: agreement, or disagreement, against known truth. Without
that overlap SELFREF_MAX_CV is a number someone made up. record_selfref writes selfref_verdict to its
own column and will not overwrite their trace-level agree_verdict, so this costs nothing.

Runs are spread ACROSS searches rather than taken in corpus order. A contiguous block would be a
handful of large studies on one or two instruments, and a quality gate calibrated on that would not
transfer to the rest of the corpus.

    python ingest/build_selfref_manifest.py --limit 500 --out selfref_manifest.tsv
"""
import argparse
import functools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
print = functools.partial(print, flush=True)   # noqa: A001

ORB = ("f.instrument_model ILIKE '%orbitrap%' OR f.instrument_model ILIKE '%exploris%' "
       "OR f.instrument_model ILIKE '%lumos%' OR f.instrument_model ILIKE '%astral%'")


def _conn():
    import psycopg2
    from refresh_leaderboards import _token
    return psycopg2.connect(
        host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
        dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
        user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
        password=_token(), sslmode="require", connect_timeout=30,
        options="-c statement_timeout=900000")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    conn = _conn()
    q = conn.cursor()
    # Already trace-validated: score these too, as calibration against known truth.
    # Resolve their spectrum lane and raw path in the same query -- a manifest row is only usable if
    # it carries both halves, and a placeholder here would fail 9 array tasks at runtime instead of
    # failing loudly now.
    q.execute("""SELECT t.run, t.lance_path, s.lance_path, f.hive_path
                 FROM delimp_xic_trace_lane t
                 JOIN delimp_spectrum_lane_runs s ON s.run = t.run
                 JOIN delimp_raw_path_basename b  ON b.raw_path = s.raw_path
                 JOIN raw_files f                 ON f.raw_basename = b.raw_basename
                 WHERE t.agree_verdict IS NOT NULL AND t.agree_verdict <> 'unscored'
                   AND f.hive_path IS NOT NULL AND s.lance_path IS NOT NULL""")
    known = {r[0]: (r[1], r[2], r[3]) for r in q.fetchall()}
    print(f"{len(known)} trace-verdict runs resolvable to (spectrum lane, raw) — forced in as calibration")

    q.execute("""
      WITH cand AS (
        SELECT DISTINCT ON (s.run)
               s.run, s.lance_path, f.hive_path, f.instrument_model, s.search_id, s.n_precursors
        FROM delimp_spectrum_lane_runs s
        JOIN delimp_raw_path_basename b ON b.raw_path = s.raw_path
        JOIN raw_files f ON f.raw_basename = b.raw_basename
        WHERE (%s) AND f.hive_path IS NOT NULL AND s.lance_path IS NOT NULL
          AND s.n_precursors > 500
        ORDER BY s.run, s.n_precursors DESC
      )
      SELECT run, lance_path, hive_path, instrument_model, search_id,
             ROW_NUMBER() OVER (PARTITION BY search_id ORDER BY run) AS rn
      FROM cand ORDER BY rn, search_id, run""" % ORB)
    rows = q.fetchall()
    conn.close()
    print(f"{len(rows):,} Orbitrap runs qualify (reachable raw + spectrum lane + >500 precursors)")

    picked, seen = [], set(known)
    # ORDER BY rn interleaves searches: one run from each search, then a second from each, ...
    for run, lance_path, hive_path, model, sid, rn in rows:
        if run in seen:
            continue
        picked.append((run, lance_path, hive_path, model, sid))
        seen.add(run)
        if len(picked) >= a.limit - len(known):
            break

    # Columns: run, spectrum lane, raw path, model, trace_lance ('-' when the run has no stored
    # dataset yet, in which case selfref_score registers a placeholder row to attach the score to).
    with open(a.out, "w") as fh:
        for run, (trace_lp, lance_path, hive_path) in known.items():
            fh.write(f"{run}\t{lance_path}\t{hive_path}\tCALIBRATION\t{trace_lp}\n")
        for run, lance_path, hive_path, model, sid in picked:
            fh.write(f"{run}\t{lance_path}\t{hive_path}\t{model}\t-\n")
    n_search = len({r[4] for r in picked})
    print(f"wrote {a.out}: {len(known)} calibration + {len(picked)} new = "
          f"{len(known)+len(picked)} runs across {n_search} searches")
    from collections import Counter
    for m, n in Counter(r[3] for r in picked).most_common():
        print(f"   {m:34s} {n}")


if __name__ == "__main__":
    main()
