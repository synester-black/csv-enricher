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
| `OLLAMA_API_KEY` | `6098bb9c2e4e4937bd784a8907357590.Zf7g2Sc3BNqB772039wUeY8j` |
| `SECRET_KEY` | *(generate a random 64-char string)* |

## Usage

1. Open your Space URL
2. Log in with `admin` / `admin123`
3. Configure Ollama in **Settings** (API key, model)
4. Upload a CSV → map columns → processing starts automatically
5. Download enriched CSV when done

## Local Development

```bash
docker compose up
# or
pip install -r requirements.txt
python app.py
```
