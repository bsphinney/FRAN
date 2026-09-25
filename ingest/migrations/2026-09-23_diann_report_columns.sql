-- Carry the DIA-NN report columns FRAN was discarding.
--
-- DIA-NN's report.parquet has 72 columns. FRAN read 20 of them. This adds the 35 that are worth
-- keeping, at the grain each one actually varies at, and deliberately leaves 16 behind. The
-- evidence for every decision below was measured on 2026-09-23 over ALL 80 DIA-NN reports in
-- /nfs/lssc0/flinders/proteomics/Data/FRAN_diann/pilot_* -- 45,058,279 rows -- not inferred from
-- the DIA-NN documentation. Every number quoted below is reproducible from those reports; the
-- measurement scripts are on Hive under ~/diann_fix/ (colscan.py, grain.py, redundancy.py).
--
-- WHY THIS IS SAFE ON A 261 GB TABLE. Every statement is ADD COLUMN ... NULL with NO DEFAULT.
-- On PostgreSQL 16 that is a catalog-only change: existing rows are never rewritten and no
-- AccessExclusiveLock is held for longer than the catalog update, so the 532M rows already in
-- delimp_precursors cost nothing. Adding a DEFAULT would rewrite the entire table. Do not add one.
--
-- TYPES. Every float in a DIA-NN report is float32 (verified: pyarrow reports `float`, not
-- `double`, for all 49 floating columns in all 80 reports). PostgreSQL `real` is also float32, so
-- `real` stores these bit-exactly and `double precision` would buy nothing but double the bytes --
-- including for the intensities, whose largest observed value is 7.08e11, far inside float4 range.
-- The existing `intensity`/`normalized_intensity` columns are `double precision` only because
-- Spectronaut writes float64; that is not a precedent for DIA-NN's columns.
--
-- NAMING. Each new column is its DIA-NN name lowercased with dots turned into underscores, with
-- no other change. That makes the mapping mechanical and self-evident in both directions.
-- The cost is that DIA-NN's British spelling now sits next to FRAN's American one:
-- `ms1_normalised` and `normalisation_factor` live in the same table as `normalized_intensity`.
-- That is deliberate -- an exception-free rule is easier to trust than a prettier table -- but it
-- is a trap for anyone writing SQL from memory, so it is called out here rather than discovered.

-- ---------------------------------------------------------------------------------------------
-- delimp_precursors -- the 25 columns that are genuinely a per-precursor-per-run measurement.
-- ---------------------------------------------------------------------------------------------

-- Peak shape. With the existing peak_fwhm these three describe the elution profile: where the
-- integration started and stopped, and how wide the peak was. The XIC lanes want exactly this.
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS rt_start  real;  -- RT.Start
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS rt_stop   real;  -- RT.Stop

-- Predicted vs observed. The whole point of keeping both is the residual: predicted_rt - rt is a
-- direct quality signal and the training target for any RT model. Same for ion mobility.
-- Predicted.RT differs from RT on 99.97% of rows, so these are not a re-statement of rt/irt.
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS predicted_rt   real;  -- Predicted.RT
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS predicted_irt  real;  -- Predicted.iRT
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS predicted_im   real;  -- Predicted.IM
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS predicted_iim  real;  -- Predicted.iIM

-- MS1 lane. FRAN has only ever stored MS2-derived quant (Precursor.Quantity). These are the
-- independent MS1 measurement of the same precursor, which is what makes an MS1/MS2 agreement
-- check possible at all.
-- ms1_normalised is kept even though it is ms1_area * normalisation_factor on 96.8% of rows:
-- 3.2% disagree, so deriving it would be quietly wrong on ~1.4M of the 45M rows measured.
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS ms1_area                real;  -- Ms1.Area
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS ms1_normalised          real;  -- Ms1.Normalised
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS ms1_apex_area           real;  -- Ms1.Apex.Area
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS ms1_apex_mz_delta       real;  -- Ms1.Apex.Mz.Delta
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS ms1_total_signal_before real;  -- Ms1.Total.Signal.Before
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS ms1_total_signal_after  real;  -- Ms1.Total.Signal.After
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS ms1_profile_corr        real;  -- Ms1.Profile.Corr

