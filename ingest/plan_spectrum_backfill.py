"""plan_spectrum_backfill.py — coverage planner for the observed-spectrum Lance lane.

For EVERY Spectronaut search in FRAN: is its FRAN report archived on Flinders (so
`backfill_fragments.py` can recover the observed spectrum), or MISSING (so a Windows ingestor must
re-export it from the `.sne` via `manageSNE -rs FRAN.rs`)? Uses the `delimp_search_provenance`
coordination table (source `.sne` + exported report path per search) matched against a filesystem
index of the archived reports.

Outputs:
  - coverage stats (found / missing / already-backfilled)
  - <out>/backfill_worklist.txt  — archived report paths ready to feed `backfill_fragments.py`
  - <out>/regen_queue.txt        — searches whose report is missing (name + source .sne)
  --enqueue  ALSO insert the missing ones into `delimp_spectrum_regen_queue` (a coordination table
             a Windows ingestor polls) so they get re-exported. Opt-in (a DB write).

    python plan_spectrum_backfill.py --reports-root /nfs/lssc0/flinders/proteomics/Data/FRAN_reports \
        --out ./backfill_plan [--enqueue]
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

REGEN_DDL = """
CREATE TABLE IF NOT EXISTS delimp_spectrum_regen_queue (
    search_id     UUID PRIMARY KEY,
    search_name   TEXT,
    sne_path      TEXT,          -- the source .sne to re-export (output_dir)
    report_path   TEXT,          -- the expected/last-known report path (may be a dead Windows path)
    status        TEXT DEFAULT 'pending',   -- pending | claimed | done | failed
    requested_at  TIMESTAMPTZ DEFAULT now(),
    node          TEXT,          -- which ingestor claimed it
    note          TEXT
);
CREATE INDEX IF NOT EXISTS idx_regen_status ON delimp_spectrum_regen_queue (status);
"""


def _conn():
    from refresh_leaderboards import _token
    import psycopg2
    return psycopg2.connect(host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
                            dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
                            user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
                            password=_token(), sslmode="require", connect_timeout=30)


def _base(p):
    """basename that also splits Windows backslash paths (report_path is often C:\\...)."""
    return os.path.basename(str(p).replace("\\", "/").rstrip("/"))


def _strip_ts(name):
    """drop a leading Spectronaut export timestamp 'YYYYMMDD_HHMMSS_' (on-disk names carry it,
    search_name usually doesn't) so the two forms compare equal."""
    return re.sub(r"^\d{8}_\d{6}_", "", name or "")


def report_name(p):
    b = re.sub(r"_Report_FRAN \(Normal\)\.(parquet|tsv)$", "", _base(p), flags=re.I)
    return b or _base(os.path.dirname(str(p).replace("\\", "/")))


def index_reports(roots):
    """key -> archived report path, keyed by the report name AND its timestamp-stripped form."""
    idx = {}
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dp, _, fns in os.walk(root):
            for fn in fns:
                if fn.lower().endswith((".parquet", ".tsv")) and "report_fran" in fn.lower():
                    p = os.path.join(dp, fn)
                    nm = report_name(p)
                    idx.setdefault(nm, p)
                    idx.setdefault(_strip_ts(nm), p)
    return idx


def match_keys(real_name, sname, report_path, output_dir):
    """all candidate keys to look up a search in the on-disk index (with/without timestamp)."""
    keys = set()
    for s in (sname, real_name):
        if s:
            keys.add(s); keys.add(_strip_ts(s))
    if report_path:
        keys.add(report_name(report_path)); keys.add(_strip_ts(report_name(report_path)))
    if output_dir:
        b = re.sub(r"\.sne$", "", _base(output_dir), flags=re.I)
        keys.add(b); keys.add(_strip_ts(b))
    return {k for k in keys if k}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reports-root", action="append", default=[],
                    help="dir(s) of archived FRAN reports (repeatable)")
    ap.add_argument("--out", default="./backfill_plan")
    ap.add_argument("--enqueue", action="store_true", help="insert missing searches into delimp_spectrum_regen_queue")
    a = ap.parse_args()
    roots = a.reports_root or ["/nfs/lssc0/flinders/proteomics/Data/FRAN_reports",
                               "/nfs/lssc0/flinders/proteomics/Data/FRAN_SNE_export"]
    os.makedirs(a.out, exist_ok=True)

    print(f"indexing archived reports under: {roots}")
    idx = index_reports(roots)
    print(f"  {len(idx):,} archived FRAN reports on disk")

    conn = _conn(); cur = conn.cursor()
    # already-backfilled searches (registry)
    cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name='delimp_spectrum_lane'")
    done_ids = set()
    if cur.fetchone():
        cur.execute("SELECT search_id FROM delimp_spectrum_lane")
        done_ids = {str(r[0]) for r in cur.fetchall()}
    # every Spectronaut search + its provenance
    cur.execute("""SELECT s.id, p.real_search_name, s.search_name, p.report_path, p.output_dir
                   FROM delimp_searches s
                   LEFT JOIN delimp_search_provenance p ON p.search_id=s.id
                   WHERE s.search_engine ILIKE '%spectro%'""")
    searches = cur.fetchall()
    print(f"  {len(searches):,} Spectronaut searches in FRAN "
          f"({len(done_ids):,} already have a spectrum-lane dataset)")

    found, missing = [], []
    for sid, real, sname, rpath, odir in searches:
        if str(sid) in done_ids:
            continue
        hit = None
        for k in match_keys(real, sname, rpath, odir):
            if k in idx:
                hit = idx[k]; break
        if hit:
            found.append((sid, sname or real, hit))
        else:
            missing.append((sid, real or sname, odir, rpath))

    with open(os.path.join(a.out, "backfill_worklist.txt"), "w") as f:
        f.writelines(p + "\n" for _, _, p in found)
    with open(os.path.join(a.out, "regen_queue.txt"), "w") as f:
        f.writelines(f"{name}\t{odir or ''}\t{rpath or ''}\n" for _, name, odir, rpath in missing)

    print(f"\nCOVERAGE (of the {len(searches)-len(done_ids):,} not-yet-backfilled):")
    print(f"  report archived -> BACKFILL now : {len(found):,}   -> {a.out}/backfill_worklist.txt")
    print(f"  report MISSING  -> REGEN needed  : {len(missing):,}   -> {a.out}/regen_queue.txt")
    print(f"  already backfilled (skipped)     : {len(done_ids):,}")
    print(f"\nNext: python backfill_fragments.py --scan {roots[0]} --out-dir <lance_dir> --register --workers 8")

    if a.enqueue and missing:
        cur.execute(REGEN_DDL)
        import psycopg2.extras
        psycopg2.extras.execute_values(cur,
            """INSERT INTO delimp_spectrum_regen_queue (search_id, search_name, sne_path, report_path)
               VALUES %s ON CONFLICT (search_id) DO UPDATE SET status='pending', requested_at=now()""",
            [(str(sid), name, odir, rpath) for sid, name, odir, rpath in missing], page_size=500)
        conn.commit()
        print(f"\nenqueued {len(missing):,} searches into delimp_spectrum_regen_queue (status=pending)")
        print("  a Windows ingestor re-exports each via: spectronaut manageSNE -sne <sne_path> -n <name> -o <out> -rs FRAN.rs")
    conn.close()


if __name__ == "__main__":
    main()
