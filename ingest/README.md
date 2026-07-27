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
| `plan_spectrum_backfill.py` | coverage: which searches' reports are on Hive (backfill now) vs missing (Windows). Writes worklists; `--enqueue` fills `delimp_spectrum_regen_queue`. |
| `pull_reports_to_hive.py` | **run on a Windows ingestor** — copies every `C:\fran_sne_export\*_Report_FRAN*.parquet` onto Flinders so no report is trapped on Windows. Idempotent. |
| `backfill_spectra.sbatch` | submit the corpus Lance backfill on a **compute node** (parallel Arrow/Lance OOM-kills the login node). |
| `provenance.py` | writes **`delimp_search_provenance`** — the ingest coordination table (source `.sne`, exported report path, every raw file, LIMS linkage). |
| `write_submission_service_dir.py` | writes `delimp_submission_service_dir` (submission → service folder ledger). |
| `backfill_protein_counts.py` | one-time corpus fix: splits `n_proteins_total` into **proteins vs protein groups** (see below). `--dry-run`, `--revert`. |
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

> **STATUS (2026-07-20): this backfill is essentially DONE — see [`INGEST_STATUS.md`](INGEST_STATUS.md).**
> The reports were pulled off Windows and ingested: **1,539 Lance datasets / 353.9M precursors /
> 2.1B fragments (~92% of the corpus)** now in `delimp_spectrum_lane`. The steps below are the
> original how-to and are kept for the long tail (~351 searches) + re-runs. The
> `delimp_spectrum_regen_queue` counts are **stale — do not trust them**; use `delimp_spectrum_lane`.

- **First: get the reports onto Hive.** Coverage (`plan_spectrum_backfill.py`) originally found only
  ~19 of 1,890 Spectronaut reports on Flinders; **~1,871 were on `C:\fran_sne_export\`** on the
  Windows export box (the report exists — it was just never copied). A Windows ingestor pulls them:
  ```bash
  # on a Windows ingestor (has C:\fran_sne_export + the Flinders share):
  python pull_reports_to_hive.py --src "C:\fran_sne_export" --dest "\\flinders\...\FRAN_reports"
  ```
  This is the cheap path (copy, no re-export). Only genuinely-missing/corrupt reports need a
  re-export via `manageSNE -rs FRAN.rs` (tracked in `delimp_spectrum_regen_queue`).
- **Then backfill on a compute node** (parallel Arrow/Lance OOM-kills the login node — use sbatch):
  ```bash
  sbatch backfill_spectra.sbatch /nfs/lssc0/flinders/proteomics/Data/FRAN_reports \
      /quobyte/proteomics-grp/brett/glendon/spectra_lance
  ```
  Parses reports in parallel; DB writes stay paced (one registry upsert per search) so the shared
  PG-Farm DB is never overloaded. Corrupt/0-byte reports are logged + skipped. Verified on one
  report: 6,594 precursors / 39,391 fragments, MS1 envelope + DIA window intact, checksummed.
- **Verify integrity** (durability check): `python verify_spectrum_lane.py`.
- **Read for training:** `lance.dataset(path).to_table()` (or point depthcharge at it) — each row
  is a precursor with its full observed spectrum.
- **Going forward:** `corpus_ingest.py --lance-dir <dir>` writes the same Lance lane for each new
  Spectronaut search (re-using `backfill_fragments.process_one`), so live + backfilled data match.

This is the acquired-data source DIA-CLIP trains on — the search engine's own recorded values,
keyed to the RT/IM already in FRAN. It replaces the sequence-guessed `top6(seq)` fragments.

## Proteins vs protein groups (fixed 2026-07-27)

Spectronaut reports **both**, and they are not the same number. A protein group's label is the
`;`-joined accessions of its members (`E2RE03;J9P669`), so a run with 635 protein groups can hold
1,350 proteins. FRAN stored only the **group** count — in a column named `n_proteins_total`, which
the UI rendered as **"Proteins"** — so every search under-reported proteins against the customer's
own Spectronaut overview (2.13x low on Ver_15; ~1.2x across the corpus).

Nothing had to be re-exported or re-parsed: the accessions were never lost, they were already inside
`delimp_proteins.protein_group`. Expanding that label on `;` reproduces the report's
`PG.ProteinAccessions` set **exactly** (verified on Ver_15 + 6 archived FRAN reports). So the fix is a
pure SQL derivation:

- `delimp_searches.n_protein_groups_total` (**new**) — the group count (what `n_proteins_total` held).
- `delimp_searches.n_proteins_total` — now the **true protein count**.

`corpus_ingest.py` writes both for every new search; `backfill_protein_counts.py` did the corpus.
It is reversible — the old value is preserved verbatim in `n_protein_groups_total`, so
`--revert` restores the previous semantics. No matview or view reads `n_proteins_total`, so the
change is live as soon as it's written (no refresh needed).

> **Caveat — how "identified" is defined.** FRAN filters precursors on `EG.Qvalue <= 0.01`, but
> Spectronaut's own summaries count what its `EG.Identified` flag marks true, which is stricter.
> On Ver_15 that is 7,517 precursors / 635 groups vs FRAN's 7,525 / 637 — the 8 extra precursors have
> **negative Cscores** (below the decoy mean), i.e. marginal hits the bare q-cutoff lets through.
> `EG.Identified` is **not** in the archived FRAN reports (128-131 cols), so aligning the corpus to it
> would need a re-export — unlike the protein-count fix. Small (+0.11% here) but systematic.

## Auth / running

Needs the PG-Farm service-account token: `$DELIMP_PG_PASSWORD`, or a file at
`$DELIMP_PG_TOKEN_FILE` / `~/.pgfarm_token`. Ingest is idempotent (delete-then-insert by
`output_dir`). **Validate with `--dry-run` before writing**, and ingest one search before bulk.

> These writers came from the DE-LIMP repo (`~/Documents/claude/scripts`); this is now their
> canonical home. If you change ingest behavior, change it here.
