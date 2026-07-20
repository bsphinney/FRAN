"""diag_resubmits.py — how much of the 'unlinked long tail' is genuinely-missing data vs resubmits
whose spectra already live under a sibling/parent dataset. READ-ONLY."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plan_spectrum_backfill as P


def main():
    c = P._conn(); cur = c.cursor()
    cur.execute("SELECT search_id FROM delimp_spectrum_lane WHERE search_id IS NOT NULL")
    linked = {str(r[0]) for r in cur.fetchall()}
    cur.execute("""SELECT s.id, s.resubmit_of_search_id, s.parent_chain_depth, s.n_precursors_total
                   FROM delimp_searches s WHERE s.search_engine ILIKE '%spectro%'""")
    rows = cur.fetchall()
    miss = [r for r in rows if str(r[0]) not in linked]

    def psum(rs):
        return sum((r[3] or 0) for r in rs)

    resub = [r for r in miss if r[1] is not None or (r[2] or 0) > 0]
    parent_linked = [r for r in resub if r[1] is not None and str(r[1]) in linked]
    nonresub = [r for r in miss if r not in resub]

    print(f"unlinked spectro searches: {len(miss):,}  ({psum(miss):,} precursors)")
    print(f"  resubmits/re-runs (chain>0 or resubmit_of set): {len(resub):,}  ({psum(resub):,} prec)")
    print(f"    ...whose PARENT search IS linked (spectra already stored): {len(parent_linked):,}"
          f"  ({psum(parent_linked):,} prec)")
    print(f"  non-resubmit unlinked (likely genuinely distinct): {len(nonresub):,}  ({psum(nonresub):,} prec)")
    c.close()


if __name__ == "__main__":
    main()
