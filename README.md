---
title: FRAN — Proteomics Corpus Browser
emoji: 🧬
colorFrom: indigo
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: FRAN — browse a live DIA proteomics corpus (peptides, proteins, species, ion mobility).
---

# FRAN — Fragment Reference & ANnotation

**Live app: https://fran.stan-proteomics.org** (free, no login)

A read-only public window onto the live PG Farm `delimp` proteomics corpus — a
DIA-MS "field guide" you can browse by **peptide, protein, gene, organism, run,
or lab**. As of mid-2026 the corpus holds roughly **335M precursor
identifications · 3.1M distinct peptides · 560K protein groups · 112 organisms**,
and **~90% of it carries ion mobility** (timsTOF / diaPASEF). GPMDB-inspired in
*function*, but built for 2026: live counts that grow as ingest proceeds, an RT ×
ion-mobility (1/K₀) showcase, per-species **% of proteome** coverage, predicted
flyability, cross-tool (DIA-NN + Spectronaut) provenance, a queryable **MCP**
endpoint for AI agents, server-side aggregation, and a hard public-layer security
boundary. Part of **STAN** (stan-proteomics.org).

## Stack
- **Backend:** FastAPI + psycopg2 (reuses the project's PG Farm connection
  pattern), a connection pool with **read-only** sessions and a **table
  allowlist** so it is structurally impossible to query the internal/customer
  layer.
- **Frontend:** single-page app, Tailwind + Chart.js, no build step.
- **Packaging:** Docker, deployable as a Hugging Face **Docker Space** (serves
  on port 7860).

## Features
- **Overview dashboard** — live counts (precursors / peptides / protein groups /
  searches / raw files / organisms / IM-bearing precursors), species, platform,
  engine and charge distributions, RT×IM density, and a *recently ingested
  searches* panel. Counts auto-refresh so you can watch the corpus populate.
- **Search** — peptide (substring or exact, trigram-indexed), protein group /
  gene.
- **Protein view** — observed peptides, per-search/run intensity, coverage.
- **Peptide/precursor view** — modified forms (ProForma) × charge, per-run
  RT / 1/K₀ / m/z / q-value / intensity, cross-engine consensus.
- **Search/run browser** — every ingested search + per-run stats.
- **Ion-mobility showcase** — full-screen RT × 1/K₀ scatter, colored by charge.

## Security & governance (enforced in `app/db.py`)
1. **Read-only.** Sessions open with `default_transaction_read_only=on`; only
   `SELECT`/`WITH` statements are allowed. No INSERT/UPDATE/DELETE/DDL path.
2. **Public-layer allowlist.** Every query declares which tables it reads; the
   set is validated against `PUBLIC_TABLES`. The confidential tables (real
   customer/PI names, file paths, submission provenance — `coreomics_*_cache`,
   `delimp_search_provenance`, `delimp_submission_service_dir`, …) are excluded
   from the public set and are reachable only when a request is authenticated
   to the confidential ("full") tier — never from the anonymous public layer.
3. **Parameterized queries only.** No raw user SQL; no string-interpolated SQL.
4. **Credentials via env / HF Secrets** — never committed (see below).

## Configuration (environment variables)
| Var | Default | Notes |
|-----|---------|-------|
| `DELIMP_PG_HOST` | `pgfarm.library.ucdavis.edu` | |
| `DELIMP_PG_PORT` | `5432` | |
| `DELIMP_PG_DB` | `uc-davis-genome-center-proteomics-core/delimp` | |
| `DELIMP_PG_USER` | `genome-proteomics-service-account` | |
| `DELIMP_PG_SSLMODE` | `require` | not `verify-full` |
| `DELIMP_PG_PASSWORD` | — | the 7-day PG Farm token (set as **HF Secret**) |
| `DELIMP_PG_TOKEN_FILE` | — | alternative: path to a token file (local dev) |
| `DELIMP_CACHE_TTL` | `20` | seconds to cache dashboard aggregates |

## ⚠️ Public-hosting security decision (READ BEFORE PUSHING)
Putting `DELIMP_PG_PASSWORD` (the live **service-account** token) into a public
HF Space lets the running container reach the DB — but that account is the same
one STAN uses daily and can see the **internal** layer at the DB level (this app
won't query it, but the *credential* is broad). HF Secrets are not exposed to
browsers, yet a public Space is a larger attack surface than an internal tool.

**Recommendation — pick one before going public:**
- **(a) Dedicated read-only credential** — ask PG Farm / Justin for a role that
  can `SELECT` only the public tables (or only sees a public schema). Put *that*
  token in the Space. Best option.
- **(b) Periodic read-only snapshot** — dump the public tables to a separate
  read-only DB (or SQLite/Parquet) on a schedule and point the Space at that.
  Decouples the public app from the live service account entirely.
- **(c) Keep it internal** — run as a private/internal Space or on the VPN with
  the existing token, accepting the tradeoff.

Do **not** ship the full live service-account secret to a *public* Space without
consciously choosing (a) or (b). This is a decision for the owner, not a default.

## Run locally
```bash
cd corpus_browser
pip install -r requirements.txt
# point at a token file (or export DELIMP_PG_PASSWORD)
export DELIMP_PG_TOKEN_FILE=/Volumes/proteomics-grp/brett/.pgfarm_token
uvicorn app.main:app --reload --port 7860
# open http://localhost:7860
```

## Deferred
- **Fragment-spectrum / peak viewer.** `delimp_precursors.peak_mz` /
  `peak_intensity` are NULL until the separate spectra-extraction step runs
  (DIA-NN's report.parquet carries coordinates, not fragment peak lists). Every
  other view is live. A spectrum viewer slots in once peaks are populated.
- **USI links** (also deferred until spectra extraction).
