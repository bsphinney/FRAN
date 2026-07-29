r"""backfill_run_overview.py — harvest Spectronaut's per-run RunOverview.tsv into raw_files.

WHY. `STORAGE_DESIGN.md` §3 asks for `irt_calibration_source` and calls it the subtle one:

    "FRAN's `irt_empirical` is only comparable across runs to the extent the runs share an iRT
     normalisation. If that varies silently, the consensus degrades and nothing in the data says so."

That matters concretely: the 7.52 s scoped-RT result pools `irt_empirical` across ~3,480 runs on the
assumption their iRT axes agree, and today nothing lets you check.

Spectronaut does NOT record a categorical "calibration method" anywhere I could find. What it does
record, per run, is better: **`Median Absolute Delta iRT`** — the measured disagreement between this
run's empirical iRT and the library's. That turns "did these runs share a normalisation?" from an
assumption into a number you can filter on. So this script stores both:

  * `irt_calibration_source`      — WHO defined the iRT axis (engine + analysis version). Two runs
                                    sharing this share a convention.
  * `irt_cal_median_abs_delta`    — HOW WELL it landed for this run. The verification.

Everything else here is harvested because it is in the same file and costs nothing extra, and several
of these columns are 0% populated corpus-wide:

  Gradient Length [min]  -> gradient_minutes_measured  (a REAL measured gradient. Deliberately a NEW
                            column, NOT an overwrite: `gradient_minutes` is 98.7% populated from the
                            EvoSep SPD map or the observed RT span, and having both lets you finally
                            validate that proxy instead of silently replacing it.)
  Cycle Time (MS1)       -> cycle_time_sec             (0% populated today)
  Instrument Name / SN   -> instrument_model / _serial (COALESCE — fills runs whose raw is unreachable,
                            which is most of the Thermo half)
  Run Date (Formatted)   -> acquisition_date           (COALESCE)
  everything else        -> run_overview_json          (record the evidence; ~50 keys, small)

FILE LAYOUT, measured — the glob depth matters and cost me a wrong answer once:
    FRAN_reports/<experiment>/<YYYYMMDD_HHMMSS_export_name>/RunSummaries/<run>.d_1_RunOverview.tsv
i.e. RunSummaries sits TWO levels below the archive root, not one. `<run>` matches
`raw_files.raw_basename`, which is the join key (see build_lane_run_index.py — raw_path does not join).

Checkpointed and flushed per STORAGE_DESIGN.md §8.1/§8.2. Run on a compute node: walking the archive
is a large filesystem traversal.

    python ingest/backfill_run_overview.py                 # dry run: coverage report only
    python ingest/backfill_run_overview.py --apply
"""
import argparse
import functools
import glob
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

print = functools.partial(print, flush=True)  # noqa: A001 — §8.2

ROOTS = [
    "/nfs/lssc0/flinders/proteomics/Data/FRAN_reports",
    "/nfs/lssc0/flinders/proteomics/Data/FRAN_SNE_export",
    "/quobyte/proteomics-grp/brett/sn21",
]

DDL = """
ALTER TABLE raw_files ADD COLUMN IF NOT EXISTS irt_calibration_source    TEXT;
ALTER TABLE raw_files ADD COLUMN IF NOT EXISTS irt_cal_median_abs_delta  REAL;
ALTER TABLE raw_files ADD COLUMN IF NOT EXISTS rt_cal_median_abs_delta   REAL;
ALTER TABLE raw_files ADD COLUMN IF NOT EXISTS gradient_minutes_measured REAL;
ALTER TABLE raw_files ADD COLUMN IF NOT EXISTS run_overview_json         JSONB;
"""


def _flt(v):
    """Spectronaut writes '0.7 min', '181.65 (NaN)', '7,517 of 7,517' — take the leading number."""
    if v is None:
        return None
    m = re.match(r"\s*(-?[\d,]*\.?\d+)", str(v).replace(",", ""))
    if not m:
        return None
    try:
        f = float(m.group(1))
        return None if f != f else f  # drop NaN
    except ValueError:
        return None


def _date(v):
    """'5/12/2026 1:29:19 PM' -> ISO. US M/D/Y with 12-hour clock; not ISO, so parse explicitly."""
    if not v:
        return None
    s = str(v).strip()
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M %p",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).isoformat(timespec="seconds")
        except ValueError:
            continue
    return None


