# HRMS v2.0 — Migration Runbook (Phases 0–4)

This runbook covers **Phase 0 (freeze & inventory)**, **Phase 1 (one-time ETL:
DuckDB → PostgreSQL)**, **Phase 2 (service-layer cutover: the existing app
runs on PostgreSQL)**, **Phase 3 (cross-cutting rules)**, and the completed
**Phase 4 capabilities (FR-JOB-01 attendance, FR-PAY-06 payroll, and corrected
FR-ATS/FR-ONB/FR-OFF lifecycle flows)** of the SRS v2.0 migration plan (§14).
Final cutover and DuckDB decommission remain tracked in §14 and the living
TO DO list.

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
  `effective_from = 1970-01-01`, with the employee's weekly-off pattern
  (default `Sat,Sun`). The v1.0 compatibility path keeps a
  `users.weekly_off_pattern` column; v2.0 reads the effective assignment and
  leave-policy fallback. Collect real employee patterns (SRS R-04) before
  relying on the job for payroll.
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

## 9. Phase 3b — CC-01 identity + public-flip readiness

### CC-01: identity keys (audit → enforced rule)
The 46 surrogate PKs in the target schema are already declared
`GENERATED BY DEFAULT AS IDENTITY`; 4 are intentional natural keys
(`users.emp_id`, `break_types.break_type`, `idempotency_keys.key`,
`alembic_version.version_num`). `db/postgres_schema.sql` records the later
flip to `GENERATED ALWAYS` once the whole service layer lands on v2.0.

This is now an enforced rule rather than a convention:

```bash
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/hrms \
  python scripts/check_cc_rules.py
# CC-01 OK — 46 identity + 4 natural keys, all sequences ahead of data
```

Plus a PG-gated unit test (`test_cc01_surrogate_keys_are_identity`): every
primary key is `a`/`d` identity unless on the natural-key whitelist, and the
50-table target schema is present.

### Public-flip probe (measured readiness)
`scripts/probe_public_flip.py` boots the *current* v1.0 app against the pure
v2.0 `public` schema on a throwaway database and measures the API surface:

```bash
docker exec hrms-pg psql -U postgres -c "DROP DATABASE IF EXISTS hrms_probe" \
    -c "CREATE DATABASE hrms_probe"
# The probe measures the app against the pure v2.0 schema — apply it first.
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/hrms_probe \
  alembic -c migrations/alembic.ini upgrade head
APP_DB=postgres APP_DB_SCHEMA=public \
  DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/hrms_probe \
  python scripts/probe_public_flip.py
```

**Result: 94/94 authenticated GET `/api/*` routes and 42/42 core write flows
serve unmodified from `public`** (login + CSRF included; write flows cover
start/end break, regularization, leave apply, notification read, Lunch break
approval request + admin approve, admin user creation, shift assignment,
FR-JOB-01 attendance finalisation, password reset, help-desk ticket
create/comment/resolve, ATS candidate → offer → accept, pre-boarding upload
and HR review, parallel offboarding with settlement maker-checker and LWD
revocation, payroll run create → Finance submit → Admin approve → finalize →
bank-file + TDS exports, and the CC-07 idempotency replay check — the same leave apply sent twice with
the same `Idempotency-Key` replays the stored response and leaves exactly one
row). `init_db` boots and fully self-seeds against the v2.0 schema — **zero
seed-time rejections**. That is a complete measured readiness picture, not a
leap of faith.

The extended write section (service-layer rewrite inc 3) was re-run against a
clean `hrms_probe` on 2026-09-24. It exposed and then fixed two adapter/export
issues: psycopg could not bind integer flag parameters to a v2.0 BOOLEAN
UPDATE, and the payroll bank-file writer was passing text to `BytesIO`. The
probe now also exercises the Finance-submit/Admin-approve payroll path and is
green on all 42 flows.

#### Adapter compat added this phase
Four small, schema-scoped pieces in `db_backend.py` made the flip possible.
All are strict no-ops on `legacy` (zero boolean columns, naive timestamps), so
Phase-2 behaviour is byte-identical (verified by the full unit + browser
suites on PostgreSQL):

