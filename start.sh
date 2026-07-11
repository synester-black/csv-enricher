#!/bin/bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# ------------------------------------------------------------------
# CSV Enricher - Quick Start
# ------------------------------------------------------------------
# For production:  docker compose up -d
# For development: bash start.sh
# ------------------------------------------------------------------

MODE="${1:-dev}"

if [ "$MODE" = "prod" ]; then
    echo "Starting in production mode (gunicorn)..."
    echo "Access at http://localhost:5000"
    export OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-https://ollama.com}"
    export OLLAMA_MODEL="${OLLAMA_MODEL:-gemma3:27b}"
    export OLLAMA_API_KEY="${OLLAMA_API_KEY:-6098bb9c2e4e4937bd784a8907357590.Zf7g2Sc3BNqB772039wUeY8j}"
    exec gunicorn --bind 0.0.0.0:5000 --workers 4 --timeout 300 app:app
fi

# Development mode
echo "================================="
echo " CSV Enricher - Dev Mode"
echo "================================="
echo " Login: admin / admin123"
echo " Ollama: https://ollama.com (gemma3:27b)"
echo " URL: http://localhost:5000"
echo "================================="

export FLASK_DEBUG=1
export OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-https://ollama.com}"
export OLLAMA_MODEL="${OLLAMA_MODEL:-gemma3:27b}"
export OLLAMA_API_KEY="${OLLAMA_API_KEY:-6098bb9c2e4e4937bd784a8907357590.Zf7g2Sc3BNqB772039wUeY8j}"
exec python3 app.py
