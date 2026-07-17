"""backfill_fragments.py — recover the REAL observed MS2 fragments from the archived
Spectronaut FRAN reports, WITHOUT re-searching, re-exporting, or predicting anything.

Why this exists
---------------
The bulk Spectronaut ingest exported fragment-level reports (`..._Report_FRAN (Normal).parquet`,
with the `F.Frg*` columns), but `corpus_ingest.py` used to COLLAPSE fragment rows down to one
precursor row and DROP the fragments (their home — the XIC lane — was never built). So FRAN's
`delimp_precursors` has no fragments. The fragments are still in the archived report parquets on
Flinders. This tool re-parses those reports and writes the observed-fragment Parquet lane
(one shard per search) — the exact same lane the fixed `corpus_ingest.py` now writes going forward.

Fragment source = the search engine's own report. No Koina, no sequence y/b guessing.

Archived reports live under (verified 2026-07-17):
    /nfs/lssc0/flinders/proteomics/Data/FRAN_reports/<name>/<ts>_<name>/<name>_Report_FRAN (Normal).parquet
    /nfs/lssc0/flinders/proteomics/Data/FRAN_SNE_export/...

Usage
-----
    # one report -> one fragment shard
    python backfill_fragments.py "<...>_Report_FRAN (Normal).parquet" --out-dir /path/to/fragments

    # sweep a whole tree of archived reports
    python backfill_fragments.py --scan /nfs/lssc0/flinders/proteomics/Data/FRAN_reports \
        --out-dir /quobyte/proteomics-grp/brett/glendon/fragments --register

    --register  also sets delimp_searches.fragments_parquet_path (matches the search by output_dir
                / real_search_name via delimp_search_provenance). Omit for a pure file-side backfill.
    --limit N   cap number of reports (for a smoke test).
    --dry-run   parse + report fragment counts, write nothing.

Each shard: one row per observed fragment — search_key, raw_path, run, stripped_seq,
modified_seq_proforma, charge, precursor_mz, rt, im, frg_mz, frg_type, frg_num, frg_charge,
frg_loss, frg_intensity. Group by (run, stripped_seq/modified_seq_proforma, charge) to assemble
the acquired spectrum for a precursor.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import spectronaut_to_corpus as s2c   # noqa: E402


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name or "search")


def report_name(report_path: str) -> str:
    """Derive a stable search name from a `..._Report_FRAN (Normal).parquet` path."""
    b = os.path.basename(report_path)
    b = re.sub(r"_Report_FRAN \(Normal\)\.(parquet|tsv)$", "", b, flags=re.I)
    return b or os.path.basename(os.path.dirname(report_path))


def fragments_from_report(report_path, out_dir, dry=False, chunksize=200_000):
    """Re-parse one FRAN report -> a per-search fragment Parquet shard. Returns (shard_path, n_frag,
    n_prec) or (None, 0, 0) if the report carries no F.* fragment columns."""
    import pandas as pd
    cols = s2c.resolve_columns(s2c.report_columns(report_path))
    if "frg_mz" not in cols:
        print(f"  [skip] no F.Frg* columns in {os.path.basename(report_path)}")
        return None, 0, 0
    name = report_name(report_path)
    rows, seen_prec = [], set()
    for rec in s2c.iter_records(report_path, chunksize=chunksize):
        fr = rec.get("fragment")
        if not fr or fr.get("mz") is None:
            continue
        run = str(rec.get("run"))
        ch = rec.get("charge")
        seen_prec.add((run, str(rec.get("modified_seq_proforma") or rec.get("stripped_seq")), ch))
        rows.append((name, None, run, rec.get("stripped_seq"), rec.get("modified_seq_proforma"),
                     int(ch) if ch else None, rec.get("precursor_mz"), rec.get("rt"), rec.get("im"),
                     fr.get("mz"), fr.get("type"), fr.get("num"), fr.get("charge"),
                     fr.get("loss"), fr.get("intensity")))
    if not rows:
        print(f"  [skip] 0 fragments parsed from {os.path.basename(report_path)}")
        return None, 0, 0
    print(f"  {name}: {len(rows):,} fragments across {len(seen_prec):,} precursors")
    if dry:
        return None, len(rows), len(seen_prec)
    os.makedirs(out_dir, exist_ok=True)
    fpath = os.path.join(out_dir, f"{_safe(name)}_fragments.parquet")
    cols_out = ["search_key", "raw_path", "run", "stripped_seq", "modified_seq_proforma", "charge",
                "precursor_mz", "rt", "im", "frg_mz", "frg_type", "frg_num", "frg_charge",
                "frg_loss", "frg_intensity"]
    pd.DataFrame(rows, columns=cols_out).to_parquet(fpath, index=False)
    return fpath, len(rows), len(seen_prec)


def _find_reports(root):
    for dp, _, fns in os.walk(root):
        for fn in fns:
            if fn.lower().endswith((".parquet", ".tsv")) and "report_fran" in fn.lower():
                yield os.path.join(dp, fn)


def _register(conn, name, fpath):
    """Best-effort: set delimp_searches.fragments_parquet_path for the search whose
    provenance real_search_name / output_dir matches this report's name."""
    import psycopg2  # noqa: F401
    cur = conn.cursor()
    cur.execute("""SELECT 1 FROM information_schema.columns
                   WHERE table_name='delimp_searches' AND column_name='fragments_parquet_path'""")
    if not cur.fetchone():
        cur.execute("ALTER TABLE delimp_searches ADD COLUMN IF NOT EXISTS fragments_parquet_path TEXT")
    cur.execute("""UPDATE delimp_searches s SET fragments_parquet_path=%s
                   FROM delimp_search_provenance p
                   WHERE p.search_id=s.id AND (p.real_search_name=%s OR s.search_name=%s)""",
                (fpath, name, name))
    n = cur.rowcount
    conn.commit()
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("report", nargs="?", help="a single ..._Report_FRAN (Normal).parquet/tsv")
    ap.add_argument("--scan", help="walk this dir for *Report_FRAN* reports")
    ap.add_argument("--out-dir", default="./fragments", help="where fragment shards go")
    ap.add_argument("--register", action="store_true", help="also set delimp_searches.fragments_parquet_path")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    reports = []
    if a.report:
        reports.append(a.report)
    if a.scan:
        reports += list(_find_reports(a.scan))
    if not reports:
        sys.exit("give a report path or --scan <dir>")
    if a.limit:
        reports = reports[:a.limit]
    print(f"{len(reports)} report(s) to backfill -> {a.out_dir}")

    conn = None
    if a.register and not a.dry_run:
        from refresh_leaderboards import _token
        import psycopg2
        conn = psycopg2.connect(host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"),
                                port=5432, dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
                                user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
                                password=_token(), sslmode="require", connect_timeout=30)

    tot_f = tot_p = done = 0
    for i, rp in enumerate(reports, 1):
        try:
            fpath, nf, npc = fragments_from_report(rp, a.out_dir, dry=a.dry_run)
        except Exception as e:  # noqa: BLE001 - one bad report never stops the sweep
            print(f"  [warn] {os.path.basename(rp)}: {str(e)[:120]}")
            continue
        tot_f += nf; tot_p += npc
        if fpath:
            done += 1
            if conn is not None:
                try:
                    n = _register(conn, report_name(rp), fpath)
                    print(f"    registered ({n} search row(s))")
                except Exception as e:  # noqa: BLE001
                    conn.rollback(); print(f"    [warn] register failed: {str(e)[:80]}")
        if i % 25 == 0:
            print(f"[{i}/{len(reports)}] {done} shards, {tot_f:,} fragments so far")
    print(f"DONE: {done} shards, {tot_f:,} fragments across {tot_p:,} precursors")
    if conn is not None:
        conn.close()


if __name__ == "__main__":
    main()