1. `translate()` rewrites `col = 0|1|?` into boolean literals / `?::boolean`
   casts for columns that are *actually* BOOLEAN in the connected schema
   (introspected once per schema). Fixes e.g. `WHERE is_read = 0` and
   `UPDATE ... SET is_read = 1`.
2. `_coerce_insert_boolean_params()` co-ercies `int 0/1` params to `bool` for
   INSERTs into v2.0 flag columns (and rewrites literal `0/1` values);
   positionally maps the VALUES list to the column list.
3. `_coerce_boolean_comparison_params()` applies the same coercion to
   UPDATE/SELECT comparisons, so psycopg never sends a `smallint` into a
   `?::boolean` placeholder.
4. The row factory strips tzinfo from returned datetimes. v2.0 stores
   `TIMESTAMPTZ` for columns v1.0 code reads back for naive arithmetic
   (`datetime.now() - row[2]`); `legacy` already stores naive `TIMESTAMP`, so
   this restores the v1.0 round-trip contract on v2.0.

#### Sample-seed correction
`salary_structures` had two unbounded ranges on the same employee (EMP002),
which violates the v2.0 CC-05 `no_overlapping_structure` exclusion constraint.
The older sample structure now belongs to EMP001 — a data fix, not a schema one.

#### Service-layer rewrite (renames + shifted columns)

The rename work below (§8) is being landed incrementally so every commit
stays green on both the v1.0 (DuckDB/legacy) and v2.0 (`public`) shapes:

- **Inc 1 (done, `8ff66bc`)** — expanded `audit_log`
  (`actor/entity/entity_id/before/after/request_id`, CC-13) and
  `notifications.category` (FR-NOT-03).
- **Inc 2 (done)** — `shift_assignments` replaces `users.shift_start/end`
  on `public`. The app now goes through schema-scoped `get_shift`/`set_shift`
  helpers; `init_db` no longer runs `ALTER TABLE users ADD COLUMN shift_*` on
  the v2.0 shape (it was silently re-adding the columns the migration
  removed). All user CRUD, break timing and attendance workday math routes
  through the helpers; on `public` a user's shift is a single open-ended
  `shift_assignments` row (`Fixed` with TIME bounds, or `24x7`), on the v1.0
  shape it is exactly the old column write. Effective-dated scheduling
  (multiple periods) is Phase 4 (FR-ATT-17).
- **Inc 3 (done)** — extended the probe write section to 42 flows and fixed
  the INSERT/export drift it surfaced: `tickets`, `offer_letters` and
  `payroll_runs` use explicit column lists valid on both shapes; the Boolean
  UPDATE parameter and payroll bank-file fixes are now verified on `public`.
  The probe now exercises FR-JOB-01 against the v2.0 identity key and
  effective-dated shift assignment, FR-PAY-06's Finance-submit/Admin-approve
  path, and the complete guarded ATS → pre-boarding → offboarding journey.

### CC-09 — transactional outbox (implemented)
`outbox.py` implements the outbox pattern against the v2.0 `outbox_events`
infrastructure table (`identity` key, `JSONB` payload, `TIMESTAMPTZ`):
pending → delivered, or attempts + exponential backoff (30s base) →
`dead_letter` after `MAX_ATTEMPTS = 5`.

- **Atomicity**: `outbox.transaction()` wraps the business write and its
  event in one DB transaction on either backend — DuckDB via
  `BEGIN`/`COMMIT` (the python client tracks it, verified in tests), PG via a
  dedicated non-autocommit connection (`db_backend.transaction()`). A
  `DuckDBCompatConnection` wraps the PG transaction so the app's
  DuckDB-flavoured SQL still translates inside it.
- **Wired flows** (event enqueued on the same tx as the write):
  - `payroll.finalized` — `POST /api/payroll-runs/<id>/finalize` → handler
    inserts a payout notification per employee on the run
  - `offer.created` — `POST /api/offers` → handler emails the candidate
  - `offer.accepted` — `POST /api/offers/<id>/accept` → handler notifies HR/Admin
    and reconciles the hire event
  - `candidate.hired` — reconciles the pre-hire checklist idempotently after
    the atomic offer-acceptance transaction
  - `credentials.issued` — step-3 provisioning issues a 24-hour reset token
    and sends the new-hire credential notification
