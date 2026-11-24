"""recompute_is_contaminant.py -- re-derive delimp_proteins.is_contaminant under the new rule.

OLD rule (corpus_ingest, until 2026-11): a NAME regex over protein_group + gene
(KRT|keratin|cont_|contaminant). It flagged the sample's own cytokeratins and disagreed with the
database a protein came from.
NEW rule (organism.is_contaminant_group): a protein group is a contaminant only when EVERY
accession in it carries a contaminant-library prefix (Cont_ / CON__ / cRAP). A group that mixes a
Cont_ accession with a real one is NOT a contaminant: the sample database explains it too.

Where the flag is stored: delimp_proteins.is_contaminant is the only per-row copy. The materialized
views delimp_mv_protein_agg, delimp_mv_species_proteins and delimp_mv_top_proteins aggregate it
(bool_or / is_cont), so refresh them after --apply (the weekly MV cron does this). delimp_search_
fastas.is_contaminant is a different thing -- whether a whole FASTA is a contaminant library -- and
is NOT touched.

    python recompute_is_contaminant.py                 # DRY RUN, 80 random searches, extrapolated
    python recompute_is_contaminant.py --sample 0      # DRY RUN over every search (exact, slower)
    python recompute_is_contaminant.py --apply         # writes is_contaminant and, if the column exists, contaminant_class; owner only, after reviewing a dry run
"""
from __future__ import annotations

import sys

from _common import base_parser, chunks, connect, all_search_ids, pick, show_examples

# SQL twin of organism.is_contaminant_group: at least one non-empty accession, and none that lacks a
# contaminant prefix. Same pattern as organism._CONTAM_ACC. Verified against the Python function
# on real protein groups before this script was handed over (see the report).
NEW_RULE = r"""(
    EXISTS (SELECT 1 FROM unnest(string_to_array(p.protein_group, ';')) AS a(x) WHERE btrim(x) <> '')
    AND NOT EXISTS (SELECT 1 FROM unnest(string_to_array(p.protein_group, ';')) AS a(x)
                     WHERE btrim(x) <> '' AND x !~* '^\s*(Cont_|CON__|cRAP[-_]?\d)')
)"""

# SQL twin of organism.contaminant_class (NULL for a group with no accession).
CLASS_RULE = r"""(CASE
    WHEN NOT EXISTS (SELECT 1 FROM unnest(string_to_array(p.protein_group, ';')) AS a(x) WHERE btrim(x) <> '') THEN NULL
    WHEN NOT EXISTS (SELECT 1 FROM unnest(string_to_array(p.protein_group, ';')) AS a(x)
                      WHERE btrim(x) <> '' AND x !~* '^\s*(Cont_|CON__|cRAP[-_]?\d)') THEN 'contaminant'
    WHEN EXISTS (SELECT 1 FROM unnest(string_to_array(p.protein_group, ';')) AS a(x)
                  WHERE btrim(x) <> '' AND x ~* '^\s*(Cont_|CON__|cRAP[-_]?\d)') THEN 'mixed'
    ELSE 'sample' END)"""


