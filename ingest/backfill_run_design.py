"""backfill_run_design.py — harvest per-run EXPERIMENTAL DESIGN from the Spectronaut reports.

THE GAP. FRAN has never recorded what a run *is* experimentally. Every design column on
delimp_sample_metadata is 0 of 19,874: tissue_name, disease_name, cell_line_name,
biological_replicate, technical_replicate, fraction, label_type, enrichment, sdrf_row_json. So
"find me a dataset with experimental conditions" -- the most common question when hunting test data
-- cannot be answered, and people fall back to reading run names.

The information exists. Spectronaut's reports carry R_Condition and R_Replicate per run, assigned by
whoever set up the analysis. Measured over 28 randomly sampled reports (42,997,403 rows read in
full, not a row-group peek): **14 of 28 carry two or more real conditions**, and they are genuine
designs, not placeholders --

    ctrl / patient          Old / Ctrl / New         SG / Control
    zfx / mIgG              GFP / Ankle2 / Ankle1    NAC / PAG / PFC
    mAb53 / mAb54 / inf     A-F / B-F / C-F / D-F

Several are directly useful beyond design: NAC/PAG/PFC are brain regions (tissue signal), and
GFP/Ankle1/Ankle2 are IP baits.

WHY THIS IS A SEPARATE, CHEAP JOB. It was originally planned as part of the big fragment re-parse on
the grounds that every payload needs the same 464 GB scan. That reasoning is WRONG for Parquet:
it is columnar and prunes, so reading five string columns is nothing like reading the fragment
columns. Measured: 5 reports / 40.3M rows in 10.7s -> ~2.1s per report -> **~72 minutes for all
2,024**, against days for the fragment re-parse. Do not bundle them.

Sentinels are nulled the way ingest/organism.py nulls 'Unknown': 'Not Defined' (Spectronaut's
default when nobody assigned groups) and 'no group specified' are ABSENCE, not a condition named
"Not Defined". The raw value is kept alongside so nothing is lost.

    python ingest/backfill_run_design.py --limit 5        # taste
    sbatch ingest/backfill_run_design.sbatch --apply      # the real run
"""
import argparse
import functools
import glob
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
print = functools.partial(print, flush=True)   # noqa: A001

ROOTS = ["/nfs/lssc0/flinders/proteomics/Data/FRAN_reports",
         "/quobyte/proteomics-grp/brett/sn21"]

# R_FileName is the run identifier and matches raw_files.raw_basename (verified: values like
# 'Ex030425_HeL50_30m_1', no extension) -- the same key build_lane_run_index.py joins on, and NOT
# raw_path, which is a synthetic Windows path.
COLS = ["R_FileName", "R_Condition", "R_Replicate", "R_Fraction", "R_Label"]

SENTINELS = {"", "not defined", "no group specified", "none", "nan", "null", "n/a", "na",
             "unknown", "undefined", "not applicable", "-"}

DDL = """
CREATE TABLE IF NOT EXISTS delimp_run_design (
    report_name    text NOT NULL,
    raw_basename   text NOT NULL,
    condition      text,          -- sentinels nulled; this is the one to query
    condition_raw  text,          -- exactly what Spectronaut wrote, including 'Not Defined'
    replicate      text,
    fraction       text,
    label          text,
    ingested_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (report_name, raw_basename)
);
CREATE INDEX IF NOT EXISTS idx_run_design_basename  ON delimp_run_design (raw_basename);
CREATE INDEX IF NOT EXISTS idx_run_design_condition ON delimp_run_design (condition)
    WHERE condition IS NOT NULL;
"""


def clean(v):
    """Sentinel -> None. Absence is not a condition named 'Not Defined'."""
    if v is None:
        return None
    s = str(v).strip()
    return None if s.lower() in SENTINELS else s


def find_reports():
    out = []
    for r in ROOTS:
        out += glob.glob(os.path.join(r, "*", "*Report*.parquet"))
        out += glob.glob(os.path.join(r, "*", "*", "*Report*.parquet"))
    return sorted(set(out))


