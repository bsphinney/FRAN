"""backfill_protein_counts.py — split FRAN's protein count into PROTEINS vs PROTEIN GROUPS.

The bug (found 2026-07-27 against Spectronaut's own Ver_15_AnalyisOverview.txt):
Spectronaut reports "Protein Groups: 635" AND "Proteins: 1,350" for the same run — a group's label
is the ';'-joined accessions of its members ("E2RE03;J9P669"), so groups expand to ~2x proteins.
FRAN recorded only the GROUP count, in a column named `n_proteins_total`, which the UI rendered as
"Proteins". Every Spectronaut search therefore under-reported proteins by roughly 2x versus the
customer's own report.

No re-export or report re-parse is needed: the accessions were never lost — they are already in
`delimp_proteins.protein_group`. Verified that expanding that label on ';' reproduces the report's
PG.ProteinAccessions set EXACTLY, on Ver_15 plus 6 archived FRAN reports on Flinders.

What this does, per search:
  n_protein_groups_total  <- the group count (exactly what n_proteins_total holds today)
  n_proteins_total        <- count(DISTINCT accession) after expanding protein_group on ';'

REVERSIBLE: n_protein_groups_total preserves the old n_proteins_total verbatim, so the change can
always be undone with `UPDATE delimp_searches SET n_proteins_total = n_protein_groups_total`.

Batched by search (delimp_proteins is ~39M rows): each statement covers a slice of searches so no
single query holds a long lock on the shared PG-Farm DB.

    python backfill_protein_counts.py --dry-run     # report only, no writes
    python backfill_protein_counts.py               # apply
    python backfill_protein_counts.py --revert      # restore the old semantics
"""
from __future__ import annotations
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BATCH = 100          # searches per statement
STMT_TIMEOUT_MS = 600_000


def _conn():
    from backfill_fragments import _pg_conn
    return _pg_conn()


def _ensure_column(cur):
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_name='delimp_searches' AND column_name='n_protein_groups_total'""")
    if cur.fetchone():
        return True
    # delimp_searches is ~2k rows so this is instant, but keep the lock_timeout guard: an
    # AccessExclusiveLock queued behind a long write would block every page that reads this table.
    cur.execute("SET LOCAL lock_timeout = '5s'")
    cur.execute("ALTER TABLE delimp_searches ADD COLUMN IF NOT EXISTS n_protein_groups_total INTEGER")
    cur.execute("RESET lock_timeout")
    print("  added delimp_searches.n_protein_groups_total")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report the change without writing")
    ap.add_argument("--revert", action="store_true", help="restore n_proteins_total from n_protein_groups_total")
    ap.add_argument("--limit", type=int, help="only process the first N searches (testing)")
    a = ap.parse_args()

    conn = _conn()
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute(f"SET statement_timeout = {STMT_TIMEOUT_MS}")

    if a.revert:
        cur.execute("""UPDATE delimp_searches SET n_proteins_total = n_protein_groups_total
                       WHERE n_protein_groups_total IS NOT NULL
                         AND n_proteins_total IS DISTINCT FROM n_protein_groups_total""")
        print(f"reverted {cur.rowcount} searches to the group count")
        conn.commit(); conn.close()
        return

    _ensure_column(cur)
    if not a.dry_run:
        conn.commit()

    # Preserve the old value FIRST — this is what makes the change reversible.
    if not a.dry_run:
        cur.execute("""UPDATE delimp_searches SET n_protein_groups_total = n_proteins_total
                       WHERE n_protein_groups_total IS NULL AND n_proteins_total IS NOT NULL""")
        print(f"  preserved group count for {cur.rowcount} searches")
        conn.commit()

    cur.execute("SELECT id FROM delimp_searches ORDER BY id" + (f" LIMIT {a.limit}" if a.limit else ""))
    ids = [str(r[0]) for r in cur.fetchall()]
    print(f"{len(ids)} searches to process, {BATCH} per batch")

    changed = same = missing = 0
    total_groups = total_prots = 0
    for off in range(0, len(ids), BATCH):
        chunk = ids[off:off + BATCH]
        # count(DISTINCT accession) per search, expanding the ';'-joined group label
        cur.execute("""
            SELECT p.search_id,
                   count(DISTINCT p.protein_group)                              AS n_groups,
                   count(DISTINCT btrim(a))                                     AS n_prots
            FROM delimp_proteins p,
                 LATERAL unnest(string_to_array(p.protein_group, ';')) AS a
            WHERE p.search_id = ANY(%s::uuid[])
              AND p.protein_group IS NOT NULL
              AND btrim(a) <> ''
              AND lower(btrim(a)) NOT IN ('nan','none','null')
            GROUP BY p.search_id""", (chunk,))
        rows = cur.fetchall()
        got = {r[0]: (r[1], r[2]) for r in rows}
        missing += len(chunk) - len(got)
        for sid, (n_groups, n_prots) in got.items():
            total_groups += n_groups
            total_prots += n_prots
            if n_prots != n_groups:
                changed += 1
            else:
                same += 1
        if not a.dry_run and got:
            from psycopg2.extras import execute_values
            execute_values(cur, """
                UPDATE delimp_searches s SET n_proteins_total = v.n_prots,
                                             n_protein_groups_total = v.n_groups
                FROM (VALUES %s) AS v(sid, n_prots, n_groups)
                WHERE s.id = v.sid::uuid""",
                [(str(sid), np, ng) for sid, (ng, np) in got.items()])
            conn.commit()
        done = min(off + BATCH, len(ids))
        print(f"  {done}/{len(ids)} searches   groups={total_groups:,} proteins={total_prots:,}", flush=True)

    ratio = (total_prots / total_groups) if total_groups else 0
    print(f"\n{'DRY RUN — no writes' if a.dry_run else 'APPLIED'}")
    print(f"  searches where proteins > groups : {changed:,}")
    print(f"  searches where they are equal    : {same:,}  (single-accession groups only)")
    print(f"  searches with no protein rows    : {missing:,}")
    print(f"  corpus totals: {total_groups:,} protein groups -> {total_prots:,} proteins  ({ratio:.2f}x)")
    conn.close()


if __name__ == "__main__":
    main()
