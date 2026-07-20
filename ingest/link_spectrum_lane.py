"""link_spectrum_lane.py — link delimp_spectrum_lane datasets to their delimp_searches.id.

Many Lance datasets registered with search_id=NULL because the report name didn't EXACTLY match a
search record (timestamp prefixes, provenance vs search_name drift). The spectra are safely stored;
this just fills in the missing link. It builds a name->search_id index from delimp_searches +
delimp_search_provenance (search_name, real_search_name, the exported report_path's name, the
source .sne/output_dir name — each also timestamp-stripped, reusing plan_spectrum_backfill's
matcher) and UPDATEs each NULL-search_id row for the UNAMBIGUOUS matches (skips any key that maps
to more than one search, so it never mislinks).

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

    cur.execute("SELECT id, search_name FROM delimp_spectrum_lane WHERE search_id IS NULL")
    nulls = cur.fetchall()
    ups, ambiguous, nomatch = [], 0, 0
    for lane_id, sname in nulls:
        keys = {sname, P._strip_ts(sname)} if sname else set()
        hit = None
        amb = False
        for k in keys:
            ids = key2ids.get(k)
            if not ids:
                continue
            if len(ids) == 1:
                hit = next(iter(ids)); break
            amb = True
        if hit:
            ups.append((hit, lane_id))
        elif amb:
            ambiguous += 1
        else:
            nomatch += 1

    print(f"{len(nulls):,} datasets with NULL search_id: "
          f"{len(ups):,} linkable, {ambiguous:,} ambiguous, {nomatch:,} no-match")
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