-- Quant quality / scoring. This is the ML feature set: DIA-NN's own per-identification evidence
-- scores. Nothing in FRAN reproduces them and they cannot be recomputed without the raw file.
-- channel_evidence is NOT plexDIA-dead despite the name -- measured range 0..1 with >8 distinct
-- values and only 5.77% zeros, so it carries a real per-precursor score in single-channel runs.
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS quantity_quality     real;  -- Quantity.Quality
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS evidence             real;  -- Evidence
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS mass_evidence        real;  -- Mass.Evidence
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS channel_evidence     real;  -- Channel.Evidence
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS averagine            real;  -- Averagine
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS normalisation_factor real;  -- Normalisation.Factor
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS normalisation_noise  real;  -- Normalisation.Noise

-- Best fragment. The m/z the quant was actually taken from, and its mass error. The XIC lane
-- currently re-derives a "best fragment" itself; this is the engine's own answer to compare against.
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS best_fr_mz       real;  -- Best.Fr.Mz
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS best_fr_mz_delta real;  -- Best.Fr.Mz.Delta

-- Peptidoform FDR. Distinct from q_value: q_value is confidence that the PRECURSOR is real,
-- peptidoform_q_value is confidence that the MODIFICATION PLACEMENT is right. FRAN's PTM surfaces
-- have had no DIA-NN-side number for that.
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS peptidoform_q_value        real;  -- Peptidoform.Q.Value
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS global_peptidoform_q_value real;  -- Global.Peptidoform.Q.Value

-- Proteotypic: is this peptide unique to one protein? One byte, and it is the analytic content of
-- the Protein.Ids string that this migration otherwise declines to store (see the exclusions).
ALTER TABLE delimp_precursors ADD COLUMN IF NOT EXISTS proteotypic boolean;  -- Proteotypic (0/1)

-- NOT ADDED, because it already exists and was simply never written on a DIA-NN search:
--   site_localization_probability  <- PTM.Site.Confidence
-- The Spectronaut adapter fills it with the minimum per-site localization probability; DIA-NN's
-- PTM.Site.Confidence is the same kind of quantity and the column's docstring meaning ("how
-- confident are we in the site") holds for both. The writer gates it on n_mods > 0, because
-- DIA-NN emits a literal 1.0 for every UNMODIFIED precursor (measured: 100% of unmodified rows),
-- and storing 1.0 on ~78% of rows that have nothing to localize would make the column useless for
-- the `site_localization_probability > 0.75` filters that already read it.

-- ---------------------------------------------------------------------------------------------
-- delimp_proteins -- the 10 columns that are NOT per-precursor facts.
--
-- Measured on 6 reports: PG.MaxLFQ, PG.MaxLFQ.Quality, PG.PEP, Global.PG.Q.Value, Lib.PG.Q.Value
-- and Protein.Q.Value are constant within (Run, Protein.Group) in 100.000% of groups, and
-- GG.Q.Value / the four Genes.MaxLFQ columns are constant within (Run, Genes) in 100.000% --
-- with Genes itself functionally determined by Protein.Group in 100.000% of protein groups.
--
-- delimp_proteins is keyed on exactly (search_id, raw_path, protein_group) and carries `gene`,
-- so it is the correct grain for all ten. This is not tidiness: delimp_proteins holds 45M rows
-- against delimp_precursors' 532M, so putting MaxLFQ here rather than on every precursor row is
-- the difference between ~180 MB and ~1.8 GB today, and between ~2 GB and ~18 GB after the
-- corpus-wide re-search this work is meant to precede.
-- ---------------------------------------------------------------------------------------------

ALTER TABLE delimp_proteins ADD COLUMN IF NOT EXISTS pg_maxlfq         real;  -- PG.MaxLFQ
ALTER TABLE delimp_proteins ADD COLUMN IF NOT EXISTS pg_maxlfq_quality real;  -- PG.MaxLFQ.Quality
ALTER TABLE delimp_proteins ADD COLUMN IF NOT EXISTS pg_pep            real;  -- PG.PEP
ALTER TABLE delimp_proteins ADD COLUMN IF NOT EXISTS global_pg_q_value real;  -- Global.PG.Q.Value
ALTER TABLE delimp_proteins ADD COLUMN IF NOT EXISTS protein_q_value   real;  -- Protein.Q.Value

