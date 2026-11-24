-- Keep the mixed case: delimp_proteins.contaminant_class = 'contaminant' | 'mixed' | 'sample'.
--
-- WHY. is_contaminant is now "every accession of the group comes from the contaminant library"
-- (organism.is_contaminant_group). A group such as 'A0A0G2JV65;Cont_P63103;P63102' contains a
-- contaminant accession AND a real one; as a boolean that fact is lost. This stores it.
--
-- NOT applied by the app or the ingest. The OWNER runs it once, as the table owner (`delimp`).
-- The ingest writes the column only when it exists, so ingest works before and after this.
--
-- SAFE ON 97 M ROWS: ADD COLUMN ... NULL with NO DEFAULT is catalog-only on PostgreSQL 16 (no
-- rewrite). lock_timeout makes it give up rather than queue behind a long query and block every
-- reader of delimp_proteins; re-run if it times out. A CHECK on a nullable column is evaluated
-- for the new (all-NULL) column only, so it does not scan the table either.
--
-- Fill history afterwards with: python ingest/repairs/recompute_is_contaminant.py --apply
-- (batched by search_id), then refresh delimp_mv_protein_agg / species_proteins / top_proteins.

SET lock_timeout = '5s';

ALTER TABLE delimp_proteins ADD COLUMN IF NOT EXISTS contaminant_class text;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'delimp_proteins_contaminant_class_check') THEN
        ALTER TABLE delimp_proteins ADD CONSTRAINT delimp_proteins_contaminant_class_check
            CHECK (contaminant_class IN ('contaminant', 'mixed', 'sample'));
    END IF;
END $$;

COMMENT ON COLUMN delimp_proteins.contaminant_class IS
  'Membership of the protein group in the contaminant library (accession prefix Cont_ / CON__ / cRAP). '
  '''contaminant'' = EVERY accession is from the contaminant library; ''mixed'' = some are (the search '
  'database also explains the group); ''sample'' = none are. NULL = not yet computed (rows ingested '
  'before 2026-11-24 until the repair runs). is_contaminant is exactly (contaminant_class = ''contaminant''), '
  'so mixed groups are counted as SAMPLE proteins by default; filter on this column to treat them differently.';

RESET lock_timeout;