- **Dispatch**: a `BackgroundScheduler` job runs every 60s; admins can
  trigger a pass manually (`POST /api/admin/outbox/dispatch`) and monitor the
  queue (`GET /api/admin/outbox`).
- The `outbox_events` DDL is added to `init_db` (self-serving
  `CREATE TABLE IF NOT EXISTS`), so the pattern is exercised on every
  backend; on the v2.0 `public` schema it maps onto the existing
  infrastructure table untouched.

### CC-07 — idempotent writes (implemented)
`idempotency.py` exposes an `@idempotent` decorator for POST routes. Writes
that carry an `Idempotency-Key` header get at-most-once semantics, and are
verified against the *real* v2.0 `idempotency_keys` (`key` natural PK, `JSONB`
`response_body`, `TIMESTAMPTZ` `expires_at`):

- **Flow**: a fresh key claims a row (`response_status = 0` in-flight
  sentinel with a NULL body) → the handler runs → on success its JSON response
  is stored (`response_status` + `response_body`). A retry with the same key
  and same payload finds the stored row and **replays the response without
  re-running the handler** — a flaky client or job retry can no longer
  double-apply (duplicate leave, double finalise, duplicate break, ...).
- **Failure semantics**: failed attempts (4xx/5xx) release the claim so a
  retry starts clean; reusing a key with a *different* payload is a 409
  (client should mint a new key for new intent); concurrent duplicates hit
  the PK and get a 409.
- **Wired routes** (dedup-relevant POSTs): `/api/leaves`, `/api/regularization`,
  `/api/start-break`, `/api/break-approvals` (Lunch request),
  `/api/payroll-runs` (create) + `/finalize`, `/api/offers` + `/accept`,
  `/api/users` (create). Header-less requests pass through unchanged.
- **Lifecycle**: `init_db` adds `idempotency_keys` (`TEXT`/`TIMESTAMP`
  shape; a no-op on `public`), and the hourly `cleanup_expired_tokens` job
  now purges expired keys too (24h TTL).
- **Verification**: 6 unit tests (DuckDB + PG + PG+Redis), and an idempotency
  write-flow in the probe that replays the leave apply against pure v2.0
  `public` and asserts exactly one row.

## 10. Phase 4 — attendance finalisation (FR-JOB-01, implemented)

`finalize_attendance_for_date()` is the service entry point used by the
nightly scheduler. For every active employee it resolves the effective shift
and weekly-off pattern, then applies this priority:

```text
Holiday → On Leave → Weekly-off → Present → Half-day → Absent
```

- A National holiday always wins. An Optional holiday wins only when the
  employee has an Approved `holiday_optins` row and the holiday location
  matches the employee's effective leave-policy location (NULL means
  organisation-wide).
- A shift is measured from first login to last logout (not the sum of
  session rows). Open/orphaned sessions are capped at scheduled hours + 25%,
  and the credited value is rounded to two decimals.
- `Present` and `Half-day` use the scheduled shift length multiplied by
  `ATTENDANCE_FULL_DAY_RATIO` (default `1.0`) and
  `ATTENDANCE_HALF_DAY_RATIO` (default `0.5`). These are configurable policy
  thresholds, not a new schema constraint.
- `run_attendance_finalization()` groups employees by their own current shift
  date and calls the transactional replacement once per date. Re-running a
  date deletes and recreates that date's rows atomically; the v1.0 shape gets
  explicit integer IDs, while v2.0 `public` uses its identity sequence.
- `attendance_days` is the single finalized source consumed by the monthly
  calendar. Payroll/report consumers should use its stored status/hours for
  LOP rather than recalculating ad hoc.
- v1.0 adds a compatibility `users.weekly_off_pattern` column. v2.0 reads
  `shift_assignments.weekly_off_pattern`, then the effective
  `leave_policy_assignments` row, and finally the configured default.

Validation:

- **72 DuckDB unit tests passed / 5 skipped**.
- **76 PostgreSQL legacy unit tests passed / 1 skipped**; the same result is
  green with Redis sessions.
- **16 Playwright tests passed on both DuckDB and PostgreSQL**.
- Clean `hrms_probe`: **94/94 GET + 42/42 write flows**, including the
  attendance, payroll maker-checker, and corrected lifecycle paths, green
  against the pure v2.0 `public` schema.

