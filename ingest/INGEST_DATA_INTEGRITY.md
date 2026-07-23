# FRAN corpus — data-integrity sweep (2026-07-22 → 07-23)

A pass over the `delimp_*` / `raw_files` corpus that found and fixed several **systematic** ingestion
errors — most importantly that ~28% of raws had the **wrong instrument** recorded. This documents what
was wrong, how it was fixed, the tools, and what's left. Numbers are from the run; re-derive with the
scripts below.

> **Ground-truth principle used throughout:** trust the *physical file* and the *raw header*, not the
> DB's recorded strings. Brett's invariant — **nothing is deleted; every raw lives in `raw_data` or
> `/quobyte/proteomics-grp/to-hive`** — drove the "not missing, just renamed/mis-recorded" conclusions.

---

## 1. Instrument metadata was systematically wrong  ⚑ the big one

**Symptom.** `raw_files.platform` claimed **18,035 timsTOF / 1,837 orbitrap**. In reality the corpus is
**12,541 Bruker (`.d`) / 7,083 Thermo (`.raw`)** — i.e. **~5,471 raws (28%) were mislabeled**, mostly
Thermo runs stamped `timstof` + `diaPASEF`.

**Why it happened.** Two independent recording errors that *agreed with each other*, so the DB couldn't
self-detect it:
- The DB's `raw_path` extension was itself wrong (`…PD_AD_01.d` for a file that is physically `.raw`).
- `acquisition_method` was set from the Spectronaut **`setup.txt` search setting** `diaPASEF
  Pre-Processing: Automatic` — a *setting*, not the acquisition — so every run (Thermo included) got
  `diaPASEF`. `diaPASEF` is Bruker-timsTOF-exclusive, so `orbitrap + diaPASEF` is physically impossible.

The instrument metadata is **not in the SN/DIA-NN reports** (checked — the report parquet has zero
instrument columns; the setup has only `Vendor:` + the misleading setting). It lives **only in the raw
header**.

**Fixes (all verified):**
| field | source of truth | how | result |
|---|---|---|---|
| `platform` | physical file extension (`.d`=timstof, `.raw`=orbitrap), cross-checked vs export `Vendor:` | `resolve_raw_hive_paths.py --fix-meta` | 0 rows disagree with the physical file |
| `acquisition_method` (coarse) | physics: DIA corpus ⇒ timstof=`diaPASEF`, orbitrap=`DIA` | `acq_fix` UPDATE | 5,723 fixed; 0 impossible `orbitrap+PASEF` |
| `instrument_model`, `instrument_serial`, exact method, `n_ms1/ms2_frames`, `mass_range_min/max` | **the raw header** | `record_raw_metadata.py --bruker --apply` | Bruker: 12,538/12,541 model + exact method (e.g. `DIA_11x3-k07t13Ra85.m`) |

