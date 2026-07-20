"""link_spectrum_lane.py — link delimp_spectrum_lane datasets to their delimp_searches.id.

Many Lance datasets registered with search_id=NULL because the report name didn't EXACTLY match a
search record (timestamp prefixes, provenance vs search_name drift). The spectra are safely stored;
this just fills in the missing link. It builds a name->search_id index from delimp_searches +
delimp_search_provenance (search_name, real_search_name, the exported report_path's name, the
source .sne/output_dir name — each also timestamp-stripped, reusing plan_spectrum_backfill's
matcher) and UPDATEs each NULL-search_id row for the UNAMBIGUOUS matches. When a name maps to more
than one search, it breaks the tie ONLY if exactly one candidate's stored n_precursors_total equals
the dataset's n_precursors — otherwise it leaves the row unlinked. So it never mislinks.

    python link_spectrum_lane.py           # link + report
    python link_spectrum_lane.py --dry-run # report only, no writes
Idempotent: only touches rows that are still search_id IS NULL.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plan_spectrum_backfill as P   # noqa: E402  (reuse match_keys / _strip_ts / _conn)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    conn = P._conn()
    cur = conn.cursor()

    # key -> set(search_id) from every name form of every Spectronaut search
    cur.execute("""SELECT s.id, p.real_search_name, s.search_name, p.report_path, p.output_dir
                   FROM delimp_searches s
                   LEFT JOIN delimp_search_provenance p ON p.search_id=s.id
                   WHERE s.search_engine ILIKE '%spectro%'""")
    key2ids = defaultdict(set)
    for sid, real, sname, rpath, odir in cur.fetchall():
        for k in P.match_keys(real, sname, rpath, odir):
            if k:
                key2ids[k].add(str(sid))

    # precursor count per search (stored column) — used to break name-collision ties
    cur.execute("SELECT id, n_precursors_total FROM delimp_searches WHERE search_engine ILIKE '%spectro%'")
    prec_count = {str(r[0]): r[1] for r in cur.fetchall() if r[1] is not None}

    cur.execute("SELECT id, search_name, n_precursors FROM delimp_spectrum_lane WHERE search_id IS NULL")
    nulls = cur.fetchall()
    ups, ambiguous, nomatch, by_count = [], 0, 0, 0
    for lane_id, sname, n_prec in nulls:
        keys = {sname, P._strip_ts(sname)} if sname else set()
        cand = set()
        for k in keys:
            cand |= key2ids.get(k, set())
        if not cand:
            nomatch += 1
            continue
        if len(cand) == 1:
            ups.append((next(iter(cand)), lane_id))
            continue
        # name is ambiguous — break the tie ONLY if exactly one candidate's stored
        # precursor count equals this dataset's n_precursors (never mislinks: unique count).
        exact = [sid for sid in cand if prec_count.get(sid) == n_prec]
        if len(exact) == 1:
            ups.append((exact[0], lane_id)); by_count += 1
        else:
            ambiguous += 1

    print(f"{len(nulls):,} datasets with NULL search_id: "
          f"{len(ups):,} linkable ({by_count:,} via precursor-count tiebreak), "
          f"{ambiguous:,} ambiguous, {nomatch:,} no-match")
    if ups and not a.dry_run:
        import psycopg2.extras
        psycopg2.extras.execute_batch(
            cur, "UPDATE delimp_spectrum_lane SET search_id=%s, updated_at=now() WHERE id=%s",
            ups, page_size=500)
        conn.commit()
        print(f"  linked {len(ups):,} datasets")
    elif a.dry_run:
        print("  (dry-run — no writes)")

    cur.execute("SELECT count(*), count(search_id) FROM delimp_spectrum_lane")
    t = cur.fetchone()
    print(f"registry now: {t[0]:,} datasets, {t[1]:,} linked to a search_id "
          f"({t[0]-t[1]:,} still unlinked)")
    conn.close()


if __name__ == "__main__":
    main()
