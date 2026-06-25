#!/usr/bin/env bash
# Spruce My Site — Lead Generator Web App
# Starts both the FastAPI backend (port 8000) and Astro frontend (port 4321).
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$ROOT/../.." && pwd)"

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  Spruce My Site — Lead Generator Web App"
echo "═══════════════════════════════════════════════════════"

# ── 1. Python deps ────────────────────────────────────────
echo ""
echo "[1/3] Installing Python dependencies…"
pip install fastapi "uvicorn[standard]" requests beautifulsoup4 --quiet

# ── 2. Node deps ──────────────────────────────────────────
echo "[2/3] Installing Node dependencies…"
cd "$ROOT/frontend"
npm install --silent

# ── 3. Build Astro ────────────────────────────────────────
echo "[3/3] Building Astro frontend…"
npm run build

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  Starting servers…"
echo "═══════════════════════════════════════════════════════"
echo ""
echo "  Backend  → http://localhost:8000"
echo "  Frontend → http://localhost:4321"
echo ""
echo "  Press Ctrl+C to stop both"
echo "═══════════════════════════════════════════════════════"
echo ""

# Kill children on Ctrl+C
trap 'kill $(jobs -p) 2>/dev/null; exit' INT TERM

# Start FastAPI backend
(cd "$ROOT/backend" && PYTHONPATH="$REPO_ROOT" python main.py) &

# Start Astro (built SSR node server)
(cd "$ROOT/frontend" && node dist/server/entry.mjs) &

wait