## 11. Phase 4 — maker-checker payroll (FR-PAY-06, implemented)

Payroll now uses the strict lifecycle:

```text
Draft → Submitted → Approved → Finalized
```

- `POST /api/payroll-runs/<id>/submit` records the submitter and transitions
  only a Draft run.
- `POST /api/payroll-runs/<id>/approve` is restricted to Finance/Admin,
  requires Submitted status, and rejects the submitter as approver.
- `POST /api/payroll-runs/<id>/finalize` is restricted to an Approved run;
  it records the transition, writes the approval trail, and enqueues
  `payroll.finalized` atomically. Bank, TDS, and payslip exports reject
  non-Finalized runs.
- `payroll_approvals` stores `Submit`, `Approve`, and `Finalize` actions with
  actor and from/to status. Each successful transition also writes the
  expanded audit log.
- Payroll creation includes Active/Onboarding users with a salary structure
  effective for the requested period. An optional `adjustment_of_run_id`
  must reference a Finalized run; finalized items are never edited in place.
- The v1.0 compatibility schema receives the new columns/table in `init_db`;
  the v2.0 `public` schema is detected and left untouched. Finance/Admin UI
  access and the role selector are wired for the new lifecycle.

Validation:

- **72 DuckDB unit tests passed / 5 skipped**.
- **76 PostgreSQL legacy unit tests passed / 1 skipped**; PostgreSQL+Redis is
  also **76 passed / 1 skipped**.
- **16 Playwright tests passed on DuckDB and PostgreSQL**.
- Clean `hrms_probe`: **94/94 GET + 42/42 write flows**, including the
  Finance-submit/Admin-approve/finalize payroll path and the complete
  ATS → pre-boarding → offboarding lifecycle.

## 12. Phase 4 — corrected ATS/onboarding/offboarding (implemented)

The lifecycle now follows the SRS §6.5–6.7 flows on both the compatibility
schemas and the v2.0 `public` schema:

- **ATS (FR-ATS-01..04):** candidates move through
  `Applied → Screened → Interviewed → Offered → Hired`; `Rejected` and
  `Withdrawn` are explicit exits, direct `Hired` is rejected, and offer
  acceptance/rejection returns 404/409 for missing or already-decided offers.
  Salary splits use `Decimal` validation and must total 100. Acceptance is the
  only employee-conversion path and atomically creates the pre-hire, effective
  salary structure, onboarding workflow, five checklist rows, and outbox
  events. `GET /api/pipeline` reports stage and conversion counts.
- **Onboarding (FR-ONB-01..06):** accepted offers issue a signed 14-day
  pre-boarding token. The token-scoped page/API works without normal login,
  accepts real PDF/JPEG/PNG bytes only (extension/MIME/signature and malware
  test checks), and creates the checklist automatically. HR review requires an
  uploaded document and a note on rejection. Step transitions are guard-based;
  physical work requires completed tasks and step 5 is restricted to HR/Admin
  or the target's assigned buddy/manager. Step 3 issues a 24-hour reset token.
- **Offboarding (FR-OFF-01..03):** resignations are first-class records with
  independent stage-2/stage-3 clearance, zero-outstanding-assets gating, and a
  Finance prepare/approve settlement trail. F&F stores pending payroll, LOP,
  leave encashment, deductions, asset damage, and total. The nightly
  `offboarding-access-revocation` job runs at 00:00 Asia/Kolkata, atomically
  closes sessions, clears permissions, disables login, and marks employees
  Inactive on LWD.
- **Schema:** compatibility DDL is additive; canonical changes are in
  `db/postgres_schema.sql` and Alembic revisions `0002_lifecycle_workflows` plus
  `0003_lifecycle_hardening`, including task stage columns, workflow timestamps,
  exit-workflow linkage, strict offer-split constraints, `offboarding_approvals`,
  and `offboarding_settlements`.

## 13. Next steps

1. Phase 5 — final cutover: flip `APP_DB_SCHEMA` to `public`, reconcile final
   data, and retire the legacy schema.
2. Phase 6 — decommission the DuckDB runtime after the defined audit fallback.
3. Follow-up hardening — complete the separate FR-USR employee-management
   contract (archive/anonymization, policy-derived balances, and bulk jobs)
   without coupling those changes to the lifecycle milestone.
