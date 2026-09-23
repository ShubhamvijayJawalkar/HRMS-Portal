# HRMS v2.0 — Migration Runbook (Phases 0–3)

This runbook covers **Phase 0 (freeze & inventory)**, **Phase 1 (one-time ETL:
DuckDB → PostgreSQL)** and **Phase 2 (service-layer cutover: the existing app
runs on PostgreSQL)** of the SRS v2.0 migration plan (§14). Later phases
(cross-cutting rules CC-01…CC-16, new capabilities, final cutover,
decommission) are tracked in §14 of the SRS and are out of scope for this
document.

> The schema is now **frozen**. Any change to the DuckDB v1.0 schema must be
> reviewed against this migration before it lands. Add changes here if you
> touch it.

---

## 1. What exists

| Layer | Before (v1.0) | After (v2.0 target) |
|-------|---------------|---------------------|
| Store | DuckDB single file (`hrms.duckdb`) | PostgreSQL 17 (`db/postgres_schema.sql`) |
| Migrations | ad-hoc ALTERs inside `init_db()` | Alembic (`migrations/`) |
| Time | naive local timestamps | `TIMESTAMPTZ`, UTC (CC-02) |
| Money | `DECIMAL(12,2)` | `NUMERIC(14,2)` (CC-03) |
| Constraints | app-level only | DB-enforced invariants (CC-05), partial unique, exclusion |

Artifacts created in this workstream:

- `db/postgres_schema.sql` — canonical target schema. **PART A** = tables/FKs;
  **PART B** = invariants activated only after data cleanup.
- `migrations/` — Alembic baseline (`0001_baseline`) that applies the canonical
  schema; `env.py` honours `DATABASE_URL`.
- `scripts/migrate_duckdb_to_postgres.py` — Phase-1 ETL with FK-safe loading,
  constraint cleanup, and two-phase reconciliation.
- `scripts/generate_data_dictionary.py` — regenerates `docs/data_dictionary.md`
  by introspecting the target DB (SRS §15: generated, not hand-maintained).

---

## 2. Phase 0 — Freeze & inventory

**Exit criteria (§14 Phase 0):** schema frozen; data dictionary exported;
target schema + Alembic baseline written and reviewed.

1. Freeze the DuckDB schema (see banner above).
2. Regenerate/export the data dictionary from the *final* target state:

   ```bash
   python scripts/generate_data_dictionary.py
   # → docs/data_dictionary.md
   ```

3. Review `db/postgres_schema.sql` with the team against SRS §7. Pay attention to:
   - mapping of shifted columns (`users.shift_start/end` → `shift_assignments`)
   - the legacy `onboarding_tasks` / `offboarding_tasks` / `exit_interviews`
     tables retained as-is while v2.0 workflow tables start empty (Phase 4)
   - `offer_letters` split backfilled to a validated 50/30/20 (was 50/20/20 = 90%, A-20)
   - money/time type upgrades (CC-02, CC-03)
   - new empty tables that Phase 3/4 will populate
     (`attendance_days`, `shift_assignments`, `resignations`, `outbox_events`,
     `idempotency_keys`, `mfa_credentials`, `user_permissions`, maker-checker, …)

---

## 3. Phase 1 — One-time ETL with reconciliation

**Prereqs**

```bash
pip install -r requirements.txt          # adds sqlalchemy, psycopg[binary], alembic
docker run -d --name hrms-pg \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=hrms \
  -p 55432:5432 \
  postgres:17
```

(Or point `DATABASE_URL` at any reachable PostgreSQL 15+.)

**Bootstrap the source file** (one-time — seeds `hrms.duckdb` via the v1.0 app):

```bash
timeout 20s python -c "import app"
```

**Run the ETL** — first in dry-run (report-only) mode:

```bash
DUCKDB_FILE=hrms.duckdb \
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/hrms \
python scripts/migrate_duckdb_to_postgres.py --reset
```

This loads all tables, runs the Phase-1 reconciliation (checksums), reports
constraint conflicts, and leaves PART B for after cleanup. Review the output
and `reports/migration_report_*.json`.

**Apply the documented cleanup, then enable the invariants:**

```bash
python scripts/migrate_duckdb_to_postgres.py --reset --apply-cleanup
```

The cleanup rules are deterministic and reviewable (each is reported with a row
count before it is applied):

