# ── Stage 1: build the React UI ────────────────────────────────────────
# Kept separate from the Python image so the ~200 MB node_modules never enters
# the runtime layer. The build needs no network and no database: `tsc` and
# `vite build` are pure functions of the source, which is what makes this stage
# reproducible and the runtime image small.
FROM node:24-slim AS ui

WORKDIR /build

# Manifest first, so `npm ci` is cached until a dependency actually changes.
COPY frontend/package.json frontend/package-lock.json* ./
# npm >= 11 blocks postinstall scripts unless they are approved. esbuild's
# postinstall is what places the platform binary Vite needs -- without approving
# it the build fails with a confusing "esbuild: Failed to install platform
# binary" rather than anything about a build step.
RUN npm install-scripts approve esbuild || true
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund

COPY frontend/ ./
# Writes to ../static/app, which is outside /build, so the output directory is
# created and owned here rather than assumed.
RUN mkdir -p /static/app \
 && npm run build \
 && test -f /static/app/index.html

# ── Stage 2: the application ───────────────────────────────────────────
FROM python:3.12-slim AS production

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt

COPY . .
# The built SPA, from stage 1. Copied after the source so a frontend-only change
# does not invalidate the (slow) pip install layer.
COPY --from=ui /static/app ./static/app

RUN groupadd --system hrms && useradd --system --gid hrms --home-dir /app hrms \
    && mkdir -p /data /app/uploads \
    && chown -R hrms:hrms /app /data
USER hrms

ENV FLASK_DEBUG=0
ENV FLASK_ENV=production
ENV PYTHONUNBUFFERED=1
# The production target is the v2.0 schema. PostgreSQL is the only backend:
# DuckDB was removed at the Phase-6 decommission.
ENV APP_DB_SCHEMA=public
ENV PORT=10000

EXPOSE 10000

# The SPA entry point, not /login: a React build that was never produced should
# turn the health check red rather than let the image look healthy while /app/
# answers 503.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -f http://localhost:10000/app/ || exit 1

CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:10000", "--workers", "1", "--timeout", "120"]