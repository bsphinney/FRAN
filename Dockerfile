# DE-LIMP Corpus Browser — Hugging Face Space (Docker SDK)
# HF Spaces serve on port 7860.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=7860

WORKDIR /app

# psycopg2-binary needs no build deps; keep image lean.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# HF runs as a non-root user; nothing here writes to disk.
EXPOSE 7860

# Honor $PORT so the SAME image runs on HF (PORT=7860, set above) and on Azure App Service
# (set app setting WEBSITES_PORT=7860, or override PORT). Shell form so ${PORT} expands.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860}
