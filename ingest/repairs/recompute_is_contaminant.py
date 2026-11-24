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
    python recompute_is_contaminant.py --apply         # writes is_contaminant and, if the column exists, contaminant_class
    python recompute_is_contaminant.py --apply --only-changed-flag   # ONLY the ~300k rows whose flag changes
    python recompute_is_contaminant.py --apply --max-searches 200 --start 0 --sleep 2   # a slice, off-peak
                                                       # (prints the next --start; idempotent, so re-running is safe); owner only, after reviewing a dry run
"""
from __future__ import annotations

import sys
import time

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


def _update(conn, cur, batch, have_cc, only_flag, retries=4):
    """UPDATE one search batch; retry on lock timeout / deadlock (a concurrent re-ingest can DELETE
    a search's rows). Returns rows updated."""
    import psycopg2.errors as pe
    if only_flag:
        sets = f"is_contaminant = {NEW_RULE}" + (f", contaminant_class = {CLASS_RULE}" if have_cc else "")
        diff = f"p.is_contaminant IS DISTINCT FROM {NEW_RULE}"
    else:
        sets = f"is_contaminant = {NEW_RULE}" + (f", contaminant_class = {CLASS_RULE}" if have_cc else "")
        diff = f"p.is_contaminant IS DISTINCT FROM {NEW_RULE}" + (
            f" OR p.contaminant_class IS DISTINCT FROM {CLASS_RULE}" if have_cc else "")
    for attempt in range(retries):
        try:
            cur.execute(f"UPDATE delimp_proteins p SET {sets} WHERE p.search_id = ANY(%s::uuid[]) AND ({diff})", (batch,))
            n = cur.rowcount
            conn.commit()
            return n
        except (pe.LockNotAvailable, pe.DeadlockDetected) as e:
            conn.rollback()
            print(f"    [retry {attempt + 1}/{retries}] {type(e).__name__}; waiting", flush=True)
            time.sleep(5 * (attempt + 1))
    raise SystemExit("giving up on a batch after repeated lock timeouts; re-run with the printed --start")


def main():
    ap = base_parser(__doc__.split("\n\n")[0])
    ap.add_argument("--sample", type=int, default=80,
                    help="searches to examine in a dry run (0 = all); the total is then an ESTIMATE")
    ap.add_argument("--only-changed-flag", action="store_true",
                    help="update only rows whose is_contaminant changes (~0.3%%), not the class of all rows")
    ap.add_argument("--max-searches", type=int, default=0, help="--apply: stop after N searches (0 = all)")
    ap.add_argument("--start", type=int, default=0, help="--apply: position in the ordered search list to resume from")
    ap.add_argument("--sleep", type=float, default=1.0, help="--apply: seconds between batches (default 1)")
    a = ap.parse_args()
    conn, cur = connect(a.apply)
    if a.apply:
        cur.execute("SET lock_timeout = '10s'")

    ids = all_search_ids(cur)
    cur.execute("""SELECT 1 FROM information_schema.columns
                    WHERE table_name='delimp_proteins' AND column_name='contaminant_class'""")
    have_cc = cur.fetchone() is not None     # the migration may not be applied yet

    if a.apply:
        # No count / example SELECTs here: they would repeat the scans the UPDATE does anyway.
        todo = ids[a.start:]
        if a.max_searches:
            todo = todo[:a.max_searches]
        done = rows = 0
        for batch in chunks(todo, a.batch):
            rows += _update(conn, cur, batch, have_cc, a.only_changed_flag)
            done += len(batch)
            print(f"  searches {a.start + done - len(batch)}..{a.start + done} of {len(ids)}: {rows:,} rows updated so far; "
                  f"resume with --start {a.start + done}", flush=True)
            time.sleep(a.sleep)
        print(f"recompute_is_contaminant: APPLIED to {done:,} searches, {rows:,} rows"
              f"{' (flag changes only)' if a.only_changed_flag else ''}; contaminant_class written: {have_cc}")
        print("  after: refresh delimp_mv_protein_agg, delimp_mv_species_proteins, delimp_mv_top_proteins")
        conn.close()
        return 0

    use, estimate = pick(ids, a.sample)
    classes: dict = {}
    cc_fill = 0                              # rows whose contaminant_class would be (re)written (all batches)
    tot_rows = to_true = to_false = flip_mixed = flip_none = 0
    examples_t, examples_f = [], []
    for batch in chunks(use, a.batch):
        cur.execute(f"""SELECT count(*),
                               count(*) FILTER (WHERE NOT p.is_contaminant AND {NEW_RULE}),
                               count(*) FILTER (WHERE p.is_contaminant AND NOT {NEW_RULE}),
                               count(*) FILTER (WHERE p.is_contaminant AND {CLASS_RULE} = 'mixed'),
                               count(*) FILTER (WHERE p.is_contaminant AND {CLASS_RULE} = 'sample')
                          FROM delimp_proteins p WHERE p.search_id = ANY(%s::uuid[])""", (batch,))
        n, t, f, fm, fn = cur.fetchone()
        tot_rows += n; to_true += t; to_false += f; flip_mixed += fm; flip_none += fn
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

    print("recompute_is_contaminant: DRY RUN (nothing written)")
    print(f"  searches examined: {len(use):,} of {len(ids):,}"
          + ("   -> counts below are for the SAMPLE; totals are ESTIMATES" if estimate else "   (exact)"))
    print(f"  protein rows examined: {tot_rows:,}")
    print(f"  would flip False -> True : {to_true:,}")
    print(f"  would flip True  -> False: {to_false:,}")
    print(f"     of the True->False flips: mixed group (has a Cont_ accession AND a real one): {flip_mixed:,}; "
          f"no contaminant accession at all (old name pattern only): {flip_none:,}")
    if tot_rows:
        print(f"  changed fraction of rows : {(to_true + to_false) / tot_rows:.2%}")
    cur.execute("SELECT reltuples::bigint FROM pg_class WHERE relname = 'delimp_proteins'")
    total_est = cur.fetchone()[0]
    if estimate and tot_rows:
        scale = total_est / tot_rows
        print(f"  ESTIMATE over ~{total_est:,} protein rows (sample fraction scaled by reltuples): "
              f"True->False ~{int(to_false * scale):,} (mixed ~{int(flip_mixed * scale):,}, name-only ~{int(flip_none * scale):,}), "
              f"False->True ~{int(to_true * scale):,}")
    print("  rows per contaminant_class (sample" + ("; ESTIMATE below" if estimate else "") + "):")
    for k in ("contaminant", "mixed", "sample", None):
        v = classes.get(k, 0)
        print(f"    {str(k):12s} {v:>12,}" + (f"   ~{int(v * (total_est / tot_rows)):,} of ~{total_est:,}"
                                             if estimate and tot_rows else ""))
    print(f"  contaminant_class column exists: {have_cc}"
          + (f"; rows whose class would be written (full run): {cc_fill:,}" if have_cc
             else " (migration 2026-11-24_proteins_contaminant_class.sql not applied; class is only counted)"))
    show_examples("flag would become False", examples_f)
    show_examples("flag would become True", examples_t)
    print("  after --apply: refresh delimp_mv_protein_agg, delimp_mv_species_proteins, delimp_mv_top_proteins")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