| Rule | Why (constraint) | Action |
|------|------------------|--------|
| `dup_pending_break_approval` | `uq_pending_lunch_approval` (FR-ATT-05) | later Pending duplicates → `Rejected` |
| `dup_pending_regularization` | `uq_pending_regularization` (FR-REG-02) | later Pending duplicates → `Cancelled` |
| `overlapping_leave` | `no_overlapping_leave` exclusion (FR-LEA-02) | greedy keep-earliest; later overlaps → `Cancelled` |
| `dup_leave_balance` | `uq_balance` (FR-LEA-06) | merge into earliest row; delete rest |
| `dup_holiday` | duplicate rows (name+date+location) | keep earliest; delete exact dupes |
| `multi_active_break` | `uq_one_active_break` (FR-ATT-02) | keep latest Active; older → `Orphaned` |
| `overlap_salary_structure` | `no_overlapping_structure` (FR-PAY-02) | close earlier open-ended structures at (next `effective_from` − 1 day); observed in the v1.0 seed (A-10) |
| `dup_payroll_run` | `uq_payroll_period` (FR-PAY-05) | later duplicate runs → `Cancelled` |
| `dup_email` | `uq_users_email_ci` (FR-USR-02) | **REPORT ONLY** — needs a human decision |

`dup_email` (and anything the automation refuses to invent) blocks PART B and
must be resolved manually before the migration can complete — the runbook stops
with exit code `2` and explicit guidance instead of half-applying constraints.

**Exit criteria (§14 Phase 1)** — the run prints, and `reports/migration_report_*.json`
records:

- Phase-1 reconciliation **passes for every table** (loaded rows match the
  target: row counts + SHA-256 checksums). Any mismatch aborts with exit `1`.
- Constraint cleanup ledger reviewed and applied.
- PART B applied with no failures.
- Phase-2 counts recorded; identity sequences advanced (`setval`) so runtime
  inserts can't collide with migrated ids (subsumed by the CC-01 pass later).
- On success the target is **stamped as Alembic `head`** (skippable with
  `--no-stamp`), so the next migration chains onto `0001_baseline` correctly.

---

## 4. Phase 2 — Service-layer cutover (app runs on PostgreSQL)

**Goal (§14 Phase 2):** prove the existing application layer runs unchanged on
PostgreSQL *before* any schema restructure lands. Achieved with a thin
DuckDB-drop-in adapter — **no application query was modified**.

### What changed

- `db_backend.py` — dialect adapter used by `get_db()` when `APP_DB=postgres`:
  `?` → `%s`, `strftime('<f>', col)` → `to_char(col, '<pg>')` (Plus `%` → `%%`
  escaping whenever parameters are passed, as psycopg requires),
  autocommit-per-statement (DuckDB parity, so errors never poison the
  connection), and a no-op `PRAGMA` / `commit()`. Python result types
  (Decimal/float/naive datetime/date) match DuckDB exactly, so JSON/template
  handling is unchanged.
- `get_db()` in `app.py` — backend switch; default remains DuckDB.
- Test harness (`tests/test_app.py`, `tests/test_playwright.py`) — when
  `APP_DB=postgres`, the `legacy` schema is dropped and recreated *before*
  `app` is imported, giving a clean DB per run.

### Schema strategy

| Schema | Holds | Used by |
|--------|-------|---------|
| `public` | v2.0 **target** schema + ETL data (Phases 0–1) | Alembic, data dictionary, diffing |
| `legacy` | v1.0 data model re-created by `init_db()` on PG | the current app (per-connection `search_path`) |

The app's SQL matches the *v1.0* data model (e.g. `users.shift_start/shift_end`,
bare 6-column `leave_balance` inserts, 0/1 integer booleans), so Phase 2 serves
it from a v1.0-shaped schema on PostgreSQL while the v2.0 target stays intact
beside it — enabling side-by-side diffing until the Phase-3 service rewrite
switches `APP_DB_SCHEMA=public`.

### Run it

```bash
# Unit suite against PostgreSQL (fresh `legacy` schema per run)
APP_DB=postgres DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/hrms \
  python -m pytest tests/test_app.py -v

# Browser suite against PostgreSQL (~2 min)
APP_DB=postgres DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/hrms \
  python -m pytest tests/test_playwright.py -v

# DuckDB (default) — unchanged
python -m pytest tests/test_app.py -v
```

The `hrms-pg` container from Phase 1 is reused; a manual dev server runs under
the new stack with `APP_DB=postgres python app.py`.

### Exit criteria (§14 Phase 2)

- [x] Existing unit suite (29 tests) passes on PostgreSQL — **29/29**
- [x] DuckDB suite still passes (29/29) — no regression
- [x] Playwright browser suite runs on PostgreSQL (parity with DuckDB baseline)
- [x] Rollback: unset `APP_DB` → DuckDB path untouched (the adapter only
      activates under `APP_DB=postgres`)

### Notes / limits

- One connection per `get_db()` call (same as DuckDB today); pooling arrives
  with the Phase-3 service layer.
- The `legacy` schema is disposable — `db_backend.reset_schema()` recreates it;
  ETL'd `public` data is never touched by Phase-2 runs.
