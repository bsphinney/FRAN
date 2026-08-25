# FRAN ingest pipeline

Everything that **fills** the FRAN corpus (`delimp_*` on PG Farm) lives here — kept in the FRAN
repo so it stays with the app it feeds, not scattered in the DE-LIMP repo. The FRAN browser app
(`../app`) only *reads* the corpus; these scripts *write* it.

> **Read first:** [`SPECTRONAUT_FRAN_INGEST.md`](SPECTRONAUT_FRAN_INGEST.md) — the full,
> verified writeup of how Spectronaut searches become FRAN rows (the `.sne` → report → corpus
> pipeline, the coordination tables, and the fragment story).

## The three ingest streams

| engine | source | command / script |
|---|---|---|
| **Spectronaut** (~96% of the corpus) | `.sne` experiment → CLI export | `spectronaut manageSNE -sne <f.sne> -n <name> -o <out> -rs FRAN.rs` → `<name>_Report_FRAN (Normal).parquet` → `corpus_ingest.py --engine spectronaut` |
| **DIA-NN** (by hand) | `out/report.parquet` | `corpus_ingest.py --engine diann <searchdir>` |
| **DIA-NN / FragPipe / Radiant, automatic** | any search the Core's Claude Code skill runs on Hive | the skill symlinks it into **`/quobyte/proteomics-grp/fran/incoming/`**; the ingest cron picks it up — see [The drop directory](#the-drop-directory-quobyteproteomics-grpfranincoming) |

## The drop directory: `/quobyte/proteomics-grp/fran/incoming/`

**This is where the Core's Claude Code pipeline skill hands over finished Hive searches, and what
the ingest cron should scan.** (DE-LIMP repo, `skill/ucdavis-proteomics-core-pipeline`, v2.3.0+;
entry point `scripts/fran_deposit.py`.) The skill does not ingest anything and needs no corpus
credential — it stages, the cron ingests.

### What an entry looks like

```
/quobyte/proteomics-grp/fran/incoming/
  out_q__a7416fdc/                 <- a REAL directory, name = <search dir>__<8 hex of its real path>
    fran_manifest.json             <- a real file: the facts the cron cannot derive
    report.parquet   -> /quobyte/proteomics-grp/brett/poplar_test/diann/out_q/report.parquet
    report.log.txt   -> ...
    report_xic/                    <- a real dir of symlinks to every *.xic.parquet (see below)
      run1.xic.parquet -> .../out/xic/t0_xic/run1.xic.parquet
      run2.xic.parquet -> .../out/xic/t1_xic/run2.xic.parquet
```

Everything except the manifest is a **symlink**. Nothing is copied — a search directory is tens of
GB. Three things about this shape are load-bearing:

- **The entry is a real directory, not a symlink to the search dir.** `find_uningested.py` walks
  with `os.walk(..., followlinks=False)`, which never descends into a symlinked *directory*. A bare
  symlink would be silently invisible and the search would never be ingested. A real directory is
  walked normally, and `os.path.exists()` follows the links inside it, so `detect_engine()` works
  unchanged.
- **The entry name is deterministic.** Re-staging the same search reuses the same path instead of
  presenting a second candidate that would ingest as a duplicate search.
- **`search_provenance.json` is deliberately NOT linked** — see the bug below.

### Chromatograms are always there for DIA-NN, and always in `report_xic/`

Every DIA-NN search from the skill now extracts XICs (`--xic 10 --mobilograms` is forced into the
cfg), so `diann_xic_to_lance.py --dir <entry>` has input for **every** staged DIA-NN search — its
default `<dir>/report_xic` is exactly where the entry puts them.

That normalisation is doing real work. DIA-NN names the XIC directory after `--out`, and the
skill's 5-step parallel chain (its default above 5 files) runs step 4 **per file**, so a 399-run
cohort leaves chromatograms in **399 directories** named `xic/t<N>_xic/` with *nothing* at
`report_xic/`. The entry flattens them into one directory of symlinks — safe because DIA-NN names
each file after its run. Measured on a real 399-run cohort: 399 files, no basename collisions,
27 GB of chromatograms handed over as 201 KB of links.

`fran_manifest.json`'s `xic` field reports `present`, `n_files`, and the source directories.

### What the cron should do with `fran_manifest.json`

```json
{
  "output_dir": "/quobyte/proteomics-grp/brett/poplar_test/diann/out_q",
  "engine": "diann", "engine_version": "2.6.0",
  "organism": "Populus trichocarpa", "taxon": 3694,
  "search_name": "poplar_qcol_test",
  "xic": {"present": false, "n_files": 0, "dirs": []},
  "suggested_ingest": ["--engine","diann","--output-dir","/quobyte/.../out_q",
                       "--organism-name","Populus trichocarpa","--taxon","3694"],
  "suggested_ingest_shell": "--engine diann --output-dir /quobyte/.../out_q ...",
  "search_provenance": { ...run_search.py's full record: exact command, files, params... }
}
```

