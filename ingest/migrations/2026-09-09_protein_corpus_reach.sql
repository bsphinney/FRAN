-- Two corpus-wide facts per gene, precomputed because both are far too slow to compute live:
-- the reach scan is 637 s over protein_group / 121 s over gene, and the percentile is 97 s.
--
-- KEYED ON upper(gene). Not protein_group: 47% of those strings are seen in exactly one search
-- because they vary between FASTAs, so rarity keyed on them measures FASTA style, not biology.
-- Not raw gene: symbols are capitalised per species (Aldoa=278 searches, ALDOA=1278), so a
-- mouse search would read as uniformly "rare" against a mostly-human corpus.
CREATE TABLE IF NOT EXISTS delimp_protein_corpus_reach (
    gene           TEXT PRIMARY KEY,   -- UPPERCASED symbol
    n_searches     INTEGER,            -- distinct searches that reported this gene
    n_samples      INTEGER,            -- distinct raw files
    mean_pct_rank  REAL,               -- mean percent_rank() of its intensity WITHIN a search
    n_pct_searches INTEGER,            -- searches contributing to mean_pct_rank
    computed_at    TIMESTAMPTZ
);