- psycopg3 has no `Connection.executemany`, so the adapter routes it through a
  cursor.

---

## 5. Phase 3a — Auth hardening (SRS CC-06)

Security lives in the Flask request pipeline (`security.py`), so it applies
identically on DuckDB and PostgreSQL; nothing here depends on the DB backend.

### Password hashing (Argon2id)

- New hashes use Argon2id (`m=19456 KiB, t=2, p=1` — OWASP floor).
- Legacy v1.0 bcrypt hashes still verify and are transparently re-hashed to
  Argon2id on the next successful login (`needs_rehash()` in the login route).
- The production boot-time seed check now *verifies* `pass123` against the two
  seed users instead of a never-matching string compare.

### CSRF

- A per-session token is issued on the first safe request and enforced on every
  POST/PUT/PATCH/DELETE via the `X-CSRF-Token` header, a `csrf_token` form
  field, or a JSON body key. A request is accepted without a token only while
  the session has none yet (bootstrap — nothing established to protect).
- `init_csrf()` injects a small `window.fetch` wrapper into every HTML response
  so browser AJAX sends the header automatically; native `<form>` elements
  carry a hidden `{{ csrf_token() }}` field.
- `GET /api/csrf-token` returns the session token for programmatic clients
  (anonymous by design; used by the test harness).
- Known trade-off: Swagger UI "Try it out" state-changing calls carry no token
  and return 403 — use the app UI instead.
- Login rate limit is now `LOGIN_RATE_LIMIT` (default `20 per minute`); the
  Playwright suite sets `60 per minute` so the green suite doesn't trip it.

### Server-side sessions (Redis)

- Set `REDIS_URL` (e.g. `redis://localhost:56379/0`) to store Flask sessions in
  Redis; the cookie then holds only an opaque session id. Default (unset) keeps
  signed cookies, so dev/CI needs no Redis.
- TTL = `PERMANENT_SESSION_LIFETIME` (8 h); logout deletes the server copy.

### Run it

```bash
REDIS_URL=redis://localhost:56379/0 python -m pytest tests/test_app.py -v  # unit on Redis sessions
```

### Exit criteria (§14 CC-06)

- [x] Unit suite (29 tests) green on DuckDB, PostgreSQL, and with Redis sessions
- [x] Playwright browser suite 15/15 on PostgreSQL (incl. the admin create-user flow)
- [x] Session lifecycle proof: token seeded → login stored in Redis → cookie is
      opaque → logout deletes the server-side session
- [x] Default path unchanged when `REDIS_URL` / `APP_DB` are unset

---

## 6. Fresh-environment alternative (Alembic)

To build the target schema from scratch (e.g. a preview/staging DB with no
legacy data):

```bash
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/hrms_fresh \
  alembic -c migrations/alembic.ini upgrade head
```

---

## 7. Rollback

- **Before** `--apply-cleanup` was run on your migration copy: drop the target
  schema (`DROP SCHEMA public CASCADE`) and re-run `--reset`.
- The source DuckDB file is never mutated — it remains the pristine reference.
- An individual cleanup rule can be inspected from the report JSON before it is
  applied; the ledger records exactly what changed and how many rows.

---

## 8. Known v1.0 → v2.0 mapping decisions

- `users.shift_start/shift_end` → one `shift_assignments` row per employee,
  `effective_from = 1970-01-01`, weekly-off default `Sat,Sun`. Collect real
  weekly-off patterns (SRS R-04) before Phase 3.
- `notifications.type` becomes `notifications.category` (preference key,
  FR-NOT-03); `type` value is copied across.
- `tickets.category` is copied to `tickets.queue` (`IT` fallback); both kept.
- `audit_log` gains `{actor, entity, entity_id, before, after, request_id}`;
  `actor` backfilled from the legacy `emp_id`.
- `offer_letters` gets a validated legacy split `50/30/20` so `split_sums_100`
  holds for historical offers (A-20).
- Legacy onboarding/offboarding tasks + exit interviews are preserved verbatim;
  the v2.0 workflow tables (`onboarding_workflow`, `onboarding_checklist`,
  `resignations`, `offboarding_workflow`) are created empty for Phase 4.

## 9. Next steps (Phase 3b →)

1. Phase 3b — remaining CC rules: CC-01 (`setval` → `GENERATED ALWAYS` identity
   + flip the service layer onto the v2.0 `public` schema), outbox (CC-09),
   idempotency (CC-07).
2. Phase 4 — new capabilities: attendance finalisation job (FR-JOB-01),
   maker-checker payroll (FR-PAY-06), corrected ATS/onboarding/offboarding flows.
3. Phase 5 — cutover; Phase 6 — decommission DuckDB.