"""backfill_organism_taxon.py — fill delimp_sample_metadata.organism_taxon_id from organism_name.

WHY. `organism_taxon_id` is only 58.7% populated (11,670 of 19,874 raws), and five species appear in
the corpus BOTH with and without it:

    Homo sapiens             7,462 with taxon   1,458 NULL   -> 9606
    Mus musculus             1,766              811          -> 10090
    Sus scrofa                  21               56          -> 9823
    Rattus norvegicus          183               45          -> 10116
    Canis lupus familiaris     745                8          -> 9615
                                              = 2,378 raws recoverable

That split one species into two anywhere a query grouped by (organism_name, organism_taxon_id) —
P61278 displayed "2 species", both 'Homo sapiens', because 150 of its runs carried taxon 9606 and 14
carried NULL. 369,401 protein groups touch at least one NULL-taxon run, so the display bug was
corpus-wide, not specific to that protein.

The queries have been fixed to group on the NAME only, so the symptom is gone either way. This fixes
the DATA, which matters for anything that legitimately keys on taxon (proteome-size lookups, the
species pages, external joins).

Deliberately conservative: only fills where the SAME organism_name already has an unambiguous taxon
elsewhere in the corpus. It never invents a mapping, never overwrites a non-NULL value, and skips any
name whose existing taxon ids disagree.

    python ingest/backfill_organism_taxon.py            # dry run
    python ingest/backfill_organism_taxon.py --apply
"""
import argparse
import functools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
print = functools.partial(print, flush=True)


def _conn():
    import psycopg2
    from refresh_leaderboards import _token
    return psycopg2.connect(
        host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
        dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
        user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
        password=_token(), sslmode="require", connect_timeout=30,
        options="-c statement_timeout=300000")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    conn = _conn(); conn.autocommit = False
    cur = conn.cursor()

    # Only names with EXACTLY ONE distinct taxon id are safe to propagate.
    cur.execute("""
        SELECT organism_name,
               min(organism_taxon_id) AS taxon,
               count(DISTINCT organism_taxon_id) AS n_distinct,
               count(*) FILTER (WHERE organism_taxon_id IS NULL) AS n_null
        FROM delimp_sample_metadata
        WHERE organism_name IS NOT NULL
        GROUP BY 1
        HAVING count(*) FILTER (WHERE organism_taxon_id IS NULL) > 0
           AND count(DISTINCT organism_taxon_id) = 1
        ORDER BY n_null DESC""")
    rows = cur.fetchall()
    total = sum(r[3] for r in rows)
    print(f"{'organism':32s} {'taxon':>8s} {'NULL rows':>10s}")
    for name, tax, nd, nnull in rows:
        print(f"{name[:32]:32s} {tax:>8} {nnull:>10,}")
    print(f"\n{len(rows)} species, {total:,} rows fillable")

    # Report (but do not touch) the ambiguous ones.
    cur.execute("""
        SELECT organism_name, count(DISTINCT organism_taxon_id)
        FROM delimp_sample_metadata WHERE organism_name IS NOT NULL
        GROUP BY 1 HAVING count(DISTINCT organism_taxon_id) > 1""")
    amb = cur.fetchall()
    if amb:
        print(f"\nSKIPPED — {len(amb)} name(s) map to >1 taxon id (ambiguous, needs a human):")
        for n, k in amb[:10]:
            print(f"  {n!r} -> {k} distinct taxon ids")

    if not a.apply:
        print("\nDRY RUN — re-run with --apply.")
        conn.close(); return

    cur.execute("""
        UPDATE delimp_sample_metadata m
           SET organism_taxon_id = src.taxon
        FROM (SELECT organism_name, min(organism_taxon_id) AS taxon
              FROM delimp_sample_metadata WHERE organism_name IS NOT NULL
              GROUP BY 1 HAVING count(DISTINCT organism_taxon_id) = 1) src
        WHERE m.organism_name = src.organism_name
          AND m.organism_taxon_id IS NULL""")
    print(f"\nupdated {cur.rowcount:,} rows")
    conn.commit()

    cur.execute("""SELECT count(*), count(organism_taxon_id) FROM delimp_sample_metadata""")
    n, have = cur.fetchone()
    print(f"organism_taxon_id now {have:,} / {n:,} ({100*have/n:.1f}%)")

    import versions as V
    V.record_run(cur, "organism_taxon_backfill", "1.0.0", notes=f"{total} rows")
    conn.commit(); conn.close()
    print("DONE")


if __name__ == "__main__":
    main()
