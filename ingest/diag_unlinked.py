"""diag_unlinked.py — diagnose the 190 still-unlinked spectrum-lane datasets and the long tail.

READ-ONLY. Two questions:
  (A) The 139 'ambiguous' NULL-search_id lane rows — can n_precursors disambiguate the name collision?
  (B) The ~351 Spectronaut searches with no lane dataset at all — what/how big are they?

    python diag_unlinked.py
"""
from __future__ import annotations
import os, sys
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plan_spectrum_backfill as P


def main():
    conn = P._conn(); cur = conn.cursor()

    # name(+stripped) -> {search_id: n_precursors_of_that_search}
    cur.execute("""SELECT s.id, p.real_search_name, s.search_name, p.report_path, p.output_dir
                   FROM delimp_searches s
                   LEFT JOIN delimp_search_provenance p ON p.search_id=s.id
                   WHERE s.search_engine ILIKE '%spectro%'""")
    rows = cur.fetchall()
    key2ids = defaultdict(set)
    for sid, real, sname, rpath, odir in rows:
        for k in P.match_keys(real, sname, rpath, odir):
            if k:
                key2ids[k].add(str(sid))

    # precursor count per search (stored column — avoids scanning the 402M-row delimp_precursors)
    cur.execute("SELECT id, n_precursors_total FROM delimp_searches WHERE search_engine ILIKE '%spectro%'")
    prec_count = {str(r[0]): r[1] for r in cur.fetchall() if r[1] is not None}

    # the still-NULL lane rows
    cur.execute("""SELECT id, search_name, n_precursors, lance_path
                   FROM delimp_spectrum_lane WHERE search_id IS NULL""")
    nulls = cur.fetchall()

    by_exact, by_near, still_ambiguous, nomatch = [], [], [], 0
    for lane_id, sname, n_prec, lpath in nulls:
        keys = {sname, P._strip_ts(sname)} if sname else set()
        cand = set()
        for k in keys:
            cand |= key2ids.get(k, set())
        if not cand:
            nomatch += 1
            continue
        if len(cand) == 1:
            continue  # would already be linked by link_spectrum_lane.py; shouldn't happen post-run
        # ambiguous by name — try to split by n_precursors (exact, then within 0.5%)
        exact = [sid for sid in cand if prec_count.get(sid) == n_prec]
        if len(exact) == 1:
            by_exact.append((lane_id, sname, exact[0], n_prec))
            continue
        near = [sid for sid in cand
                if prec_count.get(sid) and abs(prec_count[sid]-(n_prec or 0)) <= max(5, 0.005*(n_prec or 0))]
        if len(near) == 1 and not exact:
            by_near.append((lane_id, sname, near[0], n_prec))
        else:
            still_ambiguous.append((lane_id, sname, sorted(cand),
                                    n_prec, [prec_count.get(s) for s in sorted(cand)]))
    resolvable_by_count = by_exact + by_near
    print("=== (A) 190 unlinked lane datasets ===")
    print(f"  resolvable by n_precursors tiebreak : {len(resolvable_by_count):,} "
          f"({len(by_exact):,} exact-count, {len(by_near):,} near-count)")
    print(f"  still ambiguous (count can't split) : {len(still_ambiguous):,}")
    print(f"  no name match at all                : {nomatch:,}")
    for lane_id, sname, cand, n_prec, counts in still_ambiguous[:8]:
        print(f"    - '{sname}' n_prec={n_prec} -> {len(cand)} searches, their counts={counts}")

    # === (B) long tail: Spectronaut searches with no lane dataset ===
    cur.execute("SELECT search_id FROM delimp_spectrum_lane WHERE search_id IS NOT NULL")
    have = {str(r[0]) for r in cur.fetchall()}
    cur.execute("""SELECT s.id, s.search_name, s.status
                   FROM delimp_searches s WHERE s.search_engine ILIKE '%spectro%'""")
    alls = cur.fetchall()
    missing = [(str(sid), nm, st) for sid, nm, st in alls if str(sid) not in have]
    miss_prec = sum(prec_count.get(sid, 0) for sid, _, _ in missing)
    with_prec = [sid for sid, _, _ in missing if prec_count.get(sid, 0) > 0]
    by_status = defaultdict(int)
    for _, _, st in missing:
        by_status[st or "(null)"] += 1
    print("\n=== (B) long tail — Spectronaut searches with NO linked lane dataset ===")
    print(f"  {len(alls):,} Spectronaut searches total; {len(have):,} linked to a lane dataset")
    print(f"  {len(missing):,} have no linked lane dataset")
    print(f"    of those, {len(with_prec):,} have precursors ({miss_prec:,} total) = real, still to backfill")
    print(f"    {len(missing)-len(with_prec):,} have 0 precursors (empty/failed — nothing to recover)")
    print(f"    by status: {dict(by_status)}")
    conn.close()


if __name__ == "__main__":
    main()
