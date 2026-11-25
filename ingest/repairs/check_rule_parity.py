"""check_rule_parity.py -- the SQL contaminant rules must equal the Python ones.

recompute_is_contaminant.py evaluates NEW_RULE / CLASS_RULE in SQL; ingest evaluates
organism.is_contaminant_group / contaminant_class in Python. A pure-Python test cannot prove the two
agree, so this samples distinct protein_group values from the live corpus through a READ-ONLY
connection (random searches, scoped by search_id), adds hand-written edge cases, evaluates both,
and exits 1 on any mismatch.

    python check_rule_parity.py [--n 5000]
"""
from __future__ import annotations

import random
import sys

from _common import base_parser, connect
from organism import contaminant_class, is_contaminant_flag, is_contaminant_group
from recompute_is_contaminant import CLASS_RULE, NEW_RULE

EDGE = ["P05784", "Cont_P02769", "Cont_P02768;P02768", "P02768;Cont_P02768", ";", ";;", "", " ", "  ;  ",
        "CON__P02769", "cRAP-001;Cont_X", " Cont_A;Cont_B", "Cont_A;;Cont_B;", "cont_p02769", "cRAP001",
        "Cont_", "nan", " P05784 ; Cont_P02769", "Cont_A; P1", "A;B;C", "\tCont_A", "P04264", "KRT1;P04264",
        "Q9XYZ_contaminant", "P1;keratin"]
EDGE_GENES = [None, "", "KRT1", "krt6b", "ALB", "Keratin-like", "CONT_X", "contaminant", "nan"]
# every edge group with every edge gene, plus the awkward case of a name match split across the join
EDGE_PAIRS = [(g, e) for g in EDGE for e in EDGE_GENES] + [("KR", "T1"), ("cont", "_x"), ("P1;Cont_A", "KRT1")]


def main():
    ap = base_parser("SQL vs Python contaminant rule parity")
    ap.add_argument("--n", type=int, default=5000)
    a = ap.parse_args()
    conn, cur = connect(False)
    cur.execute("SELECT id FROM delimp_searches ORDER BY random() LIMIT 40")
    ids = [r[0] for r in cur.fetchall()]
    cur.execute("""SELECT DISTINCT protein_group, gene FROM delimp_proteins
                    WHERE search_id = ANY(%s::uuid[])
                      AND (protein_group ~* 'cont_|con__|crap|KRT|keratin|contaminant' OR gene ~* 'KRT|keratin|cont_|contaminant')
                    LIMIT %s""", (ids, a.n // 2))
    pairs = [tuple(r) for r in cur.fetchall()]                    # the interesting side
    cur.execute("""SELECT DISTINCT protein_group, gene FROM delimp_proteins
                    WHERE search_id = ANY(%s::uuid[]) LIMIT %s""", (ids[:10], a.n - len(pairs)))
    pairs += [tuple(r) for r in cur.fetchall()] + EDGE_PAIRS
    pairs = [p for p in dict.fromkeys(pairs) if p[0] is not None]  # protein_group is NOT NULL in the table
    bad = 0
    for i in range(0, len(pairs), 1000):
        chunk = pairs[i:i + 1000]
        vals = ",".join("(%s, %s)" for _ in chunk)
        cur.execute(f"SELECT p.protein_group, p.gene, {NEW_RULE}, {CLASS_RULE} "
                    f"FROM (VALUES {vals}) p(protein_group, gene)", [x for pr in chunk for x in pr])
        for g, gene, sql_flag, sql_cls in cur.fetchall():
            if (bool(sql_flag) != is_contaminant_flag(g, gene) or sql_cls != contaminant_class(g, gene)
                    or bool(sql_flag) != (sql_cls in ("contaminant", "suspected"))
                    or (sql_cls == "contaminant") != is_contaminant_group(g)):
                bad += 1
                if bad <= 10:
                    print(f"MISMATCH {(g, gene)!r}: sql=({sql_flag}, {sql_cls}) "
                          f"python=({is_contaminant_flag(g, gene)}, {contaminant_class(g, gene)})")
    groups = pairs
    print(f"parity: {len(groups):,} distinct (protein_group, gene) pairs compared, {bad} mismatches")
    conn.close()
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
