# HRMS-Portal

Web application for the Human Resource Management System.

## Runtime profiles

- **Production cutover target:** PostgreSQL 17 schema `public` with Redis-backed
  sessions. Production defaults `APP_DB_SCHEMA` to `public` when
  `FLASK_ENV=production`; set `APP_DB=postgres`, `APP_DB_SCHEMA=public`,
  `DATABASE_URL`, and `REDIS_URL` explicitly in deployment secrets.
- **Development/compatibility:** DuckDB remains available locally. The
  PostgreSQL test harness uses the disposable `legacy` schema by default.
- **Rollback:** `docker-compose.legacy.yml` is the temporary DuckDB audit
  fallback profile (`docker compose --profile legacy-rollback -f
  docker-compose.legacy.yml up`). Do not remove the legacy data until the
  Phase 5 fallback window has elapsed.

## Start the PostgreSQL cutover profile

```bash
cp .env.example .env
# Set SECRET_KEY and, if needed, POSTGRES_* values.
docker compose up --build
```

Compose runs `alembic upgrade head` in a one-shot migration service before the
web process starts. The web service uses PostgreSQL `public` and Redis. A
fresh production target intentionally refuses demo seeding; load the approved
ETL data first. For a disposable local demo only, set
`HRMS_ALLOW_DEMO_SEED=1`.

If the host blocks Docker bridge networking (common in Codespaces/WSL), use the
included local override:

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
```

Then open <http://localhost:5000>. The local demo login is `EMP001` /
`pass123`; change or remove demo credentials before using real data.

## Cutover preflight

The preflight command is read-only and emits a JSON reconciliation report:

```bash
DATABASE_URL=postgresql://... \
python scripts/cutover_preflight.py \
  --schema public --legacy-schema legacy \
  --duckdb-file /data/hrms.duckdb \
  --require-legacy-read-only \
  --report reports/cutover-preflight.json
```

The actual final delta sync, maintenance-window health check, traffic switch,
and DuckDB read-only lock are operator actions documented in
[`docs/MIGRATION.md`](docs/MIGRATION.md). The preflight never drops a schema,
mutates data, or changes traffic.
