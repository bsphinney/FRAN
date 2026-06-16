# Push to Hugging Face Spaces

The app is a **Docker Space** (`README.md` carries the HF metadata header:
`sdk: docker`, `app_port: 7860`). I can't push without your HF token — here's the
exact sequence for you.

## 1. Create the Space
On https://huggingface.co/new-space — Owner: your account, SDK: **Docker** (blank),
visibility: **Private** to start (see security note below).

## 2. Add the DB credential as a Space Secret (NOT a Variable)
Space → Settings → **Secrets** → New secret:
- Name: `DELIMP_PG_PASSWORD`
- Value: the current 7-day PG Farm token (contents of `.pgfarm_token`)

Optionally override defaults with Variables (host/db/user already default correctly):
`DELIMP_PG_HOST`, `DELIMP_PG_DB`, `DELIMP_PG_USER`, `DELIMP_PG_SSLMODE`.

> The 7-day token expires. For a long-lived Space, prefer a dedicated read-only
> credential (security option (a) in README.md) or a snapshot DB (option (b)).

## 3. Push the files
```bash
cd /Users/brettphinney/Documents/claude/corpus_browser
git init && git add -A && git commit -m "DE-LIMP Corpus Browser v0"
git remote add hf https://huggingface.co/spaces/<your-user>/delimp-corpus-browser
git push hf main          # paste your HF token as the password when prompted
```
(Use `dev_mock_preview.py` is dev-only; harmless to ship but you can delete it.)

## 4. Verify
The Space builds the Dockerfile and serves on 7860. Open it; the dashboard should
show live counts. `/health` should report `connected: true, read_only: on`.

## Security reminder
A **public** Space with `DELIMP_PG_PASSWORD` set means the running container can
reach the live DB using the broad service-account credential. Choose (a) a
dedicated read-only PG Farm credential, or (b) a periodic public-table snapshot,
before making the Space public. See README.md "Public-hosting security decision".
