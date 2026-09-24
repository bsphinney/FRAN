-- Six more DIA-NN report columns, following 2026-09-23_diann_report_columns.sql.
--
-- That migration carried 35 of report.parquet's 72 columns and deliberately left 16, each with a
-- measurement. Six of those 16 are taken here. The reasoning is recorded per column below; the
-- ten still left out are re-justified at the bottom so this file supersedes nothing silently.
--
-- SAFE ON A 261 GB TABLE, same as before: every statement is ADD COLUMN ... NULL with NO DEFAULT,
-- which on PostgreSQL 16 is a catalog-only change. Existing rows are not rewritten. Do NOT add a
-- DEFAULT -- that would rewrite all 532M rows.
--
-- TYPES measured from real reports on 2026-09-24, not assumed: all four Lib.* columns are pyarrow
-- `float` (float32), so `real` stores them bit-exactly. Protein.Ids and Protein.Sites are `string`.
--
-- NAMING follows the established rule exactly: the DIA-NN name lowercased, dots to underscores.

-- ---------------------------------------------------------------------------------------------
-- The Lib.* family -- properties of the spectral-library ENTRY, constant per Precursor.Id.
--
-- The previous migration called excluding these "the one exclusion that is a judgement call
-- rather than a measurement", and named the condition for reversing it: "if the decoy/ML lane
-- wants them, each is one line here and one line in the mapping dict." That lane does want them --
-- a library-side q-value is exactly the kind of feature a rescoring model needs, and it cannot be
-- recovered later without re-reading every report.
--
-- Cost, measured: 4 columns x 4 bytes x ~450M future rows ~ 7.2 GB.
-- Cardinality confirms they carry information rather than repeating one value -- on three sampled
-- reports Lib.Q.Value had 9,770 / 53,417 / 11,830 distinct values.
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS lib_q_value              real;  -- Lib.Q.Value
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS lib_peptidoform_q_value  real;  -- Lib.Peptidoform.Q.Value
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS lib_pg_q_value           real;  -- Lib.PG.Q.Value
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS lib_ptm_site_confidence  real;  -- Lib.PTM.Site.Confidence

-- ---------------------------------------------------------------------------------------------
-- Protein.Ids -- the full accession list for this precursor.
--
-- Previously excluded as "cost out of proportion to content", on the grounds that `proteotypic`
-- answers "is this peptide unique to one protein?" in one byte. That is true but narrower than
-- what this column holds: it is constant within Protein.Group in only 86.8% of groups, so it is a
-- genuine per-precursor value and is NOT derivable from anything FRAN stores.
--
-- Re-measured 2026-09-24 on three reports: ~7-12 bytes/row, i.e. ~5 GB at corpus scale -- lower
-- than the 7-11 GB originally estimated, though the same order. Accepted deliberately.
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS protein_ids   text;  -- Protein.Ids

-- Protein.Sites -- per-protein modified-site list, e.g. '[A0A669CRE8:C2502,C2508]'.
-- 71.8-80.9% empty and ~4-5 bytes/row (~2 GB), so it is cheap, and it is the only column that
-- records WHERE on the protein a modification sits. PTM work needs that; nothing else has it.
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS protein_sites text;  -- Protein.Sites

-- ---------------------------------------------------------------------------------------------
-- STILL NOT CARRIED -- 10 columns. Six were measured constant by the previous migration; four are
-- re-examined here and dropped on this migration's own measurements.
--
-- Structurally constant across all 80 reports / 45,058,279 rows (previous migration's measurement):
--   Channel, Channel.Q.Value, Decoy, Translated.Q.Value, PG.TopN, Genes.TopN
-- They are constant BECAUSE these searches were not plexDIA and did not use --translate-mods. If
-- such a search is ever ingested, add the column then and fill it forward: the historical NULL is
-- then ACCURATE rather than a gap, so nothing is lost by waiting.
--
-- Dropped on measurement taken 2026-09-24:
--   Run.Index            -- 4-12 distinct values per search. It is the run ordinal, already
--                           derivable from the raw_path FK. 8 bytes x 450M rows = 3.6 GB to store
--                           a number with twelve values.
--   Precursor.Lib.Index  -- a row number in report-lib.parquet, which FRAN does not store, so it
--                           joins to nothing. 3.6 GB. Revisit only if the library is ever stored
--                           (it is only ~131 KB per search, so that is a reasonable future ask).
--   Site.Occupancy.Probabilities
--                        -- ~18-20 bytes/row (~8.5 GB) of which 94.0% is a verbatim echo of
--                           Precursor.Id, and whose numeric content is already kept as
--                           PTM.Site.Confidence -> site_localization_probability.
--   Protein.Names        -- constant within Protein.Group in 100.000% of groups: a FASTA lookup,
--                           not a measurement, and the most expensive column in the set.
-- ---------------------------------------------------------------------------------------------
