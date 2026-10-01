# HRMS-Portal

Web application for the Human Resource Management System.

## Runtime

**PostgreSQL 17 is the only backend.** The app runs on schema `public` (the v2.0
target) with Redis-backed sessions. Production defaults `APP_DB_SCHEMA` to
`public` when `FLASK_ENV=production`; set `APP_DB_SCHEMA=public`,
`DATABASE_URL`, and `REDIS_URL` explicitly in deployment secrets.

The PostgreSQL test harness uses the disposable `legacy` schema (the v1.0 shape
the application code still speaks) by default.

DuckDB was the original runtime and was removed at the Phase-6 decommission.
There is no `APP_DB` switch and no DuckDB rollback profile; see
[`docs/MIGRATION.md`](docs/MIGRATION.md) §"Phase 6".

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
  --report reports/cutover-preflight.json
```

The actual final delta sync, maintenance-window health check, and traffic
switch are operator actions documented in
[`docs/MIGRATION.md`](docs/MIGRATION.md). The preflight never drops a schema,
mutates data, or changes traffic.
