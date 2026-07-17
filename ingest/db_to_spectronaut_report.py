#!/usr/bin/env python3
"""Regenerate a Spectronaut-style long-format Report.tsv for ONE FRAN search from the PG Farm
`delimp` database, so it can be loaded by limpa::readSpectronaut() / the DE-LIMP DPC pipeline.

READ-ONLY. Never writes to the database. Does not touch the corpus_browser app.

Why both this AND db_to_diann_report.py? The corpus stores every search's precursor data in ONE
uniform schema regardless of the engine that produced it, so either limpa reader works for any
search. db_to_diann_report.py -> limpa::readDIANN() (parquet); this -> limpa::readSpectronaut()
(tsv). Spectronaut-origin searches (the SpN_* names) feel most natural through this path.

Usage:
    python3 db_to_spectronaut_report.py <search_id> --out <path.tsv>
    python3 db_to_spectronaut_report.py --list          # re-ingested searches (intensity populated)

Default --out: ./<search_name>_spectronaut_report.tsv

Columns emitted (one row per precursor per run), named exactly as limpa::readSpectronaut() expects
by default so a bare readSpectronaut("...tsv") call works with zero extra arguments:
    R.FileName                    (run.column)
    R.Condition, R.Replicate      (run.info.columns — filled from CoreOmics when linked, else blank)
    EG.ModifiedSequence, FG.Charge        (feature.column — the precursor key)
    EG.TotalQuantity (Settings)           (intensity.column)
    PG.ProteinAccessions, PG.Genes        (annotation.columns)
    EG.Qvalue, PG.Qvalue                  (q.columns, default cutoff 0.01)
    EG.IsImputed                          (filter.column; always FALSE — the corpus stores only
                                           real observations, never Spectronaut-imputed values)

Rows with NULL intensity are dropped (can't quantify). limpa filters on EG.Qvalue<=0.01 &
PG.Qvalue<=0.01 by default exactly as it would on a native export.
"""
from __future__ import annotations

import os
import sys
import csv
import argparse

import psycopg2

# Reuse the exact auth/connect pattern from refresh_leaderboards.py (service-account
# secret -> JWT exchange).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh_leaderboards as r  # noqa: E402

HOST = "pgfarm.library.ucdavis.edu"
DBNAME = "uc-davis-genome-center-proteomics-core/delimp"
USER = os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account")
# DB is under heavy ingestion load — keep every statement bounded.
STMT_TIMEOUT_MS = 120000


def connect():
    return psycopg2.connect(
        host=HOST, port=5432, dbname=DBNAME, user=USER,
        password=os.environ.get("PGPASSWORD") or r._token(), sslmode="require",
        connect_timeout=30,
        keepalives=1, keepalives_idle=20, keepalives_interval=10, keepalives_count=6,
        options=f"-c statement_timeout={STMT_TIMEOUT_MS}",
    )


def _run_query(con, sql, params=None, retries=1):
    """Run a query, retry once on timeout/operational error (DB under load)."""
    last = None
    for attempt in range(retries + 1):
        try:
            cur = con.cursor()
            cur.execute(sql, params or ())
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            cur.close()
            return rows, cols
        except (psycopg2.OperationalError, psycopg2.errors.QueryCanceled) as e:
            last = e
            try:
                con.rollback()
            except Exception:
                pass
            if attempt < retries:
                sys.stderr.write(f"[retry] query failed ({type(e).__name__}), retrying once...\n")
            continue
    raise last


def run_basename(raw_path: str) -> str:
    """Spectronaut R.FileName convention: bare run name. raw_path may be a Windows path
    (B:\\autoSNE\\...\\run) — os.path.basename on Linux won't split '\\', so split both seps."""
    if raw_path is None:
        return ""
    import re
    base = re.split(r"[/\\]", raw_path.rstrip("/\\"))[-1]
    low = base.lower()
    for ext in (".d", ".raw", ".mzml", ".wiff", ".dia", ".sne"):
        if low.endswith(ext):
            return base[: -len(ext)]
    return base


def list_searches(con):
    sql = """
        SELECT s.id, s.search_name, COUNT(*) AS n_prec
        FROM delimp_precursors p
        JOIN delimp_searches s ON s.id = p.search_id
        WHERE p.intensity IS NOT NULL
        GROUP BY 1, 2
        ORDER BY 3 DESC
        LIMIT 30
    """
    try:
        rows, _ = _run_query(con, sql, retries=1)
    except (psycopg2.OperationalError, psycopg2.errors.QueryCanceled):
        sys.stderr.write("[warn] --list query timed out (DB under load). "
                         "Pass a search_id directly, or retry later.\n")
        return
    print(f"{'id':>38}  {'precursors':>12}  search_name")
    print("-" * 80)
    for sid, name, n in rows:
        print(f"{sid:>38}  {n:>12,}  {name}")


def fetch_search_meta(con, search_id):
    rows, _ = _run_query(
        con,
        "SELECT id, search_name, search_engine FROM delimp_searches WHERE id = %s",
        (search_id,),
    )
    if not rows:
        sys.exit(f"search_id {search_id} not found in delimp_searches")
    sid, name, engine = rows[0]
    return {"id": sid, "search_name": name, "search_engine": engine}


def fetch_gene_map(con, search_id):
    """protein_group -> gene from delimp_proteins (per search)."""
    rows, _ = _run_query(
        con,
        "SELECT protein_group, MAX(gene) FROM delimp_proteins WHERE search_id = %s GROUP BY 1",
        (search_id,), retries=1,
    )
    return {pg: (g or "") for pg, g in rows if pg is not None}


