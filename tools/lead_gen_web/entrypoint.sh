#!/usr/bin/env sh
# Starts all three processes: FastAPI backend, Astro SSR, nginx (foreground).
set -e

PORT="${PORT:-8080}"

echo "[entrypoint] Starting FastAPI backend on :8000…"
PYTHONPATH=/app /venv/bin/uvicorn main:app \
    --host 127.0.0.1 --port 8000 --workers 2 &

echo "[entrypoint] Starting Astro SSR on :4321…"
HOST=127.0.0.1 PORT=4321 node /app/frontend/dist/server/entry.mjs &

# Give both services a moment to bind before nginx starts accepting traffic
sleep 2

# Replace nginx.conf PORT placeholder
sed -i "s/\${PORT:-8080}/$PORT/g" /etc/nginx/nginx.conf

echo "[entrypoint] Starting nginx on :$PORT…"
exec nginx -g "daemon off;"
