"""backfill_organism_taxid.py — resolve curated organism_name values to NCBI taxids.

70.7% of delimp_sample_metadata carried an organism_taxon_id because it came from a hand-typed
--taxon CLI flag at ingest, supplied for common model organisms and omitted for everything else.
Absence therefore meant "nobody typed it", not "unknown species": 5,192 rows had a perfectly good
curated organism_name and no taxid.

This is a LOOKUP, not a prediction. organism_name is curated by a human; mapping it to the NCBI taxid
is deterministic, so unlike backfill_organism_from_lance.py (which infers from peptide evidence and
must write only to predicted_organism_*) this one legitimately writes the asserted column. It still
never overwrites: the UPDATE is guarded on organism_taxon_id IS NULL.

Every mapping below was resolved against NCBI E-utilities and VERIFIED by fetching the taxid back and
comparing NCBI's own scientific name to what was searched. Three categories:
  exact (83)    NCBI's current scientific name is the stored name
  species (18)  stored name is UniProt strain style, e.g. "Saccharomyces cerevisiae (strain ATCC
                204508 / S288c)". Resolved to the SPECIES binomial on purpose -- the site groups
                species by name and the pre-existing taxids are all species-level, so a strain taxid
                would split one species into two rows on the species page.
  synonym (3)   NCBI renamed the organism; same organism, current name differs. Pisum sativum ->
                Lathyrus oleraceus, Eptesicus furinalis -> Neoeptesicus furinalis, Hypocrea jecorina
                -> Trichoderma reesei.

Deliberately NOT mapped: 'translate_table: standard' (1 row), a parser artifact rather than an
organism. app/queries.py already filters it via _REAL_ORG_PRED's starts_with() guard.

    python ingest/backfill_organism_taxid.py           # dry run
    python ingest/backfill_organism_taxid.py --apply
"""
import argparse
import functools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
print = functools.partial(print, flush=True)   # noqa: A001

