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
from organism import contaminant_class, is_contaminant_group
from recompute_is_contaminant import CLASS_RULE, NEW_RULE

EDGE = ["P05784", "Cont_P02769", "Cont_P02768;P02768", "P02768;Cont_P02768", ";", ";;", "", " ", "  ;  ",
        "CON__P02769", "cRAP-001;Cont_X", " Cont_A;Cont_B", "Cont_A;;Cont_B;", "cont_p02769", "cRAP001",
        "Cont_", "nan", " P05784 ; Cont_P02769", "Cont_A; P1", "A;B;C", "\tCont_A"]


def main():
    ap = base_parser("SQL vs Python contaminant rule parity")
    ap.add_argument("--n", type=int, default=5000)
    a = ap.parse_args()
    conn, cur = connect(False)
    cur.execute("SELECT id FROM delimp_searches ORDER BY random() LIMIT 40")
    ids = [r[0] for r in cur.fetchall()]
    cur.execute("""SELECT DISTINCT protein_group FROM delimp_proteins
                    WHERE search_id = ANY(%s::uuid[]) AND protein_group ~* 'cont_|con__|crap' LIMIT %s""",
                (ids, a.n // 2))
    groups = [r[0] for r in cur.fetchall()]                       # contaminant-ish, the interesting side
    cur.execute("""SELECT DISTINCT protein_group FROM delimp_proteins
                    WHERE search_id = ANY(%s::uuid[]) LIMIT %s""", (ids[:10], a.n - len(groups)))
    groups += [r[0] for r in cur.fetchall()] + EDGE
    groups = list(dict.fromkeys(groups))
    bad = 0
    for i in range(0, len(groups), 1000):
        chunk = groups[i:i + 1000]
        vals = ",".join("(%s)" for _ in chunk)
        cur.execute(f"SELECT p.protein_group, {NEW_RULE}, {CLASS_RULE} FROM (VALUES {vals}) p(protein_group)", chunk)
        for g, sql_flag, sql_cls in cur.fetchall():
            if bool(sql_flag) != is_contaminant_group(g) or sql_cls != contaminant_class(g):
                bad += 1
                if bad <= 10:
                    print(f"MISMATCH {g!r}: sql=({sql_flag}, {sql_cls}) python=({is_contaminant_group(g)}, {contaminant_class(g)})")
    print(f"parity: {len(groups):,} distinct protein groups compared, {bad} mismatches")
    conn.close()
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
