# AGENTS.md — runbook for installing & running FRAN with an AI agent

This file is written for an AI coding agent (Claude Code, Cursor, etc.) to **install, run, and
verify FRAN** with minimal back-and-forth. Follow it top to bottom; the commands are
copy-paste runnable. If you are a human, the same steps work — but see `README.md` for context.

## What you are setting up
FRAN (*Fragment Reference & ANnotation*) is a **FastAPI** app (`app/main.py`) that serves a
single-page UI on **port 7860** and reads a live **PostgreSQL** DIA-proteomics corpus on PG Farm
over a **read-only** connection pool. There is **no build step** and **no database to create** —
you only need Python (or Docker) and a PG Farm credential.

## Prerequisites (check, do not assume)
- `python3 --version` → 3.11+  **or**  `docker --version`
- A **PG Farm credential** for the `delimp` corpus. This is a 7-day token string, or a path to a
  file containing it (`.pgfarm_token`). **Ask the user for it — never invent or hard-code one.**
- Network egress to `pgfarm.library.ucdavis.edu:5432` (the DB) and, for predicted-spectra
  features, `koina.wilhelmlab.org`. If the user is off-campus this may require VPN.

## Install & run (Python — default path)
```bash
# from the repo root
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# Credential — set EXACTLY ONE (ask the user which they have):
export DELIMP_PG_PASSWORD="<paste PG Farm token>"        # token string, OR:
# export DELIMP_PG_TOKEN_FILE="/abs/path/to/.pgfarm_token"  # token in a file

uvicorn app.main:app --port 7860
```
Defaults for host/db/user/sslmode are already correct (see `.env.example`); only the credential
is required.

## Verify — definition of done
```bash
curl -s localhost:7860/health
#   EXPECT: connected = true  AND  read_only = "on"
curl -s localhost:7860/api/overview | head -c 300
#   EXPECT: JSON with non-zero corpus counts (precursors/peptides/proteins/...)
```
Then open http://localhost:7860 — the dashboard should show live counts. If both curls pass,
you are done.

## Troubleshooting (map symptom → fix)
- **`/health` → `connected: false` or "No DB credential":** the token is not visible to the
  uvicorn process. Re-`export DELIMP_PG_PASSWORD` (or `DELIMP_PG_TOKEN_FILE`) in the **same
  shell** that runs uvicorn; confirm with `echo ${DELIMP_PG_PASSWORD:+SET}`.
- **`connected: false` with an auth error:** the 7-day PG Farm token has expired — ask the user
  for a fresh one.
- **connection timeout:** no network route to `pgfarm.library.ucdavis.edu:5432` (VPN / campus
  network needed).
- **`ModuleNotFoundError`:** the venv is not active, or `pip install -r requirements.txt` did not
  run in it.
- **UI loads but says "database unavailable":** this is the intended graceful-degrade state — a
  credential or network issue (see the two cases above), not a code bug.

## Docker alternative
```bash
docker build -t fran .
docker run --rm -p 7860:7860 -e DELIMP_PG_PASSWORD="<token>" fran
# open http://localhost:7860
```

## Deploying to a Hugging Face Space
See `DEPLOY_HF.md`. Set the credential as a **Space Secret** named `DELIMP_PG_PASSWORD` (not a
plain Variable). Read the public-hosting security note in `README.md` first — do not ship the
broad live service-account token to a *public* Space without choosing a read-only role or a
snapshot DB.

## Guardrails — do NOT
- Do **not** commit or write the token into any repo file. `.env`, `*.token`, and `.pgfarm_token`
  are gitignored; keep it that way.
- Do **not** change the DB defaults (host/db/user) unless the user explicitly asks — they are
  correct for the corpus.
- There is **no** migration/seed/admin step. The app is read-only by design; do not attempt
  writes, schema changes, or to reach the internal/customer tables (the public-layer allowlist in
  `app/db.py` blocks them anyway).

## Map of the repo (for orientation)
| Path | What |
|------|------|
| `app/main.py` | FastAPI app + routes (`/`, `/health`, `/version`, `/api/*`) |
| `app/db.py` | read-only PG Farm pool, table allowlist, `healthcheck()` |
| `app/queries.py` | the corpus SQL (parameterized, allowlisted) |
| `app/koina.py` | Koina calls (predicted fragments / flyability) |
| `app/static/`, `app/templates/` | single-page frontend (Tailwind + Chart.js, no build) |
| `requirements.txt`, `Dockerfile` | runtime deps / container |
| `.env.example` | env-var template |
