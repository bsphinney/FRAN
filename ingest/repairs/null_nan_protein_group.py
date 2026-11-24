"""null_nan_protein_group.py -- literal 'nan' / 'NaN' stored as text instead of NULL.

delimp_precursors.protein_group is nullable: the junk text becomes NULL.
delimp_proteins.protein_group is NOT NULL, and a row whose group is junk is not a protein at all
(one bogus 'protein' per run holding every unassigned precursor), so those rows are DELETED. That
includes the 'UNKNOWN' sentinel the old ingest wrote for an empty group (delimp_proteins only; it
never appears in delimp_precursors). delimp_proteins.gene junk becomes NULL.

Which junk spellings exist was MEASURED on 2026-11-24 with an equality probe per value on
idx_prec_protein_group / idx_proteins_group: 'nan' and 'NaN' occur; 'NAN', 'None', 'none', 'null',
'NULL', '', 'NA', 'na', 'N/A', 'n/a', 'Na', ' ' and 'unknown' do NOT (0 rows in both tables), so they
are probed (cheaply) but there is nothing to fix. 'UNKNOWN' occurs in delimp_proteins only.

After deleting protein rows, delimp_searches.n_protein_groups_total / n_proteins_total of the
affected searches are recomputed with the SAME definition corpus_ingest uses (distinct protein
groups; distinct non-empty accessions of the ';'-split groups). Only for a search whose STORED
numbers the current definition reproduces from its pre-repair rows -- a search with an older
definition (before 2026-07-27 n_proteins_total was a group count) is reported and left alone.
The dry run prints before / after for each affected search.

Every statement is an index probe per junk value (never a table scan) or scoped by search_id.

    python null_nan_protein_group.py            # DRY RUN, exact counts
    python null_nan_protein_group.py --apply    # writes; owner only
"""
from __future__ import annotations

import sys

from _common import base_parser, chunks, connect, show_examples

JUNK = ["nan", "NaN", "NAN", "None", "none", "NONE", "null", "NULL", "", "NA", "na", "N/A", "n/a", "Na", " "]
JUNK_PROTEINS = JUNK + ["UNKNOWN"]

# SQL twin of corpus_ingest's n_protein_groups / n_proteins for one search's rows.
COUNTS = """SELECT count(DISTINCT protein_group) FILTER (WHERE {keep}),
                  (SELECT count(DISTINCT btrim(x)) FROM delimp_proteins q, unnest(string_to_array(q.protein_group, ';')) AS u(x)
                    WHERE q.search_id = %(sid)s AND btrim(x) <> '' AND lower(btrim(x)) NOT IN ('nan','none','null','unknown')
                      AND {keep_q})
             FROM delimp_proteins WHERE search_id = %(sid)s"""


def _by_value(cur, table, col, values):
    """{search_id: n} of rows whose `col` equals any junk value, one index probe per value."""
    out: dict = {}
    for v in values:
        cur.execute(f"SELECT search_id, count(*) FROM {table} WHERE {col} = %s GROUP BY 1", (v,))
        for sid, n in cur.fetchall():
            out[sid] = out.get(sid, 0) + n
    return out


def main():
    ap = base_parser(__doc__.split("\n\n")[0])
    a = ap.parse_args()
    conn, cur = connect(a.apply)

    prec = _by_value(cur, "delimp_precursors", "protein_group", JUNK)
    prot = _by_value(cur, "delimp_proteins", "protein_group", JUNK_PROTEINS)
    gene = _by_value(cur, "delimp_proteins", "gene", JUNK)
    sids = sorted(set(prec) | set(prot) | set(gene), key=str)

    # before / after of the per-search protein counts, for searches that lose protein rows
    recount, skipped = [], []
    for sid in sorted(prot, key=str):
        cur.execute("SELECT n_protein_groups_total, n_proteins_total, search_name FROM delimp_searches WHERE id = %s", (sid,))
        r = cur.fetchone()
        if not r:
            continue
        g0, p0, name = r
        junk_sql = "protein_group <> ALL(%(j)s)"
        cur.execute(COUNTS.format(keep="TRUE", keep_q="TRUE"), {"sid": sid})
        g_all, p_all = cur.fetchone()               # pre-repair, what the stored numbers should equal
        cur.execute(COUNTS.format(keep=junk_sql, keep_q="q.protein_group <> ALL(%(j)s)"), {"sid": sid, "j": JUNK_PROTEINS})
        g1, p1 = cur.fetchone()
        # the old ingest counted the junk group as one label ('' -> counted once); allow that +1
        reproducible = (g0 is not None and g0 in (g_all, g_all - 1) and p0 == p_all)
        (recount if reproducible else skipped).append((sid, name, g0, p0, g1, p1))

    examples = []
    for sid in sids[:3]:
        cur.execute("SELECT search_name FROM delimp_searches WHERE id = %s", (sid,))
        nm = (cur.fetchone() or ["?"])[0]
        examples.append(f"search {sid} {nm!r}: {prec.get(sid, 0):,} precursor rows, "
                        f"{prot.get(sid, 0):,} protein rows, {gene.get(sid, 0):,} junk genes")
    if a.apply:
        cur.execute("SET lock_timeout = '10s'")
        for batch in chunks(sids, a.batch):
            cur.execute("UPDATE delimp_precursors SET protein_group = NULL "
                        "WHERE search_id = ANY(%s::uuid[]) AND protein_group = ANY(%s)", (batch, JUNK))
            cur.execute("DELETE FROM delimp_proteins WHERE search_id = ANY(%s::uuid[]) AND protein_group = ANY(%s)",
                        (batch, JUNK_PROTEINS))
            cur.execute("UPDATE delimp_proteins SET gene = NULL WHERE search_id = ANY(%s::uuid[]) AND gene = ANY(%s)",
                        (batch, JUNK))
            conn.commit()
        for sid, _nm, _g0, _p0, g1, p1 in recount:
            cur.execute("UPDATE delimp_searches SET n_protein_groups_total = %s, n_proteins_total = %s WHERE id = %s",
                        (g1, p1, sid))
        conn.commit()

    print("null_nan_protein_group: " + ("APPLIED" if a.apply else "DRY RUN (nothing written)"))
    print(f"  searches affected                       : {len(sids):,} (exact)")
    print(f"  delimp_precursors.protein_group -> NULL : {sum(prec.values()):,} rows in {len(prec):,} searches")
    print(f"  delimp_proteins rows to DELETE          : {sum(prot.values()):,} rows in {len(prot):,} searches (incl. 'UNKNOWN')")
    print(f"  delimp_proteins.gene junk -> NULL       : {sum(gene.values()):,} rows in {len(gene):,} searches")
    show_examples("first three affected searches", examples)
    print(f"  delimp_searches protein counts to recompute: {len(recount):,} searches "
          f"(before groups/proteins -> after groups/proteins)")
    for sid, nm, g0, p0, g1, p1 in recount[:15]:
        print(f"     {str(sid)[:8]} {str(nm)[:40]!r}: {g0}/{p0} -> {g1}/{p1}")
    if skipped:
        print(f"  left alone (stored counts not reproducible by the current definition): {len(skipped)}")
        for sid, nm, g0, p0, g1, p1 in skipped[:5]:
            print(f"     {str(sid)[:8]} {str(nm)[:40]!r}: stored {g0}/{p0}; would be {g1}/{p1}")
    print("  after --apply: refresh the MVs")
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
