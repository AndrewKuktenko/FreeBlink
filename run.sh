#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
[ -d .venv ] || { "$(command -v python3.12 || command -v python3.13 || echo python3)" -m venv .venv && .venv/bin/pip install -r requirements.txt; }
command -v "${FFMPEG:-ffmpeg}" >/dev/null || { echo "ffmpeg not found (brew install ffmpeg) - live view will not work"; }
eval "$(.venv/bin/python -c 'from app import config; print(f"HOST={config.HOST} PORT={config.PORT}")')"
exec .venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT"
