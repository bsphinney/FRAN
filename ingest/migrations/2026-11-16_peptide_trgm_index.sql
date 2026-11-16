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

-- 2. one row per distinct peptide. seq_il = I->L normalised (I/L are isobaric; the corpus stores one
--    spelling per peptide). n_precursors orders results by prevalence without touching the big table.
--    Expect minutes, not hours: a GROUP BY over idx_prec_stripped_charge (index-only if the VM is warm).
CREATE TABLE IF NOT EXISTS delimp_peptide_index AS
SELECT stripped_seq,
       replace(stripped_seq, 'I', 'L') AS seq_il,
       COUNT(*)                        AS n_precursors
FROM delimp_precursors
GROUP BY stripped_seq;

-- 3. indexes (local measurement on 2.9M real peptides: GIN 131 MB, 9.6 s build; ~4.04M => ~180 MB, ~15 s)
ALTER TABLE delimp_peptide_index ADD PRIMARY KEY (stripped_seq);
SET maintenance_work_mem = '1GB';
CREATE INDEX IF NOT EXISTS idx_peptide_index_trgm ON delimp_peptide_index USING gin (seq_il gin_trgm_ops);
ANALYZE delimp_peptide_index;

-- 4. the web service account is read-only
GRANT SELECT ON delimp_peptide_index TO "genome-proteomics-service-account";

-- Refresh after each ingest (cheap; add to the post-ingest hook next to delimp_protein_peptide_count).
-- Inserting only NEW peptides is enough because peptides are never removed, but n_precursors drifts,
-- so a periodic full rebuild is cleaner:
--   BEGIN; TRUNCATE delimp_peptide_index;
--   INSERT INTO delimp_peptide_index SELECT stripped_seq, replace(stripped_seq,'I','L'), COUNT(*)
--     FROM delimp_precursors GROUP BY stripped_seq; COMMIT; ANALYZE delimp_peptide_index;
-- (GIN is rebuilt incrementally by the INSERT; or DROP/CREATE the index around a big load.)
