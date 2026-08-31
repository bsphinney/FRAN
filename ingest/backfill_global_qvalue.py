"""backfill_global_qvalue.py — fill delimp_precursors.global_q_value for Spectronaut searches.

PR #7 fixed the INGEST: spectronaut_to_corpus hardcoded `global_q_value: None` with the comment
"Spectronaut: EG.Qvalue only (no separate global)", which is wrong -- Spectronaut exports
EG.GlobalPrecursorQvalue and it is populated in every FRAN (Normal) report checked. New ingests now
read it. Rows already in the corpus stay NULL until this runs, and that is 0 non-null across every
Spectronaut search measured, against DIA-NN at 100%.

WHY THAT ASYMMETRY MATTERS: a cross-engine comparison filtered on global FDR would use DIA-NN's
global q-value and Spectronaut's nothing -- silently favouring one engine. It is the same shape as
the protein-accession artefact: a comparison that looks like a result and is really a data gap.

RE-PARSES THROUGH THE SAME ADAPTER the ingest used (spectronaut_to_corpus.iter_records, same
q_max), rather than reading the report independently. That is deliberate: the update has to match
rows the ingest wrote, and re-deriving the key by hand is how a backfill silently updates the wrong
rows or nothing at all.

JOIN KEY is (search_id, raw_path, modified_seq_diann, charge), verified unique on 400k-row searches
with no NULLs. precursor_id_diann is NOT usable -- it is a single repeated value for Spectronaut.

REPORT LOCATION is name-mapped. delimp_search_provenance.report_path records where the INGESTING
machine saw it, and for 1,910 of 1,966 Spectronaut searches that is C:\\fran_sne_export\\... -- a
Windows path unreachable from Hive. Those same reports were pulled to FRAN_reports/<name>/<ts>/, so
mapping by name reaches 1,102 more, taking coverage from 56 to 1,158 of 1,966 (59%). The remaining
808 need their reports pulled off the Windows box first; that is an archive gap, not a code gap.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FRAN_REPORTS = "/nfs/lssc0/flinders/proteomics/Data/FRAN_reports"
_REPORT_RE = re.compile(r"_Report.*\.(parquet|tsv)$", re.I)


def locate_report(report_path: str | None) -> str | None:
    """The report as reachable from HERE, or None. Tries the recorded path, then the archived copy
    under FRAN_reports/<name>/<ts>/, then any FRAN report in that timestamp directory (the exported
    filename is not always identical)."""
    if not report_path:
        return None
    if os.path.isfile(report_path):
        return report_path
    parts = report_path.replace("\\", "/").split("/")
    low = [p.lower() for p in parts]
    # The Windows export root is not one fixed name: both C:\fran_sne_export\ and
    # C:\fran_share_export\ appear in report_path. Matching only the first silently returned
    # "unreachable" for every search exported under the other, which is most of the 725 whose
    # reports are in fact sitting on Flinders.
    i = next((k for k, x in enumerate(low) if x.startswith("fran_") and x.endswith("_export")), -1)
    if i < 0:
        return None
    cand = os.path.join(FRAN_REPORTS, *parts[i + 1:])
    if os.path.isfile(cand):
        return cand
    d = os.path.join(FRAN_REPORTS, *parts[i + 1:-1])
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            full = os.path.join(d, f)
            try:
                if _REPORT_RE.search(f) and os.path.getsize(full) > 1024:
                    return full
            except OSError:
                continue
    # The recorded TIMESTAMP directory often does not exist on Flinders even though the export was
    # pulled: report_path names the timestamp the Windows box wrote, and the archive pull created
    # its own. 725 of 808 searches that looked unreachable are actually present one level deeper
    # under FRAN_reports/<name>/. Search the NAME directory, newest export first.
    name_dir = os.path.join(FRAN_REPORTS, parts[i + 1])
    if not os.path.isdir(name_dir):
        return None
    best = None
    try:
        subs = sorted(os.listdir(name_dir), reverse=True)   # timestamped dirs sort newest-first
    except OSError:
        return None
    for sub in subs:
        sd = os.path.join(name_dir, sub)
        cands = []
        if os.path.isdir(sd):
            try:
                cands = [os.path.join(sd, f) for f in sorted(os.listdir(sd)) if _REPORT_RE.search(f)]
            except OSError:
                continue
        elif _REPORT_RE.search(sub):
            cands = [sd]
        for full in cands:
            try:
                if os.path.getsize(full) <= 1024:
                    continue
            except OSError:
                continue
            # Prefer a FRAN.rs export: a BGS Factory report has no PG.Genes, no EG.IonMobility and
            # no F.* columns, and is a different schema to the one this corpus was built from.
            if "fran" in os.path.basename(full).lower():
                return full
            best = best or full
    return best


def backfill_one(conn, search_id: str, report: str, dry: bool) -> tuple[int, int]:
    """Returns (rows_with_a_global_q_in_the_report, rows_updated)."""
    from spectronaut_to_corpus import iter_records
    cur = conn.cursor()

    # run -> raw_path for THIS search only; the same run name can exist under several paths across
    # the corpus, and updating by basename would reach into another search's rows.
    cur.execute("""SELECT rf.raw_basename, rf.raw_path
                     FROM search_raw_files f
                     JOIN raw_files rf ON rf.raw_path = f.raw_path
                    WHERE f.search_id = %s::uuid""", (search_id,))
    run2path = {r[0]: r[1] for r in cur.fetchall()}
    if not run2path:
        return 0, 0

    # DEDUPED as it is read. A FRAN.rs report is FRAGMENT-level -- many rows per precursor -- so the
    # same (raw_path, modified_seq, charge) arrives repeatedly with the same global q. Sending them
    # all would inflate the payload ~6x for no effect.
    seen: dict[tuple, float] = {}
    for rec in iter_records(report):
        g = rec.get("global_q_value")
        if g is None:
            continue
        rp = run2path.get(str(rec.get("run")))
        if rp is None:
            continue
        ms, ch = rec.get("modified_seq_diann"), rec.get("charge")
        if ms is None or ch is None:
            continue
        seen[(rp, ms, int(ch))] = float(g)
    if not seen or dry:
        return len(seen), 0

    # An inline VALUES join, not a temp table: the service account has no CREATE TEMP privilege on
    # this database ("permission denied to create temporary tables"). Batched so no single statement
    # carries the whole search.
    from psycopg2.extras import execute_values
    items = [(rp, ms, ch, gq) for (rp, ms, ch), gq in seen.items()]
    n = 0
    for i in range(0, len(items), 5000):
        chunk = items[i:i + 5000]
        sql = ("UPDATE delimp_precursors p SET global_q_value = g.gq "
               "FROM (VALUES %s) AS g(rp, ms, ch, gq) "
               f"WHERE p.search_id = '{search_id}'::uuid "
               "AND p.raw_path = g.rp AND p.modified_seq_diann = g.ms "
               "AND p.charge = g.ch::smallint "
               "AND p.global_q_value IS DISTINCT FROM g.gq::double precision")
        execute_values(cur, sql, chunk, template="(%s,%s,%s::int,%s::float8)", page_size=5000)
        n += cur.rowcount
        conn.commit()
    return len(seen), n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--search-id")
    ap.add_argument("--shard", default="", metavar="I/N")
    a = ap.parse_args()

    import corpus_ingest as ci
    conn = ci._conn(); conn.autocommit = False
    cur = conn.cursor(); cur.execute("SET statement_timeout='1800s'")
    q = """SELECT s.id, s.search_name, s.n_precursors_total, p.report_path
             FROM delimp_searches s
             JOIN delimp_search_provenance p ON p.search_id = s.id
            WHERE s.search_engine='spectronaut' AND p.report_path IS NOT NULL"""
    args: tuple = ()
    if a.search_id:
        q += " AND s.id = %s::uuid"; args = (a.search_id,)
    q += " ORDER BY s.n_precursors_total"
    cur.execute(q, args)
    rows = cur.fetchall()
    conn.commit()

    todo = []
    for sid, name, n, rp in rows:
        r = locate_report(rp)
        if r:
            todo.append((str(sid), name, n or 0, r))
    print(f"{len(rows):,} spectronaut searches with a report_path; "
          f"{len(todo):,} reachable from here", flush=True)
    if a.shard:
        i, k = (int(x) for x in a.shard.split("/"))
        todo = [t for j, t in enumerate(todo) if j % k == i]
        print(f"  shard {i}/{k}: {len(todo)}", flush=True)
    if a.limit:
        todo = todo[:a.limit]

    tot_seen = tot_upd = ok = fail = 0
    t0 = time.time()
    for i, (sid, name, n, rep) in enumerate(todo, 1):
        t = time.time()
        try:
            seen, upd = backfill_one(conn, sid, rep, dry=not a.apply)
            ok += 1
        except Exception as e:  # noqa: BLE001 - one bad report must not stop the sweep
            conn.rollback(); fail += 1
            print(f"  [fail] {name[:40]}: {type(e).__name__}: {str(e)[:90]}", flush=True)
            continue
        tot_seen += seen; tot_upd += upd
        print(f"  [{i}/{len(todo)}] {name[:38]:<40} rows={n:>8,} "
              f"global_q in report={seen:>8,} updated={upd:>8,} ({time.time()-t:.0f}s)", flush=True)
    print(f"\n{'APPLIED' if a.apply else 'DRY RUN'}: {ok} searches ok, {fail} failed, "
          f"{tot_seen:,} report rows carried a global q, {tot_upd:,} corpus rows updated, "
          f"{time.time()-t0:.0f}s", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
