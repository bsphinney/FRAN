# FRAN ingest pipeline

Everything that **fills** the FRAN corpus (`delimp_*` on PG Farm) lives here — kept in the FRAN
repo so it stays with the app it feeds, not scattered in the DE-LIMP repo. The FRAN browser app
(`../app`) only *reads* the corpus; these scripts *write* it.

> **Read first:** [`SPECTRONAUT_FRAN_INGEST.md`](SPECTRONAUT_FRAN_INGEST.md) — the full,
> verified writeup of how Spectronaut searches become FRAN rows (the `.sne` → report → corpus
> pipeline, the coordination tables, and the fragment story).

## The two ingest streams

| engine | source | command / script |
|---|---|---|
| **Spectronaut** (~96% of the corpus) | `.sne` experiment → CLI export | `spectronaut manageSNE -sne <f.sne> -n <name> -o <out> -rs FRAN.rs` → `<name>_Report_FRAN (Normal).parquet` → `corpus_ingest.py --engine spectronaut` |
| **DIA-NN** | `out/report.parquet` | `corpus_ingest.py --engine diann <searchdir>` |

## Scripts

| script | role |
|---|---|
| `sne_export.py` | finds every `.sne`, runs the `manageSNE` export against `FRAN.rs`, zips + archives to Flinders, optionally `--ingest`. `--dry-run` / `--columns`. |
| `spectronaut_to_corpus.py` | Spectronaut→FRAN column adapter (fuzzy-resolves `R./PG./PEP./EG./FG./F.` columns, parquet or TSV, streams chunks, parses fragments). |
| `corpus_ingest.py` | the ingester — writes `delimp_searches / raw_files / search_raw_files / delimp_sample_metadata / delimp_proteins / delimp_precursors`, **plus the observed-fragment Parquet lane** (see below). `--engine`, `--bulk-copy`, `--no-fragments`, `--fragments-dir`. |
| `provenance.py` | writes **`delimp_search_provenance`** — the ingest coordination table (source `.sne`, exported report path, every raw file, LIMS linkage). |
| `write_submission_service_dir.py` | writes `delimp_submission_service_dir` (submission → service folder ledger). |
| `organism.py` | canonical organism/species normalization (single source of truth). |
| `refresh_leaderboards.py` | PG-Farm auth (`_token`) + leaderboard refresh; imported by the others. |
| `sne_xic_ingest.py`, `xic_ingest.py` | ingest the GUI-exported **All-XIC SQLite** dbs → `delimp_precursor_xic` for the peptide-page chromatogram viewer (minority of runs). |
| `db_to_spectronaut_report.py` | reverse: reconstruct a Spectronaut-style report from the DB for a `search_id`. |

## Observed-fragment lane (the DIA-CLIP fix, 2026-07-17)

Spectronaut's FRAN report is **fragment-level** — it carries the real acquired MS2 fragments
(`F.FrgMz/FrgType/FrgNum/FrgZ/FrgLossType/PeakArea`). `corpus_ingest.py` used to **collapse those
rows to one precursor and drop the fragments**, so FRAN held no real spectra. It now **keeps
them**: per search it writes a Parquet shard (`<name>_fragments.parquet`, one row per observed
fragment) and registers it in `delimp_searches.fragments_parquet_path`. Bulk fragments stay in the
Parquet lane — **never** in the 402M-row `delimp_precursors` (no DB bloat).

- **Backfill the existing corpus** (no re-search, no prediction): the fragments are still in the
  archived reports on Flinders (`/nfs/lssc0/proteomics/Data/FRAN_reports/…`). Recover them with:
  ```bash
  python backfill_fragments.py --scan /nfs/lssc0/flinders/proteomics/Data/FRAN_reports \
      --out-dir /quobyte/proteomics-grp/brett/glendon/fragments --register
  ```
  (Verified: one archived report → 39,391 real y/b fragments across 6,594 precursors, median 6
  fragments/precursor.)
- **Query them:** the shards are the source of truth; put a thin DuckDB **view** over them —
  ```bash
  python build_fragments_duckdb.py --shards /path/to/fragments --db fragments.duckdb
  ```
  A view (not a monolithic table) keeps writes parallel-safe and dodges DuckDB's float16 quirk;
  DuckDB reads the Parquet glob natively, so you still get one SQL handle over the whole corpus.

This is the fragment source DIA-CLIP trains on — the search engine's own validated fragments,
keyed to the RT/IM already in FRAN. It replaces the sequence-guessed `top6(seq)` fragments.

## Auth / running

Needs the PG-Farm service-account token: `$DELIMP_PG_PASSWORD`, or a file at
`$DELIMP_PG_TOKEN_FILE` / `~/.pgfarm_token`. Ingest is idempotent (delete-then-insert by
`output_dir`). **Validate with `--dry-run` before writing**, and ingest one search before bulk.

> These writers came from the DE-LIMP repo (`~/Documents/claude/scripts`); this is now their
> canonical home. If you change ingest behavior, change it here.