def fetch_run_conditions(con, search_id, runs):
    """Best-effort Run -> (condition, replicate) from the linked CoreOmics submission.
    Returns {} if unlinked or no reliable substring match."""
    try:
        rows, _ = _run_query(
            con,
            "SELECT coreomics_submission_id FROM delimp_search_provenance WHERE search_id = %s",
            (search_id,), retries=0)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[conditions] provenance lookup skipped: {type(e).__name__}\n")
        return {}
    if not rows or rows[0][0] is None:
        return {}
    sub_id = rows[0][0]
    try:
        srows, _ = _run_query(
            con,
            "SELECT unique_id, sample_name, condition_name FROM coreomics_samples_cache "
            "WHERE submission_id = %s", (sub_id,), retries=0)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[conditions] samples_cache lookup skipped: {type(e).__name__}\n")
        return {}
    if not srows:
        return {}
    out: dict[str, str] = {}
    for run in runs:
        run_lc = run.lower()
        for uid, sname, cname in srows:
            for token in (uid, sname):
                if token and str(token).lower() in run_lc:
                    out[run] = cname
                    break
            if run in out:
                break
    return out


def fetch_precursors(con, search_id):
    sql = """
        SELECT raw_path, stripped_seq, modified_seq_diann, modified_seq_proforma,
               charge, q_value, pg_q_value, intensity, protein_group
        FROM delimp_precursors
        WHERE search_id = %s AND intensity IS NOT NULL
    """
    return _run_query(con, sql, (search_id,), retries=1)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("search_id", nargs="?", help="delimp search id (UUID)")
    ap.add_argument("--out", help="output tsv path (default ./<search_name>_spectronaut_report.tsv)")
    ap.add_argument("--list", action="store_true",
                    help="list re-ingested searches (intensity populated) and exit")
    args = ap.parse_args()

    con = connect()
    con.autocommit = True
    try:
        if args.list:
            list_searches(con)
            return
        if args.search_id is None:
            ap.error("search_id is required (or use --list)")

        meta = fetch_search_meta(con, args.search_id)
        sys.stderr.write(
            f"[search] id={meta['id']} name={meta['search_name']!r} engine={meta['search_engine']}\n")

        gene_map = fetch_gene_map(con, args.search_id)
        sys.stderr.write(f"[genes] {len(gene_map)} protein_group->gene entries\n")

        rows, cols = fetch_precursors(con, args.search_id)
        ci = {c: i for i, c in enumerate(cols)}
        sys.stderr.write(f"[precursors] {len(rows)} rows with non-null intensity\n")
        if not rows:
            sys.exit("No precursors with non-null intensity for this search "
                     "(not re-ingested? use --list to find one).")

        # Resolve Run names + best-effort conditions before writing.
        runs = sorted({run_basename(rw[ci["raw_path"]]) for rw in rows})
        run_cond = fetch_run_conditions(con, args.search_id, runs)
        if run_cond:
            sys.stderr.write(f"[conditions] {len(run_cond)}/{len(runs)} runs -> CoreOmics conditions\n")
        else:
            sys.stderr.write("[conditions] none (unlinked / no reliable match) — R.Condition blank\n")

        safe_name = "".join(c if c.isalnum() or c in "-_." else "_"
                            for c in (meta["search_name"] or f"search{meta['id']}"))
        out_path = args.out or f"./{safe_name}_spectronaut_report.tsv"

        header = ["R.FileName", "R.Condition", "R.Replicate",
                  "EG.ModifiedSequence", "FG.Charge", "EG.TotalQuantity (Settings)",
                  "PG.ProteinAccessions", "PG.Genes", "EG.Qvalue", "PG.Qvalue", "EG.IsImputed"]
        n = 0
        proteins = set()
        peptides = set()
        with open(out_path, "w", newline="") as fh:
            w = csv.writer(fh, delimiter="\t")
            w.writerow(header)
            for rw in rows:
                run = run_basename(rw[ci["raw_path"]])
                pg = rw[ci["protein_group"]] or ""
                # prefer Spectronaut-ish modified sequence; proforma is the cleanest mod string we keep
                modseq = (rw[ci["modified_seq_proforma"]] or rw[ci["modified_seq_diann"]]
                          or rw[ci["stripped_seq"]] or "")
                w.writerow([
                    run,
                    run_cond.get(run, ""),
                    "",                                   # R.Replicate — design unknown; user fills in
                    modseq,
                    rw[ci["charge"]] if rw[ci["charge"]] is not None else "",
                    rw[ci["intensity"]],
                    pg,
                    gene_map.get(pg, ""),
                    rw[ci["q_value"]] if rw[ci["q_value"]] is not None else "",
                    rw[ci["pg_q_value"]] if rw[ci["pg_q_value"]] is not None else "",
                    "False",                              # EG.IsImputed — corpus stores only real obs
                ])
                n += 1
                proteins.add(pg)
                peptides.add(rw[ci["stripped_seq"]])
        con.close()
        sys.stderr.write(
            f"[written] {out_path}  rows={n:,} runs={len(runs)} "
            f"proteins={len(proteins)} peptides={len(peptides)}\n")
        print(out_path)
        sys.stderr.write(
            "    Load in R:  limpa::readSpectronaut('%s')  then dpcQuant()/dpcDE().\n" % out_path)
    finally:
        try:
            con.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
