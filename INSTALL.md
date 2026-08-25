# Installing FRAN at your institution

FRAN is a browser over a **PostgreSQL corpus of DIA proteomics identifications**. This guide
stands up your own instance: your database, your data, your deployment. Nothing here contacts
UC Davis.

Every step below is exercised by the container install test described in
[Verifying the install](#7-verifying-the-install) — the schema is applied to an empty
PostgreSQL 16, a first ingest is written, and every materialized view is refreshed. If a step
here is wrong, that test fails.

---

## 0. What you need

| | |
|---|---|
| **PostgreSQL** | 14 or newer (16 is what we run and test against). `gen_random_uuid()` and `gen_random_bytes()` are used, both built in since PG 13. |
| **Python** | 3.11+ |
| **A DIA search result** | DIA-NN `report.parquet`/`report.tsv`, or a Spectronaut report. You need at least one to have anything to browse. |
| **Disk** | The corpus is precursor-level and grows fast: budget roughly **475 bytes per precursor including indexes**. A 3.5M-precursor search is ~1.7 GB. Our 416M-precursor corpus is 203 GB, ~90% of it one table. |

## 1. Get the code

```bash
git clone https://github.com/bsphinney/FRAN.git
cd FRAN
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Create an empty database

```bash
createdb fran                       # or: psql -c "CREATE DATABASE fran;"
export FRAN_DB_URL="postgresql://USER:PASS@HOST:5432/fran"
```

## 3. Apply the schema

```bash
psql "$FRAN_DB_URL" -v ON_ERROR_STOP=1 -f schema/fran_schema.sql
```

That creates **36 tables, 7 materialized views, 2 views, 96 indexes and 18 foreign keys**.

Two things it does that matter, and that are easy to get wrong by hand:

* It seeds `delimp_schema_version`. `delimp_precursors.ingested_schema_version` is a foreign key
  to that table, so **without the seed row your very first ingest is rejected**.
* It creates the views in dependency order (one materialized view reads another).

`schema/fran_schema.sql` is **generated**, not hand-maintained — see
[Regenerating the schema](#8-regenerating-the-schema). Do not edit it directly.

## 4. Configure

```bash
cp .env.example .env      # then edit
set -a; source .env; set +a
```

The connection variables are `DELIMP_PG_HOST`, `DELIMP_PG_PORT`, `DELIMP_PG_DB`,
`DELIMP_PG_USER` and either `DELIMP_PG_PASSWORD` or `DELIMP_PG_TOKEN_FILE`. The defaults in
`.env.example` point at UC Davis PG Farm — **change all of them** to your own database.

> The app never auto-loads `.env`; export the variables yourself, as shown.

## 5. Ingest your first search

```bash
# DIA-NN
python ingest/corpus_ingest.py /path/to/searchdir --engine diann --dry-run
python ingest/corpus_ingest.py /path/to/searchdir --engine diann --bulk-copy

# Spectronaut (a report file, not a directory)
python ingest/corpus_ingest.py "MySearch_Report.tsv" --engine spectronaut \
    --name MySearch --output-dir /stable/path/to/the/search --bulk-copy
```

Always `--dry-run` first: it parses the report and prints the precursor / run / protein-group
counts without writing, so you can check them against your search engine's own summary before
committing anything.

Notes that will save you time:

* **`--output-dir` is the identity of the search.** The `search_id` is `uuid5(namespace,
  output_dir)`, so re-running with the same value *replaces* that search (delete-then-insert)
  while a different value creates a *second* one. Pass it explicitly whenever the report lives
  somewhere other than the search directory.
* **A duplicate guard is on by default.** If another `output_dir` already holds the same set of
  raw files and the same precursor count, the ingest refuses and tells you. Override with
  `--allow-duplicate` only if it really is a separate result.
* **Spectronaut: export with the `FRAN.rs` schema, not the BGS Factory report.** A BGS report has
  no `PG.Genes`, no `EG.IonMobility` and no `F.*` fragment columns, so you lose genes, ion
  mobility and the observed-spectrum lane. See `ingest/SPECTRONAUT_FRAN_INGEST.md`.
* A re-ingest costs **delete + copy**, not just copy — `delimp_precursors` has a self-referencing
  foreign key whose check fires once per deleted row.

## 6. Refresh the materialized views, then run the app

The dashboard reads precomputed views. **Refresh them in dependency order** — refreshing one that
reads an unpopulated view fails with *"materialized view has not been populated"*. The order is
listed at the bottom of `schema/fran_schema.sql`; or just retry until nothing new succeeds:

```sql
REFRESH MATERIALIZED VIEW delimp_mv_corpus_stats;
REFRESH MATERIALIZED VIEW delimp_mv_im_scatter;
REFRESH MATERIALIZED VIEW delimp_mv_species_proteins;
REFRESH MATERIALIZED VIEW delimp_mv_top_genes;
REFRESH MATERIALIZED VIEW delimp_mv_top_peptides;
REFRESH MATERIALIZED VIEW delimp_mv_top_proteins;
REFRESH MATERIALIZED VIEW delimp_mv_protein_agg;   -- must come after species_proteins
```

```bash
uvicorn app.main:app --host 0.0.0.0 --port 7860
```

Open <http://localhost:7860>. `GET /health` reports DB connectivity; `GET /version` reports which
pipeline code produced the corpus.

Docker instead:

```bash
docker build -t fran . && docker run -p 7860:7860 --env-file .env fran
```

## 7. Verifying the install

```sql
SELECT count(*) FROM delimp_precursors;                    -- your first ingest
SELECT count(*) FROM delimp_schema_version;                -- must be >= 1
SELECT * FROM aaa_fran_read_me_first;                       -- corpus caveats, read them
```

If you want the same end-to-end proof we run, `scripts/test_install.sbatch` and
`scripts/install_inner.sh` apply the schema to a throwaway PostgreSQL 16 in a container, write a
row into all six core tables, and refresh every view. Two environment traps are baked into it
because they cost us real time:

* `PGDATA` must be on **node-local disk**. On a network filesystem `initdb` can die with
  `could not close file ...: Interrupted system call`.
* Under Apptainer, start the server and run all queries **inside one `exec`** — otherwise the
  container's mount is torn down when the starting process returns and the server dies.

## 8. Regenerating the schema

`schema/fran_schema.sql` is produced from a live instance so it cannot drift from reality:

```bash
python scripts/dump_schema.py --out schema/fran_schema.sql
```

It excludes tables specific to our deployment (LIMS mirrors, internal curation, our multi-agent
coordination bus) and includes everything else automatically, so a column added to a core table is
picked up on the next run. Re-run it and commit the result whenever the schema changes.

## 9. Security before you expose it

The public layer is enforced in `app/db.py` and is worth understanding before you put FRAN on the
open internet:

* Sessions are **read-only** (`default_transaction_read_only=on`); only `SELECT`/`WITH` runs.
* Every query declares the tables it reads, validated against a **`PUBLIC_TABLES` allowlist**.
* **Raw filenames encode customer, project and sample identity.** `app/privacy.py` replaces them
  with stable non-identifying labels (`run-a3f2c1.d`) for public callers. Real names appear only
  for an authenticated internal caller. If you add tables holding customer identity, keep them out
  of `PUBLIC_TABLES`.

## 10. Connecting to other FRAN instances

Instances can federate — see [FEDERATION.md](FEDERATION.md). Sharing is **opt-in and off by
default**: a fresh install shares nothing until you set a policy, a node id and a salt, *and* mark
individual searches shareable. Federated access is additionally bounded by shape rules, a per-response
cap, a durable per-peer row budget and enumeration detection, so a peer cannot bulk-copy your corpus
— see *Protection against bulk extraction*.
