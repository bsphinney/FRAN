-- Federation default-deny checks. In a file, not inlined with -c: nested quoting through
-- bash -> apptainer -> psql mangled the previous version so badly it printed a count of ALL rows
-- and called it "default-deny confirmed". A check whose own output contradicts its verdict is
-- worse than no check.
\pset tuples_only on
\pset format unaligned

SELECT '   federation_visibility default = ' || coalesce(column_default,'(none)')
  FROM information_schema.columns
 WHERE table_name='delimp_searches' AND column_name='federation_visibility';

SELECT '   total searches            = ' || count(*) FROM delimp_searches;
SELECT '   hidden                    = ' || count(*) FROM delimp_searches WHERE federation_visibility = 'hidden';
SELECT '   shareable (NOT hidden)    = ' || count(*) FROM delimp_searches WHERE federation_visibility <> 'hidden';

-- The assertion itself: a pre-existing row must have been backfilled to 'hidden'.
SELECT CASE WHEN count(*) = 0
            THEN '   PASS default-deny: no search is shareable on a fresh install'
            ELSE '   FAIL default-deny: ' || count(*) || ' search(es) shareable without an admin acting'
       END
  FROM delimp_searches WHERE federation_visibility <> 'hidden';

-- And that the CHECK constraint actually restricts the domain.
SELECT CASE WHEN count(*) = 1 THEN '   PASS visibility CHECK constraint present'
            ELSE '   FAIL visibility CHECK constraint missing' END
  FROM pg_constraint
 WHERE conrelid='delimp_searches'::regclass
   AND conname='delimp_searches_federation_visibility_check';

-- Anti-exfiltration objects must exist, or the guard silently has nothing to meter against.
SELECT CASE WHEN count(*) = 2 THEN '   PASS anti-exfiltration tables present (access_log, quota)'
            ELSE '   FAIL anti-exfiltration tables missing (' || count(*) || ' of 2)' END
  FROM information_schema.tables
 WHERE table_name IN ('delimp_federation_access_log','delimp_federation_quota');

SELECT CASE WHEN count(*) >= 3 THEN '   PASS access-log indexes present (budget + novelty scans)'
            ELSE '   FAIL access-log indexes missing (' || count(*) || ' of 3)' END
  FROM pg_indexes WHERE tablename = 'delimp_federation_access_log';

-- A fresh node must start with an empty ledger: no spend, nothing served.
SELECT '   access log rows on a fresh install = ' || count(*) FROM delimp_federation_access_log;
