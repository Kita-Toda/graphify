# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 — Build Astro frontend
# Build context must be the repo root:
#   docker build -f tools/lead_gen_web/Dockerfile -t spruce-lead-gen .
# ─────────────────────────────────────────────────────────────────────────────
FROM node:22-alpine AS frontend-builder

WORKDIR /build
COPY tools/lead_gen_web/frontend/package*.json ./
RUN npm ci --silent

COPY tools/lead_gen_web/frontend/ ./

# Empty API_URL → Astro uses relative URLs (/api/…) in the browser,
# which nginx then proxies to the FastAPI backend.
ENV API_URL=""
RUN npm run build


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — Production runtime (Node + Python + nginx)
# ─────────────────────────────────────────────────────────────────────────────
FROM node:22-slim

# System packages
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
        nginx curl \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Python virtualenv so pip doesn't conflict with system packages
RUN python3 -m venv /venv
ENV PATH="/venv/bin:$PATH"

# Python deps
COPY tools/lead_gen_web/backend/requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# ── App source ────────────────────────────────────────────────────────────────
WORKDIR /app

# FastAPI backend
COPY tools/lead_gen_web/backend/ ./backend/

# Lead-gen engine (imported by backend)
COPY tools/lead_gen/ ./tools/lead_gen/
RUN touch ./tools/__init__.py

# Astro SSR built output from Stage 1
COPY --from=frontend-builder /build/dist ./frontend/dist

# ── Config ────────────────────────────────────────────────────────────────────
COPY tools/lead_gen_web/nginx.conf      /etc/nginx/nginx.conf
COPY tools/lead_gen_web/entrypoint.sh  /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && rm -f /etc/nginx/sites-enabled/default

# ── Runtime ───────────────────────────────────────────────────────────────────
EXPOSE 8080
ENV PORT=8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:${PORT}/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
