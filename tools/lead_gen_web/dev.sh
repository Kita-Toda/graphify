#!/usr/bin/env bash
# Dev mode — hot-reload for both backend and frontend.
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$ROOT/../.." && pwd)"

trap 'kill $(jobs -p) 2>/dev/null; exit' INT TERM

echo ""
echo "  Spruce My Site — Lead Generator (DEV MODE)"
echo "  Backend  → http://localhost:8000  (auto-reload)"
echo "  Frontend → http://localhost:4321  (HMR)"
echo "  Press Ctrl+C to stop"
echo ""

# FastAPI with reload
(cd "$ROOT/backend" && PYTHONPATH="$REPO_ROOT" uvicorn main:app --reload --port 8000) &

# Astro dev with HMR
(cd "$ROOT/frontend" && API_URL=http://localhost:8000 npm run dev) &

wait