ALTER TABLE delimp_proteins ADD COLUMN IF NOT EXISTS genes_maxlfq                real;  -- Genes.MaxLFQ
ALTER TABLE delimp_proteins ADD COLUMN IF NOT EXISTS genes_maxlfq_unique         real;  -- Genes.MaxLFQ.Unique
ALTER TABLE delimp_proteins ADD COLUMN IF NOT EXISTS genes_maxlfq_quality        real;  -- Genes.MaxLFQ.Quality
ALTER TABLE delimp_proteins ADD COLUMN IF NOT EXISTS genes_maxlfq_unique_quality real;  -- Genes.MaxLFQ.Unique.Quality
ALTER TABLE delimp_proteins ADD COLUMN IF NOT EXISTS gg_q_value                  real;  -- GG.Q.Value

-- ---------------------------------------------------------------------------------------------
-- DELIBERATELY NOT ADDED -- 16 columns, with the measurement that justifies each.
--
-- Structurally constant. ONE distinct value across all 80 reports and all 45,058,279 rows:
--   Channel              = ''    -- plexDIA only
--   Channel.Q.Value      = 0.0   -- plexDIA only
--   Decoy                = 0     -- a filtered report contains no decoys by construction
--   Translated.Q.Value   = 0.0   -- requires --translate-mods / MBR translation, unused here
--   PG.TopN              = 0.0   -- top-N quant not requested
--   Genes.TopN           = 0.0   -- top-N quant not requested
-- These are the honest win: six columns x 4 bytes that would be identical on every row forever.
-- If a plexDIA or --translate-mods search is ever ingested they stop being constant, which is why
-- the audit in docs/ records the exact query to re-check rather than treating this as permanent.
--
-- Redundant with something FRAN already stores:
--   Run.Index            -- the run's ordinal; FRAN resolves Run -> raw_path (a real FK) already
--   Precursor.Lib.Index  -- a row number in a spectral library FRAN does not store, so it cannot
--                           be joined to anything and is not comparable between searches
--   Protein.Names        -- constant within Protein.Group in 100.000% of groups: a lookup from the
--                           FASTA, not a measurement. Derivable; as text it would also be the most
--                           expensive column here.
--
-- Cost out of proportion to content:
--   Protein.Ids          -- genuinely per-precursor (constant within Protein.Group in only 86.8%
--                           of groups, so it is NOT a protein-level lookup), but it is a long
--                           accession list: ~7-11 GB of text after the corpus-wide re-search. The
--                           analytic question it answers -- "is this peptide unique to one
--                           protein?" -- is answered by `proteotypic` above in one byte.
--   Site.Occupancy.Probabilities
--                        -- NOT empty, contrary to expectation: DIA-NN always writes it, echoing
--                           Precursor.Id verbatim when there is nothing to localize (94.0% of rows
--                           are that exact echo). The 2-15% that differ carry per-site braces,
--                           e.g. 'AAEVWM(UniMod:35){1.000000}DEFK2' -- whose numeric content is
--                           PTM.Site.Confidence, which we DO keep, into site_localization_probability.
--   Protein.Sites        -- 83.9% empty; PTM-search-only, e.g. '[A0A669CRE8:C2502,C2508]'.
--
-- Library-entry constants (the Lib.* family). Each is a property of the spectral library ENTRY,
-- identical for a given Precursor.Id across every run and every row of a search (measured:
-- constant per Precursor.Id in 100.000% of cases). They describe the library, which FRAN does not
-- store, rather than the sample that was actually run:
--   Lib.Q.Value, Lib.Peptidoform.Q.Value, Lib.PG.Q.Value, Lib.PTM.Site.Confidence
-- Together they are 4 x 4 bytes x ~450M future rows ~ 7 GB to record four numbers per library
-- entry. This is the one exclusion that is a judgement call rather than a measurement: if the
-- decoy/ML lane wants them, each is one line here and one line in the mapping dict.
-- ---------------------------------------------------------------------------------------------