def read_one(path):
    """[(basename, condition_raw, replicate, fraction, label)] distinct, for one report."""
    import pyarrow.parquet as pq
    names = set(pq.ParquetFile(path).schema_arrow.names)
    have = [c for c in COLS if c in names]
    if "R_FileName" not in have:
        return None
    t = pq.read_table(path, columns=have)
    # distinct over the whole file: a report has millions of fragment rows but only a handful of runs
    import pyarrow as pa
    tbl = t.group_by(have).aggregate([])
    d = tbl.to_pydict()
    n = tbl.num_rows
    get = lambda c: d[c] if c in d else [None] * n            # noqa: E731
    fn, cond, rep = get("R_FileName"), get("R_Condition"), get("R_Replicate")
    frac, lab = get("R_Fraction"), get("R_Label")
    return [(str(fn[i]), cond[i], rep[i], frac[i], lab[i]) for i in range(n) if fn[i] is not None]


def _conn():
    import psycopg2
    from refresh_leaderboards import _token
    return psycopg2.connect(
        host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
        dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
        user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
        password=_token(), sslmode="require", connect_timeout=30,
        options="-c statement_timeout=300000")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--all", action="store_true", help="re-read reports already ingested")
    a = ap.parse_args()

    reports = find_reports()
    print(f"{len(reports):,} report parquets found")

    conn = _conn(); conn.autocommit = False
    cur = conn.cursor()
    cur.execute("SET LOCAL lock_timeout = '10s'")
    if a.apply:
        cur.execute(DDL); conn.commit(); print("table delimp_run_design ready")
        if not a.all:
            cur.execute("SELECT DISTINCT report_name FROM delimp_run_design")
            done = {r[0] for r in cur.fetchall()}
            before = len(reports)
            reports = [p for p in reports if os.path.basename(p) not in done]
            print(f"{before - len(reports):,} already ingested, {len(reports):,} to do")
    if a.limit:
        reports = reports[:a.limit]

    import psycopg2.extras
    t0 = time.time(); n_rows = 0; n_ok = 0; n_skip = 0; n_real = 0
    for i, p in enumerate(reports, 1):
        name = os.path.basename(p)
        try:
            rows = read_one(p)
        except Exception as e:                                    # noqa: BLE001
            print(f"  SKIP {name[:52]}: {type(e).__name__} {str(e)[:60]}"); n_skip += 1; continue
        if not rows:
            n_skip += 1; continue
        vals = [(name, bn, clean(c), (str(c) if c is not None else None),
                 clean(r), clean(f), clean(l)) for bn, c, r, f, l in rows]
        if any(v[2] for v in vals):
            n_real += 1
        n_ok += 1; n_rows += len(vals)
        if a.apply:
            psycopg2.extras.execute_values(cur, """
                INSERT INTO delimp_run_design
                  (report_name, raw_basename, condition, condition_raw, replicate, fraction, label)
                VALUES %s
                ON CONFLICT (report_name, raw_basename) DO UPDATE SET
                  condition=EXCLUDED.condition, condition_raw=EXCLUDED.condition_raw,
                  replicate=EXCLUDED.replicate, fraction=EXCLUDED.fraction,
                  label=EXCLUDED.label, ingested_at=now()""", vals, page_size=500)
            conn.commit()     # checkpoint per report: a kill costs one report, not the run
        if i % 50 == 0 or i == len(reports):
            el = time.time() - t0; rate = i / el if el else 0
            print(f"  [{i:,}/{len(reports):,}] {n_rows:,} run-rows, {n_real:,} reports with a real "
                  f"condition, {el/60:.1f} min, eta {((len(reports)-i)/rate)/60 if rate else 0:.0f} min")

    print(f"\n{n_ok:,} reports read, {n_skip:,} skipped, {n_rows:,} (report, run) rows")
    print(f"{n_real:,} reports carry at least one real condition")
    if not a.apply:
        print("\nDRY RUN — re-run with --apply"); conn.rollback(); conn.close(); return

    cur.execute("""SELECT count(*), count(DISTINCT raw_basename), count(condition),
                          count(DISTINCT condition) FROM delimp_run_design""")
    tot, runs, withc, distinct = cur.fetchone()
    print(f"\nstored {tot:,} rows over {runs:,} distinct runs; "
          f"{withc:,} have a real condition ({distinct:,} distinct values)")
    cur.execute("""SELECT count(DISTINCT d.raw_basename) FROM delimp_run_design d
                   JOIN raw_files rf ON rf.raw_basename = d.raw_basename""")
    print(f"runs that join to raw_files: {cur.fetchone()[0]:,}")

    import versions as V
    V.record_run(cur, "run_design_backfill", "1.0.0", notes=f"{tot} rows, {runs} runs")
    conn.commit(); conn.close()
    print("DONE")


if __name__ == "__main__":
    main()
