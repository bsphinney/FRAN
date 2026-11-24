"""null_nan_protein_group.py -- literal 'nan' / 'None' / '' stored as text instead of NULL.

delimp_precursors.protein_group is nullable: the junk text becomes NULL.
delimp_proteins.protein_group is NOT NULL, and a row whose group is junk is not a protein at all
(it was one bogus 'protein' per run holding every unassigned precursor), so those rows are DELETED,
and delimp_proteins.gene junk is set to NULL. After --apply, delimp_searches.n_protein_groups_total /
n_proteins_total of the affected searches should be recomputed (this script lists them) -- it does
not change them itself, because that is a decision about historical numbers.

Searches with junk are found through idx_prec_protein_group (an equality probe on the junk strings;
no table scan), then each statement is scoped by search_id.

    python null_nan_protein_group.py            # DRY RUN, exact counts
    python null_nan_protein_group.py --apply    # writes; owner only
"""
from __future__ import annotations

import sys

from _common import base_parser, chunks, connect, show_examples

JUNK = ["nan", "NaN", "NAN", "None", "none", "null", ""]


def main():
    ap = base_parser(__doc__.split("\n\n")[0])
    a = ap.parse_args()
    conn, cur = connect(a.apply)

    cur.execute("SELECT search_id, count(*) FROM delimp_precursors WHERE protein_group = ANY(%s) GROUP BY 1", (JUNK,))
    prec = dict(cur.fetchall())
    cur.execute("SELECT search_id, count(*) FROM delimp_proteins WHERE protein_group = ANY(%s) GROUP BY 1", (JUNK,))
    prot = dict(cur.fetchall())
    cur.execute("SELECT search_id, count(*) FROM delimp_proteins WHERE gene = ANY(%s) GROUP BY 1", (JUNK,))
    gene = dict(cur.fetchall())
    sids = sorted(set(prec) | set(prot) | set(gene), key=str)

    examples = []
    for sid in sids[:3]:
        cur.execute("SELECT search_name FROM delimp_searches WHERE id = %s", (sid,))
        nm = (cur.fetchone() or ["?"])[0]
        examples.append(f"search {sid} {nm!r}: {prec.get(sid, 0):,} precursor rows, "
                        f"{prot.get(sid, 0):,} protein rows, {gene.get(sid, 0):,} junk genes")
    if a.apply:
        for batch in chunks(sids, a.batch):
            cur.execute("UPDATE delimp_precursors SET protein_group = NULL "
                        "WHERE search_id = ANY(%s::uuid[]) AND protein_group = ANY(%s)", (batch, JUNK))
            cur.execute("DELETE FROM delimp_proteins WHERE search_id = ANY(%s::uuid[]) AND protein_group = ANY(%s)", (batch, JUNK))
            cur.execute("UPDATE delimp_proteins SET gene = NULL WHERE search_id = ANY(%s::uuid[]) AND gene = ANY(%s)", (batch, JUNK))
            conn.commit()

    print("null_nan_protein_group: " + ("APPLIED" if a.apply else "DRY RUN (nothing written)"))
    print(f"  searches affected                       : {len(sids):,} (exact)")
    print(f"  delimp_precursors.protein_group -> NULL : {sum(prec.values()):,} rows in {len(prec):,} searches")
    print(f"  delimp_proteins rows to DELETE          : {sum(prot.values()):,} rows in {len(prot):,} searches")
    print(f"  delimp_proteins.gene junk -> NULL       : {sum(gene.values()):,} rows in {len(gene):,} searches")
    show_examples("first three affected searches", examples)
    print("  after --apply: recompute n_protein_groups_total / n_proteins_total for the searches above, refresh the MVs")
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