def main():
    ap = base_parser(__doc__.split("\n\n")[0])
    ap.add_argument("--sample", type=int, default=80,
                    help="searches to examine in a dry run (0 = all); the total is then an ESTIMATE")
    a = ap.parse_args()
    conn, cur = connect(a.apply)

    ids = all_search_ids(cur)
    use, estimate = pick(ids, 0 if a.apply else a.sample)
    cur.execute("""SELECT 1 FROM information_schema.columns
                    WHERE table_name='delimp_proteins' AND column_name='contaminant_class'""")
    have_cc = cur.fetchone() is not None     # the migration may not be applied yet
    classes: dict = {}
    cc_fill = 0                              # rows whose contaminant_class would be (re)written
    tot_rows = to_true = to_false = 0
    examples_t, examples_f, changed_searches = [], [], 0
    for batch in chunks(use, a.batch):
        cur.execute(f"""SELECT count(*),
                               count(*) FILTER (WHERE NOT p.is_contaminant AND {NEW_RULE}),
                               count(*) FILTER (WHERE p.is_contaminant AND NOT {NEW_RULE})
                          FROM delimp_proteins p WHERE p.search_id = ANY(%s::uuid[])""", (batch,))
        n, t, f = cur.fetchone()
        tot_rows += n; to_true += t; to_false += f
        cur.execute(f"SELECT {CLASS_RULE} AS k, count(*) FROM delimp_proteins p "
                    f"WHERE p.search_id = ANY(%s::uuid[]) GROUP BY 1", (batch,))
        for k, cnt in cur.fetchall():
            classes[k] = classes.get(k, 0) + cnt
        if have_cc:
            cur.execute(f"""SELECT count(*) FROM delimp_proteins p WHERE p.search_id = ANY(%s::uuid[])
                             AND p.contaminant_class IS DISTINCT FROM {CLASS_RULE}""", (batch,))
            cc_fill += cur.fetchone()[0]
        if len(examples_t) < 3 and t:
            cur.execute(f"""SELECT p.search_id, p.protein_group, p.gene FROM delimp_proteins p
                             WHERE p.search_id = ANY(%s::uuid[]) AND NOT p.is_contaminant AND {NEW_RULE} LIMIT 3""", (batch,))
            examples_t += [f"False -> True  {r}" for r in cur.fetchall()]
        if len(examples_f) < 3 and f:
            cur.execute(f"""SELECT p.search_id, p.protein_group, p.gene FROM delimp_proteins p
                             WHERE p.search_id = ANY(%s::uuid[]) AND p.is_contaminant AND NOT {NEW_RULE} LIMIT 3""", (batch,))
            examples_f += [f"True -> False  {r}" for r in cur.fetchall()]
        if a.apply and (t or f or (have_cc and cc_fill)):
            sets = f"is_contaminant = {NEW_RULE}" + (f", contaminant_class = {CLASS_RULE}" if have_cc else "")
            diff = f"p.is_contaminant IS DISTINCT FROM {NEW_RULE}" + (
                f" OR p.contaminant_class IS DISTINCT FROM {CLASS_RULE}" if have_cc else "")
            cur.execute(f"""UPDATE delimp_proteins p SET {sets}
                             WHERE p.search_id = ANY(%s::uuid[]) AND ({diff})""", (batch,))
            conn.commit()
            changed_searches += 1

    mode = "APPLIED" if a.apply else "DRY RUN (nothing written)"
    print(f"recompute_is_contaminant: {mode}")
    print(f"  searches examined: {len(use):,} of {len(ids):,}"
          + ("   -> counts below are for the SAMPLE; totals are ESTIMATES" if estimate else "   (exact)"))
    print(f"  protein rows examined: {tot_rows:,}")
    print(f"  would flip False -> True : {to_true:,}")
    print(f"  would flip True  -> False: {to_false:,}")
    if tot_rows:
        print(f"  changed fraction of rows : {(to_true + to_false) / tot_rows:.2%}")
    cur.execute("SELECT reltuples::bigint FROM pg_class WHERE relname = 'delimp_proteins'")
    total_est = cur.fetchone()[0]
    if estimate and tot_rows:
        total = total_est
        scale = total / tot_rows
        print(f"  ESTIMATE over ~{total:,} protein rows (sample fraction scaled by reltuples): "
              f"True->False ~{int(to_false * scale):,}, False->True ~{int(to_true * scale):,}")
    print("  rows per contaminant_class (sample" + ("; ESTIMATE below" if estimate else "") + "):")
    for k in ("contaminant", "mixed", "sample", None):
        v = classes.get(k, 0)
        print(f"    {str(k):12s} {v:>12,}" + (f"   ~{int(v * (total_est / tot_rows)):,} of ~{total_est:,}"
                                             if estimate and tot_rows else ""))
    print(f"  contaminant_class column exists: {have_cc}"
          + (f"; rows whose class would be written: {cc_fill:,}" if have_cc
             else " (migration 2026-11-24_proteins_contaminant_class.sql not applied; class is only counted)"))
    show_examples("flag would become False", examples_f)
    show_examples("flag would become True", examples_t)
    print("  after --apply: refresh delimp_mv_protein_agg, delimp_mv_species_proteins, delimp_mv_top_proteins")
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
