FROM python:3.12-slim AS production

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt

COPY . .

RUN groupadd --system hrms && useradd --system --gid hrms --home-dir /app hrms \
    && mkdir -p /data /app/uploads \
    && chown -R hrms:hrms /app /data
USER hrms

ENV FLASK_DEBUG=0
ENV FLASK_ENV=production
ENV PYTHONUNBUFFERED=1
# Phase 5 production target. Override APP_DB/APP_DB_SCHEMA only for the
# explicitly retained DuckDB rollback image.
ENV APP_DB=postgres
ENV APP_DB_SCHEMA=public
ENV DB_FILE=/data/hrms.duckdb
ENV PORT=10000

EXPOSE 10000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -f http://localhost:10000/login || exit 1

CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:10000", "--workers", "1", "--timeout", "120"]
