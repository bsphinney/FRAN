"""annotate_schema.py — put FRAN's precedence-of-truth INTO the database as column comments.

WHY. Every trap in this corpus has now been rediscovered at least twice, by a human or by an agent,
because the knowledge lived in a document instead of next to the data. A COMMENT ON COLUMN is visible
to anything that introspects the schema — psql \\d+, information_schema.col_description(), most ORMs,
and any AI agent that dumps the schema before querying. It travels with the table.

The specific incident this was written after: an agent compared `instrument_model` from one search
against the file extension from a DIFFERENT search and reported a vendor mismatch that did not exist.
A second reader then misdiagnosed the same rows as a stale `raw_path` extension. Both readings were
avoidable if the columns had said what they are.

Comments are metadata-only: no table rewrite, no data change, and no AccessExclusiveLock on the heap,
so this is safe to run against the live corpus at any time (unlike the ALTER that stalled things on
2026-07-29).

    python ingest/annotate_schema.py            # show what would be set
    python ingest/annotate_schema.py --apply
"""
import argparse
import functools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
print = functools.partial(print, flush=True)   # noqa: A001

# (table, column, comment). Column None => COMMENT ON TABLE.
COMMENTS = [
    # ---- raw_files: the vendor-truth precedence, in the order to trust it -------------------
    ("raw_files", None,
     "One row per raw FILE (raw_path). NOTE the biological unit is the ACQUISITION "
     "(raw_basename, ~11,671 distinct) -- 4,534 basenames appear at more than one path, and those "
     "are the SAME acquisition (verified: 0 differ in acquisition_date or instrument_serial). "
     "Use raw_basename as the denominator for any coverage claim."),
    ("raw_files", "raw_path",
     "PRIMARY KEY and the join key to search_raw_files / delimp_precursors / delimp_proteins / "
     "delimp_sample_metadata. It is an IDENTIFIER, NOT A DESCRIPTION: the path is SYNTHETIC and its "
     "file EXTENSION was GUESSED from `platform` at ingest ('.d' if timstof else '.raw'). The "
     "2026-07 sweep later corrected platform from the physical file but did not rewrite this, so "
     "5,629 rows end in '.d' while the real file is '.raw'. NEVER infer vendor or file format from "
     "this column -- use hive_path, platform, instrument_model or instrument_serial. Do not 'fix' "
     "it; rewriting it would cascade through four tables."),
    ("raw_files", "hive_path",
     "The RESOLVED, real on-disk location (resolve_raw_hive_paths.py). AUTHORITATIVE for file format "
     "and existence, unlike raw_path. ~98.7% populated; 250 rows have none, and for those there is "
     "no authoritative location to fall back on."),
    ("raw_files", "instrument_serial",
     "Read from the physical file header, so it is DECISIVE FOR VENDOR when other fields disagree: "
     "Thermo serials look like 'MA10354C' or 'fsn20215'; Bruker like '1854399.00153'. A Thermo-format "
     "serial cannot come from a .d directory."),
    ("raw_files", "instrument_model",
     "From the raw header (Bruker analysis.tdf GlobalMetadata / Thermo ThermoRawFileParser), ~99% "
     "populated. Trust this over raw_path's extension. Beware leading whitespace in some values "
     "(' timsTOF Pro' vs 'timsTOF Pro') -- normalise before grouping."),
    ("raw_files", "platform",
     "'timstof' or 'orbitrap', corrected from the physical file in the 2026-07 sweep (~28% had been "
     "wrong). Agrees with hive_path's extension; does NOT agree with raw_path's."),
    ("raw_files", "gradient_minutes",
     "98.7% populated, but it is a PROXY: the EvoSep SPD->gradient map when SPD is known, else the "
     "observed RT span. gradient_minutes_measured (from Spectronaut RunOverview) is the measured one "
     "where available -- keep them separate rather than overwriting."),
    ("raw_files", "acquisition_date",
     "From the raw header. Also the COLUMN-AGING PROXY: runs close in time share an LC column, which "
     "is why column_id/column_age were dropped as fields with no upstream source."),

    # ---- the aggregate-grain traps -----------------------------------------------------------
    ("delimp_proteins", None,
     "One row per (protein_group x search x run). Aggregates here are PER-RUN quantities: "
     "SUM(n_unique_peptides) is a sum of per-run counts, NOT a distinct peptide count -- it "
     "overstated by up to 56x on the gene page. For a true peptide count use "
     "COUNT(DISTINCT stripped_seq) FROM delimp_precursors WHERE protein_group = ..."),
    ("delimp_proteins", "n_unique_peptides",
     "PER-RUN count for this protein in THIS run. Do not SUM across runs and call it a peptide "
     "count; use COUNT(DISTINCT stripped_seq) on delimp_precursors instead."),
    ("delimp_proteins", "is_contaminant",
     "A property of WHICH FASTA ENTRY the match came from (the cRAP contaminant database vs the "
     "sample proteome), NOT of the molecule. The same gene is flagged both ways: ALB 12,279 "
     "contaminant / 14,125 not; KRT1 17,459 / 32; CSN1S1 7,701 / 1,203. So do NOT filter on it to "
     "'clean' data -- that deletes casein from milk, keratin from skin and albumin from plasma, "
     "which are the analyte in those samples (caseins discriminate milk runs 90x while flagged "
     "contaminant in all of them). Treat it as a FEATURE, not a filter."),

    # ---- delimp_precursors --------------------------------------------------------------------
    ("delimp_precursors", "best_q_value",
     "~99.9% NULL corpus-wide (56 of 41,849 sampled). ORDER BY on it is a total tie and returns an "
     "arbitrary row -- this silently picked unrepresentative spectra for months. Use q_value."),
    ("delimp_precursors", "protein_group",
     "100% populated and INDEXED (idx_prec_protein_group, 2026-07-30), so "
     "WHERE protein_group = ... is fast (~0.05s). Before that index existed the app used a "
     "co-observation heuristic that silently returned 0 peptides for low-abundance proteins."),

    # ---- searches: name is not a key -----------------------------------------------------------
    ("delimp_searches", "search_name",
     "NOT UNIQUE and NOT A KEY: 1,963 searches carry 1,927 distinct names -- 16 names are reused, "
     "for 36 excess rows. The bigger hazard is substring matching: ILIKE '%<lab>%' spans unrelated "
     "projects on different instruments, which has already produced a false 'instrument mismatch' "
     "report (a model read from one search was held against a file extension from another). "
     "Separately, one legitimate search CAN span two instrument models (3 such searches exist), so "
     "a model/vendor split within a search is not by itself evidence of an error. Carry "
     "delimp_searches.id (UUID) through every follow-up query instead of re-matching on name."),
    ("delimp_searches", "id",
     "The stable identifier. Deterministic uuid5 over output_dir. Use this, not search_name, to "
     "carry a search between queries."),
    ("delimp_searches", "n_proteins_total",
     "MEANING CHANGED 2026-07-27: previously the protein GROUP count, now the true protein count "
     "(4.75M groups -> 6.00M proteins). The old value moved to n_protein_groups_total. Any protein "
     "figure quoted from before that date is a group count."),
    ("delimp_searches", "fasta_path",
     "Only ~157 of 1,963 populated, and fasta_md5 / fasta_n_proteins are never written -- "
     "corpus_ingest.py records no FASTA at all. The searched proteome is therefore NOT recoverable "
     "for historical searches (Spectronaut's AnalysisLog logs 'Digesting Fasta...' without the "
     "filename; .params files are 126-byte export summaries)."),

    # ---- sample metadata: the empty annotation block -------------------------------------------
    ("delimp_sample_metadata", None,
     "Per-RAW annotation, keyed raw_path. The ontology block (tissue_name, tissue_efo_accession, "
     "cell_line_name, disease_name, sdrf_row_json, biological_replicate, label_type, enrichment, "
     "fraction) is designed but 0/19,874 POPULATED -- FRAN cannot currently answer 'find a dataset "
     "with experimental conditions'. Experimental groups DO exist upstream in the Spectronaut "
     "reports as R_Condition / R_Replicate but have never been ingested."),
    ("delimp_sample_metadata", "sample_type",
     "Hardcoded to the literal 'study_sample' on every row. Carries zero information; do not filter "
     "or group on it."),
    ("delimp_sample_metadata", "organism_name",
     "CURATED/declared value. Route new values through ingest/organism.py canonical_organism(), which "
     "nulls junk sentinels ('Unknown', 'nan', ...) and strips '(Human)'-style suffixes."),
    ("delimp_sample_metadata", "organism_taxon_id",
     "~70.7% populated. It was a hand-typed --taxon CLI flag, supplied for common model organisms "
     "and omitted for the rest, so 105 of 115 distinct organism names have no taxon. Absence means "
     "'nobody typed it', NOT 'unknown species'."),
    ("delimp_sample_metadata", "predicted_organism_name",
     "INFERRED, never curated. Derived from the Lance lane's PEP.AllOccurringOrganisms by modal "
     "organism-unique peptide count. Keep separate from organism_name: never COALESCE the two "
     "without also exposing which one you used."),

    # ---- stale / misleading tables --------------------------------------------------------------
    ("delimp_spectrum_regen_queue", None,
     "STALE AND ABANDONED planning tracker from 2026-07. Still reads ~1,871 rows that mean nothing. "
     "The source of truth for the spectrum lane is delimp_spectrum_lane."),
    ("delimp_spectrum_lane_runs", None,
     "The run <-> Lance dataset bridge. Lance datasets are one per SEARCH, not per run (only 218 of "
     "1,552 hold a single run), so this is how you scope by run. Join to raw_files on "
     "raw_files.raw_basename = run -- NOT on raw_path, which is NULL in the Lance data. That join is "
     "1:many (~1.8x) but the duplicates are the same acquisition, so use SELECT DISTINCT and never "
     "COUNT(*) through it. Note the same run appears in multiple datasets (1.34x, up to 14) because "
     "the same raw was searched repeatedly; collapse that before counting observations."),
    ("delimp_precursor_xic", "search_id",
     "TEXT, and 13 of 23 distinct values are name-slugs rather than UUIDs, so this does NOT join to "
     "delimp_searches.id."),
    ("delimp_xic_fragment", None,
     "delimp_precursor_xic.fragments flattened to one row per fragment with a btree on mz, so the "
     "shared-transition panel is an index range scan instead of a 264k-row jsonb explosion. DERIVED "
     "-- rebuild after any XIC ingest (ingest/build_xic_fragment_index.py)."),
]