# organism_name (exactly as stored) -> NCBI taxid. See the module docstring for how each was derived.
TAXIDS = {
    'Acinetobacter baumannii 6014059': 525242,
    'Acipenser ruthenus': 7906,
    'Aedes aegypti': 7159,
    'Akkermansia muciniphila': 239935,
    'Ananas comosus': 4615,
    'Arabidopsis thaliana': 3702,
    'Arachis hypogaea': 3818,
    'Artibeus jamaicensis': 9417,
    'Aspergillus niger': 5061,
    'Aspergillus niger (strain ATCC MYA-4892 / CBS 513.88 / FGSC A1513)': 5061,  # species-of(Aspergillus niger) -> Aspergillus niger
    'Aspergillus oryzae': 5062,
    'Aspergillus oryzae (strain ATCC 42149 / RIB 40)': 5062,  # species-of(Aspergillus oryzae) -> Aspergillus oryzae
    'Avena sativa': 4498,
    'Bacillus subtilis (strain 168)': 1423,  # species-of(Bacillus subtilis) -> Bacillus subtilis
    'Caenorhabditis elegans': 6239,
    'Cannabis sativa': 3483,
    'Capra hircus': 9925,
    'Cicer arietinum': 3827,
    'Coccidioides posadasii (strain C735)': 199306,  # species-of(Coccidioides posadasii) -> Coccidioides posadasii
    'Cucurbita maxima': 3661,
    'Cucurbita moschata': 3662,
    'Desmodus rotundus': 9430,
    'Digitaria exilis': 1010633,
    'Drosophila melanogaster': 7227,
    'Ectocarpus siliculosus': 2880,
    'Edwardsiella piscicida': 1263550,
    'Enhygromyxa salina': 215803,
    'Eptesicus furinalis': 3371123,  # synonym -> Neoeptesicus furinalis
    'Equus caballus': 9796,
    'Escherichia coli (strain B / BL21-DE3)': 562,  # species-of(Escherichia coli) -> Escherichia coli
    'Escherichia coli (strain K12)': 562,  # species-of(Escherichia coli) -> Escherichia coli
    'Escherichia coli O157:H7': 83334,
    'Felis catus': 9685,
    'Flavobacterium columnare': 996,
    'Flavobacterium columnare (strain ATCC 49512 / CIP 103533 / TG 44/87)': 996,  # species-of(Flavobacterium columnare) -> Flavobacterium columnare
    'Fusobacterium ulcerans': 861,
    'Galleria mellonella': 7137,
    'Glycine max': 3847,
    'Gossypium hirsutum': 3635,
    'Haemorhous mexicanus': 30427,
    'Helianthus annuus': 4232,
    'Hordeum vulgare subsp. vulgare': 112509,
    'Hypocrea jecorina (strain QM6a)': 51453,  # synonym -> Trichoderma reesei
    'Hypsibius dujardini': 232323,
    'Juglans regia': 51240,
    'Junco hyemalis': 40217,
    'Komagataella pastoris': 4922,
    'Komagataella phaffii (strain GS115 / ATCC 20864)': 460519,  # species-of(Komagataella phaffii) -> Komagataella phaffii
    'Lactococcus petauri': 1940789,
    'Lupinus albus': 3870,
    'Lupinus angustifolius': 3871,
    'Medicago truncatula': 3880,
    'Meloidogyne javanica': 6303,
    'Mesocricetus auratus': 10036,
    'Mesomycoplasma ovipneumoniae 14811': 1188239,
    'Molossus nigricans': 2997257,
    'Naegleria fowleri': 5763,
    'Neisseria meningitidis serogroup B (strain MC58)': 487,  # species-of(Neisseria meningitidis) -> Neisseria meningitidis
    'Nicotiana benthamiana': 4100,
    'Nicotiana tabacum': 4097,
    'Octopus vulgaris': 6645,
    'Olea europaea subsp. europaea': 158383,
    'Oncorhynchus kisutch': 8019,
    'Oreochromis niloticus': 8128,
    'Oryza sativa subsp. indica': 4530,  # species-of(Oryza sativa) -> Oryza sativa
    'Oryza sativa subsp. japonica': 4530,  # species-of(Oryza sativa) -> Oryza sativa
    'Peromyscus californicus': 42520,
    'Pisum sativum': 3888,  # synonym -> Lathyrus oleraceus
    'Populus alba': 43335,
    'Pseudomonas aeruginosa (strain ATCC 15692 / DSM 22644 / CIP 104116 / JCM 14847 / LMG 12228 / 1C / PRS 101 / PAO1)': 287,  # species-of(Pseudomonas aeruginosa) -> Pseudomonas aeruginosa
    'Pseudomonas aeruginosa (strain UCBPP-PA14)': 287,  # species-of(Pseudomonas aeruginosa) -> Pseudomonas aeruginosa
    'Pteronotus mesoamericanus': 1884717,
    'Rousettus aegyptiacus': 9407,
    'Saccharina japonica': 88149,
    'Saccharomyces cerevisiae': 4932,
    'Saccharomyces cerevisiae (strain ATCC 204508 / S288c)': 4932,  # species-of(Saccharomyces cerevisiae) -> Saccharomyces cerevisiae
    'Solanum lycopersicum': 4081,
    'Solanum tuberosum': 4113,
    'Sorghum bicolor': 4558,
    'Spirodela intermedia': 51605,
    'Spironucleus salmonicida': 348837,
    'Spodoptera frugiperda': 7108,
    'Stentor coeruleus': 5963,
    'Tadarida brasiliensis': 9438,
    'Tetrahymena thermophila (strain SB210)': 5911,  # species-of(Tetrahymena thermophila) -> Tetrahymena thermophila
    'Thalassiosira pseudonana': 35128,
    'Thunnus albacares': 8236,
    'Thunnus thynnus': 8237,
    'Toxoplasma gondii': 5811,
    'Toxoplasma gondii (strain ATCC 50853 / GT1)': 5811,  # species-of(Toxoplasma gondii) -> Toxoplasma gondii
    'Toxoplasma gondii (strain ATCC 50861 / VEG)': 5811,  # species-of(Toxoplasma gondii) -> Toxoplasma gondii
    'Trichechus manatus latirostris': 127582,
    'Trifolium pratense': 57577,
    'Triticum aestivum': 4565,
    'Vibrio cholerae serotype O1 (strain ATCC 39315 / El Tor Inaba N16961)': 666,  # species-of(Vibrio cholerae) -> Vibrio cholerae
    'Vicia faba': 3906,
    'Vigna radiata var. radiata': 3916,
    'Vitis vinifera': 29760,
    'Xenopus laevis': 8355,
    'Xylella fastidiosa': 2371,
    'Zea mays': 4577,
    'Zonotrichia albicollis': 44394,
    'Zonotrichia querula': 44390,
    'cyanobacterium endosymbiont of Braarudosphaera bigelowii': 1285375,
}


