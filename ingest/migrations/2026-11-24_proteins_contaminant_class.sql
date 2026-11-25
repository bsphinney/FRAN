-- MANUAL MIGRATION (not for ingest/migrate_diann_columns.py: it also adds a CHECK and a comment).
-- Keep the mixed case: delimp_proteins.contaminant_class = 'contaminant' | 'mixed' | 'suspected' | 'sample'.
--
-- WHY. is_contaminant is now "every accession of the group comes from the contaminant library"
-- (organism.is_contaminant_flag: contaminant OR suspected). A group such as 'A0A0G2JV65;Cont_P63103;P63102' contains a
-- contaminant accession AND a real one; as a boolean that fact is lost. This stores it.
--
-- RUN IT IN AUTOCOMMIT (plain `psql -f`): never with `psql -1` or inside BEGIN. In one transaction the
-- ACCESS EXCLUSIVE lock taken by ADD COLUMN would be held through the VALIDATE scan of the whole table.
--
-- NOT applied by the app or the ingest. The OWNER runs it once, as the table owner (`delimp`).
-- The ingest writes the column only when it exists, so ingest works before and after this.
--
-- SAFE ON 105 M ROWS: ADD COLUMN ... NULL with NO DEFAULT is catalog-only on PostgreSQL 16 (no
-- rewrite). lock_timeout makes it give up rather than queue behind a long query and block every
-- reader of delimp_proteins; re-run if it times out.
-- The CHECK is added NOT VALID: a plain ADD CONSTRAINT ... CHECK scans ALL 105 M rows while holding
-- ACCESS EXCLUSIVE (minutes of website downtime). NOT VALID only takes the catalog lock; new and
-- updated rows are checked from then on. VALIDATE CONSTRAINT is a separate statement, OUTSIDE the
-- lock_timeout block, and holds only SHARE UPDATE EXCLUSIVE (readers and writers continue).
--
-- Fill history afterwards with: python ingest/repairs/recompute_is_contaminant.py --apply
-- (batched by search_id), then refresh delimp_mv_protein_agg / species_proteins / top_proteins.

SET lock_timeout = '5s';

ALTER TABLE delimp_proteins ADD COLUMN IF NOT EXISTS contaminant_class text;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'delimp_proteins_contaminant_class_check') THEN
        ALTER TABLE delimp_proteins ADD CONSTRAINT delimp_proteins_contaminant_class_check
            CHECK (contaminant_class IN ('contaminant', 'mixed', 'suspected', 'sample')) NOT VALID;
    END IF;
END $$;

COMMENT ON COLUMN delimp_proteins.contaminant_class IS
  'Membership of the protein group in the contaminant library (accession prefix Cont_ / CON__ / cRAP). '
  '''contaminant'' = EVERY accession is from the contaminant library; ''mixed'' = some are (the search '
  'database also explains the group); ''suspected'' = none are, but the legacy name rule matches '
  '(KRT / keratin / cont_ / contaminant in protein_group or gene), e.g. KRT1 under a plain UniProt accession; '
  '''sample'' = none are and no name match. NULL = not yet computed (rows ingested before 2026-11-24 '
  'until the repair runs). is_contaminant = (contaminant_class IN (''contaminant'', ''suspected'')), '
  'so mixed groups are counted as SAMPLE proteins by default; filter on this column to treat them differently.';

RESET lock_timeout;

-- Separate statement, no short lock_timeout: it scans the table but does not block readers/writers.
ALTER TABLE delimp_proteins VALIDATE CONSTRAINT delimp_proteins_contaminant_class_check;
