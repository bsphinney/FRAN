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
| `corpus_ingest.py` | the ingester — writes `delimp_searches / raw_files / search_raw_files / delimp_sample_metadata / delimp_proteins / delimp_precursors`, **plus the observed-spectrum Lance lane** (see below). `--engine`, `--bulk-copy`, `--no-fragments`, `--lance-dir`. |
| `backfill_fragments.py` | corpus-wide recovery: archived FRAN report → per-search **Lance** dataset (fragments + MS1 envelope + extras) + registry. `--scan`, `--workers`, `--register`. |
| `spectrum_lance.py` | the Lance schema (48 cols, fragments as list columns) + `delimp_spectrum_lane` registry helpers. |
| `verify_spectrum_lane.py` | walk the registry, confirm each Lance dataset exists + content-md5 matches (durability / loss check). |
| `provenance.py` | writes **`delimp_search_provenance`** — the ingest coordination table (source `.sne`, exported report path, every raw file, LIMS linkage). |
| `write_submission_service_dir.py` | writes `delimp_submission_service_dir` (submission → service folder ledger). |
| `organism.py` | canonical organism/species normalization (single source of truth). |
| `refresh_leaderboards.py` | PG-Farm auth (`_token`) + leaderboard refresh; imported by the others. |
| `sne_xic_ingest.py`, `xic_ingest.py` | ingest the GUI-exported **All-XIC SQLite** dbs → `delimp_precursor_xic` for the peptide-page chromatogram viewer (minority of runs). |
| `db_to_spectronaut_report.py` | reverse: reconstruct a Spectronaut-style report from the DB for a `search_id`. |

## Observed-spectrum lane (the DIA-CLIP fix, 2026-07-17) — **Lance + DB registry**

Spectronaut's FRAN report is **fragment-level** with 131 columns; the old ingest kept ~18
precursor fields and DROPPED the rest — so FRAN held no real spectra, no MS1 isotope pattern, no
DIA window, no predicted-vs-observed RT/intensity. All of that is recovered now, into a **Lance
dataset per search** — one row per precursor, the observed MS2 spectrum + MS1 envelope as Lance
**list columns** (a precursor's whole spectrum in one row). `spectrum_lance.py` holds the 48-col
schema. What we store (audited against all 131 report columns):

- **fragments** (list cols): `frg_mz, frg_type, frg_num, frg_ion, frg_charge, frg_loss,
  frg_peak_area, frg_norm_area, frg_measured_relint, frg_predicted_relint, frg_mass_acc_ppm`.
- **MS1 isotope envelope** (list cols): `ms1_iso_measured`, `ms1_iso_rel_measured`,
  `ms1_iso_rel_predicted`.
- **precursor extras** (scalars): `prec_window` (DIA isolation window), `rt`/`rt_predicted`/
  `irt_empirical`/`irt_predicted`, `signal_to_noise`, `int_corr_score`, `ms1_quantity`/
  `ms2_quantity`, `interference_ms1/ms2`, `is_decoy`, `missed_cleavages`, `is_proteotypic`,
  `ptm_localization`, `xicdbid`, `fragment_count`, protein/genes/organism, q-values, precursor m/z.

### Why Lance + a registry (not the PG corpus, not loose files)
This is how DL people store training data: train from a **columnar file format**, not a relational
DB. Lance is Arrow-based, versioned, and built for fast random-access sample fetching — the format
**depthcharge/Casanovo** upgraded to. The durability worry ("loose files get lost") is solved by
the **DB registry** `delimp_spectrum_lane`: every dataset is recorded with `lance_path`, row
counts, and a **content md5**, so a lost/corrupt dataset is *detectable* (`verify_spectrum_lane.py`)
and re-derivable from the archived report on Flinders. The data has two independent homes; PG stays
the manifest + labels; nothing bulk touches the 402M-row `delimp_precursors`.

- **Backfill the existing corpus** (no re-search, no prediction — reports are on Flinders):
  ```bash
  python backfill_fragments.py --scan /nfs/lssc0/flinders/proteomics/Data/FRAN_reports \
      --out-dir /quobyte/proteomics-grp/brett/glendon/spectra_lance --register --workers 8
  ```
  Parses reports in parallel (`--workers`, files/CPU-bound); DB writes stay paced (one registry
  upsert per search) so the shared PG-Farm DB is never overloaded. Verified on one archived report:
  6,594 precursors / 39,391 fragments, MS1 envelope + DIA window intact, checksummed.
- **Verify integrity** (durability check): `python verify_spectrum_lane.py`.
- **Read for training:** `lance.dataset(path).to_table()` (or point depthcharge at it) — each row
  is a precursor with its full observed spectrum.
- **Going forward:** `corpus_ingest.py --lance-dir <dir>` writes the same Lance lane for each new
  Spectronaut search (re-using `backfill_fragments.process_one`), so live + backfilled data match.

This is the acquired-data source DIA-CLIP trains on — the search engine's own recorded values,
keyed to the RT/IM already in FRAN. It replaces the sequence-guessed `top6(seq)` fragments.

## Auth / running

Needs the PG-Farm service-account token: `$DELIMP_PG_PASSWORD`, or a file at
`$DELIMP_PG_TOKEN_FILE` / `~/.pgfarm_token`. Ingest is idempotent (delete-then-insert by
`output_dir`). **Validate with `--dry-run` before writing**, and ingest one search before bulk.

> These writers came from the DE-LIMP repo (`~/Documents/claude/scripts`); this is now their
> canonical home. If you change ingest behavior, change it here.
