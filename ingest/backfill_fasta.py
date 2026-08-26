#!/usr/bin/env python3
"""backfill_fasta.py — fill delimp_searches.fasta_* for searches already in the corpus.

Companion to the ingest-side fix in corpus_ingest.py. Every search row carries `output_dir`,
so the same detector that runs at ingest can be replayed over the archive.

Coverage will be PARTIAL and that is expected, not a failure:
  * Spectronaut records the database only in *ExperimentSetupOverview*.txt / setup.txt.
    RunSummaries/*_RunOverview.tsv -- the file that rescued version detection for archived
    CLI exports -- does NOT contain it (verified 2026-08-25). Archived dirs that kept only
    RunSummaries will stay NULL until their originating export dir is pulled.
  * Spectronaut stores a BARE FILENAME, so md5 / n_proteins fill only when that name resolves
    under --fasta-root.
  * DIA-NN logs carry an absolute path, so those resolve whenever the path is mounted here.

Dry by default. Nothing is written without --commit.

  python3 backfill_fasta.py --limit 200
  python3 backfill_fasta.py --fasta-root /quobyte/proteomics-grp/MRS --commit
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine_fasta import detect  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="write; otherwise dry-run")
    ap.add_argument("--limit", type=int, default=0, help="0 = all")
    ap.add_argument("--engine", default=None, help="restrict to one engine")
    ap.add_argument("--fasta-root", action="append", default=[],
                    help="directory to resolve bare FASTA filenames against; repeatable")
    ap.add_argument("--redo", action="store_true",
                    help="also revisit rows that already have a fasta_path")
    a = ap.parse_args()

    from corpus_ingest import _conn  # reuse the ingester's own credential handling
    cn = _conn()
    cur = cn.cursor()
    where = ["output_dir IS NOT NULL"]
    if not a.redo:
        where.append("(fasta_path IS NULL OR fasta_md5 IS NULL OR fasta_n_proteins IS NULL)")
    if a.engine:
        where.append("search_engine = %(eng)s")
    sql = ("SELECT id, search_engine, output_dir FROM delimp_searches WHERE "
           + " AND ".join(where) + " ORDER BY ingested_at DESC")
    if a.limit:
        sql += f" LIMIT {int(a.limit)}"
    cur.execute(sql, {"eng": a.engine})
    rows = cur.fetchall()
    print(f"{len(rows)} candidate searches")

    found = md5s = counts = 0
    for sid, engine, outdir in rows:
        got = detect(engine, None, outdir, search_roots=a.fasta_root) or {}
        if not got.get("fasta_path"):
            continue
        found += 1
        md5s += bool(got.get("fasta_md5"))
        counts += bool(got.get("fasta_n_proteins"))
        print(f"  {engine:12s} {os.path.basename(str(got['fasta_path']))[:52]:52s} "
              f"n={got.get('fasta_n_proteins') or '-'}")
        if a.commit:
            cur.execute(
                """UPDATE delimp_searches
                      SET fasta_path        = COALESCE(%s, fasta_path),
                          fasta_md5         = COALESCE(%s, fasta_md5),
                          fasta_n_proteins  = COALESCE(%s, fasta_n_proteins),
                          contaminant_lib   = COALESCE(%s, contaminant_lib)
                    WHERE id = %s""",
                (got.get("fasta_path"), got.get("fasta_md5"),
                 got.get("fasta_n_proteins"), got.get("contaminant_lib"), sid))
    if a.commit:
        cn.commit()
    print(f"\nresolved a database for {found}/{len(rows)} "
          f"({100*found/max(1,len(rows)):.1f}%); md5 for {md5s}; entry count for {counts}")
    print("DRY RUN — re-run with --commit to write" if not a.commit else "committed")
    cn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
