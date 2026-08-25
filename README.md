# FRAN — Fragment Reference & ANnotation

<p align="center">
  <img src="docs/fran-mascot.png" alt="FRAN the mascot — a glamorous drag queen in a purple sequinned gown and FRAN sash, holding a bubbling Erlenmeyer flask beside a mass spectrometer and a whiteboard of equations, giving a thumbs up and saying &quot;Fast, Robust Analysis, darling!&quot;" width="380">
</p>

<p align="center"><em><b>Fast, Robust Analysis, darling!</b></em></p>

<p align="center">
  <a href="https://fran.stan-proteomics.org"><img alt="live app" src="https://img.shields.io/badge/live%20app-fran.stan--proteomics.org-0E6F79"></a>
  <img alt="python" src="https://img.shields.io/badge/python-3.11-blue">
  <img alt="corpus" src="https://img.shields.io/badge/corpus-434M%20precursors-2E7346">
  <img alt="ion mobility" src="https://img.shields.io/badge/ion%20mobility-92%25-A8681A">
</p>

## What it is

When you identify a peptide in a DIA run, the obvious questions have no good public
answer. *Has anyone ever seen this peptide before? At what retention time, what charge,
what ion mobility? Does a different search engine find it in the same file? Is it worth
building an assay around, or does it barely fly?* GPMDB answered a version of this for
DDA twenty years ago. For DIA — and especially for diaPASEF, where every identification
carries an ion-mobility coordinate — there is essentially nowhere to look.

FRAN is that place. It is a read-only public window onto a live proteomics corpus, browsable
by **peptide, protein, gene, organism, run, or lab**. The corpus currently holds:

| | |
|---|---|
| **434,154,365** precursor identifications | **3,238,768** distinct peptides |
| **593,541** protein groups | **114** organisms |
| **2,011** searches across **20,988** raw files | **92%** carry ion mobility (timsTOF / diaPASEF) |

What makes it different from a static database: counts are **live** and grow as ingest
proceeds; every peptide carries its real measured **RT × 1/K₀** distribution rather than a
single consensus value; searches from **four engines** (Spectronaut, DIA-NN, FragPipe,
Radiant) sit side by side on the *same raw files*, so you can see where they disagree; and
there is a queryable **MCP endpoint** so an AI agent can ask the corpus questions directly.

