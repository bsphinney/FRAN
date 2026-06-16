# FRAN — Fragment Reference & ANnotation

**A live, DIA-only proteomics corpus browser.** FRAN is a read-only public window onto a
growing PG Farm corpus of DIA-MS results — browse by **peptide, protein, gene, organism, or
run**, see real **MS2 spectra**, **XIC chromatograms**, **ion mobility (1/K₀)**, predicted
**fragment intensities** (Koina), and predicted **flyability** (Koina PFly).

🔗 **Live app:** https://huggingface.co/spaces/brettsp/delimp-corpus-browser
🌐 **About page:** https://bsphinney.github.io/FRAN/

GPMDB-inspired in *function*, but built for 2026 and deliberately **DIA, not DDA** — which
is what sets it apart: cross-run indexed RT (iRT), the ion-mobility dimension, and full
fragment/XIC data kept for reuse and AI training.

![dashboard](docs/screenshot_dashboard.png)

## Highlights
- **Live dashboard** — counts (precursors / peptides / proteins / genes / runs / organisms /
  IM-bearing precursors) that auto-refresh as ingest proceeds; species, platform, engine and
  charge distributions; an **IM × iRT** map (toggles to an iRT × peptide-count histogram);
  and a **flyability × intensity** scatter.
- **Peptide page** — modified forms (ProForma) × charge, per-run RT / 1/K₀ / m/z / q-value /
  intensity, dual-pane **XIC** (MS1 + top quant fragments), **predicted vs acquired** mirror
  spectra (Koina Prosit / AlphaPeptDeep / ms2pip), shared-transition **interference**, and
  **predicted flyability** (PFly).
- **Protein & gene pages** — observed peptides with sequence-mapped coverage, per-search/run
  stats, plain-language summaries, STRING links.
- **Ion-mobility showcase**, **search/run browser**, and a word-hunt for English words hidden
  in peptide sequences.

## Architecture
- **Backend:** FastAPI + psycopg2 against PG Farm, with a **read-only** connection pool and a
  **table allowlist** so the public layer structurally cannot reach the internal/customer
  tables (`app/db.py`).
- **Frontend:** single-page app — Tailwind + Chart.js, no build step (`app/static/app.js`,
  `app/templates/index.html`).
- **External models:** Koina (Wilhelm lab) for fragment-intensity and flyability prediction
  (`app/koina.py`).
- **Packaging:** Docker; deployed as a Hugging Face Docker Space (port 7860).

## Security & governance (`app/db.py`)
1. **Read-only sessions** (`default_transaction_read_only=on`); only `SELECT`/`WITH` allowed.
2. **Public-layer allowlist** — every query declares its tables, validated against
   `PUBLIC_TABLES`; internal/customer tables are absent and unreachable.
3. **Parameterized queries only** — no raw or string-interpolated user SQL.
4. **Credentials via env / Space secrets** — never committed. See `README_HF.md` for the
   public-hosting credential decision (use a dedicated read-only role or a snapshot DB).

## Run locally
```bash
pip install -r requirements.txt
export DELIMP_PG_TOKEN_FILE=/path/to/.pgfarm_token   # or export DELIMP_PG_PASSWORD / DELIMP_PG_SECRET
uvicorn app.main:app --reload --port 7860
# open http://localhost:7860
```
Configuration env vars and the Hugging Face deploy notes are in **`README_HF.md`** and
**`DEPLOY_HF.md`**.

## What FRAN is part of
FRAN is the public corpus browser for **[DE-LIMP](https://github.com/bsphinney/DE-LIMP)**, a
Shiny proteomics pipeline for DIA-NN data. Searches analyzed in DE-LIMP are ingested into the
shared corpus that FRAN serves.

## License
MIT
