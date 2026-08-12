"""build_gene_reference.py — a real gene list, so "% of proteome identified" is a measurement.

THE PROBLEM. delimp_proteome_reference stores only COUNTS (ncbi_protein_coding = 20,076 for human),
with no gene list to intersect against. So coverage was computed as
"distinct observed symbols / 20,076", and FRAN observes ~24-25k distinct human symbols -- a ratio of
125%, clamped to 100 for display. The number shown was an upper bound wearing a measurement's
clothes.

The excess is not extra genes. It is non-coding and pseudogene symbols, immunoglobulin and TCR
variable-region genes, and above all OBSOLETE symbols: a corpus spanning years of FASTA releases
carries names HGNC has since retired, and each one counted as a distinct gene.

THE FIX. HGNC's complete set gives 19,297 approved protein-coding genes plus 35,961 alias and 9,234
previous symbols -- 64,492 lookup keys onto 19,297 genes. With that, an observed symbol either
RESOLVES to a protein-coding gene (and counts once, under its approved name, however it was spelled)
or it does not (and is excluded, not counted as a novel gene). Coverage becomes a true intersection.

Human only. HGNC is the human nomenclature authority; other species need their own source (MGI for
mouse, RGD for rat), and the table is keyed by taxon so they can be added without redesign. Species
without a reference keep the old count-based estimate, which is flagged as an estimate.

    python ingest/build_gene_reference.py <hgnc_complete_set.txt>          # dry run
    python ingest/build_gene_reference.py <hgnc_complete_set.txt> --apply
"""
import argparse
import csv
import functools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
print = functools.partial(print, flush=True)   # noqa: A001

HUMAN = 9606
SOURCE = "HGNC_complete_set"

DDL = """
CREATE TABLE IF NOT EXISTS delimp_gene_reference (
    taxon_id        integer NOT NULL,
    symbol_upper    text    NOT NULL,   -- the LOOKUP key: approved, alias or previous, upper-cased
    approved_symbol text    NOT NULL,   -- what it resolves to; the unit of counting
    symbol_kind     text    NOT NULL,   -- 'approved' | 'alias' | 'previous'
    locus_group     text,
    source          text    NOT NULL,
    PRIMARY KEY (taxon_id, symbol_upper)
);
CREATE INDEX IF NOT EXISTS idx_generef_approved ON delimp_gene_reference (taxon_id, approved_symbol);
"""


def _conn():
    import psycopg2
    from refresh_leaderboards import _token
    return psycopg2.connect(
        host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
        dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
        user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
        password=_token(), sslmode="require", connect_timeout=30,
        options="-c statement_timeout=600000")


def parse(path):
    """HGNC -> lookup rows. Approved symbols win any collision with an alias/previous symbol:
    a name that is someone's current symbol must resolve to that gene, not to a gene that used
    to be called it."""
    out, seen = {}, set()
    with open(path, encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh, delimiter="\t")
                if r.get("status") == "Approved" and r.get("locus_group") == "protein-coding gene"]
    for r in rows:                                   # pass 1: approved
        sym = (r.get("symbol") or "").strip()
        if not sym:
            continue
        out[sym.upper()] = (sym, "approved", r.get("locus_group"))
        seen.add(sym.upper())
    for r in rows:                                   # pass 2: alias / previous, never overwriting
        sym = (r.get("symbol") or "").strip()
        for field, kind in (("alias_symbol", "alias"), ("prev_symbol", "previous")):
            for alt in (r.get(field) or "").split("|"):
                a = alt.strip().upper()
                if a and a not in seen:
                    out[a] = (sym, kind, r.get("locus_group"))
    return [(HUMAN, k, v[0], v[1], v[2], SOURCE) for k, v in out.items()], len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("hgnc")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if not os.path.exists(a.hgnc):
        sys.exit(f"no such file: {a.hgnc}")

    rows, n_genes = parse(a.hgnc)
    from collections import Counter
    kinds = Counter(r[3] for r in rows)
    print(f"{n_genes:,} approved protein-coding genes; {len(rows):,} lookup keys {dict(kinds)}")

    conn = _conn(); conn.autocommit = False
    cur = conn.cursor()
    cur.execute("SET LOCAL lock_timeout = '10s'")

    if not a.apply:
        print("\nDRY RUN — re-run with --apply.")
        conn.rollback(); conn.close(); return

    cur.execute(DDL); conn.commit()
    cur.execute("DELETE FROM delimp_gene_reference WHERE taxon_id = %s AND source = %s",
                (HUMAN, SOURCE))
    import psycopg2.extras
    psycopg2.extras.execute_values(cur, """
        INSERT INTO delimp_gene_reference
          (taxon_id, symbol_upper, approved_symbol, symbol_kind, locus_group, source)
        VALUES %s ON CONFLICT (taxon_id, symbol_upper) DO UPDATE
          SET approved_symbol = EXCLUDED.approved_symbol,
              symbol_kind = EXCLUDED.symbol_kind""", rows, page_size=2000)
    conn.commit()
    print(f"stored {len(rows):,} lookup keys")

    # ---- the whole point: what does coverage become when it is a real intersection? ----
    cur.execute("SET work_mem = '256MB'")
    cur.execute("""
        WITH obs AS (
          SELECT DISTINCT upper(btrim(g)) AS sym
          FROM delimp_mv_species_proteins, unnest(string_to_array(gene, ';')) g
          WHERE organism_name = 'Homo sapiens' AND gene IS NOT NULL AND btrim(g) <> ''
        )
        SELECT count(*)                                             AS observed_symbols,
               count(*) FILTER (WHERE r.symbol_upper IS NOT NULL)    AS resolvable,
               count(DISTINCT r.approved_symbol)                     AS distinct_genes
        FROM obs LEFT JOIN delimp_gene_reference r
          ON r.taxon_id = 9606 AND r.symbol_upper = obs.sym""")
    obs, resolvable, genes = cur.fetchone()
    cur.execute("""SELECT count(DISTINCT approved_symbol) FROM delimp_gene_reference
                   WHERE taxon_id = %s""", (HUMAN,))
    denom = cur.fetchone()[0]
    print(f"\nHUMAN COVERAGE, measured rather than estimated:")
    print(f"   observed distinct symbols   {obs:,}")
    print(f"   resolve to a coding gene    {resolvable:,}  ({100*resolvable/max(obs,1):.1f}%)")
    print(f"   DISTINCT genes covered      {genes:,} of {denom:,}  = {100*genes/max(denom,1):.1f}%")
    print(f"   unresolvable symbols        {obs-resolvable:,} (non-coding, pseudogene, IG/TCR "
          f"variable, contaminants, non-human)")

    import versions as V
    V.record_run(cur, "gene_reference", "1.0.0",
                 notes=f"HGNC {n_genes} genes, {len(rows)} keys; human coverage {genes}/{denom}")
    conn.commit(); conn.close()
    print("DONE")


if __name__ == "__main__":
    main()