# A COMMENT is only seen by someone who thinks to run \d+ on the right table. This view is seen by
# anyone who lists tables at all: the name sorts to the TOP of \dt and of any
# information_schema.tables ordered by name, and reads as an instruction.
READ_ME_FIRST = """
CREATE OR REPLACE VIEW aaa_fran_read_me_first AS
SELECT * FROM (VALUES
  (1, 'vendor / file format',
      'NEVER infer vendor from raw_path. Its extension was GUESSED from platform at ingest and 5,629 rows are stale. Use hive_path, platform, instrument_model, or instrument_serial (Thermo MA10354C/fsn20215 vs Bruker 1854399.00153).'),
  (2, 'identifying a search',
      'search_name is NOT a key: 1,963 searches, 1,927 names. ILIKE matching spans unrelated projects on different instruments. Carry delimp_searches.id through every follow-up query. One legitimate search CAN span two instrument models, so a vendor split inside a search is not itself a bug.'),
  (3, 'never compare across rows',
      'Before concluding two fields contradict each other, confirm they came from the SAME row. Comparing a model from one search against a file extension from another has already produced a false bug report. This is the single most common error made against this corpus.'),
  (4, 'counting',
      'raw_files has 19,874 rows but 11,671 distinct raw_basename; the basename is the acquisition. delimp_proteins.n_unique_peptides is PER-RUN -- SUMming it overstated a peptide count by up to 56x. Use COUNT(DISTINCT stripped_seq) FROM delimp_precursors.'),
  (5, 'contaminants',
      'is_contaminant marks WHICH FASTA the match came from, not what the molecule is. The same gene is flagged both ways (ALB 12,279 / 14,125). Filtering on it deletes casein from milk and keratin from skin -- the analyte. Use it as a feature, not a filter.'),
  (6, 'absent means unrecorded',
      'organism_taxon_id (70.7%) was a hand-typed CLI flag; absence means nobody typed it, not unknown species. best_q_value is ~99.9% NULL so ORDER BY on it returns an arbitrary row. sample_type is the constant ''study_sample''.'),
  (7, 'experimental design',
      'tissue/disease/cell_line/biological_replicate/sdrf_row_json are 0% populated corpus-wide. Experimental groups DO exist upstream in the Spectronaut reports as R_Condition / R_Replicate but have never been ingested. Do not conclude from an empty column that the information does not exist.'),
  (8, 'meaning changes',
      'delimp_searches.n_proteins_total changed 2026-07-27 from a protein GROUP count to a true protein count (4.75M -> 6.00M). Any protein figure quoted from before that date is a group count. Check COMMENT ON COLUMN before trusting a historical number.'),
  (9, 'stale objects',
      'delimp_spectrum_regen_queue is abandoned (~1,871 meaningless rows); the spectrum lane truth is delimp_spectrum_lane. delimp_precursor_xic.search_id is TEXT and 13 of 23 values are name-slugs, so it does NOT join to delimp_searches.id.'),
  (10, 'full detail',
      'Every column above carries a COMMENT with the specifics. Read them with: SELECT c.relname, a.attname, col_description(c.oid, a.attnum) FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid WHERE a.attnum>0 AND col_description(c.oid, a.attnum) IS NOT NULL ORDER BY 1,2;')
) AS t(n, topic, rule)
"""

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

    # only annotate what exists
    cur.execute("""SELECT table_name, column_name FROM information_schema.columns
                   WHERE table_schema='public'""")
    have_col = {(t, c) for t, c in cur.fetchall()}
    cur.execute("""SELECT table_name FROM information_schema.tables WHERE table_schema='public'""")
    have_tab = {r[0] for r in cur.fetchall()}

    todo, skipped = [], []
    for tbl, col, txt in COMMENTS:
        if col is None:
            (todo if tbl in have_tab else skipped).append((tbl, col, txt))
        else:
            (todo if (tbl, col) in have_col else skipped).append((tbl, col, txt))

    print(f"{len(todo)} comments to set, {len(skipped)} skipped (object absent)")
    for tbl, col, _ in skipped:
        print(f"  SKIP {tbl}.{col or '(table)'}")
    if not a.apply:
        print("\nDRY RUN — re-run with --apply. Comments are metadata only: no rewrite, no data")
        print("change, no lock on the heap, safe against the live corpus.")
        for tbl, col, txt in todo[:4]:
            print(f"\n  {tbl}.{col or '(table)'}:\n    {txt[:150]}...")
        conn.close(); return

    n = 0
    for tbl, col, txt in todo:
        target = f'"{tbl}"."{col}"' if col else f'"{tbl}"'
        kind = "COLUMN" if col else "TABLE"
        cur.execute(f"COMMENT ON {kind} {target} IS %s", (txt,))
        n += 1
    cur.execute(READ_ME_FIRST)
    cur.execute("COMMENT ON VIEW aaa_fran_read_me_first IS "
                "'START HERE. The traps in this corpus, as rows. Named to sort first in \\dt.'")
    conn.commit()
    print(f"\napplied {n} comments + the aaa_fran_read_me_first view")

    cur.execute("""
        SELECT c.relname, a.attname, col_description(c.oid, a.attnum)
        FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid
        WHERE c.relname IN ('raw_files','delimp_proteins','delimp_searches')
          AND a.attnum > 0 AND col_description(c.oid, a.attnum) IS NOT NULL
        ORDER BY 1,2 LIMIT 6""")
    print("\nspot-check (what an agent introspecting the schema will now see):")
    for t, col, d in cur.fetchall():
        print(f"  {t}.{col}: {str(d)[:96]}...")

    import versions as V
    V.record_run(cur, "schema_annotations", "1.0.0", notes=f"{n} comments")
    conn.commit(); conn.close()
    print("DONE")


if __name__ == "__main__":
    main()
