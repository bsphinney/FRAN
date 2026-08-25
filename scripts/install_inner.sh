#!/bin/bash
# Runs INSIDE the postgres:16 container. Everything happens in this one process lifetime, because
# apptainer tears down the container's squashfuse mount as soon as the exec that started a daemon
# returns — which silently kills a pg_ctl-started server.
set -euo pipefail
P="psql -h /run/postgresql -U fran"

echo "== initdb =="
initdb -D /data -U fran -A trust >/tmp/initdb.log 2>&1 || { echo "initdb FAILED:"; tail -20 /tmp/initdb.log; exit 1; }
echo "   ok"

echo "== start postgres =="
pg_ctl -D /data -o "-k /run/postgresql -h ''" -l /data/pg.log -w start \
  || { echo "pg_ctl FAILED, server log:"; cat /data/pg.log; exit 1; }

$P -d postgres -q -c "CREATE DATABASE fran;"

echo "== apply schema/fran_schema.sql to the EMPTY database (ON_ERROR_STOP=1) =="
$P -d fran -v ON_ERROR_STOP=1 -q -f /sql/fran_schema.sql
echo "   applied with ZERO errors"

echo "== what exists now =="
$P -d fran -At -c "select '   tables='||count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname='public' and c.relkind='r'"
$P -d fran -At -c "select '   matviews='||count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname='public' and c.relkind='m'"
$P -d fran -At -c "select '   indexes='||count(*) from pg_indexes where schemaname='public'"
$P -d fran -At -c "select '   foreign_keys='||count(*) from pg_constraint where contype='f'"

echo "== simulate a first ingest (day one for a new institution) =="
# Supplies every NOT-NULL-without-default column across all six core tables, exactly as
# ingest/corpus_ingest.py does. Writing an abbreviated insert here tests the test, not the schema.
$P -d fran -v ON_ERROR_STOP=1 -q -c "
INSERT INTO delimp_searches (id, search_name, output_dir, submitted_at, search_engine,
    search_engine_version, pipeline_id, n_raw_files, n_precursors_total, n_proteins_total,
    n_protein_groups_total, status, ingested_schema_version)
  VALUES (gen_random_uuid(), 'install-smoke', '/tmp/install-smoke', NOW(), 'diann',
    '2.0', 'diann-uploader', 1, 1, 1, 1, 'completed', '1.0.0');
INSERT INTO raw_files (raw_path, raw_basename, raw_name_anonymized, platform, ingested_schema_version)
  VALUES ('/tmp/install-smoke/a.d', 'a', 'run-000000', 'timstof', '1.0.0');
INSERT INTO delimp_sample_metadata (raw_path, sample_type, organism_name, ingested_schema_version)
  VALUES ('/tmp/install-smoke/a.d', 'study_sample', 'Homo sapiens', '1.0.0');
INSERT INTO search_raw_files (search_id, raw_path, n_precursors)
  SELECT id, '/tmp/install-smoke/a.d', 1 FROM delimp_searches WHERE search_name='install-smoke';
INSERT INTO delimp_proteins (search_id, raw_path, protein_group, gene, n_unique_peptides,
    n_precursors, ingested_schema_version)
  SELECT id, '/tmp/install-smoke/a.d', 'P00000', 'GENE1', 1, 1, '1.0.0'
  FROM delimp_searches WHERE search_name='install-smoke';
INSERT INTO delimp_precursors (search_id, raw_path, stripped_seq, modified_seq_proforma, charge,
    precursor_mz, rt, im, q_value, intensity, protein_group, ingested_schema_version)
  SELECT id, '/tmp/install-smoke/a.d', 'PEPTIDEK', 'PEPTIDEK', 2, 500.25, 12.3, 0.95, 0.001, 1e6,
         'P00000', '1.0.0'
  FROM delimp_searches WHERE search_name='install-smoke';"
for T in delimp_searches raw_files delimp_sample_metadata search_raw_files delimp_proteins delimp_precursors; do
  $P -d fran -At -c "select '   $T rows='||count(*) from $T"
done
echo "   every core table accepted a row -> a real ingest can write to a fresh install"

echo "== apply schema/federation.sql on top (the federation add-on) =="
$P -d fran -v ON_ERROR_STOP=1 -q -f /sql/federation.sql
$P -d fran -v ON_ERROR_STOP=1 -q -f /sql/fed_checks.sql

echo "== refresh every matview (dependency order, by retrying) =="
# A matview that reads another matview cannot refresh until that one is populated, and the
# dependency order is not alphabetical -- delimp_mv_protein_agg reads delimp_mv_species_proteins.
# Retrying until no further progress resolves any order without hard-coding one.
remaining=$($P -d fran -At -c "select matviewname from pg_matviews order by 1")
pass=0
while [ -n "$remaining" ]; do
  pass=$((pass+1)); progressed=""; still=""
  for MV in $remaining; do
    if $P -d fran -q -c "REFRESH MATERIALIZED VIEW $MV" >/tmp/mv.log 2>&1; then
      echo "   OK (pass $pass)  $MV"; progressed=1
    else
      still="$still $MV"
    fi
  done
  remaining="$still"
  if [ -z "$progressed" ]; then
    for MV in $remaining; do echo "   FAILED  $MV -- $(grep -m1 ERROR /tmp/mv.log || tail -1 /tmp/mv.log)"; done
    break
  fi
done
[ -z "$remaining" ] && echo "   all matviews refreshed" || echo "   UNREFRESHABLE:$remaining"

pg_ctl -D /data stop >/dev/null 2>&1 || true
echo "INNER OK"
