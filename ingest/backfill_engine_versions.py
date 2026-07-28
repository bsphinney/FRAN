"""backfill_engine_versions.py — fill delimp_searches.search_engine_version for existing searches.

Nothing ever wrote this column: as of 2026-07-27 it was NULL for all 1,891 Spectronaut searches and
68 of 71 DIA-NN ones. New ingests record it (corpus_ingest -> engine_version.detect); this recovers
what it can for the history.

WHERE IT CAN COME FROM (in priority order), all read-only:
  1. the search's own `output_dir`, when that path is reachable from here (mostly DIA-NN: the
     `report.log.txt` next to report.parquet carries "DIA-NN 1.8.1")
  2. an archived export DIR under FRAN_reports/<name>/<ts>/ that kept its sidecars
     (*_setup.txt / *_AnalysisLog.txt / RunSummaries/*_RunOverview.tsv)
  3. a zipped experiment FRAN_reports/<name>.zip — read in place, never extracted
  4. a full export dir under FRAN_SNE_export/

COVERAGE IS LIMITED and that is a property of the archive, not this script: `pull_reports_to_hive.py`
copied only `*_Report_FRAN*.parquet` off the Windows box, so 1,730 of 1,737 archived report dirs have
NO sidecar to read. Measured reachable: ~7 dirs + 29 zips + the FRAN_SNE_export dirs. The remaining
~97% still have their setup.txt on `C:\\fran_sne_export\\` — see --emit-windows-plan.

The report PARQUET itself does NOT carry the engine version: its `created_by` is the writer library
("Parquet.Net version 5.5.0"), which is NOT Spectronaut's version and must never be stored as one.

    python backfill_engine_versions.py --dry-run
    python backfill_engine_versions.py
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine_version import _DIANN, _SN, _SN_ANALYSIS, detect  # noqa: E402

REPORTS = "/nfs/lssc0/flinders/proteomics/Data/FRAN_reports"
SNE_EXPORT = "/nfs/lssc0/flinders/proteomics/Data/FRAN_SNE_export"
_SIDECAR = re.compile(r"setup\.txt$|[Aa]nalysis.*[Ll]og.*\.txt$|RunOverview\.tsv$|\.log\.txt$")


def _scan_text(text, engine):
    pats = (_SN, _SN_ANALYSIS) if (engine or "").startswith("spectronaut") else (_DIANN,)
    for p in pats:
        m = p.search(text)
        if m:
            return m.group(1)
    return None


def _from_dir(d, engine):
    if not d or not os.path.isdir(d):
        return None
    v = detect(engine, None, d)
    if v:
        return v
    for sub in glob.glob(os.path.join(d, "*")):      # exports nest under a <ts>_<name>/ dir
        if os.path.isdir(sub):
            v = detect(engine, None, sub)
            if v:
                return v
    return None


def _from_zip(path, engine):
    try:
        with zipfile.ZipFile(path) as zf:
            for n in [x for x in zf.namelist() if _SIDECAR.search(x)][:8]:
                try:
                    v = _scan_text(zf.read(n)[:20000].decode("utf-8", "replace"), engine)
                except Exception:  # noqa: BLE001
                    continue
                if v:
                    return v
    except Exception:  # noqa: BLE001 - a corrupt/partial zip must not stop the sweep
        return None
    return None


def find_version(name, output_dir, engine):
    """Try every known source for one search. Returns (version, source) or (None, None)."""
    v = _from_dir(output_dir, engine)
    if v:
        return v, "output_dir"
    if name:
        d = os.path.join(REPORTS, name)
        v = _from_dir(d, engine)
        if v:
            return v, "archived dir"
        z = d + ".zip"
        if os.path.exists(z):
            v = _from_zip(z, engine)
            if v:
                return v, "zip"
        d2 = os.path.join(SNE_EXPORT, name)
        v = _from_dir(d2, engine)
        if v:
            return v, "sne_export"
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--emit-windows-plan", metavar="CSV",
                    help="write the list of searches still missing a version, for the Windows box to resolve")
    # The archive roots are POSIX (Hive) by default. A Windows node sees the SAME share as
    # R:\Data\FRAN_reports — so it can run this directly instead of waiting for the Hive login node.
    ap.add_argument("--reports-root", help=r"override FRAN_reports root (Windows: R:\Data\FRAN_reports)")
    ap.add_argument("--sne-export-root", help=r"override FRAN_SNE_export root (Windows: R:\Data\FRAN_SNE_export)")
    a = ap.parse_args()

    global REPORTS, SNE_EXPORT
    if a.reports_root:
        REPORTS = a.reports_root
    if a.sne_export_root:
        SNE_EXPORT = a.sne_export_root
    print(f"reports root: {REPORTS}")

    from backfill_fragments import _pg_conn
    conn = _pg_conn()
    cur = conn.cursor()
    cur.execute("""SELECT s.id, s.search_name, s.output_dir, s.search_engine,
                          COALESCE(p.real_search_name, s.search_name)
                   FROM delimp_searches s
                   LEFT JOIN delimp_search_provenance p ON p.search_id = s.id
                   WHERE s.search_engine_version IS NULL
                   ORDER BY s.search_name""" + (f" LIMIT {a.limit}" if a.limit else ""))
    rows = cur.fetchall()
    print(f"{len(rows)} searches missing a version")

    found, by_source, updates, missing = 0, {}, [], []
    for sid, name, outdir, engine, real in rows:
        v, src = find_version(real or name, outdir, engine)
        if not v:
            v, src = find_version(name, outdir, engine)
        if v:
            found += 1
            by_source[src] = by_source.get(src, 0) + 1
            updates.append((v, sid))
        else:
            missing.append((name, engine, outdir))
        if (len(updates) + len(missing)) % 250 == 0:
            print(f"  ...{len(updates)+len(missing)}/{len(rows)} scanned, {found} found", flush=True)

    if updates and not a.dry_run:
        from psycopg2.extras import execute_values
        execute_values(cur, """UPDATE delimp_searches s SET search_engine_version = v.ver
                               FROM (VALUES %s) AS v(ver, sid)
                               WHERE s.id = v.sid::uuid""", [(v, str(i)) for v, i in updates])
        conn.commit()

    print(f"\n{'DRY RUN' if a.dry_run else 'APPLIED'}: {found}/{len(rows)} resolved")
    for k, n in sorted(by_source.items(), key=lambda x: -x[1]):
        print(f"    {k:14s} {n}")
    print(f"  still missing: {len(missing)}")
    if a.emit_windows_plan:
        import csv
        with open(a.emit_windows_plan, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["search_name", "engine", "output_dir"])
            w.writerows(missing)
        print(f"  wrote {a.emit_windows_plan} ({len(missing)} rows) — resolve these on the Windows box")
    conn.close()


if __name__ == "__main__":
    main()
