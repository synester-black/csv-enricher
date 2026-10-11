---
title: CSV Enricher
emoji: 🚀
colorFrom: indigo
colorTo: gray
sdk: docker
app_port: 7860
---

# CSV Enricher

Enrich lead CSVs with company software stack, funding intent, and lead validation using Ollama LLM.

## Setup on Hugging Face

1. Fork this repo or connect it at https://huggingface.co/new-space
2. Set **SDK** → **Docker**
3. Add these **Secrets** in Settings → Repository Secrets:

| Name | Value |
|---|---|
| `OLLAMA_BASE_URL` | `https://ollama.com` |
| `OLLAMA_MODEL` | `gemma3:27b` |
| `OLLAMA_API_KEY` | *your Ollama API key* |
| `SECRET_KEY` | *(generate a random 64-char string)* |

## Database (optional)

Uploads and the master company cache can be persisted to Postgres. Set
`DATABASE_URL` to a connection string (e.g. Neon). Known companies are resolved
from the DB first; the model only fills missing fields.

```bash
# create the schema and (re)load the master database workbook
python -c "import db; db.init_schema()"
python ingest_master.py "MASTER DATABASE Oct 2026.xlsx" --create
```

## Usage

1. Open your Space URL
2. Log in with `admin` / `admin123`
3. Configure Ollama in **Settings** (API key, model)
4. Upload a CSV / XLSX / XLS → map columns → processing starts automatically
5. Download enriched CSV when done

## Local Development

```bash
docker compose up
# or
pip install -r requirements.txt
python app.py
```