FRAN is part of **[STAN](https://github.com/bsphinney/stan)** (stan-proteomics.org).

## Using FRAN in a core facility

The public site is anonymised: filenames become `run-3c5f57`, search names become `search-f0313c`.
That is the *public* face of the same deployment. Logged in, FRAN is also a **core-facility
instrument** — the corpus you already generated, with the customer names put back.

Access is **per request** and **fails closed**, in three tiers:

| Tier | Who | Sees |
|---|---|---|
| `public` | anyone, no login | anonymised corpus — the website |
| `lab` | a PI / collaborator | **only their own submissions**, real names |
| `full` | core staff | everything, plus the cross-lab directory |

In production the gate is Azure Easy Auth + Microsoft Entra: a caller is authorised only if the
platform-verified principal carries one Entra security-group object id (`FRAN_REQUIRED_GROUP`).
Membership in that group *is* the entire access list — managed in Entra, no redeploy. No principal,
no configured group, or group absent → public. Self-hosted instances can instead use a shared
internal key (`DELIMP_INTERNAL_KEY`, sent as `X-Internal-Key`).

**Keeping track of customer samples.** The collaborator directory keys on the actual
service-directory customer folder rather than a free-text `client` field — which matters, because
grouping on `client` collapsed all 453 on-campus searches into one generic "UC Davis" while the real
lab was sitting in `pi`. From there: labs grouped by canonical institution (UC Davis spelling
variants merged, deduped by PI, annotated with college/department), each lab's submissions, and each
submission's searches and runs. Crucially it shows the **full customer base, not just the corpus** —
labs whose data is on the share but *not yet ingested* appear with an "on-share / un-ingested"
status, so a sample that never got analysed is visible instead of silently absent.

**Writing support letters.** FRAN does not write the letter, but it answers the questions a letter
needs, per PI, in one place: how many submissions and searches, over what period, how many runs and
on which instruments, how many proteins and peptides were identified, and which projects they belong
to. `/api/internal/lab/{pi}` is that page. For a grant renewal or a letter of support this is the
difference between "we have worked with this lab for years" and a number you can stand behind.

**Querying across collaborators.** At `full` tier the directory is cross-lab: search people by name,
list every collaborator with their search / PI / project counts and LIMS linkage, or pivot by
institution. This is the query that is genuinely hard to do any other way — "which labs have we run
timsTOF phospho for, and how much", "who else submitted this organism", "which of our collaborators
has data we could reuse for a method comparison".

**Handing work back.** Three exports, gated by the same tiers (a `lab` caller can only export their
own submissions):

| Export | What it is |
|---|---|
| `/api/export/diann_report/{search_id}` | the search as a DIA-NN-shaped report |
| `/api/export/research_brief/{search_id}` | a markdown packet pre-filled for re-searching + analysing a search |
| `/api/export/resubmit_brief/{submission_id}` | the same, for a submission whose raw data is on the service directory but not yet in FRAN |

> **Before you enable any of this**, read [INSTALL.md §9](INSTALL.md). The confidential tables hold
> real customer and PI names, file paths and submission provenance. The public layer is safe because
> those tables are not in its allowlist — that property is what you are switching off.

> ### 🔎 Just want to look something up?
> **You do not need to install anything.** The public instance is free and needs no login:
> **[fran.stan-proteomics.org](https://fran.stan-proteomics.org)**
>
> Everything below is for running **your own** FRAN, on your own data.

## Quick install — pick your mode

| If you… | Use mode | Time | Guide |
|---|---|---|---|
| Just want to browse the public corpus | **None — use the website** | 0 min | [fran.stan-proteomics.org](https://fran.stan-proteomics.org) |
| Want FRAN over **your own** data, on your laptop | **A — local** | ~30 min | [INSTALL.md](INSTALL.md) |
| Want an instance your institution can use | **B — hosted** | 1–3 h | [INSTALL.md](INSTALL.md) + §9 |
| Want your instance to exchange data with other FRANs | **C — federated** | +30 min | [FEDERATION.md](FEDERATION.md) |

FRAN is **not UC-Davis-specific**. `schema/fran_schema.sql` creates the whole corpus schema —
36 tables, 7 materialized views, 56 indexes — and it is *generated from a live instance* by
`scripts/dump_schema.py` rather than hand-maintained, so it cannot drift from what the code
expects. It has been verified by applying it to an empty PostgreSQL 16 and running a first
ingest through to a served page.

### Mode A — local, on your own data

```bash
git clone https://github.com/bsphinney/FRAN.git && cd FRAN
pip install -r requirements.txt

createdb fran                                   # any PostgreSQL 14+
psql fran -f schema/fran_schema.sql             # 36 tables, 7 matviews, 56 indexes

cp .env.example .env && $EDITOR .env            # point DELIMP_PG_* at YOUR database
set -a; source .env; set +a                     # the app never auto-loads .env

python ingest/corpus_ingest.py /path/to/searchdir --engine diann --dry-run
python ingest/corpus_ingest.py /path/to/searchdir --engine diann --bulk-copy
python ingest/refresh_leaderboards.py           # Highlights are matviews; refresh after ingest

uvicorn app.main:app --reload --port 7860       # open http://localhost:7860
```

Or with Docker (serves on 7860):

```bash
docker build -t fran . && docker run -p 7860:7860 --env-file .env fran
```

Always `--dry-run` first — it parses the report and prints precursor / run / protein-group counts
without writing, so you can check them against your search engine's own summary before committing.

[INSTALL.md](INSTALL.md) walks through each step, including per-engine ingest commands (Spectronaut
takes a report *file*, not a directory) and a verification pass. **Read §9 (Security) before
exposing an instance to anyone.**

## What you can browse

- **Overview** — live counts, species, platform, engine and charge distributions, an RT×IM
  density map, and recently ingested searches. Auto-refreshes as the corpus grows.
- **Search** — peptide (substring or exact, trigram-indexed), protein group, or gene.
- **Peptide / precursor view** — modified forms (ProForma) × charge, per-run RT / 1/K₀ / m/z /
  q-value / intensity, predicted flyability, cross-engine consensus.
- **Protein view** — observed peptides, per-search and per-run intensity, sequence coverage.
- **Cross-engine comparison** (`#/engines`) — for any raw file searched by more than one
  engine: agreement at precursor / peptide / protein level, quantitative correlation, and the
  peptides each engine claims alone.
- **Ion-mobility showcase** — full-screen RT × 1/K₀ scatter, coloured by charge.
- **MCP endpoint** — the corpus as a tool an AI agent can query.

## Training on real data, not predictions

Most deep-learning work in proteomics is trained on **predicted** fragment intensities, or on
curated synthetic-peptide libraries. A predicted spectrum is a model's opinion about what an
instrument would have measured. FRAN's Lance lanes hold what instruments **actually measured**, at a
scale that is usually only available inside a vendor:

| Lane | Contents |
|---|---|
| **Spectrum lane** | 1,545 datasets · **351,567,481** precursors · **2,084,278,701** annotated fragments |
| **XIC lane** | **3,511,456** precursors · **37,686,542** chromatogram traces |

Spread across **20,993 runs**, **114 organisms** and both major DIA platforms — timsTOF (13,708 runs)
and Orbitrap (7,285) — so a model trained on it is not learning one instrument's quirks.

**Measured and predicted sit in the same row.** Each precursor carries `frg_measured_relint`
*and* `frg_predicted_relint`, `ms1_iso_rel_measured` *and* `ms1_iso_rel_predicted`, `rt` *and*
`rt_predicted`, `irt_empirical` *and* `irt_predicted`. So the corpus is both a training set and its
own benchmark: you can fit on what was measured and, in the same query, quantify where the existing
predictor was wrong — per fragment, per peptide, per instrument.

It also carries the things that make aggregates correct rather than merely large:
`frg_excluded` is the engine's own verdict on whether a fragment was used for quantification (31–48%
are `True`); averaging intensities the engine itself discarded produces a confidently wrong number.
`frg_chan_interference`, `signal_to_noise`, `int_corr_score` and per-fragment `frg_mass_acc_ppm` are
stored for the same reason. Decoys are excluded from the lanes entirely.

**Why Lance and not the database.** One row per precursor, with the whole spectrum and its
chromatograms as Arrow list columns — the shape a training `DataLoader` fetches by index. It is
columnar, versioned and random-access, the same move `depthcharge`/Casanovo made for MS training
data. Bulk traces in PostgreSQL cost roughly 6.1 KB/row, so the dog chromatogram set alone would be
~22 GB there against ~13 GB as Lance. The database keeps the **registry**: every dataset is recorded
with a content md5 and row counts, so a lost or corrupted dataset is *detectable* and re-derivable
from the archived reports rather than quietly wrong. 1,545 datasets have been verified against those
checksums.

> ### ⚠️ Read this before training on it
> These traces and spectra are **identification-conditioned**, not an unbiased sample of the raw
> data. Every engine's export covers only the precursors that engine *reported* — DIA-NN's `--xic`
> writes traces for its identification list, and Spectronaut's `.xic.db` has the same limitation.
> A precursor absent from a lane means "this engine did not report it here", **never** "there was no
> signal there".
>
> The practical consequence: you cannot learn what a *non-hit* looks like from this corpus, so it
> does not by itself support training a discriminator on positives-vs-negatives. Neutral extraction
> straight from the raw files is on the roadmap for exactly this reason. Everything else — intensity
> prediction, RT and ion-mobility prediction, peak-shape and co-elution modelling, benchmarking a
> predictor against measurement — is well served.

**Current coverage.** The spectrum lane is broad. The XIC lane is Spectronaut-only today; DIA-NN
chromatograms are in progress (`ingest/diann_xic_to_lance.py`), and FragPipe exports none at all.
See [STORAGE_DESIGN.md](STORAGE_DESIGN.md) and
[LANCE_PRIORS_AND_XIC_SPEC.md](LANCE_PRIORS_AND_XIC_SPEC.md).

## Architecture

```
  search engine output                 ingest (Python, offline)          serving (FastAPI)
  ────────────────────────             ────────────────────────          ─────────────────
  Spectronaut  .sne / FRAN.rs  ─┐
  DIA-NN       report.parquet  ─┤                                        app/queries.py
  FragPipe     report.tsv      ─┼──▶  ingest/corpus_ingest.py  ──▶ PostgreSQL ──▶ app/db.py
  Radiant      fulcrum parquet ─┘        engine adapters          (delimp)      (read-only +
                                         + duplicate guard            │          allowlist)
                                                                      │              │
  raw chromatograms ───────────▶  Lance lanes (columnar, on disk) ─────┘         app/static
    Spectronaut .xic.db                spectrum · xic · xic-trace                 (SPA, no
    DIA-NN --xic                       registered in PostgreSQL                  build step)
```

Bulk trace data lives in **Lance** (columnar, versioned, on disk), not in PostgreSQL — a
3.5M-precursor chromatogram set costs ~13 GB as Lance against ~22 GB in PG. PostgreSQL holds
the registry plus whatever curated subset a page actually serves.

## Key design decisions

- **Read-only is structural, not a convention.** Sessions open with
  `default_transaction_read_only=on` and only `SELECT`/`WITH` is accepted. There is no
  INSERT/UPDATE/DELETE/DDL code path in the serving app at all.
- **Every query declares the tables it reads**, validated against a `PUBLIC_TABLES` allowlist.
  Confidential tables (customer names, file paths, submission provenance) are simply not in
  the public set, so a query that touches one fails rather than leaking.
- **Filenames are sanitised, not trusted.** Raw filenames routinely contain PI names and
  project codes, so the public layer renders them as `run-<sha1[:6]>`.
- **The schema is generated, never hand-written.** `scripts/dump_schema.py` dumps it from a
  live database, so `schema/fran_schema.sql` cannot silently drift from the code.
- **Ingest refuses to duplicate.** A guard rejects a write when another `output_dir` already
  holds the same raw-file set and precursor count — the corpus has 184 duplicate groups from
  before it existed, and that is how they got there.
- **Versions are recorded with the data.** Ingester, guard and lane-writer versions are
  stamped on every search and Lance dataset, so "which code produced this row?" is answerable
  after the fact rather than guessed.

## Security & governance

Enforced in `app/db.py` and `app/privacy.py`:

1. **Read-only sessions.** No write path exists.
2. **Public-layer allowlist.** Per-query table declarations validated against `PUBLIC_TABLES`.
   Confidential tables are reachable only from an authenticated "full" tier, never anonymously.
3. **Parameterized queries only.** No raw user SQL, no string-interpolated SQL.
4. **Identity sanitisation.** Filenames, search names and project strings are anonymised in
   the public layer.
5. **Credentials via environment only** — never committed.
6. **Confidential access is per-request and fails closed.** Three tiers (`public` / `lab` / `full`);
   absent or unverifiable authorization always resolves to `public`. See
   [Using FRAN in a core facility](#using-fran-in-a-core-facility).

> **⚠️ Before you expose an instance:** do not point a public deployment at a database
> credential that can read your internal layer, even if this app never queries it — the
> *credential* is the exposure, not the query. Use a role that can `SELECT` only the public
> tables, or serve from a periodic read-only snapshot. [INSTALL.md §9](INSTALL.md) covers this.

## Federation (optional, off by default)

FRAN instances can share **precursor-level, de-identified** observations with each other, so a
peptide you have never seen locally can still tell you "three other labs have observed this, at
these retention times". Nothing is shared unless you turn it on: `federation_visibility`
defaults to `hidden` on every row.

Bulk extraction is defended against explicitly — per-query row caps, shape checks, durable
per-peer budgets and novelty detection — so a peer cannot walk your corpus by issuing many
small queries. See **[FEDERATION.md](FEDERATION.md)**, design notes in
[FEDERATION_DESIGN.md](FEDERATION_DESIGN.md).

## Configuration

| Var | Default | Notes |
|-----|---------|-------|
| `DELIMP_PG_HOST` | `pgfarm.library.ucdavis.edu` | point at your own PostgreSQL |
| `DELIMP_PG_PORT` | `5432` | |
| `DELIMP_PG_DB` | `uc-davis-genome-center-proteomics-core/delimp` | |
| `DELIMP_PG_USER` | `genome-proteomics-service-account` | use a read-only role |
| `DELIMP_PG_SSLMODE` | `require` | not `verify-full` |
| `DELIMP_PG_PASSWORD` | — | set via environment / deployment secret |
| `DELIMP_PG_TOKEN_FILE` | — | alternative: path to a token file (local dev) |
| `DELIMP_CACHE_TTL` | `20` | seconds to cache dashboard aggregates |
| `FRAN_REQUIRED_GROUP` | — | Entra security-group object id that grants the confidential tier |
| `DELIMP_INTERNAL_KEY` | — | shared key for self-hosted internal access (`X-Internal-Key`) |
| `FRAN_DEV_AUTH` | — | `1` enables local dev auth shortcuts — **never set in production** |

## Implementation status

| Component | Status | Notes |
|---|---|---|
| Corpus browsing, search, peptide/protein views | ✅ shipped | |
| Ingest: Spectronaut, DIA-NN, FragPipe, Radiant | ✅ shipped | four engines, `ingest/corpus_ingest.py` |
| Cross-engine comparison page | ✅ shipped | v0.18.0, pairwise per raw file |
| Installable schema + install guide | ✅ shipped | container-verified against empty PG 16 |
| MCP endpoint | ✅ shipped | rate-limited |
| Spectrum lane (observed MS2) | ✅ shipped | Lance, 1,553 datasets verified |
| XIC lane — Spectronaut | ✅ shipped | from `.xic.db` |
| XIC lane — DIA-NN | 🟡 in progress | adapter written + tested; traces being copied over |
| XIC lane — FragPipe | ❌ not possible today | FragPipe's bundled DIA-NN runs without `--xic` |
| Federation | 🟡 built, not wired | modules and tests exist; endpoints not mounted in `main.py` |
| Fragment/peak viewer, USI links | ⛔ deferred | needs the spectra-extraction step |
| LICENSE | ✅ shipped | FRAN Academic License — free for academic/non-profit, commercial use needs a licence |

## Roadmap

**High**
- Wire the federation endpoints into `app/main.py` (the modules are inert today).
- Finish the DIA-NN XIC lane and add per-engine chromatogram overlays to the comparison page.
- Backfill `search_engine_version` for the ~650 archived Spectronaut searches whose sidecars
  were never pulled off the Windows box.

**Medium**
- Neutral chromatogram extraction from raw files, so a peptide *missed* by an engine can still
  be shown — every engine's XIC export only covers what that engine reported.
- Peptide-level `n_engines_confirming` and I/L normalisation across engines.
- Resolve the remaining 162 duplicate search groups.

## Documentation

| Doc | Contents |
|---|---|
| [INSTALL.md](INSTALL.md) | Stand up your own instance, step by step |
| [FEDERATION.md](FEDERATION.md) | Connect your node to other FRANs |
| [FEDERATION_DESIGN.md](FEDERATION_DESIGN.md) | Why federation works the way it does |
| [FRAN_ANALYST_GUIDE.md](FRAN_ANALYST_GUIDE.md) | Using FRAN as a working analyst |
| [STORAGE_DESIGN.md](STORAGE_DESIGN.md) | Lance lanes, and what lives where |
| [LANCE_PRIORS_AND_XIC_SPEC.md](LANCE_PRIORS_AND_XIC_SPEC.md) | Chromatogram storage spec |
| [AGENTS.md](AGENTS.md) | Conventions for agents working in this repo |

## Search engines

| Engine | Ingest | Chromatograms | Notes |
|---|---|---|---|
| Spectronaut | ✅ `.sne` / FRAN.rs report | ✅ `.xic.db` | 15 through 21 |
| DIA-NN | ✅ `report.parquet` / `.tsv` | 🟡 `--xic` | 1.7 through 2.6 |
| FragPipe (DIA) | ✅ `report.tsv` | ❌ | diaTracer → MSFragger → DIA-NN 1.8.2b8 |
| Radiant / Fulcrum | ✅ fulcrum-results parquet | ❌ | mzML/Parquet only — cannot read Bruker `.d` |

## Contributing

Issues and pull requests welcome. If you are adding an ingest adapter for another engine, the
existing ones live in `ingest/` and each is a `_records()` dispatch plus a version detector —
`ingest/radiant_to_corpus.py` is the smallest complete example.

## License

[FRAN Academic License](LICENSE) — the same licence STAN uses. Free for academic, non-profit,
educational and personal research use, including fee-for-service work by academic core
facilities. Commercial use requires prior written permission: bsphinney@ucdavis.edu.

## Links

- **Live app:** https://fran.stan-proteomics.org
- **STAN:** https://github.com/bsphinney/stan · https://stan-proteomics.org
- **UC Davis Proteomics Core:** https://proteomics.ucdavis.edu