def run_of(path):
    """'<run>.htrms_10_RunOverview.tsv' -> '<run>'.

    The extension is usually **.htrms**, not .d/.raw — Spectronaut converts to htrms first, and the
    RunSummaries filenames carry the converted name. Missing that matches nothing at all. The numeric
    suffix before _RunOverview is a per-run index and is not zero-padded consistently
    ('..._09_' and '..._10_'), so it is stripped by pattern, not by length."""
    b = os.path.basename(path)
    b = re.sub(r"_\d+_RunOverview\.tsv$", "", b)
    b = re.sub(r"_RunOverview\.tsv$", "", b)
    return re.sub(r"\.(htrms|d|raw|mzml|mzML)$", "", b, flags=re.I)


def parse(path):
    """RunOverview.tsv is a two-column Parameter/Value table, not a wide one."""
    kv = {}
    try:
        with open(path, errors="replace") as fh:
            for i, line in enumerate(fh):
                parts = line.rstrip("\n").split("\t")
                if i == 0 and parts and parts[0].strip().lower() == "parameter":
                    continue
                if len(parts) >= 2 and parts[0].strip():
                    kv[parts[0].strip()] = parts[1].strip()
    except OSError:
        return None
    return kv or None


def find_files():
    out = {}
    for r in ROOTS:
        if not os.path.isdir(r):
            print(f"  (root absent: {r})")
            continue
        # RunSummaries is TWO levels down in FRAN_reports; sn21 has it one level down. Cover both.
        pats = [os.path.join(r, "*", "*", "RunSummaries", "*RunOverview.tsv"),
                os.path.join(r, "*", "RunSummaries", "*RunOverview.tsv")]
        n0 = len(out)
        for p in pats:
            for f in glob.glob(p):
                out.setdefault(run_of(f), f)   # first wins; re-exports collapse to one
        print(f"  {r}: +{len(out) - n0:,} runs")
    return out


NEW_COLS = ("irt_calibration_source", "irt_cal_median_abs_delta", "rt_cal_median_abs_delta",
            "gradient_minutes_measured", "run_overview_json")


