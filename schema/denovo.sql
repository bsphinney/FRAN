-- FRAN de novo lane. Apply AFTER schema/fran_schema.sql:
--     psql "$FRAN_DB_URL" -f schema/denovo.sql
-- Additive and idempotent. Written by the DE-LIMP ingestor; FRAN only reads.

-- One row per de novo run. Deliberately NOT delimp_searches: a de novo run has no protein groups,
-- no q-values and no FASTA, and would be mostly NULL there.
CREATE TABLE IF NOT EXISTS delimp_denovo_run (
    run_id            uuid PRIMARY KEY,
    run_name          text NOT NULL,
    cohort            text,
    source_raw        text,
    denovo_engine     text NOT NULL,
    engine_version    text NOT NULL,
    weights           text,
    -- Provenance, because a cohort spanning two GPU architectures carries a ~1% floor of pure
    -- infrastructure difference: measured 99.186% sequence agreement between cu124/A100 and
    -- cu128/Blackwell on the same mzML with the same casanovo build.
    gpu_arch          text,
    bundle_format     text NOT NULL,
    -- Identity is found by SEARCHING the run name, never by positional parsing: the Parker hair
    -- cohort has at least four filename grammars and four positional attempts gave four different
    -- wrong counts.
    donor_id          text,
    sample_role       text NOT NULL DEFAULT 'unknown'
                      CHECK (sample_role IN ('donor','standard','control','unknown')),
    replicate_of      uuid REFERENCES delimp_denovo_run(run_id),
    -- Three phenomena, three downstream behaviours: prefer the good run / average the technical
    -- pair / treat as independent batch corroboration. One flag would flatten them.
    replicate_kind    text CHECK (replicate_kind IN
                      ('failed_reacquisition','technical','cross_batch')),
    n_psms            integer NOT NULL,
    n_peptides        integer NOT NULL,
    len_median        integer,
    conf_geomean_median real,
    -- Chance-match rate for this run's corpus matching, from decoy peptides run through the
    -- identical search. A candidate count without this overstates the result ~2x.
    match_fdr         real,
    ingest_flag       text,
    ingested_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_denovo_run_donor  ON delimp_denovo_run (donor_id);
CREATE INDEX IF NOT EXISTS idx_denovo_run_cohort ON delimp_denovo_run (cohort);

CREATE TABLE IF NOT EXISTS delimp_denovo_peptide (
    run_id        uuid NOT NULL REFERENCES delimp_denovo_run(run_id) ON DELETE CASCADE,
    stripped_seq  text NOT NULL,
    seq_il        text NOT NULL,
    length        smallint NOT NULL,
    n_psms        integer NOT NULL,
    peptide_score real,
    conf_geomean  real,
    corpus_class  text NOT NULL DEFAULT 'novel'
                  CHECK (corpus_class IN ('conserved','gvp','novel')),
    PRIMARY KEY (run_id, stripped_seq)
);
-- seq_il leads, not run_id: cross-run queries are the point, per-run is the special case.
CREATE INDEX IF NOT EXISTS idx_denovo_pep_seqil ON delimp_denovo_peptide (seq_il);
CREATE INDEX IF NOT EXISTS idx_denovo_pep_class ON delimp_denovo_peptide (corpus_class);

CREATE TABLE IF NOT EXISTS delimp_denovo_psm (
    run_id        uuid NOT NULL REFERENCES delimp_denovo_run(run_id) ON DELETE CASCADE,
    scan          integer NOT NULL,
    stripped_seq  text NOT NULL,
    seq_il        text NOT NULL,
    sequence      text,              -- WITH modifications: N+0.984 vs a plain D is unrecoverable
    charge        smallint,
    exp_mz        real,
    peptide_score real,
    aa_scores     real[],            -- per-residue; phase 2 reads these AT the variant site
    PRIMARY KEY (run_id, scan)
);
CREATE INDEX IF NOT EXISTS idx_denovo_psm_seqil ON delimp_denovo_psm (seq_il);

-- A JOIN TABLE: the I/L collapse is one-to-many in reverse, so one de novo peptide can match
-- several real corpus spellings (8.2% of crane matches did).
CREATE TABLE IF NOT EXISTS delimp_denovo_corpus_match (
    run_id              uuid NOT NULL REFERENCES delimp_denovo_run(run_id) ON DELETE CASCADE,
    stripped_seq        text NOT NULL,
    corpus_stripped_seq text NOT NULL,
    match_kind          text NOT NULL CHECK (match_kind IN ('exact','il')),
    n_candidates        smallint NOT NULL,
    corpus_n_obs        bigint,
    corpus_n_searches   integer,
    corpus_irt_mean     double precision,
    corpus_im_mean      double precision,
    PRIMARY KEY (run_id, stripped_seq, corpus_stripped_seq)
);
CREATE INDEX IF NOT EXISTS idx_denovo_match_corpus
    ON delimp_denovo_corpus_match (corpus_stripped_seq);

-- The corpus-side half of the I/L key. An EXPRESSION index avoids adding a column, a writer and a
-- maintenance job to a table FRAN already owns. On delimp_peptide_consensus (2.93M rows, 1,291 MB)
-- and deliberately NOT on delimp_precursors (485M rows) -- tens of GB for no extra reach.
CREATE INDEX IF NOT EXISTS idx_consensus_seq_il
    ON delimp_peptide_consensus (replace(stripped_seq, 'I', 'L'));

COMMENT ON COLUMN delimp_denovo_peptide.peptide_score IS
    'Casanovo peptide score = PRODUCT of per-residue scores, so it is ~p^L and filters LENGTH, not '
    'quality. Measured over 10.76M PSMs: per-residue confidence PEAKS at length 20 while the '
    'peptide score ranks those below every 7-mer. Never threshold on it; compare conf_geomean.';
COMMENT ON COLUMN delimp_denovo_run.sample_role IS
    'Defaults to unknown. Folder membership does not imply donor status: a bovine BSA standard sits '
    'in the hair cohort with 7,844 PSMs and normal confidence, invisible to any yield check.';
COMMENT ON COLUMN delimp_denovo_run.match_fdr IS
    'Chance-match rate from decoy peptides (interior-reversed, matched on length/composition/termini) '
    'run through the identical pigeonhole search. Measured 38.7% on the crane cohort, corroborated '
    'at 38.0% by an independent reversed-corpus null.';
