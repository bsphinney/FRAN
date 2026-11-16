-- Substring peptide search ("NEEDS" in the peptide box, exact unchecked) timed out because it was
-- `stripped_seq ILIKE '%NEEDS%'` over delimp_precursors (763M rows, 559 GB, only btree indexes).
-- This builds a ~4M-row DISTINCT-peptide relation with an I/L-normalised pg_trgm GIN index.
-- The app (queries._peptide_index_available) starts using it automatically within 60 s of it existing.
--
-- NOT applied by the app (read-only by design). The OWNER runs this once:
--   step 1 needs a role that may CREATE EXTENSION (PG Farm admin / postgres); pg_trgm is AVAILABLE
--   but not installed in `delimp` as of 2026-11-16 (installed: plpgsql, pg_stat_statements,
--   pgstattuple, amcheck).
--   steps 2-4 run as the table owner (the `delimp` role that owns delimp_precursors).

-- 1. extension (admin)
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 2. one row per distinct peptide, carrying the SAME list-row aggregates queries._PEPTIDE_SEARCH_COLS
--    computes live, so a substring search never touches delimp_precursors (a join-back was measured
--    at >120 s for the 10 most-observed peptides). seq_il = I->L normalised (I/L are isobaric; the
--    corpus stores one spelling per peptide).
CREATE TABLE IF NOT EXISTS delimp_peptide_index (
    stripped_seq text PRIMARY KEY,
    seq_il       text NOT NULL,
    n_precursors bigint, n_modforms bigint, n_charges int, n_runs bigint, n_searches bigint,
    best_q_value double precision, has_im boolean
);

--    BUILD COST (not measured end to end -- no DDL/long scans were run): the aggregate is one pass over
--    the 559 GB table with COUNT(DISTINCT raw_path / search_id / modified_seq_proforma), i.e. sort
--    heavy. A probe of just the first-letter range stripped_seq < 'AC' did not finish inside 120 s on
--    the shared service account, so budget HOURS and build it in first-letter chunks (each its own
--    bounded transaction; resumable; run with statement_timeout=0 and a big work_mem as the owner):
--      SET statement_timeout = 0; SET work_mem = '2GB'; SET max_parallel_workers_per_gather = 4;
--    psql: run the next line, then  \gexec  to execute one INSERT per first letter:
SELECT format($f$INSERT INTO delimp_peptide_index
         SELECT stripped_seq, replace(stripped_seq,'I','L'), COUNT(*), COUNT(DISTINCT modified_seq_proforma),
                COUNT(DISTINCT charge), COUNT(DISTINCT raw_path), COUNT(DISTINCT search_id),
                MIN(q_value), bool_or(im IS NOT NULL)
         FROM delimp_precursors WHERE stripped_seq >= %L AND stripped_seq < %L
         GROUP BY stripped_seq ON CONFLICT DO NOTHING$f$, c, c || 'ÿ')
FROM unnest(string_to_array('A C D E F G H I K L M N P Q R S T V W Y', ' ')) AS c;
--  \gexec

-- 3. trigram index. Local measurement on 2.9M real corpus peptides (PG16): build 9.6 s, GIN 131 MB
--    (+ 99 MB pk + 211 MB table); scale to ~4.04M peptides => ~15 s, ~180 MB GIN.
SET maintenance_work_mem = '1GB';
CREATE INDEX IF NOT EXISTS idx_peptide_index_trgm ON delimp_peptide_index USING gin (seq_il gin_trgm_ops);
ANALYZE delimp_peptide_index;

-- 4. the web service account is read-only
GRANT SELECT ON delimp_peptide_index TO "genome-proteomics-service-account";

-- Keep it current: re-run step 2 for peptides not yet present after each ingest (ON CONFLICT DO NOTHING
-- adds new peptides; the counts of existing ones drift until a periodic TRUNCATE + rebuild). Wire it
-- next to ingest/build_protein_peptide_counts.py. Until refreshed, a peptide first seen after the last
-- build is absent from substring results (exact-sequence lookup still finds it).