def ensure_columns(conn):
    """Add the new raw_files columns, but NEVER queue for the lock. Returns True if all are present.

    This bit is load-bearing and was learned the hard way on 2026-07-29. `ADD COLUMN` needs an
    AccessExclusiveLock on raw_files. The first run of this script queued behind the concurrent Thermo
    metadata backfill's open transaction and sat ungranted for 475 s — and while an ungranted
    AccessExclusiveLock is in the queue, Postgres makes every SUBSEQUENT reader queue behind it too,
    including the live website's. That is the same shape as the 2026-06-15 PG-Farm stall that
    corpus_ingest.py's comments warn about.

    Two guards, both from that comment block:
      1. Check the catalog first — a lockless read. If the columns exist, take no lock at all.
      2. `SET LOCAL lock_timeout` so an ALTER that cannot get the lock fails in seconds instead of
         blocking the database. Retry later rather than wait.
    """
    cur = conn.cursor()
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_schema='public' AND table_name='raw_files'""")
    have = {r[0] for r in cur.fetchall()}
    missing = [c for c in NEW_COLS if c not in have]
    if not missing:
        return True
    print(f"  adding columns: {', '.join(missing)}")
    try:
        cur.execute("SET LOCAL lock_timeout = '5s'")
        for stmt in filter(str.strip, DDL.split(";")):
            cur.execute(stmt)
        conn.commit()
        print("  columns added")
        return True
    except Exception as e:  # noqa: BLE001 — lock_timeout fires as a normal error
        conn.rollback()
        print(f"  COULD NOT ALTER raw_files: {str(e).strip()[:120]}")
        print("  Something else is writing raw_files (check: SELECT * FROM pg_stat_activity WHERE")
        print("  query ILIKE '%raw_files%'). NOT waiting — a queued AccessExclusiveLock would block")
        print("  every reader including the website. Re-run when the other writer has finished.")
        return False


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    print("scanning for RunOverview.tsv ...")
    files = find_files()
    print(f"distinct runs with a RunOverview: {len(files):,}")
    if not files:
        sys.exit("nothing found — check the archive roots")

    conn = _conn(); conn.autocommit = False
    cur = conn.cursor()
    if a.apply and not ensure_columns(conn):
        sys.exit("aborting: columns are missing and could not be added right now — see message above")

    names = list(files)
    cur.execute("SELECT raw_basename FROM raw_files WHERE raw_basename = ANY(%s)", (names,))
    known = {r[0] for r in cur.fetchall()}
    print(f"of those, present in raw_files: {len(known):,}  (unmatched {len(names) - len(known):,})")

    todo = [(n, files[n]) for n in names if n in known]
    if a.limit:
        todo = todo[:a.limit]
    if not a.apply:
        # Show what a real run would write, without writing.
        sample = [t for t in todo[:3]]
        for n, p in sample:
            kv = parse(p) or {}
            print(f"\n  {n}")
            print(f"     irt delta   {_flt(kv.get('Median Absolute Delta iRT'))}")
            print(f"     rt delta    {_flt(kv.get('Median Absolute Delta RT'))}")
            print(f"     gradient    {_flt(kv.get('Gradient Length [min]'))} min (measured)")
            print(f"     cycle MS1   {_flt(kv.get('Cycle Time (MS1)'))} s")
            print(f"     instrument  {kv.get('Instrument Name')!r} / {kv.get('Instrument SN')!r}")
            print(f"     run date    {_date(kv.get('Run Date (Formatted)'))}")
            print(f"     analysis    {kv.get('Analysis Version')!r}")
            print(f"     keys        {len(kv)}")
        print(f"\nDRY RUN — {len(todo):,} runs would be updated. Re-run with --apply.")
        conn.close(); return

    n_ok = n_skip = n_written = 0
    import psycopg2.extras
    batch = []
    for i, (run, path) in enumerate(todo, 1):
        kv = parse(path)
        if not kv:
            n_skip += 1
            continue
        engine_ver = kv.get("Analysis Version")
        src = f"spectronaut:{engine_ver}" if engine_ver else "spectronaut:unknown"
        batch.append((
            run, src,
            _flt(kv.get("Median Absolute Delta iRT")),
            _flt(kv.get("Median Absolute Delta RT")),
            _flt(kv.get("Gradient Length [min]")),
            _flt(kv.get("Cycle Time (MS1)")) or _flt(kv.get("Cycle Time (MS2)")),
            kv.get("Instrument Name") or None,
            kv.get("Instrument SN") or None,
            _date(kv.get("Run Date (Formatted)")),
            json.dumps(kv),
        ))
        n_ok += 1
        if len(batch) >= 400 or i == len(todo):
            psycopg2.extras.execute_batch(cur, """
                UPDATE raw_files SET
                  irt_calibration_source    = COALESCE(%s, irt_calibration_source),
                  irt_cal_median_abs_delta  = COALESCE(%s, irt_cal_median_abs_delta),
                  rt_cal_median_abs_delta   = COALESCE(%s, rt_cal_median_abs_delta),
                  gradient_minutes_measured = COALESCE(%s, gradient_minutes_measured),
                  cycle_time_sec            = COALESCE(%s, cycle_time_sec),
                  instrument_model          = COALESCE(instrument_model, %s),
                  instrument_serial         = COALESCE(instrument_serial, %s),
                  acquisition_date          = COALESCE(acquisition_date, %s::timestamptz),
                  run_overview_json         = COALESCE(%s::jsonb, run_overview_json)
                WHERE raw_basename = %s""",
                [(b[1], b[2], b[3], b[4], b[5], b[6], b[7], b[8], b[9], b[0]) for b in batch],
                page_size=200)
            n_written += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            conn.commit()      # §8.1: checkpoint
            batch = []
            print(f"  [{i:,}/{len(todo):,}] parsed={n_ok:,} skipped={n_skip:,}")

    import versions as V
    V.record_run(cur, "run_overview_backfill", "1.0.0", notes=f"{n_ok} runs")
    conn.commit()

    print("\n=== resulting coverage ===")
    for c in ("irt_calibration_source", "irt_cal_median_abs_delta", "gradient_minutes_measured",
              "cycle_time_sec", "instrument_model", "acquisition_date"):
        cur.execute(f"SELECT count({c}) FROM raw_files")
        print(f"  {c:26s} {cur.fetchone()[0]:>7,} / 19,874")

    # The point of the exercise: does the measured gradient agree with the proxy?
    cur.execute("""SELECT count(*), round(avg(abs(gradient_minutes - gradient_minutes_measured))::numeric,2),
                          round(max(abs(gradient_minutes - gradient_minutes_measured))::numeric,2)
                   FROM raw_files
                   WHERE gradient_minutes IS NOT NULL AND gradient_minutes_measured IS NOT NULL""")
    n, avg, mx = cur.fetchone()
    print(f"\n  proxy vs measured gradient: n={n:,} mean|diff|={avg} max|diff|={mx}")
    cur.execute("""SELECT count(DISTINCT irt_calibration_source) FROM raw_files
                   WHERE irt_calibration_source IS NOT NULL""")
    print(f"  distinct iRT calibration sources: {cur.fetchone()[0]:,}")
    conn.close()
    print("DONE")


if __name__ == "__main__":
    main()