**Thermo enrichment deferred** (Brett's call). Thermo rows are correct at the coarse level
(`orbitrap`/`DIA`) but `instrument_model`/`serial` remain NULL until the Thermo pass runs
(ThermoRawFileParser `-m`, needs the `dotnet-core-sdk/8.0.4` module + `DOTNET_ROOT`).

---

## 2. `raw_files.hive_path` was NULL for every row

We tracked that raws *existed* but not *where on Hive*. Resolved by **basename** (the DB `raw_path` is
the original Windows/multi-renamed path and doesn't match Flinders' instrument layout):

- **Roots walked:** `/nfs/lssc0/flinders/proteomics/Data` (`raw_data` **and** the `lab/service/…`
  dirs) **+** `/quobyte/proteomics-grp/to-hive` (older archive).
- **Two-copy practice + symlinked datasets (bigDOG):** a raw appears under several paths → collapse by
  `realpath`, prefer the `raw_data` copy — *not* ambiguity.
- **Extension-aware:** the DB `raw_path` extension is unreliable, so match on the extension-stripped
  basename and take the format from the physical file.
- **Basename-extension bug:** some `raw_basename` values include `.d`/`.raw` (`…21552.d`), some don't —
  normalize both sides (this alone recovered 247 "missing").

**Result: 19,624 / 19,872 located (98.75%).** `resolve_raw_hive_paths.py` writes `hive_path` +
`hive_verified_at`. Full audit trail: `raw_resolution.csv` on Hive (`glendon/`).

### The 248 not-located — **renamed, not missing**
Confirmed via a Bruker `.d`'s internal `SampleInfo.xml`: a file acquired as
`01232023_100spd_DIA_DM97_S3-F1_1_3300` was renamed to `PD_AD_01` (what the search/report/DB
recorded) and again to `neg_Case01` (current disk, "Publication_Data"). **Three names, no recorded
bridge.** The reports faithfully hold the *search-time* name; the disk name diverged. These need the
lab's rename maps or positional mapping — documented residual, ~1.25%.

---

## 3. `delimp_precursors.q_value = NaN`  (~64,960 rows)

Spectronaut emits `NaN` for some `EG.Qvalue`; `corpus_ingest._flt` let it through (the float4
under/overflow guards don't catch NaN, and `NaN is not None`). Harmless to FDR filters (Postgres sorts
NaN high, so it fails `q ≤ 0.01`) but breaks any math/ML on `q_value`.
- **Data:** `UPDATE delimp_precursors SET q_value=NULL WHERE q_value='NaN'::real` → 64,960 nulled, 0 left.
- **Root cause:** guard added (`f != f`) in `corpus_ingest._flt` and `spectronaut_to_corpus._f`.
- **Scope:** only `q_value` (global_q/pg_q/pep/best_q are clean); DIA-NN path unaffected.
- **Lance lane:** verified clean (0 NaN in 11.3M sampled rows) — no correction needed.

---

## 4. Other audit findings (`db_audit.py`)

- `delimp_precursor_xic.search_id` — 13/23 are name-slugs, not uuids, so the XIC lane doesn't join to
  `delimp_searches`. **Not yet fixed.**
- `delimp_spectrum_lane` — 115 datasets still `search_id IS NULL` (ambiguous duplicates + no-name-match).
- `raw_files.md5` / `nce` / `ms2_resolution` — still NULL for all (headers can fill some; deferred).
- `sharing_status = 'private'` for all searches — by design (public layer serves aggregate only).

---

## 5. PG Farm token (self-refreshing)

There is **no token-refresh cron.** `_token()` swaps a **long-lived service-account secret** for a
fresh JWT on every call. If a `.pgfarm_token` file holds a *JWT* it will expire; it must hold the
**secret** (self-exchanging). Canonical secret:
`/quobyte/proteomics-grp/de-limp/fran_refresh/.pgfarm_token`. Brett's `~/.pgfarm_token` was refreshed
from it on 2026-07-23.

---

## 6. Tooling (all in `ingest/`)

| script | does |
|---|---|
| `resolve_raw_hive_paths.py` | locate raws by basename across all roots; fill `hive_path`; `--fix-meta` corrects `platform`; `--dump` writes the resolution CSV |
| `record_raw_metadata.py` | read instrument metadata from raw headers → `raw_files` (Bruker `.d` via `analysis.tdf`; Thermo `.raw` via ThermoRawFileParser) |
| `db_audit.py` | scan the corpus for metadata-vs-format conflicts, NULL columns, broken links, out-of-range values (pg_stats, no big scans) |
| `corpus_ingest.py` / `spectronaut_to_corpus.py` | NaN→NULL guard added to the float coercion |

**Run environment:** Hive compute nodes (never the login node for walks/reads);
`/quobyte/proteomics-grp/brett/envs/alphadia2/bin/python`; `dotnet-core-sdk/8.0.4` module for Thermo.

---

## 7. Still open
- Thermo metadata enrichment (`instrument_model`/`serial`/exact method) — pass is ready, not run.
- 248 renamed raws — need lab rename maps.
- `delimp_precursor_xic` slug `search_id`s → remap to uuids.
- `raw_files.md5` (integrity + a real unique-raw table); `nce`/`ms2_resolution` from headers.
- Cosmetic: 3 rows read `" timsTOF Pro"` (leading space) — trim.