def _conn():
    import psycopg2
    from refresh_leaderboards import _token
    return psycopg2.connect(
        host=os.environ.get("DELIMP_PG_HOST", "pgfarm.library.ucdavis.edu"), port=5432,
        dbname=os.environ.get("DELIMP_PG_DB", "uc-davis-genome-center-proteomics-core/delimp"),
        user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
        password=_token(), sslmode="require", connect_timeout=30,
        options="-c statement_timeout=120000")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    conn = _conn(); conn.autocommit = False
    cur = conn.cursor()
    cur.execute("SET LOCAL lock_timeout = '10s'")

    cur.execute("""SELECT count(*) FILTER (WHERE organism_taxon_id IS NOT NULL), count(*)
                   FROM delimp_sample_metadata""")
    before, total = cur.fetchone()
    print(f"before: organism_taxon_id {before:,}/{total:,} ({100*before/total:.1f}%)")

    pairs = list(TAXIDS.items())
    cur.execute("""
        SELECT count(*) FROM delimp_sample_metadata m
        JOIN (SELECT * FROM unnest(%s::text[], %s::int[]) AS t(nm, tid)) t
          ON t.nm = m.organism_name
        WHERE m.organism_taxon_id IS NULL""",
        ([n for n, _ in pairs], [t for _, t in pairs]))
    eligible = cur.fetchone()[0]
    print(f"{len(TAXIDS)} names map; {eligible:,} rows are eligible (name matches AND taxid IS NULL)")

    if not a.apply:
        print("\nDRY RUN — re-run with --apply. Writes organism_taxon_id only where it IS NULL;")
        print("never overwrites a curated value, and touches no other column.")
        conn.rollback(); conn.close(); return

    cur.execute("""
        UPDATE delimp_sample_metadata m
        SET organism_taxon_id = t.tid
        FROM (SELECT * FROM unnest(%s::text[], %s::int[]) AS t(nm, tid)) t
        WHERE t.nm = m.organism_name AND m.organism_taxon_id IS NULL""",
        ([n for n, _ in pairs], [t for _, t in pairs]))
    n = cur.rowcount

    cur.execute("""SELECT count(*) FILTER (WHERE organism_taxon_id IS NOT NULL), count(*)
                   FROM delimp_sample_metadata""")
    after, total = cur.fetchone()
    print(f"updated {n:,} rows")
    print(f"after:  organism_taxon_id {after:,}/{total:,} ({100*after/total:.1f}%)")

    cur.execute("""SELECT organism_name, count(*) FROM delimp_sample_metadata
                   WHERE organism_name IS NOT NULL AND organism_taxon_id IS NULL
                   GROUP BY 1 ORDER BY 2 DESC LIMIT 5""")
    rest = cur.fetchall()
    print(f"still unresolved: {sum(k for _, k in rest)} rows across {len(rest)} names")
    for nm, k in rest:
        print(f"   {str(nm)[:50]:52s} {k:>5,}")

    # one name must never map to two taxids, or the species page splits
    cur.execute("""SELECT organism_name, count(DISTINCT organism_taxon_id)
                   FROM delimp_sample_metadata WHERE organism_taxon_id IS NOT NULL
                   GROUP BY 1 HAVING count(DISTINCT organism_taxon_id) > 1""")
    split = cur.fetchall()
    if split:
        print(f"\n!! {len(split)} names now carry MORE THAN ONE taxid — rolling back")
        for nm, k in split[:5]:
            print(f"   {nm}: {k} taxids")
        conn.rollback(); conn.close(); sys.exit(1)
    print("check: no organism_name maps to more than one taxid")

    import versions as V
    V.record_run(cur, "organism_taxid_backfill", "1.0.0",
                 notes=f"{n} rows, {len(TAXIDS)} names, NCBI-verified")
    conn.commit(); conn.close()
    print("DONE")


if __name__ == "__main__":
    main()
