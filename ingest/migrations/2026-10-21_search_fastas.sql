-- delimp_search_fastas -- EVERY protein database a search used, one row each, contaminant lists
-- included.
--
-- WHY A TABLE AND NOT MORE COLUMNS. delimp_searches has room for one database (fasta_path,
-- fasta_md5, fasta_n_proteins) plus one contaminant_lib, and engine_fasta.detect() fills it with
-- whichever non-contaminant file is listed FIRST. Searches routinely use more: of a 36-search
-- sample spanning Spectronaut 13.8 -> 18.x, most used two or three (a contaminant list, the
-- proteome, sometimes an iRT or virus file). On a bat plasma search the order was contaminants,
-- a 10-entry Marburg virus file, then the 60,832-entry bat proteome -- so "first listed" recorded
-- the virus file as the search database, with an entry count off by four orders of magnitude.
-- Entries-per-gene, the number that decides whether a protein-count gap between two searches is
-- depth or database redundancy, is meaningless for the wrong database. Brett, 2026-10-21: record
-- all of them, contaminants too.
--
-- WHERE THE ROWS COME FROM (`source`):
--   sne        the databases embedded in the Spectronaut .sne -- every entry, so entry and gene
--              counts are exact even when the FASTA file is gone (engine_fasta.sne_databases).
--   diann_log  each --fasta on DIA-NN's logged command line; counts only when the file is readable.
--   sn_setup   a Spectronaut ExperimentSetupOverview / setup.txt: names only.
--
-- file_path / file_md5 are filled only for a file that was READ and, for a .sne row, whose content
-- hash equals the embedded database's -- a FASTA with the right name beside the .sne is not proof
-- it is the same file (proteomes get re-downloaded under the same name).
--
-- COST. A few rows per search, ~2,800 searches: about 2 MB. No foreign key, like
-- delimp_search_sources: corpus_ingest deletes and re-inserts the delimp_searches row on every
-- re-ingest (same uuid5 id), and a cascading FK would take these rows with it.

CREATE TABLE IF NOT EXISTS delimp_search_fastas (
    search_id      uuid     NOT NULL,   -- delimp_searches.id
    ordinal        smallint NOT NULL,   -- position in the engine's own list, from 0
    file_name      text,                -- basename as the engine recorded it; NULL if unreadable
    is_contaminant boolean  NOT NULL,   -- by name (contaminant / cRAP) or by >=50% Cont_/CON__ entries
    n_entries      integer,             -- FASTA entries ('>' headers)
    n_genes        integer,             -- distinct GN=; NULL when fewer than half the entries carry one
    content_sha256 text,                -- sha256 over the sorted per-entry sha256("header\nSEQUENCE"):
                                        -- equal for the same entries whatever the wrapping or order
    file_path      text,                -- a file on disk verified to hold this database
    file_md5       text,                -- md5 of that file's bytes
    source         text     NOT NULL,   -- 'sne' | 'diann_log' | 'sn_setup'
    source_path    text,                -- the .sne / log / setup file that was read
    recorded_at    timestamptz DEFAULT now(),
    PRIMARY KEY (search_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_search_fastas_content ON delimp_search_fastas (content_sha256);