Ingest the entry with those arguments — verified working end to end (402,522 precursors parsed
through the symlinks, 2026-08-25):

```python
cmd = [PY, "corpus_ingest.py", entry] + manifest["suggested_ingest"]      # argv LIST
subprocess.run(cmd)
```

Two of those fields matter more than they look:

- **`output_dir`** — pass it as `--output-dir`. Without it the corpus records the *drop path* as
  the search's location: provenance points at the handover instead of the search, and
  `corpus_ingest`'s `.d`-lookup for platform/SPD detection (which resolves raw files relative to
  `output_dir`) misses.
- **`organism` / `taxon`** — **a DIA-NN report has no organism column.** Ingest without these and
  the row is `NULL` and the search never appears on FRAN's species page. The skill has them because
  the user *confirmed* the organism during the run; nothing downstream can recover them. They are
  absent from the manifest rather than guessed when genuinely unknown.

Use `suggested_ingest` (the array) with `subprocess`, or `suggested_ingest_shell` if it has to go
through a shell. **Never `" ".join(suggested_ingest)`** — every species name contains a space, so
that fails on the first real ingest with `unrecognized arguments: sapiens`.

### 🔴 What the cron still needs: read the manifest for identity + organism

As of `2a58b3e` the scanner takes `os.path.realpath(dirpath)` as a dropped search's identity.
That is the right idea, but it does not fire for these entries: **the entry is a real directory,
so `realpath()` returns the entry itself.** Measured on Hive against a real staged search:

```
entry           : /quobyte/proteomics-grp/fran/incoming/out_q__a7416fdc
engine detected : diann                                    <- correct
realpath()      : /quobyte/proteomics-grp/fran/incoming/out_q__a7416fdc
manifest says   : /quobyte/proteomics-grp/brett/poplar_test/diann/out_q
MATCH?          : False
```

Two consequences, both silent — the ingest succeeds either way:

1. `--output-dir` records the **drop box** as the search's location. `search_id` is
   `uuid5(namespace, output_dir)`, so when a later scan reaches the same search at its real path
   under `/quobyte/proteomics-grp/brett` it looks un-ingested and is ingested **a second time**
   under a different id — exactly the duplicate `2a58b3e` set out to prevent.
2. The **organism is lost**. A DIA-NN report has no organism column, so the row lands `NULL`
   and the search never appears on FRAN's species page. The skill knows it (the user confirmed
   it during the run) and puts it in the manifest; nothing else can recover it.

The fix is to prefer the manifest wherever one exists — it is authoritative for both:

```python
man = os.path.join(dirpath, "fran_manifest.json")
if os.path.isfile(man):
    m = json.load(open(man))
    identity = m["output_dir"]                 # the real search dir, not the drop entry
    extra = m["suggested_ingest"]              # includes --organism-name/--taxon/--engine
else:
    identity = os.path.realpath(dirpath)       # existing behaviour, for bare symlinks
```

(The entry is a real directory of links rather than a bare symlink on purpose — see the
engine-marker bug below, which a bare symlink would walk straight into.)

### 🔴 A bug in `find_uningested.py` this handover has to work around

`ENGINE_MARKERS` lists **`search_provenance.json` as a Radiant marker**, and Radiant is tested
before DIA-NN. But `run_search.py` writes that file into **every** search directory it produces,
whatever the engine. So any directory from the Core pipeline skill — DIA-NN, FragPipe, Radiant —
is detected as **Radiant**. That is not specific to the drop directory: it already mislabels
skill-produced DIA-NN searches under `/quobyte/proteomics-grp/brett`, which is a default scan root.

The skill works around it by not linking that file (its contents are in the manifest instead), and
by writing the true engine into the manifest. **The real fix belongs here**: drop
`search_provenance.json` from Radiant's marker list — Radiant is already identified by
`radiant_results/fulcrum-results` and `fulcrum-results/_SUCCESS` — or read the `engine` key out of
it instead of treating its existence as a marker.

Verified with the current `detect_engine()` against real staged entries on Hive (2026-08-25):
DIA-NN → `diann`, FragPipe → `fragpipe`, Radiant → `radiant`. All three route correctly **because**
the provenance file is withheld.

### Telling the skill an entry is done (optional)

`fran_deposit.py verify` reports a search as ingested if either the corpus holds it *or* the entry
contains **`fran_ingested.json`**. If the cron writes that marker (any JSON; `search_id` and
`n_precursors` are useful), then Core members with no corpus credential can still confirm the
hand-over landed. Without it, `verify` falls back to querying the corpus, which needs a token.
Removing or moving an ingested entry is also fine — `verify` then reports it from the corpus.

### If you change `corpus_ingest.py`'s CLI, this lane is a caller
The manifest emits `--engine`, `--output-dir`, `--name`, `--organism-name`, `--taxon`. Renaming any
of them breaks automatic ingests.

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
