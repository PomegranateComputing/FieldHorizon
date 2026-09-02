#!/usr/bin/env bash
set -euo pipefail

echo "=== Python ==="
python3 --version
python3 -m pip --version || true

echo "=== SQLite ==="
sqlite3 --version || echo "sqlite3 missing"

echo "=== Ollama ==="
ollama --version || echo "ollama missing"
ollama list || true

echo "=== Project ==="
pwd
find . -maxdepth 2 -type f | sort
