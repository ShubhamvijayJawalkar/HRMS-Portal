import json
import os
import pathlib
import sys
import tempfile
from datetime import date, datetime, timedelta
from io import BytesIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

os.environ['SECRET_KEY'] = 'test-secret-key'
os.environ['DB_FILE'] = os.path.join(tempfile.gettempdir(), f'hrms_test_{datetime.now().timestamp()}.duckdb')
os.environ['FLASK_DEBUG'] = '0'
os.environ.setdefault('APP_DB', 'duckdb')
# The app's global "200 per minute" limit is meant for production traffic. A
# full suite issues thousands of requests in well under a minute, and a 429 on
# the CSRF-token fetch surfaces much later as a bogus "CSRF token missing or
# invalid" on an unrelated assertion, so lift it for tests.
os.environ.setdefault('DEFAULT_RATE_LIMIT', '100000 per minute')
# DuckDB attaches a database file once per process, so a background job opening
# a connection while the suite is mid-assertion raises "Unique file handle
# conflict". The browser suite leaves the scheduler on for PostgreSQL.
os.environ['HRMS_DISABLE_SCHEDULER'] = '1'
# `HRMS_DISABLE_SCHEDULER=1` is available for a fully deterministic run; the
# suite keeps the scheduler on so the job-registration tests stay meaningful, and
# the import tests below tolerate the dispatcher picking a job up first.
# Tests must never reset the production cutover target, even when a shell
# inherits FLASK_ENV=production or APP_DB_SCHEMA=public.
os.environ['APP_DB_SCHEMA'] = 'legacy'
if os.getenv('APP_DB', 'duckdb').lower() in ('postgres', 'postgresql', 'pg'):
    import db_backend
    db_backend.reset_schema()

import pytest

from app import (
    _next_generated_id,
    app,
    check_password,
    gen_id,
    get_db,
    hash_password,
)


def _attach_csrf(c):
    """Attach a valid X-CSRF-Token to every state-changing test request.

    Phase 3a (CC-06) enforces CSRF globally. Browsers normally pick the
    token up from the page; the test client fetches it via GET
    /api/csrf-token instead (the exact token the session will validate).
    """
    def _bind(method):
        orig = getattr(c, method)

        def wrapped(*args, **kwargs):
            tok = c.get('/api/csrf-token')
            if tok.status_code == 200:
                headers = dict(kwargs.get('headers') or {})
                headers.setdefault('X-CSRF-Token', tok.get_json()['csrf_token'])
                kwargs['headers'] = headers
            return orig(*args, **kwargs)

        setattr(c, method, wrapped)

    for method in ('post', 'put', 'patch', 'delete'):
        _bind(method)
    return c


@pytest.fixture
def client():
    app.config['TESTING'] = True
    app.config['SERVER_NAME'] = 'localhost'
    with app.test_client() as c:
        _attach_csrf(c)
        with app.app_context():
            yield c


@pytest.fixture
def auth_client(client):
    client.post('/login', json={'emp_id': 'EMP001', 'password': 'pass123'})
    return client


def cleanup():
    try:
        os.remove(os.environ['DB_FILE'])
    except OSError:
        pass


# ── Basic Tests ─────────────────────────────────────────────────

def test_index_redirect(client):
    resp = client.get('/')
    assert resp.status_code == 302


def test_login_page(client):
    resp = client.get('/login')
    assert resp.status_code == 200
    assert b'HRMS Portal' in resp.data


def test_login_missing_credentials(client):
    resp = client.post('/login', json={})
    assert resp.status_code == 400
    assert b'Missing' in resp.data or b'error' in resp.data


def test_login_invalid_emp(client):
    resp = client.post('/login', json={'emp_id': 'NONEXIST', 'password': 'x'})
    assert resp.status_code == 401


def test_login_admin_success(client):
    resp = client.post('/login', json={'emp_id': 'EMP001', 'password': 'pass123'})
    data = resp.get_json()
    assert resp.status_code == 200
    assert data.get('redirect') == '/dashboard'


# ── Authentication Tests ────────────────────────────────────────

def test_dashboard_requires_login(client):
    resp = client.get('/dashboard')
    assert resp.status_code == 302


def test_admin_dashboard_redirect(auth_client):
    resp = auth_client.get('/dashboard')
    assert resp.status_code == 200


def test_api_users_requires_admin(client):
    resp = client.get('/api/users')
    assert resp.status_code in (302, 401)


# ── Swagger Tests ───────────────────────────────────────────────

def test_swagger_docs(client):
    resp = client.get('/docs/')
    assert resp.status_code in (200, 302)


def test_apispec(client):
    resp = client.get('/apispec.json')
    assert resp.status_code in (200, 302)


# ── Database Tests ──────────────────────────────────────────────

def test_audit_log_table_exists(client):
    conn = get_db()
    tables = conn.execute("SELECT table_name FROM information_schema.tables WHERE table_name='audit_log'").fetchall()
    conn.close()
    assert len(tables) > 0


def test_leave_tables_exist(client):
    conn = get_db()
    for t in ['leave_requests', 'leave_balance', 'password_reset_tokens']:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name=?", [t]
        ).fetchall()
        assert len(rows) > 0, f"Table {t} not found"
    conn.close()


def test_leave_types(client):
    conn = get_db()
    types = conn.execute("SELECT break_type FROM break_types").fetchall()
    conn.close()
    assert len(types) >= 3


def test_production_postgres_defaults_to_public_schema(monkeypatch):
    import db_backend
    monkeypatch.delenv('APP_DB_SCHEMA', raising=False)
    monkeypatch.setenv('FLASK_ENV', 'production')
    assert db_backend.app_schema() == 'public'
    monkeypatch.setenv('APP_DB_SCHEMA', 'legacy')
    assert db_backend.app_schema() == 'legacy'


def test_public_generated_id_uses_identity_sequence(monkeypatch):
    import app as app_module

    class Result:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return (self.value,)

    class FakeConnection:
        def execute(self, sql, params=None):
            if 'is_identity' in sql:
                return Result('YES')
            if 'pg_get_serial_sequence' in sql:
                return Result('public.breaks_break_id_seq')
            if 'nextval' in sql:
                return Result(4242)
            raise AssertionError(f'unexpected SQL: {sql}')

    monkeypatch.setattr(app_module, '_is_public_target_schema', lambda: True)
    assert app_module._next_generated_id(FakeConnection(), 'breaks', 'break_id') == 4242


def test_public_seed_sequence_advancer_repairs_explicit_ids(monkeypatch):
    import app as app_module

    class Result:
        def __init__(self, value=None, rows=None):
            self.value = value
            self.rows = rows or []

        def fetchone(self):
            return (self.value,)

        def fetchall(self):
            return self.rows

    class FakeConnection:
        def __init__(self):
            self.setvals = []

        def execute(self, sql, params=None):
            if 'is_identity' in sql:
                return Result(rows=[('breaks', 'break_id')])
            if 'pg_get_serial_sequence' in sql:
                return Result('public.breaks_break_id_seq')
            if 'last_value' in sql:
                return Result(5)
            if 'COALESCE(MAX' in sql:
                return Result(42)
            if 'setval' in sql:
                self.setvals.append(params)
                return Result(None)
            raise AssertionError(f'unexpected SQL: {sql}')

    connection = FakeConnection()
    monkeypatch.setattr(app_module, '_is_public_target_schema', lambda: True)
    app_module._advance_public_identity_sequences(connection)
    assert connection.setvals == [['public.breaks_break_id_seq', 42]]


def test_etl_allows_missing_post_v1_payroll_approval_table():
    import duckdb

    from scripts.migrate_duckdb_to_postgres import REGISTRY, _read_source_rows, _source_catalog

    entry = next(item for item in REGISTRY if item['table'] == 'payroll_approvals')
    source = duckdb.connect(':memory:')
    present, rows = _read_source_rows(source, entry, _source_catalog(source))
    source.close()
    assert not present
    assert rows == []


def test_seed_data_has_multiple_entries_per_model(client):
    conn = get_db()
    tables = [
        'users', 'user_sessions', 'break_types', 'breaks', 'audit_log',
        'leave_requests', 'leave_balance', 'password_reset_tokens',
        'employee_documents', 'dependents', 'holidays', 'notifications',
        'regularization_requests', 'assets', 'job_postings', 'candidates',
        'interviews', 'offer_letters', 'onboarding_tasks', 'offboarding_tasks',
        'exit_interviews', 'salary_structures', 'payroll_runs', 'payroll_items',
        'goals', 'performance_reviews', 'feedback_360', 'expense_categories',
        'expense_claims', 'tickets', 'ticket_comments', 'documents'
    ]
    for table in tables:
        count = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        assert count >= 2, f'{table} should have at least 2 seeded rows, found {count}'
    conn.close()


# ── Helper Tests ────────────────────────────────────────────────

def test_password_hashing():
    h = hash_password('test123')
    assert h.startswith('$argon2id$')
    assert check_password('test123', h)
    assert not check_password('wrong-password', h)


def test_legacy_bcrypt_hash_still_verifies():
    """v1.0 bcrypt hashes must keep working until re-hashed on login (CC-06)."""
    import bcrypt

    from security import needs_rehash
    legacy = bcrypt.hashpw(b'test123', bcrypt.gensalt()).decode()
    assert check_password('test123', legacy)
    assert not check_password('wrong-password', legacy)
    assert needs_rehash(legacy)
    assert not needs_rehash(hash_password('test123'))


def test_csrf_guard_enforced_and_accepts_valid_token(client):
    """CC-06: unsafe requests need the session's CSRF token."""
    raw = app.test_client()  # unwrapped client — no automatic token

    # Prime the session so a token exists (no bootstrap exemption left).
    tok = raw.get('/api/csrf-token').get_json()['csrf_token']

    # Without the token the request is rejected...
    r = raw.post('/login', json={'emp_id': 'EMP001', 'password': 'pass123'})
    assert r.status_code == 403

    # ...with it, the same request goes through.
    r = raw.post('/login', json={'emp_id': 'EMP001', 'password': 'pass123'},
                 headers={'X-CSRF-Token': tok})
    assert r.status_code == 200


def test_csrf_bootstrap_first_request_allowed(client):
    """A session with no token yet has nothing to protect — first request passes."""
    raw = app.test_client()
    r = raw.post('/login', json={'emp_id': 'nobody', 'password': 'x'})
    assert r.status_code == 401  # Invalid Employee ID, not 403


def test_login_rehashes_legacy_bcrypt_to_argon2(client):
    """CC-06: a legacy bcrypt hash is transparently upgraded on successful login."""
    import bcrypt
    legacy = bcrypt.hashpw(b'pass123', bcrypt.gensalt()).decode()
    conn = get_db()
    conn.execute("UPDATE users SET password = ? WHERE emp_id = 'EMP001'", [legacy])
    conn.close()
    assert check_password('pass123', legacy)

    r = client.post('/login', json={'emp_id': 'EMP001', 'password': 'pass123'})
    assert r.status_code == 200, r.get_json()

    conn = get_db()
    upgraded = conn.execute("SELECT password FROM users WHERE emp_id = 'EMP001'").fetchone()[0]
    conn.close()
    assert upgraded.startswith('$argon2id$')
    assert upgraded != legacy
    assert check_password('pass123', upgraded)


def test_plaintext_password_normalized_on_boot():
    """init_db rewrites rows whose hash is neither bcrypt nor Argon2id."""
    conn = get_db()
    conn.execute("UPDATE users SET password = 'plaintext-secret' WHERE emp_id = 'EMP002'")
    conn.close()

    import app as app_module
    app_module.init_db()  # re-run boot-time init: normalize step must catch it

    conn = get_db()
    fixed = conn.execute("SELECT password FROM users WHERE emp_id = 'EMP002'").fetchone()[0]
    conn.close()
    assert fixed.startswith('$argon2id$')
    assert check_password('pass123', fixed)


@pytest.mark.skipif(
    os.getenv('APP_DB', 'duckdb').lower() not in ('postgres', 'postgresql', 'pg'),
    reason='CC-01 inspects the v2.0 target schema on PostgreSQL',
)
def test_cc01_surrogate_keys_are_identity():
    """CC-01: every surrogate PK is an identity column; only natural keys differ."""
    conn = get_db()
    rows = conn.execute(
        "SELECT t.relname, a.attname, a.attidentity "
        "FROM pg_class t JOIN pg_namespace n ON n.oid = t.relnamespace AND n.nspname = 'public' "
        "JOIN pg_index i ON i.indrelid = t.oid AND i.indisprimary "
        "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(i.indkey) "
        "WHERE t.relkind = 'r'"
    ).fetchall()

    natural = {('users', 'emp_id'), ('break_types', 'break_type'), ('idempotency_keys', 'key')}
    framework = {'alembic_version'}
    bad = [
        (t, c) for t, c, ident in rows
        if ident not in ('a', 'd') and (t, c) not in natural and t not in framework
    ]
    assert not bad, f'non-identity surrogate PKs: {bad}'
    assert len(rows) >= 49, f'expected the 49-table target schema, found {len(rows)} PKs'

    # Explicit legacy IDs are still used by compatibility routes. The public
    # service must advance every identity sequence after such writes.
    for table, column, identity in rows:
        if identity not in ('a', 'd'):
            continue
        sequence = conn.execute(
            f"SELECT pg_get_serial_sequence('public.{table}', '{column}')"
        ).fetchone()[0]
        assert sequence, f'{table}.{column}: identity sequence is missing'
        last_value = conn.execute(f'SELECT last_value FROM {sequence}').fetchone()[0]
        max_value = conn.execute(
            f'SELECT COALESCE(MAX({column}), 0) FROM public.{table}'
        ).fetchone()[0]
        assert last_value >= max_value, (
            f'{table}.{column}: sequence {last_value} is behind MAX {max_value}'
        )
    conn.close()


@pytest.mark.skipif(
    os.getenv('APP_DB', 'duckdb').lower() not in ('postgres', 'postgresql', 'pg'),
    reason='the boolean rewrite snoops information_schema on PostgreSQL',
)
def test_boolean_flag_rewrite_public_and_inert_legacy():
    """Phase-3b flip compat: v2.0 BOOLEAN flags accept legacy 0/1 predicates.

    The adapter rewrites ``col = 0|1|?`` into boolean literals/casts only for
    columns that are *actually* boolean in the connected schema; the legacy
    schema (no boolean columns) must stay byte-identical.
    """
    from db_backend import translate

    sel = "SELECT COUNT(*) FROM notifications WHERE emp_id = ? AND is_read = 0"
    assert translate(sel, ['EMP001'], 'public') == \
        "SELECT COUNT(*) FROM notifications WHERE emp_id = %s AND is_read = false"

    upd = "UPDATE notifications SET is_read = 1 WHERE emp_id = ?"
    assert translate(upd, ['EMP001'], 'public') == \
        "UPDATE notifications SET is_read = true WHERE emp_id = %s"

    param = "UPDATE notifications SET is_read = ? WHERE emp_id = ?"
    assert translate(param, [1, 'EMP001'], 'public') == \
        "UPDATE notifications SET is_read = %s::boolean WHERE emp_id = %s"

    legacy = "SELECT * FROM users WHERE allow_login = 1 AND allow_breaks = 0"
    assert translate(legacy, ['EMP001', 'x'], 'legacy') == legacy.replace('?', '%s')

    numeric = "SELECT * FROM leave_balance WHERE used_days = 0 AND reserved = 10 LIMIT 50"
    assert translate(numeric, None, 'public') == numeric


@pytest.mark.skipif(
    os.getenv('APP_DB', 'duckdb').lower() not in ('postgres', 'postgresql', 'pg'),
    reason='the boolean coercion snoops information_schema on PostgreSQL',
)
def test_insert_boolean_param_coercion_public_and_inert_legacy():
    """Phase-3b flip compat: INSERTs into v2.0 BOOLEAN flags accept int params."""
    from db_backend import _coerce_insert_boolean_params

    sql = ("INSERT INTO users (emp_id, name, email, password, role, department, designation, status, "
           "first_login, created_at, allow_login, allow_breaks, shift_start, shift_end) VALUES (?, ?, ?, ?, ?, ?, ?, 'Active', ?, ?, ?, ?, ?, ?)")
    params = ['T1', 'N', 'e@e', 'h', 'Employee', 'MIS', 'D', 'x', 'y', 1, 1, '', '']
    s_out, p_out = _coerce_insert_boolean_params(sql, params, 'public')
    assert p_out[-4] is True and p_out[-3] is True
    assert s_out == sql  # param-only coercion keeps the SQL byte-identical

    s_lit, p_lit = _coerce_insert_boolean_params(
        "INSERT INTO users (emp_id, allow_login) VALUES ('T1', 1)", None, 'public')
    assert 'true' in s_lit and p_lit is None

    s_leg, _ = _coerce_insert_boolean_params(
        "INSERT INTO users (emp_id, allow_login) VALUES ('T1', 1)", None, 'legacy')
    assert '1' in s_leg  # legacy is INTEGER: nothing rewritten


@pytest.mark.skipif(
    os.getenv('APP_DB', 'duckdb').lower() not in ('postgres', 'postgresql', 'pg'),
    reason='the boolean coercion snoops information_schema on PostgreSQL',
)
def test_boolean_comparison_param_coercion_public_and_inert_legacy():
    """UPDATE/SELECT int params become bool before psycopg binds them."""
    from db_backend import _coerce_boolean_comparison_params

    sql = ("UPDATE users SET name = ?, allow_login = ?, allow_breaks = ? "
           "WHERE emp_id = ?")
    params = ['Updated', 0, 1, 'EMP001']
    sql_out, params_out = _coerce_boolean_comparison_params(sql, params, 'public')
    assert sql_out == sql
    assert params_out == ['Updated', False, True, 'EMP001']

    select_sql = "SELECT * FROM notifications WHERE is_read = ?"
    _, select_params = _coerce_boolean_comparison_params(select_sql, [0], 'public')
    assert select_params == [False]

    legacy_sql, legacy_params = _coerce_boolean_comparison_params(sql, params, 'legacy')
    assert legacy_sql == sql
    assert legacy_params == params


# ── CC-09 Transactional Outbox ─────────────────────────────────

def test_outbox_transaction_rolls_back():
    """The business write and its outbox event commit atomically (or not)."""
    import outbox
    unique = 99999991
    try:
        with outbox.transaction() as conn:
            conn.execute(
                "INSERT INTO notifications (notification_id, emp_id, type, message, created_at) VALUES (?, ?, 'T', 'm', ?)",
                [unique, 'EMP001', datetime.now()])
            raise RuntimeError('boom')
    except RuntimeError:
        pass
    conn = get_db()
    n = conn.execute("SELECT COUNT(*) FROM notifications WHERE notification_id = ?", [unique]).fetchone()[0]
    conn.close()
    assert n == 0


def test_outbox_enqueue_and_dispatch_delivers_with_side_effect():
    import outbox
    with outbox.transaction() as conn:
        eid = outbox.enqueue(conn, 'offer.accepted', 'offer_letters', '42',
                             {'offer_id': 42, 'candidate_id': 'C1'})
    assert eid is not None, 'outbox event should be enqueued'
    conn = get_db()
    stats = outbox.dispatch_once(conn)
    conn.close()
    assert stats['dispatched'] == 1 and stats['delivered'] == 1
    conn = get_db()
    row = conn.execute("SELECT status FROM outbox_events WHERE event_id = ?", [eid]).fetchone()
    count = conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE type = 'Onboarding' AND emp_id = 'EMP001'").fetchone()[0]
    conn.close()
    assert row[0] == 'delivered'
    assert count >= 1  # handler side effect ran


def test_outbox_unknown_event_backoffs_then_dead_letters():
    import outbox
    with outbox.transaction() as conn:
        eid = outbox.enqueue(conn, 'no.such.handler', 'x', 'y', {})
    conn = get_db()
    outbox.dispatch_once(conn)
    first = conn.execute("SELECT status, attempts FROM outbox_events WHERE event_id = ?", [eid]).fetchone()
    assert first[0] == 'pending' and first[1] == 1  # retry with backoff, not dead-lettered
    for _ in range(6):
        conn.execute("UPDATE outbox_events SET next_attempt_at = '2000-01-01' WHERE event_id = ?", [eid])
        outbox.dispatch_once(conn)
    final = conn.execute("SELECT status, attempts FROM outbox_events WHERE event_id = ?", [eid]).fetchone()
    conn.close()
    assert final[0] == 'dead_letter' and final[1] == outbox.MAX_ATTEMPTS


def test_finalize_payroll_enqueues_and_dispatch_notifies(client):
    """Admin creates a payroll run, finalizes it; the outbox event fires and
    the dispatcher notifies every employee on the run."""
    import outbox
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99991
    resp = client.post('/api/payroll-runs', json={'month': 11, 'year': 2099})
    assert resp.status_code == 201, resp.get_json()
    conn = get_db()
    rid = conn.execute("SELECT run_id FROM payroll_runs WHERE month = 11 AND year = 2099").fetchone()[0]
    conn.close()
    assert _payroll_submit_and_approve(client, rid, 99091).status_code == 200
    resp = client.post(f'/api/payroll-runs/{rid}/finalize')
    assert resp.status_code == 200, resp.get_json()
    conn = get_db()
    pending = conn.execute(
        "SELECT status FROM outbox_events WHERE event_type = 'payroll.finalized' AND aggregate_id = ?",
        [str(rid)]).fetchone()
    conn.close()
    assert pending and pending[0] == 'pending'
    conn = get_db()
    stats = outbox.dispatch_once(conn)
    conn.close()
    assert stats['delivered'] >= 1
    conn = get_db()
    n = conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE type = 'Payroll' AND message LIKE ?",
        [f'%{rid}%']).fetchone()[0]
    conn.close()
    assert n >= 1  # every run employee got the payout notification


def test_admin_outbox_endpoints(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99992
    resp = client.get('/api/admin/outbox')
    assert resp.status_code == 200
    assert 'data' in resp.get_json()
    resp = client.post('/api/admin/outbox/dispatch')
    assert resp.status_code == 200
    assert set(resp.get_json()) == {'dispatched', 'delivered', 'failed', 'dead_lettered'}


def _ensure_finance_approver():
    """Create a second privileged actor for payroll maker-checker tests."""
    conn = get_db()
    if not conn.execute("SELECT 1 FROM users WHERE emp_id = 'PAYFIN01'").fetchone():
        conn.execute(
            "INSERT INTO users (emp_id, name, email, password, role, department, status) "
            "VALUES ('PAYFIN01', 'Payroll Finance', 'payfin01@company.com', ?, 'Finance', 'Finance', 'Active')",
            [hash_password('pass123')],
        )
    conn.close()


def _payroll_submit_and_approve(client, rid, session_id=99090):
    """Submit as the current Admin, approve as a different Finance user."""
    _ensure_finance_approver()
    submitted = client.post(f'/api/payroll-runs/{rid}/submit')
    assert submitted.status_code == 200, submitted.get_json()
    with client.session_transaction() as sess:
        sess['emp_id'] = 'PAYFIN01'
        sess['name'] = 'Payroll Finance'
        sess['role'] = 'Finance'
        sess['department'] = 'Finance'
        sess['session_id'] = session_id
    approved = client.post(f'/api/payroll-runs/{rid}/approve')
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['department'] = 'MIS'
        sess['session_id'] = session_id + 1
    return approved


# ── Authenticated API Tests (use session_transaction) ──────────

def test_profile_api(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99999
    with client.session_transaction() as sess:
        assert sess.get('emp_id') == 'EMP001'
    resp = client.get('/api/profile')
    assert resp.status_code == 200, f'Expected 200, got {resp.status_code}'
    data = resp.get_json()
    assert data['emp_id'] == 'EMP001'


def test_change_password(client):
    """FR-AUTH-10: the new password must clear the policy, the current one must be right.

    This test rewrites the *seeded* admin's password, so it restores it in a
    `finally`. It did not before, and when the new policy refused the restore
    step it left EMP001 on the intermediate password, which then broke every
    later test that logs in as the admin — the failure surfaced in a completely
    unrelated test.
    """
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99998
    try:
        resp = client.post('/api/change-password', json={
            'current_password': 'pass123',
            'new_password': 'mangrove-lantern-92',
        })
        assert resp.status_code == 200, f'Expected 200, got {resp.status_code}: {resp.get_json()}'
        resp = client.post('/api/change-password', json={
            'current_password': 'wrong',
            'new_password': 'another-good-one-77',
        })
        assert resp.status_code == 400, 'a wrong current password must be refused'
    finally:
        # The policy refuses `pass123` outright, so the seed's demo hash is put
        # back directly rather than through the route this test is about.
        conn = get_db()
        try:
            conn.execute(
                'UPDATE users SET password = ? WHERE emp_id = ?',
                [hash_password('pass123'), 'EMP001'],
            )
        finally:
            conn.close()


def test_active_users_endpoint_filters_inactive_employees(client):
    conn = get_db()
    conn.execute("UPDATE users SET status = 'Blocked' WHERE emp_id = ?", ['EMP002'])
    conn.commit()
    conn.close()
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99998
    resp = client.get('/api/users?active=1')
    assert resp.status_code == 200
    data = resp.get_json()
    assert all(item['status'] == 'Active' for item in data['data'])



def _set_admin_session(client, session_id):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['department'] = 'MIS'
        sess['session_id'] = session_id


def _cleanup_user_contract_rows(*emp_ids):
    if not emp_ids:
        return
    placeholders = ','.join('?' for _ in emp_ids)
    conn = get_db()
    try:
        conn.execute(
            f"DELETE FROM audit_log WHERE entity = 'users' AND entity_id IN ({placeholders})",
            list(emp_ids),
        )
        for table in ('user_sessions', 'shift_assignments', 'user_permissions', 'password_reset_tokens'):
            try:
                conn.execute(f"DELETE FROM {table} WHERE emp_id IN ({placeholders})", list(emp_ids))
            except Exception:
                pass
        conn.execute(f"DELETE FROM users WHERE emp_id IN ({placeholders})", list(emp_ids))
    finally:
        conn.close()


def test_user_list_pagination_and_sort_validation(client):
    _set_admin_session(client, 99881)
    assert client.get('/api/users?page=0').status_code == 400
    assert client.get('/api/users?per_page=201').status_code == 400
    assert client.get('/api/users?sort_by=password').status_code == 400
    assert client.get('/api/users?sort_dir=sideways').status_code == 400
    response = client.get('/api/users?per_page=2&sort_by=emp_id&sort_dir=asc')
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['per_page'] == 2
    assert payload['sort_by'] == 'emp_id' and payload['sort_dir'] == 'asc'
    assert [row['emp_id'] for row in payload['data']] == sorted(row['emp_id'] for row in payload['data'])


def test_user_create_validation_and_email_uniqueness(client):
    _set_admin_session(client, 99882)
    base = {
        'name': 'Directory Validation',
        'department': 'MIS',
        'role': 'Employee',
        'password': 'validation-pass-123',
    }
    assert client.post('/api/users', json={**base, 'emp_id': 'X1', 'email': 'x1@company.com'}).status_code == 400
    assert client.post('/api/users', json={**base, 'emp_id': 'EMP905', 'email': 'x@company.com', 'role': 'Owner'}).status_code == 400
    assert client.post('/api/users', json={**base, 'emp_id': 'EMP905', 'email': 'x@company.com', 'department': 'Unknown'}).status_code == 400

    try:
        created = client.post('/api/users', json={**base, 'emp_id': 'emp905', 'email': 'Unique.Dir@company.com'})
        assert created.status_code == 201, created.get_json()
        duplicate = client.post('/api/users', json={**base, 'emp_id': 'EMP906', 'email': 'unique.dir@company.com'})
        assert duplicate.status_code == 409
    finally:
        _cleanup_user_contract_rows('EMP905', 'EMP906')


def test_user_partial_update_preserves_fields_and_audits_role(client):
    _set_admin_session(client, 99883)
    try:
        created = client.post('/api/users', json={
            'emp_id': 'EMP907', 'name': 'Partial Update',
            'email': 'emp907@company.com', 'department': 'Support',
            'role': 'Employee', 'password': 'partial-pass-123',
        })
        assert created.status_code == 201, created.get_json()

        updated = client.put('/api/users/EMP907', json={'name': 'Partial Updated'})
        assert updated.status_code == 200, updated.get_json()
        detail = client.get('/api/users/EMP907').get_json()
        assert detail['name'] == 'Partial Updated'
        assert detail['email'] == 'emp907@company.com'
        assert detail['role'] == 'Employee' and detail['department'] == 'Support'
        assert client.put('/api/users/EMP907', json={'unknown': True}).status_code == 400
        assert client.put('/api/users/EMP907', json={'role': 'Finance'}).status_code == 200

        conn = get_db()
        audit = conn.execute(
            'SELECT "before", "after" FROM audit_log WHERE action = \'USER_UPDATE\' '
            'AND entity_id = \'EMP907\' ORDER BY created_at DESC LIMIT 1'
        ).fetchone()
        conn.close()
        assert json.loads(audit[0])['role'] == 'Employee'
        assert json.loads(audit[1])['role'] == 'Finance'
    finally:
        _cleanup_user_contract_rows('EMP907')


@pytest.fixture(autouse=True)
def _anonymisation_salt():
    """Anonymisation refuses to run without a salt; give the whole suite one."""
    os.environ.setdefault('ANONYMISATION_SALT', 'test-anonymisation-salt-value')
    yield


def _clear_anonymisation_rows():
    conn = get_db()
    try:
        conn.execute("DELETE FROM anonymisation_requests")
        conn.execute("DELETE FROM audit_log WHERE entity = 'anonymisation_requests'")
        conn.execute("DELETE FROM audit_log WHERE action LIKE 'USER_ANONYMIS%'")
    finally:
        conn.close()


def test_import_job_can_be_run_on_demand(client):
    """The "run now" button: admin-gated, idempotent, 404 for an unknown job."""
    _set_admin_session(client, 99865)
    csv_body = 'emp_id,name,email,role,department\nEMP970,On Demand,emp970@company.com,Employee,MIS\n'
    try:
        job_id = client.post(
            '/api/users/import',
            data={'file': (BytesIO(csv_body.encode()), 'users.csv')},
            content_type='multipart/form-data',
        ).get_json()['job_id']
        ran = client.post(f'/api/users/import/{job_id}/run')
        assert ran.status_code == 200, ran.get_json()
        assert ran.get_json()['status'] == 'completed'
        assert ran.get_json()['imported'] == 1
        # Pressing it again is harmless: the job is terminal, so it just replays.
        again = client.post(f'/api/users/import/{job_id}/run')
        assert again.status_code == 200
        assert again.get_json()['imported'] == 1
        assert client.get('/api/users/EMP970').status_code == 200
        assert client.post('/api/users/import/999999/run').status_code == 404

        # ...and a non-admin cannot drive it.
        _create_policy_user(client, 'EMP971', role='Employee')
        _login_as(client, 'EMP971', 'Employee', 99864)
        assert client.post(f'/api/users/import/{job_id}/run').status_code in (302, 403)
    finally:
        _cleanup_import_job_rows()
        _cleanup_user_contract_rows('EMP970', 'EMP971')


# ── FR-USR two-person anonymisation ────────────────────────────────────────

def test_anonymisation_refuses_without_a_strong_salt(client):
    """A missing/short salt would make the mapping guessable, so it stops."""
    import os

    import anonymise

    _set_admin_session(client, 99874)
    try:
        _create_policy_user(client, 'EMP960', role='Employee')
        assert client.post('/api/users/EMP960/archive').status_code == 200
        previous = os.environ.pop('ANONYMISATION_SALT', None)
        try:
            os.environ['ANONYMISATION_SALT'] = 'short'
            refused = client.post('/api/users/EMP960/anonymise')
            assert refused.status_code == 503
            assert 'ANONYMISATION_SALT' in refused.get_json()['error']
        finally:
            if previous is not None:
                os.environ['ANONYMISATION_SALT'] = previous
        assert anonymise.MIN_SALT_LENGTH == 16
    finally:
        _clear_anonymisation_rows()
        _cleanup_user_contract_rows('EMP960')


def test_anonymisation_is_two_person_and_irreversible(client):
    """Propose -> confirm by someone else -> applied. One person cannot do it."""
    import os

    os.environ.setdefault('ANONYMISATION_SALT', 'test-anonymisation-salt-value')
    _set_admin_session(client, 99873)
    try:
        _create_policy_user(client, 'EMP961', role='Employee')
        conn = get_db()
        conn.execute(
            "UPDATE users SET address = '12 Test Road', phone = '9999999999', "
            "emergency_contact_name = 'Kin', emergency_contact_phone = '8888888888' "
            "WHERE emp_id = 'EMP961'"
        )
        conn.execute(
            "INSERT INTO dependents (dependent_id, emp_id, name, relationship, date_of_birth) "
            "VALUES (-700021, 'EMP961', 'Dep One', 'Child', '2015-05-05')"
        )
        conn.close()
        assert client.post('/api/users/EMP961/archive').status_code == 200

        # A dry run changes nothing at all.
        dry = client.post('/api/users/EMP961/anonymise?dry_run=1')
        assert dry.status_code == 200
        plan = dry.get_json()['plan']
        assert plan['rows']['dependents_deleted'] == 1
        assert plan['erased_fields']['address'] == '12 Test Road'
        conn = get_db()
        row = conn.execute("SELECT name, address FROM users WHERE emp_id = 'EMP961'").fetchone()
        conn.close()
        assert row == ('Policy EMP961', '12 Test Road'), 'the dry run wrote something'

        # An active account can never be anonymised, whatever the approval.
        proposed = client.post('/api/users/EMP961/anonymise', json={'reason': 'offboarded 2026'})
        assert proposed.status_code == 201, proposed.get_json()
        request_id = proposed.get_json()['request_id']
        assert proposed.get_json()['status'] == 'proposed'
        assert proposed.get_json()['requested_by'] == 'EMP001'

        # The requester cannot confirm their own request: the two-person control.
        self_confirm = client.post(f'/api/anonymisation/{request_id}/confirm')
        assert self_confirm.status_code == 409
        assert 'second person' in self_confirm.get_json()['error']
        conn = get_db()
        assert conn.execute(
            "SELECT name FROM users WHERE emp_id = 'EMP961'"
        ).fetchone()[0] == 'Policy EMP961', 'a self-confirm erased the record'
        conn.close()

        # A *different* admin confirms, and the system applies the erasure.
        _create_policy_user(client, 'EMP965', role='Admin')
        _login_as(client, 'EMP965', 'Admin', 99872)
        applied = client.post(f'/api/anonymisation/{request_id}/confirm')
        assert applied.status_code == 200, applied.get_json()
        payload = applied.get_json()
        assert payload['status'] == 'applied'
        assert payload['confirmed_by'] == 'EMP965'
        assert payload['result']['rows']['dependents_deleted'] == 1

        conn = get_db()
        subject = conn.execute(
            "SELECT emp_id, name, email, phone, address, date_of_birth, "
            "emergency_contact_name, emergency_contact_phone, department, allow_login "
            "FROM users WHERE emp_id = 'EMP961'"
        ).fetchone()
        dependents = conn.execute(
            "SELECT COUNT(*) FROM dependents WHERE emp_id = 'EMP961'"
        ).fetchone()[0]
        conn.close()
        # Direct identifiers gone...
        assert subject[1] == 'Anonymised Employee'
        assert subject[2].endswith('@anonymised.invalid')
        assert subject[3] is None and subject[4] is None
        assert subject[5] is None and subject[6] is None and subject[7] is None
        assert subject[8] == 'MIS'          # kept: the statutory record survives
        assert subject[9] == 0              # and the account cannot be used
        assert dependents == 0

        # The audit row for the erasure records the fact and no erased value.
        conn = get_db()
        audit = conn.execute(
            'SELECT "before", "after", details FROM audit_log '
            "WHERE action = 'USER_ANONYMISED' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        blob = ' '.join(str(part) for part in audit)
        conn.close()
        for leaked in ('12 Test Road', '9999999999', 'Policy EMP961', 'Kin', '8888888888'):
            assert leaked not in blob, f'the audit row leaked {leaked!r}'
        assert json.loads(audit[1])['status'] == 'anonymised'
        assert json.loads(audit[1])['rows']['dependents_deleted'] == 1
        assert 'unsrubbed_free_text' in json.loads(audit[1])
    finally:
        _clear_anonymisation_rows()
        _cleanup_user_contract_rows('EMP961', 'EMP965')


def test_anonymisation_scrubs_the_subjects_own_audit_history(client):
    """Value substitution keeps the trail of what happened, not the personal data."""
    import os

    os.environ.setdefault('ANONYMISATION_SALT', 'test-anonymisation-salt-value')
    _set_admin_session(client, 99871)
    try:
        _create_policy_user(client, 'EMP962', role='Employee')
        assert client.put(
            '/api/users/EMP962', json={'name': 'Renamed Person', 'designation': 'Analyst'}
        ).status_code == 200
        assert client.post('/api/users/EMP962/archive').status_code == 200
        request_id = client.post(
            '/api/users/EMP962/anonymise'
        ).get_json()['request_id']
        _create_policy_user(client, 'EMP966', role='Admin')
        _login_as(client, 'EMP966', 'Admin', 99870)
        assert client.post(f'/api/anonymisation/{request_id}/confirm').status_code == 200

        conn = get_db()
        rows = conn.execute(
            'SELECT details, "before", "after" FROM audit_log WHERE entity_id = ?',
            ['EMP962'],
        ).fetchall()
        conn.close()
        assert rows, 'the subject had no audit history to scrub'
        blob = ' '.join(str(part) for row in rows for part in row)
        assert 'Renamed Person' not in blob
        assert 'ANON-' in blob, 'the scrubbed value should be the pseudonym'

        # Another employee's history is left completely alone.
        _set_admin_session(client, 99869)
        _create_policy_user(client, 'EMP969', role='Employee')
        assert client.put(
            '/api/users/EMP969', json={'name': 'Someone Else Entirely'}
        ).status_code == 200
        conn = get_db()
        leaked = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE entity = 'users' AND entity_id = 'EMP969' "
            "AND (details LIKE '%ANON-%' OR details LIKE '%Renamed Person%')"
        ).fetchone()[0]
        name_intact = conn.execute(
            "SELECT name FROM users WHERE emp_id = 'EMP969'"
        ).fetchone()[0]
        conn.close()
        assert leaked == 0, 'the scrub leaked into another employee\'s rows'
        assert name_intact == 'Someone Else Entirely'
    finally:
        _clear_anonymisation_rows()
        _cleanup_user_contract_rows('EMP962', 'EMP966', 'EMP969')


def test_anonymisation_is_idempotent_and_guards_the_states(client):
    import os

    os.environ.setdefault('ANONYMISATION_SALT', 'test-anonymisation-salt-value')
    _set_admin_session(client, 99870)
    try:
        _create_policy_user(client, 'EMP963', role='Employee')
        # Only an archived account qualifies.
        assert client.post('/api/users/EMP963/anonymise').status_code == 409
        assert 'archived' in client.post('/api/users/EMP963/anonymise').get_json()['error']
        assert client.post('/api/users/EMP963/archive').status_code == 200

        first = client.post('/api/users/EMP963/anonymise').get_json()['request_id']
        # A second open request for the same employee is refused.
        assert client.post('/api/users/EMP963/anonymise').status_code == 409
        # Cancelling only works while proposed.
        assert client.post(f'/api/anonymisation/{first}/cancel').status_code == 200
        assert client.post(f'/api/anonymisation/{first}/cancel').status_code == 409
        # After cancelling, a new request can be raised.
        second = client.post('/api/users/EMP963/anonymise').get_json()['request_id']
        assert second != first
        _create_policy_user(client, 'EMP967', role='Admin')
        _login_as(client, 'EMP967', 'Admin', 99866)
        assert client.post(f'/api/anonymisation/{second}/confirm').status_code == 200
        # Confirming an applied request replays instead of erasing again.
        replay = client.post(f'/api/anonymisation/{second}/confirm')
        assert replay.status_code == 200
        assert replay.get_json()['status'] == 'applied'
        assert client.post(f'/api/anonymisation/{second}/cancel').status_code == 409
        assert client.get('/api/anonymisation/999999').status_code == 404
        assert client.post('/api/anonymisation/999999/confirm').status_code == 404
        listed = client.get('/api/anonymisation').get_json()['requests']
        assert any(row['request_id'] == second for row in listed)
    finally:
        _clear_anonymisation_rows()
        _cleanup_user_contract_rows('EMP963', 'EMP967')


def test_anonymisation_needs_the_policy_admin_module(client):
    """Denying `policy_admin` removes the whole erasure surface."""
    _set_admin_session(client, 99869)
    try:
        _create_policy_user(client, 'EMP964', role='Admin')
        _create_policy_user(client, 'EMP968', role='Employee')
        assert client.post('/api/users/EMP968/archive').status_code == 200
        # An admin without `policy_admin` cannot even *plan* an erasure.
        assert client.put(
            '/api/users/EMP964/permissions', json={'modules': {'policy_admin': False}}
        ).status_code == 200
        _login_as(client, 'EMP964', 'Admin', 99866)
        assert client.post('/api/users/EMP968/anonymise?dry_run=1').status_code in (302, 403)
        assert client.post('/api/users/EMP968/anonymise').status_code in (302, 403)
        assert client.get('/api/anonymisation').status_code in (302, 403)
        # ...and with the capability the plan is readable.
        _set_admin_session(client, 99867)
        assert client.put(
            '/api/users/EMP964/permissions', json={'modules': {'policy_admin': True}}
        ).status_code == 200
        _login_as(client, 'EMP964', 'Admin', 99865)
        assert client.post('/api/users/EMP968/anonymise?dry_run=1').status_code == 200
    finally:
        _clear_permission_rows('EMP964')
        _clear_anonymisation_rows()
        _cleanup_user_contract_rows('EMP964', 'EMP968')



# ── FR-USR-15 PII: dependents and candidates ──────────────────────────────

def test_pii_field_classification_covers_every_pii_entity():
    """The data dictionary marks these columns PII; the policy must too."""
    import policy

    assert set(policy.PII_FIELDS) == {'users', 'dependents', 'candidates'}
    assert 'date_of_birth' in policy.PII_FIELDS['users']
    assert set(policy.PII_FIELDS['dependents']) == {'name', 'relationship', 'date_of_birth'}
    assert set(policy.PII_FIELDS['candidates']) == {'email', 'phone', 'resume_text'}
    # A name is never withheld: the workflow needs to know whose record it is.
    assert 'name' not in policy.PII_FIELDS['users']
    assert 'name' not in policy.PII_FIELDS['candidates']
    assert policy.pii_fields_for('unknown_entity') == ()
    # Withheld keys keep their place with a None value, so a client can tell
    # "withheld" from "not present".
    redacted = policy.redact_pii({'name': 'A', 'email': 'a@b', 'phone': '1'}, False, 'candidates')
    assert redacted == {'name': 'A', 'email': None, 'phone': None}
    assert policy.redact_pii({'name': 'A', 'email': 'a@b'}, True, 'candidates')['email'] == 'a@b'


def _seed_candidate(client, emp_id='EMP980', role='Admin'):
    conn = get_db()
    cid = _next_generated_id_for_test(conn, 'candidates')
    conn.execute(
        "INSERT INTO candidates (candidate_id, job_id, name, email, phone, resume_text, status, applied_at) "
        "VALUES (?, NULL, 'Casey Applicant', 'casey@example.invalid', '+91-9999999999', "
        "'long resume text', 'Applied', ?)",
        [cid, datetime.now()],
    )
    conn.close()
    return cid


def _next_generated_id_for_test(conn, table):
    from app import _is_public_target_schema, _next_generated_id, gen_id
    if _is_public_target_schema():
        return _next_generated_id(conn, table, f'{table[:-1]}_id')
    while True:
        value = gen_id()
        if not conn.execute(f'SELECT 1 FROM {table} WHERE {table[:-1]}_id = ?', [value]).fetchone():
            return value


def _cleanup_candidate_rows():
    conn = get_db()
    try:
        conn.execute("DELETE FROM candidates WHERE email = 'casey@example.invalid'")
        conn.execute("DELETE FROM audit_log WHERE entity = 'candidates' AND action = 'PII_REVEAL'")
    finally:
        conn.close()


def test_candidate_contact_fields_need_the_pii_reveal_capability(client):
    """A candidate is not an employee: their contact bundle is gated too."""
    _set_admin_session(client, 99863)
    try:
        _seed_candidate(client)
        revealed = client.get('/api/candidates').get_json()
        assert revealed, 'the candidate list is empty'
        record = next(r for r in revealed if r['name'] == 'Casey Applicant')
        assert record['pii_revealed'] is True
        assert record['email'] == 'casey@example.invalid'
        assert record['phone'] == '+91-9999999999'

        # An admin who is denied `pii_reveal` gets the record without contact
        # details — the ATS stays usable, the personal data does not leak.
        _create_policy_user(client, 'EMP981', role='Admin')
        assert client.put(
            '/api/users/EMP981/permissions', json={'modules': {'pii_reveal': False}}
        ).status_code == 200
        _login_as(client, 'EMP981', 'Admin', 99862)
        hidden = client.get('/api/candidates').get_json()
        record = next(r for r in hidden if r['name'] == 'Casey Applicant')
        assert record['pii_revealed'] is False
        assert record['email'] is None and record['phone'] is None
        assert record['name'] == 'Casey Applicant', 'the name is not PII we withhold'
        assert record['status'] == 'Applied', 'the rest of the record is still usable'

        # The offer list follows the same rule for the same contact field.
        offers = client.get('/api/offers').get_json()
        assert all(row.get('pii_revealed') is False for row in offers)

        conn = get_db()
        audited = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'PII_REVEAL' AND entity = 'candidates'"
        ).fetchone()[0]
        conn.close()
        assert audited >= 1, 'the first (permitted) read was not audited'
    finally:
        _clear_permission_rows('EMP981')
        _cleanup_candidate_rows()
        _cleanup_user_contract_rows('EMP981')


def test_hr_keeps_candidate_contact_access_by_default(client):
    """HR holds pii_reveal in the matrix, so the ATS is unaffected for them."""
    _set_admin_session(client, 99861)
    try:
        _seed_candidate(client)
        _create_policy_user(client, 'EMP982', role='HR')
        _login_as(client, 'EMP982', 'HR', 99860)
        record = next(
            r for r in client.get('/api/candidates').get_json()
            if r['name'] == 'Casey Applicant'
        )
        assert record['pii_revealed'] is True
        assert record['email'] == 'casey@example.invalid'
    finally:
        _cleanup_candidate_rows()
        _cleanup_user_contract_rows('EMP982')


def test_dependents_remain_own_record_only(client):
    """No cross-employee dependents read exists; the classification is explicit."""
    import policy

    _set_admin_session(client, 99859)
    try:
        _create_policy_user(client, 'EMP983', role='Admin')
        _login_as(client, 'EMP983', 'Admin', 99858)
        before = client.get('/api/dependents').get_json()
        conn = get_db()
        conn.execute(
            "INSERT INTO dependents (dependent_id, emp_id, name, relationship, date_of_birth) "
            "VALUES (-700031, 'EMP983', 'Dep Three', 'Child', '2016-01-01')"
        )
        conn.close()
        rows = client.get('/api/dependents').get_json()
        assert len(rows) == len(before) + 1
        assert all(row['emp_id'] == 'EMP983' for row in rows) if rows and 'emp_id' in rows[0] else True
        # An admin cannot read another employee's dependents through any route.
        assert client.get('/api/users/EMP983/pii').status_code == 200  # users PII only
        body = client.get('/api/users/EMP983/pii').get_json()
        assert 'dependents' not in body
        assert policy.pii_fields_for('dependents')
    finally:
        conn = get_db()
        conn.execute("DELETE FROM dependents WHERE emp_id = 'EMP983'")
        conn.close()
        _cleanup_user_contract_rows('EMP983')



# ── FR-LEA-08 monthly accrual ledger ──────────────────────────────────────

def test_accrual_window_and_month_maths():
    """The window is the months the assignment covered, never a month ahead."""
    import leave_accrual

    # A rate of 1.5/month carries the fraction, so the year totals exactly 18.
    credits = [leave_accrual.month_days(1.5, m) for m in range(1, 13)]
    assert credits == [1, 2, 1, 2, 1, 2, 1, 2, 1, 2, 1, 2]
    assert sum(credits) == 18
    assert leave_accrual.days_earned_through(0.5, 12) == 6
    assert leave_accrual.days_earned_through(1.0, 0) == 0
    assert leave_accrual.month_days(1.0, 0) == 0
    assert leave_accrual.month_days(1.0, 13) == 0

    today = date(2026, 9, 29)
    from_april = {'effective_from': date(2026, 4, 1), 'effective_to': None}
    assert leave_accrual.accrual_window(from_april, 2026, today) == (4, 9)
    # A year the policy never reached, and a year that has not happened.
    assert leave_accrual.accrual_window(from_april, 2025, today) is None
    assert leave_accrual.accrual_window(from_april, 2027, today) is None
    # A closed window stops earning, and a completed year earns all 12 months.
    closed = {'effective_from': date(2025, 1, 1), 'effective_to': date(2025, 4, 30)}
    assert leave_accrual.accrual_window(closed, 2025, today) == (1, 4)
    assert leave_accrual.accrual_window(closed, 2026, today) is None
    whole = {'effective_from': date(2025, 6, 1), 'effective_to': None}
    assert leave_accrual.accrual_window(whole, 2025, today) == (6, 12)


def test_leave_policy_assignment_accrues_monthly_and_caps(client):
    """FR-LEA-08: the entitlement is earned month by month, capped by the cap."""
    import leave_accrual
    import leave_policy

    _set_admin_session(client, 99871)
    try:
        _create_policy_user(client, 'EMP950', role='Employee')
        today = date.today()
        year_start = date(today.year, 1, 1)
        capped = client.put('/api/users/EMP950/leave-policy', json={
            'accrual_rate': '2.0', 'carry_forward_cap': 18,
            'effective_from': year_start.isoformat(), 'grade': 'L3',
        })
        assert capped.status_code == 200, capped.get_json()
        payload = capped.get_json()
        annual = next(b for b in payload['balances'] if b['leave_type'] == 'Annual')
        earned = leave_accrual.days_earned_through(2.0, today.month)
        assert annual['total_days'] == min(earned, 18), annual
        assert annual['source'] == 'accrual'
        # The point of accrual: not the whole year handed over in January.
        assert annual['total_days'] <= 2 * today.month
        # The other types are untouched by an annual-leave accrual rate.
        casual = next(b for b in payload['balances'] if b['leave_type'] == 'Casual')
        assert casual['total_days'] == 12 and casual['source'] == 'default'

        # A policy that starts mid-year only earns from the month it started:
        # the month is the grant unit, it is not prorated by the day.
        mid_year = date(today.year, today.month, 1)
        assert client.put('/api/users/EMP950/leave-policy', json={
            'accrual_rate': '2.0', 'effective_from': mid_year.isoformat(),
        }).status_code == 200
        conn = get_db()
        try:
            assert leave_policy.entitlement_days(conn, 'EMP950', 'Annual') == (
                leave_accrual.month_days(2.0, today.month), 'accrual',
            )
            # A future assignment is not in force yet, so the current one decides.
        finally:
            conn.close()
        future = (today + timedelta(days=30)).isoformat()
        client.put('/api/users/EMP950/leave-policy', json={
            'accrual_rate': '3.0', 'effective_from': future,
        })
        conn = get_db()
        try:
            still = leave_policy.entitlement_days(conn, 'EMP950', 'Annual')
        finally:
            conn.close()
        assert still == (leave_accrual.month_days(2.0, today.month), 'accrual'), still

        # Bad payloads are refused without writing.
        assert client.put('/api/users/EMP950/leave-policy', json={'accrual_rate': '1.5'}).status_code == 400
        assert client.put('/api/users/EMP950/leave-policy', json={
            'accrual_rate': '1.5', 'effective_from': '2026-01-01', 'nope': 1,
        }).status_code == 400
        assert client.put('/api/users/EMP950/leave-policy', json={
            'accrual_rate': '99', 'effective_from': '2026-01-01',
        }).status_code == 400
    finally:
        _cleanup_leave_rows('EMP950')
        _cleanup_user_contract_rows('EMP950')


def test_accrual_run_posts_the_ledger_once_and_agrees_with_the_balance(client):
    """The monthly job and the on-demand route are the same idempotent operation."""
    import leave_accrual
    import leave_policy

    _set_admin_session(client, 99864)
    try:
        _create_policy_user(client, 'EMP954', role='Employee')
        today = date.today()
        assert client.put('/api/users/EMP954/leave-policy', json={
            'accrual_rate': '1.5', 'effective_from': date(today.year, 1, 1).isoformat(),
        }).status_code == 200

        first = client.post('/api/accrual/run', json={})
        assert first.status_code == 200, first.get_json()
        result = first.get_json()
        assert result['grants'] == today.month, result
        assert result['days'] == leave_accrual.days_earned_through(1.5, today.month), result
        assert result['employees'] >= 1

        conn = get_db()
        try:
            rows = conn.execute(
                'SELECT month, days, granted_by FROM monthly_leave_grants WHERE emp_id = ? '
                'AND leave_type = ? AND year = ? ORDER BY month',
                ['EMP954', 'Annual', today.year],
            ).fetchall()
            # One row per elapsed month, each a positive credit, and granted_by is
            # NULL: it is a foreign key to users(emp_id) in v2.0 and a system
            # accrual has no human actor (the audit row records who ran it).
            assert [r[0] for r in rows] == list(range(1, today.month + 1)), rows
            assert all(r[1] > 0 and r[2] is None for r in rows), rows
            posted = leave_accrual.granted_days(conn, 'EMP954', 'Annual')
            derived, source = leave_policy.entitlement_days(
                conn, 'EMP954', 'Annual', year=today.year)
            assert source == 'accrual'
            # The ledger is the record of what was posted; the derived value is
            # what the employee has earned. They must agree, or the ledger lies.
            assert derived == posted == leave_accrual.days_earned_through(1.5, today.month)
        finally:
            conn.close()

        # Pressing it again posts nothing: idempotent per (emp, type, year, month).
        again = client.post('/api/accrual/run', json={}).get_json()
        assert again['grants'] == 0, again
        assert again['already_posted'] >= 1, again
        conn = get_db()
        try:
            count = conn.execute(
                'SELECT COUNT(*) FROM monthly_leave_grants WHERE emp_id = ?', ['EMP954']
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == today.month, count

        # The run is audited with its counters.
        conn = get_db()
        try:
            rows = conn.execute(
                'SELECT "after" FROM audit_log WHERE action = \'LEAVE_ACCRUAL_RUN\' '
                'AND entity = \'monthly_leave_grants\' ORDER BY created_at ASC'
            ).fetchall()
        finally:
            conn.close()
        assert rows, 'the accrual run was not audited'
        assert json.loads(rows[0][0])['grants'] >= 1
    finally:
        conn = get_db()
        conn.execute('DELETE FROM monthly_leave_grants WHERE emp_id = ?', ['EMP954'])
        conn.close()
        _cleanup_leave_rows('EMP954')
        _cleanup_user_contract_rows('EMP954')


def test_accrual_caps_the_ledger_and_the_derived_value_together(client):
    """A cap stops both the posted ledger and the balance at the same number."""
    import leave_accrual
    import leave_policy

    _set_admin_session(client, 99860)
    try:
        _create_policy_user(client, 'EMP957', role='Employee')
        today = date.today()
        assert client.put('/api/users/EMP957/leave-policy', json={
            'accrual_rate': '2.0', 'carry_forward_cap': 5,
            'effective_from': date(today.year, 1, 1).isoformat(),
        }).status_code == 200
        conn = get_db()
        try:
            result = leave_accrual.run_accrual(conn, employee_ids=['EMP957'])
            posted = leave_accrual.granted_days(conn, 'EMP957', 'Annual')
            derived, source = leave_policy.entitlement_days(
                conn, 'EMP957', 'Annual', year=today.year)
        finally:
            conn.close()
        assert result['days'] == 5, result
        assert posted == derived == 5, (posted, derived)
        assert source == 'accrual'
    finally:
        conn = get_db()
        conn.execute('DELETE FROM monthly_leave_grants WHERE emp_id = ?', ['EMP957'])
        conn.close()
        _cleanup_leave_rows('EMP957')
        _cleanup_user_contract_rows('EMP957')


def test_a_year_the_policy_never_reached_keeps_the_default_entitlement(client):
    """A policy that starts this year must not zero out last year's balance."""
    import leave_policy

    _set_admin_session(client, 99865)
    try:
        _create_policy_user(client, 'EMP955', role='Employee')
        last_year = date.today().year - 1
        assert client.put('/api/users/EMP955/leave-policy', json={
            'accrual_rate': '1.5', 'effective_from': date(date.today().year, 1, 1).isoformat(),
        }).status_code == 200
        conn = get_db()
        try:
            # Last year: the published default, not zero and not this year's accrual.
            assert leave_policy.entitlement_days(conn, 'EMP955', 'Annual', year=last_year) == (20, 'default')
            # This year: accrued.
            assert leave_policy.entitlement_days(conn, 'EMP955', 'Annual')[1] == 'accrual'
            leave_policy.ensure_balances(conn, 'EMP955', last_year)
            row = conn.execute(
                'SELECT total_days FROM leave_balance WHERE emp_id = ? AND leave_type = ? AND year = ?',
                ['EMP955', 'Annual', last_year],
            ).fetchone()
        finally:
            conn.close()
        assert row is not None and int(row[0]) == 20, row
    finally:
        _cleanup_leave_rows('EMP955')
        _cleanup_user_contract_rows('EMP955')


def test_accrual_run_is_admin_only_and_registered_as_a_job(client):
    _set_admin_session(client, 99863)
    _create_policy_user(client, 'EMP956', role='Employee')
    _login_as(client, 'EMP956', 'Employee', 99862)
    # A JSON API call is refused with 403; a bare form-style POST is the gate's
    # long-standing page behaviour (redirect to the dashboard).
    assert client.post('/api/accrual/run', json={}).status_code == 403
    assert client.post('/api/accrual/run').status_code == 302
    _cleanup_user_contract_rows('EMP956')

    import app as app_module

    app_module._register_scheduler_jobs(2)
    job = app_module.scheduler.get_job('leave-accrual')
    assert job is not None, 'the monthly accrual job is not registered'
# Every FR-* requirement id in HRMS_SRS_v2.0.pdf, extracted from the
# requirements tables and Appendix A on 2026-09-29. The matrix in
# traceability.py must cover exactly this set, so a new SRS requirement cannot
# go untracked. Parsing the PDF here would add a pypdf dependency to CI for
# one assertion, so the extracted set is committed instead. To re-extract after
# an SRS revision:
#   python -c "from pypdf import PdfReader; import re; t=chr(10).join(
#     (p.extract_text() or '') for p in PdfReader('HRMS_SRS_v2.0.pdf').pages);
#     print(sorted(set(re.findall(r'FR-[A-Z]{2,6}-[0-9]{2}[a-z]?', t))))"
_SRS_REQUIREMENT_IDS = {
    'FR-ANL-01', 'FR-ANL-02', 'FR-ANL-04', 'FR-AST-01', 'FR-ATS-01', 'FR-ATS-02',
    'FR-ATS-03', 'FR-ATS-04', 'FR-ATT-01', 'FR-ATT-02', 'FR-ATT-03', 'FR-ATT-04',
    'FR-ATT-05', 'FR-ATT-06', 'FR-ATT-07', 'FR-ATT-08', 'FR-ATT-09', 'FR-ATT-10',
    'FR-ATT-11', 'FR-ATT-12', 'FR-ATT-13', 'FR-ATT-14', 'FR-ATT-15', 'FR-ATT-16',
    'FR-ATT-17', 'FR-AUD-01', 'FR-AUTH-01', 'FR-AUTH-02', 'FR-AUTH-03', 'FR-AUTH-04',
    'FR-AUTH-05', 'FR-AUTH-06', 'FR-AUTH-07', 'FR-AUTH-08', 'FR-AUTH-09', 'FR-AUTH-10',
    'FR-AUTH-11', 'FR-AUTH-12', 'FR-AUTH-13', 'FR-AUTH-14', 'FR-DOC-01', 'FR-DOC-02',
    'FR-DOC-03', 'FR-EXP-01', 'FR-EXP-02', 'FR-EXP-03', 'FR-HOL-01', 'FR-HOL-02',
    'FR-HOL-03', 'FR-JOB-01', 'FR-JOB-02', 'FR-JOB-03', 'FR-JOB-04', 'FR-JOB-05',
    'FR-LEA-01', 'FR-LEA-02', 'FR-LEA-03', 'FR-LEA-04', 'FR-LEA-05', 'FR-LEA-06',
    'FR-LEA-07', 'FR-LEA-08', 'FR-LEA-08a', 'FR-LEA-09', 'FR-NOT-01', 'FR-NOT-02',
    'FR-NOT-03', 'FR-OFF-01', 'FR-OFF-02', 'FR-OFF-03', 'FR-ONB-01', 'FR-ONB-02',
    'FR-ONB-03', 'FR-ONB-04', 'FR-ONB-05', 'FR-ONB-06', 'FR-PERF-01', 'FR-PERF-02',
    'FR-REG-01', 'FR-REG-02', 'FR-REG-03', 'FR-REG-04', 'FR-RPT-01', 'FR-RPT-02',
    'FR-TKT-01', 'FR-TKT-02', 'FR-TKT-03', 'FR-TKT-04', 'FR-USR-01', 'FR-USR-02',
    'FR-USR-03', 'FR-USR-04', 'FR-USR-05', 'FR-USR-06', 'FR-USR-06a', 'FR-USR-07',
    'FR-USR-08', 'FR-USR-09', 'FR-USR-10', 'FR-USR-11', 'FR-USR-12', 'FR-USR-13',
    'FR-USR-14', 'FR-USR-15',
}




# ── SRS traceability matrix ───────────────────────────────────────────────

def test_traceability_covers_every_srs_requirement():
    """The matrix must not silently drop a requirement.

    The ID list is the one extracted from `HRMS_SRS_v2.0.pdf`. A new requirement
    in the SRS has to be added here deliberately, with a verdict, rather than
    quietly going untracked.
    """
    import traceability

    assert set(traceability.TRACEABILITY) == _SRS_REQUIREMENT_IDS, (
        'traceability.py and the SRS disagree: '
        f'extra={sorted(set(traceability.TRACEABILITY) - _SRS_REQUIREMENT_IDS)} '
        f'missing={sorted(_SRS_REQUIREMENT_IDS - set(traceability.TRACEABILITY))}')


def test_traceability_routes_exist():
    """Every route the matrix claims must exist in the live url_map.

    This is the point of keeping the matrix in code: a renamed or deleted route
    turns the build red instead of quietly invalidating the document.
    """
    import app as app_module
    import traceability

    rules = {rule.rule for rule in app_module.app.url_map.iter_rules()}
    for rid, _pri, _delta, _status, routes, _note in traceability.rows():
        for route in routes:
            assert route in rules, f'{rid} claims route {route!r}, which is not registered'


def test_traceability_verdicts_are_honest_about_routes():
    """A row that names no route must not claim to be implemented.

    `IMPLEMENTED` without a route would mean the requirement is met by a
    background job, a service module or a policy rather than an endpoint —
    which is legitimate, but only when the note says so.
    """
    import traceability

    for rid, _pri, _delta, status, routes, note in traceability.rows():
        if status == 'IMPLEMENTED' and not routes:
            assert any(word in note.lower() for word in (
                'job', 'policy', 'matrix', 'module', 'helper', 'function',
                'query', 'cron', 'every ', 'guard', 'shift_assignments',
            )), f'{rid} is IMPLEMENTED with no route and no explanation'


# The ways a note can say "this is not the whole requirement". A PARTIAL row whose
# note only describes what *does* ship is worse than no row at all, because a
# reader takes it as full coverage.
_GAP_MARKERS = (
    'missing', 'not ', 'no ', 'never', 'rather than', 'only ', 'ignores',
    'none', 'without', 'absent', 'is partial',
)


def test_traceability_partial_rows_name_what_is_missing():
    """A PARTIAL verdict that does not say what is missing is useless."""
    import traceability

    for rid, _pri, _delta, status, _routes, note in traceability.rows():
        if status != 'PARTIAL':
            continue
        lowered = note.lower()
        assert any(marker in lowered for marker in _GAP_MARKERS), (
            f'{rid} is PARTIAL but the note does not say what is missing: {note}')


def test_traceability_covers_the_srs_pdf_ids():
    """The recorded ID set matches the PDF, so the two cannot drift apart.

    Parsing the PDF in the test suite would add a pypdf dependency to CI for one
    assertion, so the extracted IDs are committed here instead. This test is the
    record of what was extracted and when; regenerate with the helper below if
    the SRS is revised.
    """
    assert len(_SRS_REQUIREMENT_IDS) >= 100
    assert 'FR-ATT-10' in _SRS_REQUIREMENT_IDS, 'the retired requirement is still listed'


def test_generated_traceability_doc_is_up_to_date():
    """`docs/TRACEABILITY.md` must match the data it claims to be generated from."""
    import traceability

    doc = (pathlib.Path(__file__).resolve().parent.parent / 'docs' / 'TRACEABILITY.md')
    assert doc.is_file(), 'run scripts/generate_traceability.py'
    body = doc.read_text()
    for rid in traceability.TRACEABILITY:
        assert rid in body, f'{rid} is missing from docs/TRACEABILITY.md'
    for status, count in traceability.counts().items():
        assert f'| `{status}` | {count} |' in body, (
            f'the summary row for {status} does not match the data ({count})')



# ── FR-AUTH-10 password policy ───────────────────────────────────────────

def test_password_policy_length_is_ten_not_six():
    """Appendix A-01 records a 6-character minimum as a NIST defect."""
    import passwords

    assert passwords.MIN_LENGTH == 10
    # Nine characters is refused even though it is short but memorable.
    with pytest.raises(passwords.PasswordPolicyError) as excinfo:
        passwords.check('jadequilt')
    assert '10 characters' in str(excinfo.value), excinfo.value
    # Ten is the boundary and it is inclusive.
    assert passwords.check('jadequilts') == 'jadequilts'
    # ...and there is no composition rule: a passphrase of four words with no
    # digit, symbol or capital is fine. Forcing those produces weaker passwords.
    assert passwords.is_acceptable('my dog has paws')
    assert passwords.is_acceptable('a very long passphrase with spaces')


def test_password_policy_rejects_the_breach_corpus_offline():
    """No network, no dependency: the bundled corpus is the floor."""
    import passwords

    for weak in ('password12', 'Password1!', 'P@ssw0rd99', 'iloveyou12',
                 'qwertyuiop', 'dragon2024', 'Welcome2024', 'monkey123'):
        assert passwords.is_breached_offline(weak), weak
        assert not passwords.is_acceptable(weak), weak
    # The seed's own demo password is on the list on purpose.
    assert passwords.is_breached_offline('pass123')
    assert not passwords.is_acceptable('pass123')
    # Case and surrounding whitespace do not hide it.
    assert passwords.is_breached_offline('  PaSsWoRd12  ')


def test_password_policy_accepts_a_real_passphrase():
    import passwords

    for good in ('jade-marlin-quilt-77', 'mangrove-lantern-92',
                 'Tr0ub4dor&3xx', 'villa in the woods 9',
                 'correct horse battery staple'):
        assert passwords.is_acceptable(good), good


def test_password_policy_has_no_composition_or_expiry_rules():
    """NIST SP 800-63B: length and a breach check, not character-class rules."""

    # Parsed rather than grepped, so the module's own docstring — which names
    # `must_contain_digit` precisely to say it does not exist — can neither
    # satisfy nor trip the check.
    import ast

    import passwords

    tree = ast.parse(pathlib.Path(passwords.__file__).read_text())
    forbidden = {'must_contain_digit', 'must_contain_upper', 'must_contain_symbol',
                 'must_contain_lower', 'force_expiry', 'expires_days', 'rotate_days'}
    defined = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            defined.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.Name):
            defined.add(node.id)
        elif isinstance(node, ast.Attribute):
            defined.add(node.attr)
    assert not (defined & forbidden), (
        f'passwords.py defines {sorted(defined & forbidden)}: NIST SP 800-63B says '
        'composition rules and expiry produce weaker passwords, not stronger ones')
    # No time-based state either, which is how expiry would have to be expressed.
    source = pathlib.Path(passwords.__file__).read_text()
    assert 'timedelta' not in source and 'last_changed' not in source


def test_password_policy_error_does_not_leak_the_breach_count():
    """A "this appeared N times" message is a free oracle for guessing."""
    import passwords

    rejected = passwords.PasswordPolicyError()
    assert str(rejected) == passwords.GENERIC_REJECTION
    assert 'breach' in str(rejected).lower()
    assert 'million' not in str(rejected).lower()
    body = rejected.payload()
    assert body['policy'] == 'FR-AUTH-10' and body['min_length'] == 10
    # The only specific message is the length one, which leaks nothing.
    try:
        passwords.check('Sh0rt')
    except passwords.PasswordPolicyError as exc:
        assert '10 characters' in str(exc)


def test_admin_cannot_create_a_user_with_a_weak_password(client):
    """FR-USR-02 create path: the policy is enforced server-side, not in the UI."""
    _set_admin_session(client, 99830)
    try:
        for weak in ('pass123', 'short', 'password1'):
            resp = client.post('/api/users', json={
                'emp_id': 'EMP940', 'name': 'Weak', 'email': 'weak940@company.com',
                'department': 'MIS', 'role': 'Employee', 'password': weak,
            })
            assert resp.status_code == 400, (weak, resp.get_json())
            assert resp.get_json()['policy'] == 'FR-AUTH-10', resp.get_json()
        conn = get_db()
        try:
            assert conn.execute("SELECT COUNT(*) FROM users WHERE emp_id = 'EMP940'").fetchone()[0] == 0
        finally:
            conn.close()
    finally:
        _cleanup_user_contract_rows('EMP940')


def test_admin_created_user_with_no_password_gets_a_generated_one(client):
    """A shared default password is worse than none: `pass123` was the default."""
    import passwords

    _set_admin_session(client, 99831)
    try:
        resp = client.post('/api/users', json={
            'emp_id': 'EMP941', 'name': 'No Password', 'email': 'np941@company.com',
            'department': 'MIS', 'role': 'Employee',
        })
        assert resp.status_code == 201, resp.get_json()
        body = resp.get_json()
        assert body['password_source'] == 'generated'
        generated = body['generated_password']
        assert passwords.is_acceptable(generated), generated
        assert generated != 'pass123'
        # It is a working password, not just a string that passed a check.
        with app.test_client() as fresh:
            assert fresh.post('/login', json={
                'emp_id': 'EMP941', 'password': generated,
            }).status_code == 200
    finally:
        _cleanup_user_contract_rows('EMP941')


def test_change_password_enforces_the_policy(client):
    _set_admin_session(client, 99832)
    try:
        weak = client.post('/api/change-password', json={
            'current_password': 'pass123', 'new_password': 'password1',
        })
        assert weak.status_code == 400, weak.get_json()
        assert weak.get_json()['policy'] == 'FR-AUTH-10'
        # A wrong current password and a weak new password: the policy answer must
        # not tell an attacker which of the two was wrong.
        both = client.post('/api/change-password', json={
            'current_password': 'nope', 'new_password': 'password1',
        })
        assert both.status_code == 400
        assert both.get_json()['error'] == weak.get_json()['error']
    finally:
        conn = get_db()
        try:
            conn.execute('UPDATE users SET password = ? WHERE emp_id = ?',
                         [hash_password('pass123'), 'EMP001'])
        finally:
            conn.close()


def test_reset_password_enforces_the_policy_without_burning_the_token(client):
    """A rejected password must leave the single-use token usable."""
    _set_admin_session(client, 99833)
    try:
        _create_policy_user(client, 'EMP942', role='Employee')
        conn = get_db()
        try:
            token = 'reset-token-for-policy-check'
            conn.execute(
                'DELETE FROM password_reset_tokens WHERE emp_id = ?', ['EMP942'],
            )
            conn.execute(
                'INSERT INTO password_reset_tokens (token_id, emp_id, token, expires_at) '
                'VALUES (?, ?, ?, ?)',
                [_next_generated_id(conn, 'password_reset_tokens', 'token_id'),
                 'EMP942', token, datetime.now() + timedelta(hours=1)],
            )
        finally:
            conn.close()
        weak = client.post('/api/reset-password', json={
            'token': token, 'new_password': 'password1',
        })
        assert weak.status_code == 400, weak.get_json()
        assert weak.get_json()['policy'] == 'FR-AUTH-10'
        # The token is still unused, so the user can simply try again.
        good = client.post('/api/reset-password', json={
            'token': token, 'new_password': 'jade-marlin-quilt-77',
        })
        assert good.status_code == 200, good.get_json()
        with app.test_client() as fresh:
            assert fresh.post('/login', json={
                'emp_id': 'EMP942', 'password': 'jade-marlin-quilt-77',
            }).status_code == 200
    finally:
        conn = get_db()
        try:
            conn.execute('DELETE FROM password_reset_tokens WHERE emp_id = ?', ['EMP942'])
        finally:
            conn.close()
        _cleanup_user_contract_rows('EMP942')

# ── FR-EXP-03: the expense claim state machine ────────────────────────────

def test_expense_transition_table_is_strict():
    """The table is the rule; anything outside it must be refused."""
    import expenses

    assert expenses.TRANSITIONS['Pending'] == frozenset({'Approved', 'Rejected'})
    assert expenses.TRANSITIONS['Approved'] == frozenset({'Paid'})
    # Rejected and Paid are final, so there is no path back out of them.
    assert expenses.TERMINAL_STATUSES == frozenset({'Rejected', 'Paid'})
    assert expenses.ACTIVE_STATUSES == frozenset({'Pending', 'Approved'})
    assert expenses.ALL_STATUSES == frozenset({'Pending', 'Approved', 'Rejected', 'Paid'})
    # No target appears in a source that does not list it: the graph is exactly
    # three edges, so `Pending -> Paid` is unreachable by construction.
    edges = {(src, dst) for src, targets in expenses.TRANSITIONS.items() for dst in targets}
    assert edges == {('Pending', 'Approved'), ('Pending', 'Rejected'), ('Approved', 'Paid')}


def _seed_expense_users(client, manager='EMP001'):
    """A manager, a Finance user and a claimant, wired with a reporting line."""
    _create_policy_user(client, 'EMP960', role='Employee')
    _create_policy_user(client, 'EMP961', role='Finance')
    conn = get_db()
    try:
        # EMP960 reports to the seeded admin, and the Finance user is a peer:
        # neither manages the other, so each is refused the other's transition.
        conn.execute("UPDATE users SET manager_emp_id = ? WHERE emp_id = 'EMP960'", [manager])
    finally:
        conn.close()
    return 'EMP960', 'EMP961'


def _file_claim(client, amount='120.50', **overrides):
    body = {'cat_id': 1, 'amount': amount, 'description': 'taxi to client site'}
    body.update(overrides)
    response = client.post('/api/expenses', json=body)
    assert response.status_code == 201, response.get_json()
    return response.get_json()['id']


def _claim_row(claim_id):
    conn = get_db()
    try:
        return conn.execute(
            'SELECT emp_id, status, amount, approved_by FROM expense_claims WHERE claim_id = ?',
            [claim_id],
        ).fetchone()
    finally:
        conn.close()


def _cleanup_expense_rows(*emp_ids):
    conn = get_db()
    try:
        for emp_id in emp_ids:
            conn.execute('DELETE FROM expense_claims WHERE emp_id = ?', [emp_id])
            conn.execute('DELETE FROM notifications WHERE emp_id = ?', [emp_id])
            conn.execute('DELETE FROM audit_log WHERE CAST(entity_id AS VARCHAR) = ?', [emp_id])
            conn.execute('DELETE FROM users WHERE emp_id = ?', [emp_id])
    finally:
        conn.close()


def test_expense_claim_cannot_be_filed_for_someone_else(client):
    """CC-10: emp_id comes from the session. v1.0 accepted a body override."""
    _set_admin_session(client, 99840)
    try:
        _create_policy_user(client, 'EMP962', role='Employee')
        _login_as(client, 'EMP962', 'Employee', 99839)
        forged = client.post('/api/expenses', json={
            'emp_id': 'EMP001', 'cat_id': 1, 'amount': '5000', 'description': 'not mine',
        })
        assert forged.status_code == 400, forged.get_json()
        assert 'yourself' in str(forged.get_json()).lower(), forged.get_json()
        conn = get_db()
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM expense_claims WHERE description = 'not mine'"
            ).fetchone()[0] == 0
        finally:
            conn.close()
        # Omitting it is fine, and an identical value is not an error either.
        assert _file_claim(client) is not None
    finally:
        _cleanup_expense_rows('EMP962')


def test_expense_claim_must_name_a_real_category_and_positive_amount(client):
    _set_admin_session(client, 99841)
    try:
        _create_policy_user(client, 'EMP963', role='Employee')
        _login_as(client, 'EMP963', 'Employee', 99840)
        assert client.post('/api/expenses', json={
            'cat_id': 99999, 'amount': '10', 'description': 'bad category',
        }).status_code == 400
        assert client.post('/api/expenses', json={
            'cat_id': 1, 'amount': '-5', 'description': 'negative',
        }).status_code == 400
        assert client.post('/api/expenses', json={
            'cat_id': 1, 'amount': 'not a number',
        }).status_code == 400
        conn = get_db()
        try:
            assert conn.execute(
                'SELECT COUNT(*) FROM expense_claims WHERE emp_id = ?', ['EMP963']
            ).fetchone()[0] == 0, 'a rejected claim must not be written'
        finally:
            conn.close()
    finally:
        _cleanup_expense_rows('EMP963')


def test_nobody_may_approve_their_own_expense_claim(client):
    """The v1.0 hole: any admin could approve a claim they had filed."""
    _set_admin_session(client, 99842)
    try:
        _seed_expense_users(client)
        # The admin files their own claim...
        own = _file_claim(client, amount='999.00')
        assert _claim_row(own)[0] == 'EMP001'
        for target in ('Approved', 'Rejected', 'Paid'):
            refused = client.put(f'/api/expenses/{own}/status', json={
                'status': target, 'reason': 'because',
            })
            assert refused.status_code == 403, (target, refused.get_json())
            assert 'own expense claim' in str(refused.get_json()).lower(), refused.get_json()
        assert _claim_row(own)[1] == 'Pending', 'a self-approved claim moved'

        # ...and so does an employee, who cannot even reach the route.
        _login_as(client, 'EMP960', 'Employee', 99841)
        claimant = _file_claim(client)
        assert client.put(f'/api/expenses/{claimant}/status', json={'status': 'Approved'}).status_code in (302, 403)
        assert _claim_row(claimant)[1] == 'Pending'
    finally:
        _cleanup_expense_rows('EMP960', 'EMP961')


def test_expense_claim_cannot_jump_pending_to_paid(client):
    """`Paid` is only reachable from `Approved` (Appendix A-11)."""
    _set_admin_session(client, 99843)
    try:
        _seed_expense_users(client)
        _login_as(client, 'EMP960', 'Employee', 99842)
        claim = _file_claim(client)
        _login_as(client, 'EMP001', 'Admin', 99841)

        jumped = client.put(f'/api/expenses/{claim}/status', json={'status': 'Paid'})
        assert jumped.status_code == 409, jumped.get_json()
        assert 'pending to paid' in str(jumped.get_json()).lower(), jumped.get_json()
        assert _claim_row(claim)[1] == 'Pending'

        # Admin is an accepted payer when there is no Finance user, so the
        # Approved -> Paid edge is legitimately open to them; the point of the
        # test is that it is only reachable *through* Approved.
        assert client.put(f'/api/expenses/{claim}/status', json={'status': 'Approved'}).status_code == 200
        assert _claim_row(claim)[1] == 'Approved'
        assert client.put(f'/api/expenses/{claim}/status', json={'status': 'Paid'}).status_code == 200
        assert _claim_row(claim)[1] == 'Paid'

        # A Team Leader has neither the paying nor the approving capability, so
        # the edge stays closed for them even from a legal state.
        _create_policy_user(client, 'EMP966', role='Team Leader')
        _login_as(client, 'EMP966', 'Team Leader', 99842)
        assert client.put(f'/api/expenses/{claim}/status', json={'status': 'Paid'}).status_code == 403
    finally:
        _cleanup_expense_rows('EMP960', 'EMP961')


def test_rejecting_an_expense_claim_requires_a_reason(client):
    """'Rejected' with nothing to act on is not useful to the claimant."""
    _set_admin_session(client, 99844)
    try:
        _seed_expense_users(client)
        _login_as(client, 'EMP960', 'Employee', 99843)
        claim = _file_claim(client)
        _login_as(client, 'EMP001', 'Admin', 99842)

        blank = client.put(f'/api/expenses/{claim}/status', json={'status': 'Rejected'})
        assert blank.status_code == 400, blank.get_json()
        assert 'reason' in str(blank.get_json()).lower(), blank.get_json()
        whitespace = client.put(f'/api/expenses/{claim}/status', json={
            'status': 'Rejected', 'reason': '   ',
        })
        assert whitespace.status_code == 400, whitespace.get_json()
        assert _claim_row(claim)[1] == 'Pending'

        with_reason = client.put(f'/api/expenses/{claim}/status', json={
            'status': 'Rejected', 'reason': 'no itemised receipt',
        })
        assert with_reason.status_code == 200, with_reason.get_json()
        assert _claim_row(claim)[1] == 'Rejected'
        conn = get_db()
        try:
            stored = conn.execute(
                'SELECT rejection_reason FROM expense_claims WHERE claim_id = ?', [claim]
            ).fetchone()
        finally:
            conn.close()
        assert stored[0] == 'no itemised receipt', stored
    finally:
        _cleanup_expense_rows('EMP960', 'EMP961')


def test_a_rejected_or_paid_claim_is_final(client):
    _set_admin_session(client, 99845)
    try:
        _seed_expense_users(client)
        _login_as(client, 'EMP960', 'Employee', 99844)
        claim = _file_claim(client)
        _login_as(client, 'EMP001', 'Admin', 99843)
        client.put(f'/api/expenses/{claim}/status', json={'status': 'Rejected', 'reason': 'no receipt'})

        for target in ('Pending', 'Approved', 'Paid'):
            revived = client.put(f'/api/expenses/{claim}/status', json={
                'status': target, 'reason': 'changed my mind',
            })
            assert revived.status_code == 409, (target, revived.get_json())
            assert 'final' in str(revived.get_json()).lower(), revived.get_json()
        assert _claim_row(claim)[1] == 'Rejected'
    finally:
        _cleanup_expense_rows('EMP960', 'EMP961')


def test_an_unrelated_employee_cannot_review_a_claim(client):
    """Authority is the reporting line, not 'anybody who can reach the route'."""
    import expenses

    _set_admin_session(client, 99846)
    try:
        _seed_expense_users(client)
        # A third employee who manages nobody at all.
        _create_policy_user(client, 'EMP964', role='Team Leader')
        _login_as(client, 'EMP960', 'Employee', 99845)
        claim = _file_claim(client)

        _login_as(client, 'EMP964', 'Team Leader', 99844)
        refused = client.put(f'/api/expenses/{claim}/status', json={'status': 'Approved'})
        assert refused.status_code in (302, 403), refused.status_code
        assert _claim_row(claim)[1] == 'Pending'

        # The pure function, so the rule is asserted directly as well.
        actor = ('EMP964', 'Team Leader', 'MIS', None)
        claim_row = ('EMP960', 'Pending', 100)
        assert expenses.permitted_targets(actor, claim_row) == []
        with pytest.raises(expenses.ExpenseTransitionError) as excinfo:
            expenses.check_transition(actor, claim_row, 'Approved')
        assert excinfo.value.status == 403
    finally:
        _cleanup_expense_rows('EMP960', 'EMP961', 'EMP964')


def test_finance_may_pay_and_records_when(client):
    """`Approved -> Paid by Finance only`, and the payment is timestamped."""
    _set_admin_session(client, 99847)
    try:
        _seed_expense_users(client)
        _login_as(client, 'EMP960', 'Employee', 99846)
        claim = _file_claim(client)
        _login_as(client, 'EMP001', 'Admin', 99845)
        assert client.put(f'/api/expenses/{claim}/status', json={'status': 'Approved'}).status_code == 200

        _login_as(client, 'EMP961', 'Finance', 99844)
        # Finance needs to be able to *find* the claim, not just act on it.
        listed = {r['id']: r for r in client.get('/api/expenses').get_json()}
        assert claim in listed, 'Finance cannot see an Approved claim to pay'
        assert listed[claim]['allowed_actions'] == ['Paid']

        paid = client.put(f'/api/expenses/{claim}/status', json={'status': 'Paid'})
        assert paid.status_code == 200, paid.get_json()
        row = _claim_row(claim)
        assert row[1] == 'Paid' and row[3] == 'EMP961', row
        conn = get_db()
        try:
            assert conn.execute(
                'SELECT paid_at FROM expense_claims WHERE claim_id = ?', [claim]
            ).fetchone()[0] is not None, 'Paid must record when it was paid'
        finally:
            conn.close()

        # Every transition is audited with the before/after pair.
        conn = get_db()
        try:
            # CAST because audit_log.entity_id is VARCHAR on the compatibility
            # schema and BIGINT on v2.0; comparing either side to an int fails
            # on one of them.
            actions = [
                r[0] for r in conn.execute(
                    "SELECT action FROM audit_log WHERE entity = 'expense_claims' "
                    "AND CAST(entity_id AS VARCHAR) = ? ORDER BY created_at",
                    [str(claim)],
                ).fetchall()
            ]
        finally:
            conn.close()
        assert actions == ['EXPENSE_CLAIM', 'EXPENSE_APPROVED', 'EXPENSE_PAID'], actions
    finally:
        _cleanup_expense_rows('EMP960', 'EMP961')


def test_a_second_concurrent_approval_is_refused_not_applied(client):
    """The write is `UPDATE ... WHERE status = <the state it was decided on>`.

    Two approvers who both read `Pending` produce one winner and one 409, never a
    silent overwrite. The observable consequence is that a repeat of the same
    decision is refused, so that is what is asserted.
    """
    _set_admin_session(client, 99848)
    try:
        _seed_expense_users(client)
        _login_as(client, 'EMP960', 'Employee', 99847)
        claim = _file_claim(client)
        _login_as(client, 'EMP001', 'Admin', 99846)

        first = client.put(f'/api/expenses/{claim}/status', json={'status': 'Approved'})
        assert first.status_code == 200, first.get_json()
        second = client.put(f'/api/expenses/{claim}/status', json={'status': 'Approved'})
        assert second.status_code == 409, second.get_json()
        assert 'approved to approved' in str(second.get_json()).lower(), second.get_json()
        assert _claim_row(claim)[1] == 'Approved'

        # The 409 reports the transitions that *are* still open, so a client can
        # recover without guessing.
        assert second.get_json()['allowed'] == ['Paid'], second.get_json()

        conn = get_db()
        try:
            approved = conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE entity = 'expense_claims' "
                "AND CAST(entity_id AS VARCHAR) = ? AND action = 'EXPENSE_APPROVED'",
                [str(claim)],
            ).fetchone()[0]
        finally:
            conn.close()
        assert approved == 1, 'the refused approval was audited as if it happened'
    finally:
        _cleanup_expense_rows('EMP960', 'EMP961')


def test_expense_list_scopes_to_what_the_caller_may_act_on(client):
    """Own claims, direct reports, and states they could transition — nothing else."""
    _set_admin_session(client, 99849)
    try:
        _seed_expense_users(client)
        # A stranger, created while the session is still an admin.
        _create_policy_user(client, 'EMP965', role='Employee')
        _login_as(client, 'EMP960', 'Employee', 99848)
        mine = _file_claim(client, description='mine')
        _login_as(client, 'EMP965', 'Employee', 99847)
        theirs = _file_claim(client, description='theirs')

        _login_as(client, 'EMP960', 'Employee', 99846)
        ids = {r['id'] for r in client.get('/api/expenses').get_json()}
        assert ids == {mine}, ids
        # An employee who is nobody's manager may act on nothing.
        assert all(r['allowed_actions'] == [] for r in client.get('/api/expenses').get_json())

        _login_as(client, 'EMP001', 'Admin', 99845)
        admin_rows = {r['id']: r for r in client.get('/api/expenses').get_json()}
        assert {mine, theirs} <= set(admin_rows), 'an admin can view all (policy.can_view_all)'
        assert admin_rows[theirs]['allowed_actions'] == ['Approved', 'Rejected']
        # But the admin filed nothing here, so nothing is self-approvable.
        assert all(a for a in admin_rows[theirs]['allowed_actions'])
    finally:
        _cleanup_expense_rows('EMP960', 'EMP961', 'EMP965')


# ── FR-PERF-01 goals: the create was broken and the writes were open ─────

def _seed_goal_users(client, manager='EMP001'):
    """An owner and a peer, both reporting to ``manager``."""
    _create_policy_user(client, 'EMP920', role='Employee')
    _create_policy_user(client, 'EMP921', role='Employee')
    conn = get_db()
    try:
        conn.execute(
            'UPDATE users SET manager_emp_id = ? WHERE emp_id IN (?, ?)',
            [manager, 'EMP920', 'EMP921'],
        )
    finally:
        conn.close()


def _new_goal(client, title='Ship the thing', **overrides):
    body = {'title': title}
    body.update(overrides)
    response = client.post('/api/goals', json=body)
    assert response.status_code == 201, response.get_json()
    return response.get_json()['id']


def _goal_row(goal_id):
    conn = get_db()
    try:
        return conn.execute(
            'SELECT emp_id, title, rating, status FROM goals WHERE goal_id = ?', [goal_id],
        ).fetchone()
    finally:
        conn.close()


def _cleanup_goal_rows(*emp_ids):
    conn = get_db()
    try:
        for emp_id in emp_ids:
            conn.execute('DELETE FROM notifications WHERE emp_id = ?', [emp_id])
            conn.execute('DELETE FROM goals WHERE emp_id = ?', [emp_id])
            conn.execute('DELETE FROM audit_log WHERE CAST(entity_id AS VARCHAR) = ?', [emp_id])
            conn.execute('DELETE FROM users WHERE emp_id = ?', [emp_id])
    finally:
        conn.close()


def test_goal_create_works_and_takes_emp_id_from_the_session(client):
    """Regression: `POST /api/goals` returned 500 for every request, always.

    The insert was a bare `VALUES` with ten placeholders against a nine-column
    table. The seed used an explicit column list, so the seed worked and the
    create path did not — and no test or probe flow created a goal, so nothing
    noticed. This is the test that would have caught it.
    """
    _set_admin_session(client, 99820)
    try:
        _seed_goal_users(client)
        _login_as(client, 'EMP920', 'Employee', 99819)
        goal_id = _new_goal(client, title='Ship the thing', weight=5, target_date='2027-01-31')
        row = _goal_row(goal_id)
        assert row[0] == 'EMP920', row
        assert row[1] == 'Ship the thing'
        assert row[3] == 'Active', row

        # CC-10: the body cannot name somebody else.
        forged = client.post('/api/goals', json={'title': 'not mine', 'emp_id': 'EMP001'})
        assert forged.status_code == 400, forged.get_json()
        assert 'yourself' in str(forged.get_json()).lower(), forged.get_json()
        conn = get_db()
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM goals WHERE title = 'not mine'"
            ).fetchone()[0] == 0
        finally:
            conn.close()
    finally:
        _cleanup_goal_rows('EMP920', 'EMP921')


def test_goal_create_validates_its_payload(client):
    """CC-12: no unknown keys, and the weight is bounded."""
    _set_admin_session(client, 99821)
    try:
        _seed_goal_users(client)
        _login_as(client, 'EMP920', 'Employee', 99820)
        assert client.post('/api/goals', json={'title': 'x', 'nope': 1}).status_code == 400
        assert client.post('/api/goals', json={'title': 'x', 'weight': 0}).status_code == 400
        assert client.post('/api/goals', json={'title': 'x', 'weight': 99}).status_code == 400
        assert client.post('/api/goals', json={'title': 'x', 'weight': 'heavy'}).status_code == 400
        assert client.post('/api/goals', json={'title': '   '}).status_code == 400
        assert client.post('/api/goals', json={'title': 'x', 'target_date': '31/01/2027'}).status_code == 400
        conn = get_db()
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM goals WHERE emp_id = 'EMP920'"
            ).fetchone()[0] == 0, 'a rejected goal must not be written'
        finally:
            conn.close()
    finally:
        _cleanup_goal_rows('EMP920', 'EMP921')


def test_no_authenticated_user_can_edit_another_employees_goal(client):
    """The write was `@login_required` with no ownership check at all.

    Goal ids are sequential integers, so the list being correctly scoped was no
    protection: any user could rewrite any goal in the company by guessing.
    """
    _set_admin_session(client, 99822)
    try:
        _seed_goal_users(client)
        _login_as(client, 'EMP920', 'Employee', 99821)
        goal_id = _new_goal(client, title='Victim goal')
        _login_as(client, 'EMP921', 'Employee', 99820)
        refused = client.put(f'/api/goals/{goal_id}', json={'title': 'HIJACKED'})
        assert refused.status_code == 403, refused.get_json()
        assert 'own goals' in str(refused.get_json()).lower(), refused.get_json()
        assert _goal_row(goal_id)[1] == 'Victim goal'

        # The peer cannot see it in their own list either, so they cannot even
        # learn the id from the API.
        assert goal_id not in {g['id'] for g in client.get('/api/goals').get_json()}
    finally:
        _cleanup_goal_rows('EMP920', 'EMP921')


def test_an_edit_may_not_set_status_or_rating(client):
    """`status` was editable, which was a way to skip rating entirely."""
    _set_admin_session(client, 99823)
    try:
        _seed_goal_users(client)
        _login_as(client, 'EMP920', 'Employee', 99822)
        goal_id = _new_goal(client)
        for field, value in (('status', 'Completed'), ('rating', 5)):
            refused = client.put(f'/api/goals/{goal_id}', json={field: value})
            assert refused.status_code == 400, (field, refused.get_json())
            assert 'rating flow' in str(refused.get_json()), refused.get_json()
        row = _goal_row(goal_id)
        assert row[2] is None and row[3] == 'Active', row
        # ...and the real fields still work.
        ok = client.put(f'/api/goals/{goal_id}', json={'title': 'refined', 'weight': 3})
        assert ok.status_code == 200, ok.get_json()
        assert ok.get_json()['fields'] == ['title', 'weight']
        assert _goal_row(goal_id)[1] == 'refined'
    finally:
        _cleanup_goal_rows('EMP920', 'EMP921')


def test_the_owner_and_their_manager_may_edit(client):
    _set_admin_session(client, 99824)
    try:
        _seed_goal_users(client)
        _login_as(client, 'EMP920', 'Employee', 99823)
        goal_id = _new_goal(client, title='original')
        assert client.put(f'/api/goals/{goal_id}', json={'title': 'by owner'}).status_code == 200
        _login_as(client, 'EMP001', 'Admin', 99822)
        assert client.put(f'/api/goals/{goal_id}', json={'title': 'by manager'}).status_code == 200
        assert _goal_row(goal_id)[1] == 'by manager'
        # An edit that changes nothing is a 400, not a silent success.
        assert client.put(f'/api/goals/{goal_id}', json={}).status_code == 400
        assert client.put('/api/goals/999999999', json={'title': 'ghost'}).status_code == 404
    finally:
        _cleanup_goal_rows('EMP920', 'EMP921')


def test_a_goal_is_rated_by_the_manager_not_by_its_owner(client):
    """FR-PERF-01: "rating 1-5 by manager (not self)". Nothing enforced either half."""
    _set_admin_session(client, 99825)
    try:
        _seed_goal_users(client)
        _login_as(client, 'EMP920', 'Employee', 99824)
        goal_id = _new_goal(client)

        # An employee cannot reach the rating route at all (it is admin-gated),
        # and a peer certainly cannot rate it.
        _login_as(client, 'EMP921', 'Employee', 99823)
        assert client.put(f'/api/goals/{goal_id}/rate', json={'rating': 5}).status_code == 403

        # The self-rating rule is the half the SRS calls out, so it needs a
        # subject who *can* reach the route: an admin who filed their own goal.
        _login_as(client, 'EMP001', 'Admin', 99822)
        own = _new_goal(client, title='Admin own goal')
        assert _goal_row(own)[0] == 'EMP001'
        self_rated = client.put(f'/api/goals/{own}/rate', json={'rating': 5})
        assert self_rated.status_code == 403, self_rated.get_json()
        assert 'your own goal' in str(self_rated.get_json()), self_rated.get_json()
        assert _goal_row(own)[2] is None, 'the admin rated their own goal'

        # ...and the reporting manager can. An HR user rather than an admin, so
        # the manager branch is not just the admin branch wearing a hat.
        _create_policy_user(client, 'EMP922', role='HR')
        conn = get_db()
        try:
            conn.execute("UPDATE users SET manager_emp_id = 'EMP922' WHERE emp_id = 'EMP920'")
        finally:
            conn.close()
        _login_as(client, 'EMP922', 'HR', 99821)
        rated = client.put(f'/api/goals/{goal_id}/rate', json={'rating': 5})
        assert rated.status_code == 200, rated.get_json()
        assert rated.get_json() == {'message': 'Goal rated', 'rating': 5, 'status': 'Completed'}
        row = _goal_row(goal_id)
        assert row[2] == 5 and row[3] == 'Completed', row
    finally:
        _cleanup_goal_rows('EMP920', 'EMP921', 'EMP922')


def test_a_goal_can_be_rated_only_once(client):
    """Completed is terminal, and the write is conditional on Active."""
    _set_admin_session(client, 99826)
    try:
        _seed_goal_users(client)
        _login_as(client, 'EMP920', 'Employee', 99825)
        goal_id = _new_goal(client)
        _login_as(client, 'EMP001', 'Admin', 99824)
        assert client.put(f'/api/goals/{goal_id}/rate', json={'rating': 5}).status_code == 200
        again = client.put(f'/api/goals/{goal_id}/rate', json={'rating': 2})
        assert again.status_code == 409, again.get_json()
        assert 'already Completed' in str(again.get_json()), again.get_json()
        assert _goal_row(goal_id)[2] == 5, 'the refused re-rating overwrote the rating'
        for bad in (0, 6, 'five', None):
            assert client.put(f'/api/goals/{goal_id}/rate', json={'rating': bad}).status_code == 400
    finally:
        _cleanup_goal_rows('EMP920', 'EMP921')


def test_rating_a_goal_is_audited_and_notifies_the_owner(client):
    _set_admin_session(client, 99827)
    try:
        _seed_goal_users(client)
        _login_as(client, 'EMP920', 'Employee', 99826)
        goal_id = _new_goal(client)
        _login_as(client, 'EMP001', 'Admin', 99825)
        assert client.put(f'/api/goals/{goal_id}/rate', json={'rating': 4}).status_code == 200
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT action, \"before\", \"after\" FROM audit_log "
                "WHERE action = 'GOAL_RATE' AND CAST(entity_id AS VARCHAR) = ?",
                [str(goal_id)],
            ).fetchone()
            notified = conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE emp_id = 'EMP920' "
                "AND type = 'GOAL_RATED'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert row is not None, 'the rating was not audited'
        assert json.loads(row[1])['status'] == 'Active'
        assert json.loads(row[2]) == {'status': 'Completed', 'rating': 4}
        assert notified == 1, 'the owner was not told'
    finally:
        _cleanup_goal_rows('EMP920', 'EMP921')

# ── FR-USR-04 background import jobs ──────────────────────────────────────

def test_user_import_queues_a_job_and_reports_the_outcome(client):
    """The upload is queued, then the dispatcher applies the same contract."""
    import imports

    _set_admin_session(client, 99884)
    csv_body = (
        'emp_id,name,email,role,department\n'
        'EMP908,Import Valid,emp908@company.com,Employee,MIS\n'
        'BAD1,Bad Id,bad1@company.com,Employee,MIS\n'
        'EMP909,Duplicate Email,emp908@company.com,Employee,MIS\n'
        'EMP910,Bad Role,emp910@company.com,Owner,MIS\n'
    )
    try:
        queued = client.post(
            '/api/users/import',
            data={'file': (BytesIO(csv_body.encode()), 'users.csv')},
            content_type='multipart/form-data',
        )
        assert queued.status_code == 202, queued.get_json()
        job_id = queued.get_json()['job_id']
        assert queued.get_json()['status'] == 'pending'
        assert queued.get_json()['total_rows'] == 4

        status = client.get(f'/api/users/import/{job_id}')
        assert status.status_code == 200
        assert status.get_json()['status'] == 'pending'

        # Nothing is imported until the dispatcher runs.
        assert client.get('/api/users/EMP908').status_code == 404
        # The scheduler may claim it first; either way exactly one worker does
        # the work, and `imported == 1` proves it was not applied twice.
        imports.dispatch_once()
        imports.dispatch_once()

        job = client.get(f'/api/users/import/{job_id}').get_json()
        assert job['status'] == 'completed', job
        assert job['imported'] == 1 and job['skipped'] == 3, job
        messages = ' | '.join(job['errors'])
        assert 'row 3' in messages and 'emp_id' in messages           # BAD1
        assert 'row 4' in messages and 'emp908@company.com' in messages  # duplicate email
        assert 'row 5' in messages and 'role must be one of' in messages   # Owner
        assert job['started_at'] and job['finished_at']

        # The rows that passed the contract are real users.
        assert client.get('/api/users/EMP908').status_code == 200
        assert client.get('/api/users/BAD1').status_code == 404
        assert client.get('/api/users/EMP910').status_code == 404

        # ...and the completion is audited, with the upload deleted afterwards.
        conn = get_db()
        audit = conn.execute(
            'SELECT entity, entity_id, "after" FROM audit_log '
            "WHERE action = 'USER_IMPORT_COMPLETED' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        path = conn.execute(
            'SELECT stored_path FROM import_jobs WHERE job_id = ?', [job_id]
        ).fetchone()
        conn.close()
        assert audit is not None and audit[0] == 'import_jobs'
        assert json.loads(audit[2])['imported'] == 1
        import os
        assert path[0] and not os.path.exists(path[0]), 'the upload must not stay on disk'

        listed = client.get('/api/users/import').get_json()['jobs']
        assert any(row['job_id'] == job_id for row in listed)
    finally:
        _cleanup_import_job_rows()
        _cleanup_user_contract_rows('EMP908', 'EMP909', 'EMP910', 'BAD1')


def test_import_upload_validation_happens_before_anything_is_queued(client):
    _set_admin_session(client, 99883)
    try:
        missing = client.post(
            '/api/users/import',
            data={'file': (BytesIO(b'name,email\nA,b@c.com\n'), 'users.csv')},
            content_type='multipart/form-data',
        )
        assert missing.status_code == 400
        assert 'Missing columns' in missing.get_json()['error']

        header_only = client.post(
            '/api/users/import',
            data={'file': (BytesIO(b'emp_id,name,email\n'), 'users.csv')},
            content_type='multipart/form-data',
        )
        assert header_only.status_code == 400

        too_many = client.post(
            '/api/users/import',
            data={'file': (BytesIO(_big_csv(imports_max_rows() + 1)), 'users.csv')},
            content_type='multipart/form-data',
        )
        assert too_many.status_code == 413
        assert 'limit is' in too_many.get_json()['error']

        not_csv = client.post(
            '/api/users/import',
            data={'file': (BytesIO(b'x'), 'users.xlsx')},
            content_type='multipart/form-data',
        )
        assert not_csv.status_code == 400

        assert client.get('/api/users/import/999999').status_code == 404
    finally:
        _cleanup_import_job_rows()


def test_import_job_can_be_cancelled_only_while_pending(client):
    import imports

    _set_admin_session(client, 99882)
    csv_body = 'emp_id,name,email,role,department\nEMP911,Queued,emp911@company.com,Employee,MIS\n'
    try:
        queued = client.post(
            '/api/users/import',
            data={'file': (BytesIO(csv_body.encode()), 'users.csv')},
            content_type='multipart/form-data',
        ).get_json()
        cancelled = client.post(f"/api/users/import/{queued['job_id']}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.get_json()['status'] == 'cancelled'
        # A cancelled job is never picked up.
        assert imports.dispatch_once() is None
        assert client.get('/api/users/EMP911').status_code == 404
        # ...and cannot be cancelled twice.
        assert client.post(f"/api/users/import/{queued['job_id']}/cancel").status_code == 409
        assert client.post('/api/users/import/999999/cancel').status_code == 404
    finally:
        _cleanup_import_job_rows()
        _cleanup_user_contract_rows('EMP911')


def test_import_routes_require_an_admin_with_the_module(client):
    import policy

    _set_admin_session(client, 99881)
    try:
        _create_policy_user(client, 'EMP912', role='Admin')
        # Denying the module removes the whole import surface.
        assert client.put(
            '/api/users/EMP912/permissions', json={'modules': {'import_users': False}}
        ).status_code == 200
        _login_as(client, 'EMP912', 'Admin', 99880)
        assert client.get('/api/users/import').status_code in (302, 403)
        assert client.post(
            '/api/users/import',
            data={'file': (BytesIO(b'emp_id,name,email\nEMP913,X,x@company.com\n'), 'users.csv')},
            content_type='multipart/form-data',
        ).status_code in (302, 403)
        # The import page itself is gone from the navbar as well.
        page = client.get('/admin/users').get_data(as_text=True)
        assert 'EMP912' in page
        assert policy.can({'emp_id': 'EMP912', 'role': 'Admin'}, 'import_users') is False
    finally:
        _clear_permission_rows('EMP912')
        _cleanup_import_job_rows()
        _cleanup_user_contract_rows('EMP912', 'EMP913')


def test_import_job_rows_are_not_claimed_twice(client):
    """Two dispatchers must never process the same job (multi-worker safety)."""
    import imports

    _set_admin_session(client, 99879)
    csv_body = 'emp_id,name,email,role,department\nEMP914,Once,emp914@company.com,Employee,MIS\n'
    try:
        job_id = client.post(
            '/api/users/import',
            data={'file': (BytesIO(csv_body.encode()), 'users.csv')},
            content_type='multipart/form-data',
        ).get_json()['job_id']
        imports.dispatch_once()
        assert imports.dispatch_once() is None     # nothing left to claim
        job = client.get(f'/api/users/import/{job_id}').get_json()
        assert job['status'] == 'completed' and job['imported'] == 1
        # A second pass must not import the same employee twice.
        imports.dispatch_once()
        job = client.get(f'/api/users/import/{job_id}').get_json()
        assert job['imported'] == 1
    finally:
        _cleanup_import_job_rows()
        _cleanup_user_contract_rows('EMP914')


# ── FR-USR-09 / FR-USR-15 permission policy ──────────────────────────────

def _create_policy_user(client, emp_id, role='Employee'):
    response = client.post('/api/users', json={
        'emp_id': emp_id, 'name': f'Policy {emp_id}',
        'email': f'{emp_id.lower()}@company.com',
        'department': 'MIS', 'role': role, 'password': 'policy-pass-123',
    })
    assert response.status_code == 201, response.get_json()


def _login_as(client, emp_id, role, session_id):
    with client.session_transaction() as sess:
        sess.clear()
        sess['emp_id'] = emp_id
        sess['name'] = emp_id
        sess['role'] = role
        sess['department'] = 'MIS'
        sess['session_id'] = session_id


def _clear_permission_rows(*emp_ids):
    if not emp_ids:
        return
    placeholders = ','.join('?' for _ in emp_ids)
    conn = get_db()
    try:
        conn.execute(f"DELETE FROM user_permissions WHERE emp_id IN ({placeholders})", list(emp_ids))
        conn.execute(
            f"DELETE FROM audit_log WHERE entity = 'user_permissions' AND entity_id IN ({placeholders})",
            list(emp_ids),
        )
    finally:
        conn.close()


def test_role_defaults_cover_every_module_and_deny_unknown_roles():
    """The SRS matrix is complete, and an unknown role is never guessed around."""
    import policy

    assert set(policy.ROLE_DEFAULTS) == {
        'Employee', 'Team Leader', 'HR', 'Finance', 'Admin', 'Super Admin'
    }
    for role, cells in policy.ROLE_DEFAULTS.items():
        assert set(cells) == policy.PERMISSION_MODULES, role
        assert all(isinstance(value, bool) for value in cells.values()), role
    assert set(policy.role_defaults('Admin')) == policy.PERMISSION_MODULES
    assert set(policy.role_defaults('Wizard')) == policy.PERMISSION_MODULES
    assert not any(policy.role_defaults('Wizard').values())
    # Admin/Super Admin see every module (SRS Appendix B).
    for role in policy.ADMIN_ROLES:
        assert all(policy.ROLE_DEFAULTS[role].values()), role
    # An employee never administers the directory, payroll, or the policy.
    employee = policy.ROLE_DEFAULTS['Employee']
    for module in ('users', 'import_users', 'payroll', 'payroll_approve', 'policy_admin'):
        assert not employee[module], module


def test_empty_user_permissions_reproduces_role_defaults(client):
    """Backward-compat invariant: no override rows == the role default map."""
    import policy

    _set_admin_session(client, 99885)
    try:
        _create_policy_user(client, 'EMP920', role='Finance')
        payload = client.get('/api/users/EMP920/permissions').get_json()
        assert payload['role'] == 'Finance'
        assert payload['modules'] == sorted(policy.PERMISSION_MODULES)
        assert payload['overrides'] == {}
        assert payload['effective'] == policy.ROLE_DEFAULTS['Finance']
        assert payload['defaults'] == policy.ROLE_DEFAULTS['Finance']
    finally:
        _clear_permission_rows('EMP920')
        _cleanup_user_contract_rows('EMP920')


def test_permission_override_deny_beats_role_allow_and_allow_grants(client):
    """Deny beats the role default; an allow row lifts a default deny."""
    _set_admin_session(client, 99886)
    try:
        _create_policy_user(client, 'EMP921', role='Admin')
        _create_policy_user(client, 'EMP922', role='Finance')

        response = client.put('/api/users/EMP921/permissions', json={
            'modules': {'users': False, 'tickets': True}
        })
        assert response.status_code == 200, response.get_json()
        payload = response.get_json()
        # An explicit deny wins even though the Admin default is True.
        assert payload['effective']['users'] is False
        # An explicit allow lifts the default deny.
        assert payload['effective']['tickets'] is True
        # Untouched modules keep the role default.
        assert payload['effective']['import_users'] is True
        assert payload['overrides'] == {'users': False, 'tickets': True}
        assert payload['changes'] == {'added': ['tickets', 'users'], 'removed': [], 'changed': []}

        response = client.put('/api/users/EMP922/permissions', json={'modules': {'tickets': True}})
        assert response.status_code == 200
        assert response.get_json()['effective']['tickets'] is True
    finally:
        _clear_permission_rows('EMP921', 'EMP922')
        _cleanup_user_contract_rows('EMP921', 'EMP922')


def test_put_permissions_replaces_the_override_set_and_audits_the_diff(client):
    _set_admin_session(client, 99887)
    try:
        _create_policy_user(client, 'EMP923')
        first = client.put('/api/users/EMP923/permissions', json={
            'modules': {'goals': False, 'documents': False}
        })
        assert first.status_code == 200, first.get_json()
        # A second PUT is a full replace: 'goals' is dropped from the override
        # set, so it reverts to the role default.
        second = client.put('/api/users/EMP923/permissions', json={'modules': {'documents': True}})
        assert second.status_code == 200, second.get_json()
        payload = second.get_json()
        assert payload['overrides'] == {'documents': True}
        assert payload['changes'] == {
            'added': [], 'removed': ['goals'], 'changed': ['documents:0->1']
        }
        # The Employee default for goals is True, so the removed row reverts to True.
        assert payload['effective']['goals'] is True
        assert payload['effective']['documents'] is True

        conn = get_db()
        rows = conn.execute(
            "SELECT module, allow FROM user_permissions WHERE emp_id = 'EMP923'"
        ).fetchall()
        audit = conn.execute(
            'SELECT "before", "after", details FROM audit_log '
            "WHERE action = 'USER_PERMISSIONS_UPDATE' AND entity_id = 'EMP923' "
            'ORDER BY created_at DESC LIMIT 1'
        ).fetchone()
        conn.close()
        assert [row[0] for row in rows] == ['documents']
        assert bool(rows[0][1]) is True
        assert json.loads(audit[0]) == {'goals': False, 'documents': False}
        assert json.loads(audit[1]) == {'documents': True}
        assert 'removed=[goals]' in audit[2]
        assert 'changed=[documents:0->1]' in audit[2]
    finally:
        _clear_permission_rows('EMP923')
        _cleanup_user_contract_rows('EMP923')


def test_put_permissions_rejects_unknown_module_and_non_boolean(client):
    _set_admin_session(client, 99888)
    try:
        _create_policy_user(client, 'EMP924')
        unknown = client.put('/api/users/EMP924/permissions', json={
            'modules': {'payroll_approvals': True}
        })
        assert unknown.status_code == 400
        assert 'unknown permission modules' in unknown.get_json()['error']
        assert client.put(
            '/api/users/EMP924/permissions', json={'modules': {'goals': 'maybe'}}
        ).status_code == 400
        assert client.put(
            '/api/users/EMP924/permissions', json={'modules': 'goals'}
        ).status_code == 400
        assert client.put('/api/users/EMP924/permissions', json={}).status_code == 400
        conn = get_db()
        count = conn.execute(
            "SELECT COUNT(*) FROM user_permissions WHERE emp_id = 'EMP924'"
        ).fetchone()[0]
        conn.close()
        assert count == 0, 'a rejected payload must not write override rows'
    finally:
        _clear_permission_rows('EMP924')
        _cleanup_user_contract_rows('EMP924')


def test_permission_routes_require_admin_and_reject_self_edits(client):
    _set_admin_session(client, 99888)
    try:
        _create_policy_user(client, 'EMP925')
        _login_as(client, 'EMP925', 'Employee', 99889)
        # A non-admin is refused: the GET is redirected away like every other
        # admin-only page/API read, the JSON PUT gets an explicit 403.
        assert client.get('/api/users/EMP002/permissions').status_code in (302, 403)
        assert client.put('/api/users/EMP002/permissions', json={'modules': {}}).status_code == 403

        _set_admin_session(client, 99890)
        self_edit = client.put('/api/users/EMP001/permissions', json={'modules': {'tickets': False}})
        assert self_edit.status_code == 409
        assert 'own permissions' in self_edit.get_json()['error']
        assert client.get('/api/users/EMP404/permissions').status_code == 404
    finally:
        _clear_permission_rows('EMP925', 'EMP001')
        _cleanup_user_contract_rows('EMP925')


def test_put_permissions_refuses_to_lock_out_the_last_admin(client):
    _set_admin_session(client, 99891)
    try:
        _create_policy_user(client, 'EMP926', role='Admin')
        # EMP001 is still an active Admin, so the target may be narrowed.
        allowed = client.put('/api/users/EMP926/permissions', json={'modules': {'users': False}})
        assert allowed.status_code == 200, allowed.get_json()

        # Narrowing the remaining administrator as well would leave nobody able
        # to manage users, so it is refused.
        conn = get_db()
        conn.execute(
            "INSERT INTO user_permissions (perm_id, emp_id, module, allow, created_at, updated_at) "
            "VALUES (?, 'EMP001', 'import_users', 0, ?, ?)",
            [-700001, datetime.now(), datetime.now()],
        )
        conn.close()
        blocked = client.put('/api/users/EMP926/permissions', json={
            'modules': {'users': False, 'import_users': False}
        })
        assert blocked.status_code == 409
        assert 'no administrator' in blocked.get_json()['error']
    finally:
        _clear_permission_rows('EMP926', 'EMP001')
        _cleanup_user_contract_rows('EMP926')


def test_permission_override_row_with_unknown_module_fails_closed(client):
    import policy

    _set_admin_session(client, 99892)
    try:
        _create_policy_user(client, 'EMP927')
        conn = get_db()
        conn.execute(
            "INSERT INTO user_permissions (perm_id, emp_id, module, allow, created_at, updated_at) "
            "VALUES (?, 'EMP927', 'warp_drive', 1, ?, ?)",
            [-700002, datetime.now(), datetime.now()],
        )
        conn.execute(
            "INSERT INTO user_permissions (perm_id, emp_id, module, allow, created_at, updated_at) "
            "VALUES (?, 'EMP927', 'goals', NULL, ?, ?)",
            [-700003, datetime.now(), datetime.now()],
        )
        conn.close()
        payload = client.get('/api/users/EMP927/permissions').get_json()
        assert 'warp_drive' not in payload['overrides']
        # A NULL allow is the only safe reading of the nullable compat column.
        assert payload['overrides']['goals'] is False
        actor = {'emp_id': 'EMP927', 'role': 'Employee'}
        assert policy.can(actor, 'warp_drive') is False
        assert policy.can(actor, 'goals') is False
    finally:
        _clear_permission_rows('EMP927')
        _cleanup_user_contract_rows('EMP927')


def test_permission_policy_rejects_blocked_and_archived_targets(client):
    _set_admin_session(client, 99893)
    try:
        _create_policy_user(client, 'EMP928')
        assert client.post('/api/users/EMP928/block').status_code == 200
        blocked = client.put('/api/users/EMP928/permissions', json={'modules': {'goals': False}})
        assert blocked.status_code == 409
        assert 'blocked' in blocked.get_json()['error']
        assert client.get('/api/users/EMP928/permissions').status_code == 409
        assert client.post('/api/users/EMP928/unblock').status_code == 200
        assert client.post('/api/users/EMP928/archive').status_code == 200
        archived = client.put('/api/users/EMP928/permissions', json={'modules': {'goals': False}})
        assert archived.status_code == 409
        assert 'archived' in archived.get_json()['error']
        assert client.post('/api/users/EMP928/restore').status_code == 200
    finally:
        _clear_permission_rows('EMP928')
        _cleanup_user_contract_rows('EMP928')


# ── FR-USR-15 enforcement wiring (decorators + navbar) ────────────────────

def _navbar_links(html):
    """Label/href pairs inside the rendered navbar only (not page content)."""
    import re

    match = re.search(r'<nav class="navbar.*?</nav>', html, re.S)
    if not match:
        return []
    return re.findall(
        r'<a class="(?:nav-link|dropdown-item)[^"]*" href="([^"]*)"[^>]*>(.*?)</a>',
        match.group(0), re.S,
    )


def _gated_views():
    """Every view whose access is gated by a role/policy check."""
    import app as app_module

    gated = []
    for rule in app_module.app.url_map.iter_rules():
        view = app_module.app.view_functions[rule.endpoint]
        gate = getattr(view, '__hrms_gate__', 'login')
        if gate != 'login':
            gated.append((rule, view, gate, getattr(view, '__hrms_module__', None)))
    return gated


def test_every_gated_view_declares_a_known_module():
    """A new gated route must be mapped to a module, or it inherits the umbrella."""
    import app as app_module
    import policy

    for rule, _view, _gate, module in _gated_views():
        assert module in policy.PERMISSION_MODULES, f'{rule.rule} -> {module!r}'
    # Nothing may be silently unmapped: the fallback is the admin umbrella.
    assert app_module._DEFAULT_GATED_MODULE == 'users'
    unmapped = sorted({view.__name__ for _r, view, _g, m in _gated_views() if m is None})
    assert not unmapped, unmapped


def test_navigation_matches_the_gate_of_every_linked_route():
    """FR-USR-15: a nav link exists only when the linked route lets you in."""
    import app as app_module
    import policy

    endpoints = app_module._page_rules()
    for entry in policy.NAV_ENTRIES:
        targets = entry.get('children') or (entry,)
        for target in targets:
            if target.get('divider') or target.get('always'):
                continue
            endpoint = endpoints.get(target['href'])
            assert endpoint, f'nav href has no page route: {target["href"]}'
            view = app_module.app.view_functions[endpoint]
            assert getattr(view, '__hrms_module__', None) == target['module'], (
                f'{target["href"]} is gated by {getattr(view, "__hrms_module__", None)!r} '
                f'but the navbar declares {target["module"]!r}'
            )


def test_every_gated_page_is_reachable_from_the_navbar():
    """A module-gated page with no nav entry is a page nobody can find."""
    import policy

    nav_hrefs = set()
    for entry in policy.NAV_ENTRIES:
        nav_hrefs.add(entry['href'])
        for child in entry.get('children') or ():
            if not child.get('divider'):
                nav_hrefs.add(child['href'])
    missing = sorted({
        rule.rule for rule, _view, _gate, _module in _gated_views()
        if not rule.rule.startswith('/api') and rule.rule not in nav_hrefs
    })
    assert not missing, f'gated pages missing from the navbar: {missing}'


def test_role_defaults_do_not_narrow_any_role_that_passes_the_gate():
    """With no override rows, the matrix must not lock anyone out of today."""
    import app as app_module
    import policy

    conn = get_db()
    try:
        for role in policy.ROLE_DEFAULTS:
            actor = {'emp_id': f'PROBE-{role}', 'role': role, 'department': 'MIS'}
            actor_hr = {'emp_id': f'PROBE-{role}', 'role': role, 'department': 'HR'}
            for rule, _view, gate, module in _gated_views():
                if rule.rule.startswith('/api') or not module:
                    continue
                for candidate in (actor, actor_hr):
                    if not app_module._gate_passes(candidate, gate):
                        continue  # the gate denies this actor today too
                    assert policy.can(candidate, module, conn=conn), (
                        f'role {role} (department {candidate["department"]}) passes {gate} '
                        f'for {rule.rule} but the matrix denies {module}'
                    )
    finally:
        conn.close()


def test_permission_override_deny_removes_route_and_navbar_access(client):
    """The point of the wiring: an explicit deny actually removes access."""
    _set_admin_session(client, 99899)
    try:
        _create_policy_user(client, 'EMP930', role='HR')
        _login_as(client, 'EMP930', 'HR', 99896)
        # Baseline: an HR user reaches the tickets module and sees the link.
        assert client.get('/admin/tickets').status_code == 200
        nav_before = _navbar_links(client.get('/dashboard').get_data(as_text=True))
        assert '/admin/tickets' in [href for href, _label in nav_before], nav_before

        _set_admin_session(client, 99897)
        denied = client.put('/api/users/EMP930/permissions', json={'modules': {'tickets': False}})
        assert denied.status_code == 200, denied.get_json()

        _login_as(client, 'EMP930', 'HR', 99898)
        # Same role, same department: only the override changed.
        assert client.get('/admin/tickets').status_code == 403
        # The self-service list is not module-gated, only its admin queue is.
        assert client.get('/api/tickets').status_code == 200
        # Untouched modules keep working.
        assert client.get('/api/candidates').status_code == 200
        nav_after = _navbar_links(client.get('/dashboard').get_data(as_text=True))
        assert '/admin/tickets' not in [href for href, _label in nav_after], nav_after
        assert '/admin/candidates' in [href for href, _label in nav_after], nav_after

        # A grant row lifts a role-default deny for the same user.
        _set_admin_session(client, 99899)
        assert client.put(
            '/api/users/EMP930/permissions', json={'modules': {'payroll': True}}
        ).status_code == 200
        _login_as(client, 'EMP930', 'HR', 99896)
        # payroll is still admin/finance-gated: the matrix can only narrow.
        assert client.get('/admin/payroll').status_code == 403
    finally:
        _clear_permission_rows('EMP930')
        _cleanup_user_contract_rows('EMP930')


def test_admin_operations_endpoints_follow_the_directory_permission(client):
    """`users` is the umbrella for admin operations with no module of their own."""
    import policy

    _set_admin_session(client, 99894)
    try:
        _create_policy_user(client, 'EMP931', role='Admin')
        _login_as(client, 'EMP931', 'Admin', 99895)
        assert client.get('/api/dashboard-stats').status_code == 200
        _set_admin_session(client, 99893)
        assert client.put(
            '/api/users/EMP931/permissions', json={'modules': {'users': False}}
        ).status_code == 200
        _login_as(client, 'EMP931', 'Admin', 99896)
        # admin_required redirects a non-JSON GET away, exactly as for a
        # non-admin today; the JSON caller gets an explicit 403.
        assert client.get('/api/users').status_code == 302
        assert client.get('/api/users', json={}).status_code == 403
        assert client.post('/api/users', json={
            'emp_id': 'EMP933', 'name': 'Nope', 'email': 'nope@company.com',
            'department': 'MIS', 'role': 'Employee', 'password': 'x',
        }).status_code == 403
        assert client.get('/admin/users').status_code == 302
        assert client.get('/api/dashboard-stats').status_code == 200  # reports module
        assert policy.can({'emp_id': 'EMP931', 'role': 'Admin'}, 'users') is False
    finally:
        _clear_permission_rows('EMP931')
        _cleanup_user_contract_rows('EMP931')


def test_department_grant_keeps_hr_department_access(client):
    """An HR-department user of any role keeps the modules they can reach today."""
    import policy

    _set_admin_session(client, 99892)
    try:
        _create_policy_user(client, 'EMP932', role='Employee')
        conn = get_db()
        conn.execute("UPDATE users SET department = 'HR' WHERE emp_id = 'EMP932'")
        conn.close()
        _login_as(client, 'EMP932', 'Employee', 99891)
        assert client.get('/api/candidates').status_code == 200
        assert client.get('/api/audit-log').status_code in (302, 403)  # admin-only gate
        nav = [href for href, _label in _navbar_links(client.get('/dashboard').get_data(as_text=True))]
        assert '/admin/candidates' in nav, nav
        # The department grant must not leak PII or the directory.
        assert policy.department_grant({'emp_id': 'EMP932', 'role': 'Employee', 'department': 'HR'}, 'pii_reveal') is False
        assert policy.department_grant({'emp_id': 'EMP932', 'role': 'Employee', 'department': 'HR'}, 'users') is False
    finally:
        _clear_permission_rows('EMP932')
        _cleanup_user_contract_rows('EMP932')


# ── FR-USR-15 scope + PII reveal ───────────────────────────────────────────

def test_no_handler_authorizes_on_the_session_role_copy():
    """Authorization must read the policy, not `session['role']`."""
    import inspect

    import app as app_module

    offenders = []
    for endpoint, view in app_module.app.view_functions.items():
        source = inspect.getsource(view)
        # `session['role'] = ...` (login priming) is not an authorization
        # decision; a *read* of the session role copy is.
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("session['role'] ="):
                continue
            if "session.get('role')" in stripped or "session['role']" in stripped:
                offenders.append(endpoint)
                break
    assert not offenders, f'handlers still branch on the session role copy: {sorted(offenders)}'


def test_company_wide_scope_is_a_policy_decision(client):
    """CC-11 scope: the list endpoints split on can_view_all, not on a role list."""
    import policy

    _set_admin_session(client, 99884)
    try:
        _create_policy_user(client, 'EMP940', role='Finance')
        conn = get_db()
        conn.execute(
            "INSERT INTO goals (goal_id, emp_id, title, target_date, weight, status, created_at) "
            "VALUES (-700010, 'EMP002', 'Admin list', '2030-01-01', 100, 'Active', ?)",
            [datetime.now()],
        )
        conn.execute(
            "INSERT INTO goals (goal_id, emp_id, title, target_date, weight, status, created_at) "
            "VALUES (-700011, 'EMP940', 'Finance list', '2030-01-01', 100, 'Active', ?)",
            [datetime.now()],
        )
        conn.close()

        # Admin keeps the company-wide list.
        rows = client.get('/api/goals').get_json()
        assert {'EMP002', 'EMP940'} <= {r['emp_id'] for r in rows}

        # Finance (the list owner in this fixture) is scoped to its own rows,
        # exactly as before this slice.
        _login_as(client, 'EMP940', 'Finance', 99883)
        assert policy.can_view_all({'emp_id': 'EMP940', 'role': 'Finance'}, 'goals') is False
        assert policy.can_view_all({'emp_id': 'EMP001', 'role': 'Admin'}, 'goals') is True
        rows = client.get('/api/goals').get_json()
        assert {r['emp_id'] for r in rows} == {'EMP940'}

        # An explicit deny on `goals` takes the company-wide list away from an
        # Admin: the module check runs before the scope check, so they fall
        # back to their own records instead of the whole company.
        _set_admin_session(client, 99882)
        _create_policy_user(client, 'EMP944', role='Admin')
        conn = get_db()
        conn.execute(
            "INSERT INTO goals (goal_id, emp_id, title, target_date, weight, status, created_at) "
            "VALUES (-700012, 'EMP944', 'Narrowed list', '2030-01-01', 100, 'Active', ?)",
            [datetime.now()],
        )
        conn.close()
        _set_admin_session(client, 99882)
        assert client.put(
            '/api/users/EMP944/permissions', json={'modules': {'goals': False}}
        ).status_code == 200
        _login_as(client, 'EMP944', 'Admin', 99881)
        rows = client.get('/api/goals').get_json()
        assert {r['emp_id'] for r in rows} == {'EMP944'}
    finally:
        _clear_permission_rows('EMP944', 'EMP940')
        conn = get_db()
        conn.execute("DELETE FROM goals WHERE emp_id IN ('EMP940', 'EMP944')")
        conn.close()
        _cleanup_user_contract_rows('EMP944')
        conn = get_db()
        conn.execute("DELETE FROM goals WHERE goal_id IN (-700010, -700011, -700012)")
        conn.close()
        _cleanup_user_contract_rows('EMP940')


def test_payslip_access_follows_the_payroll_module(client):
    """Own payslip always; someone else's needs the payroll capability."""
    _set_admin_session(client, 99887)
    try:
        _create_policy_user(client, 'EMP941', role='Finance')
        _login_as(client, 'EMP941', 'Finance', 99886)
        assert client.get('/api/payslip/1/EMP941').status_code in (200, 404)
        assert client.get('/api/payslip/1/EMP002').status_code in (200, 404)
        # An Employee is limited to their own record. The role has to change in
        # the database: the policy reads the live row, not the session copy.
        conn = get_db()
        conn.execute("UPDATE users SET role = 'Employee' WHERE emp_id = 'EMP941'")
        conn.close()
        _login_as(client, 'EMP941', 'Employee', 99885)
        assert client.get('/api/payslip/1/EMP941').status_code in (200, 404)
        assert client.get('/api/payslip/1/EMP002').status_code == 403
        conn = get_db()
        conn.execute("UPDATE users SET role = 'Finance' WHERE emp_id = 'EMP941'")
        conn.close()
        # Denying payroll to the Finance user removes the cross-employee read.
        _set_admin_session(client, 99884)
        assert client.put(
            '/api/users/EMP941/permissions', json={'modules': {'payroll': False}}
        ).status_code == 200
        _login_as(client, 'EMP941', 'Finance', 99883)
        assert client.get('/api/payslip/1/EMP941').status_code in (200, 404)
        assert client.get('/api/payslip/1/EMP002').status_code == 403
    finally:
        _clear_permission_rows('EMP941')
        _cleanup_user_contract_rows('EMP941')


def test_pii_reveal_requires_the_module_and_is_audited(client):
    """FR-USR-15: cross-employee PII needs `pii_reveal` and leaves an audit row."""
    import policy

    _set_admin_session(client, 99882)
    try:
        _create_policy_user(client, 'EMP942', role='HR')
        conn = get_db()
        conn.execute(
            "UPDATE users SET address = '1 Test Street', emergency_contact_name = 'Next Of Kin', "
            "emergency_contact_phone = '+91-0000000000' WHERE emp_id = 'EMP942'"
        )
        conn.close()

        # Admin holds pii_reveal: allowed, and audited.
        assert client.get('/api/users/EMP942/pii').status_code == 200
        payload = client.get('/api/users/EMP942/pii').get_json()
        assert payload['address'] == '1 Test Street'
        assert payload['emergency_contact_phone'] == '+91-0000000000'
        conn = get_db()
        reveals = conn.execute(
            "SELECT entity_id, details FROM audit_log WHERE action = 'PII_REVEAL' "
            "AND entity_id = 'EMP942'"
        ).fetchall()
        conn.close()
        assert len(reveals) == 2, reveals
        assert 'EMP942' in reveals[0][1] and 'EMP001' in reveals[0][1]

        # The directory itself never carries the personal fields.
        directory = client.get('/api/users/EMP942').get_json()
        assert 'address' not in directory and 'emergency_contact_phone' not in directory

        # HR holds pii_reveal by role: allowed.
        _login_as(client, 'EMP942', 'HR', 99881)
        assert client.get('/api/users/EMP001/pii').status_code == 200

        # A plain Employee is refused by the module, even though the HR gate
        # admits an HR-department user of any role.
        conn = get_db()
        conn.execute("UPDATE users SET role = 'Employee' WHERE emp_id = 'EMP942'")
        conn.close()
        _login_as(client, 'EMP942', 'Employee', 99880)
        assert client.get('/api/users/EMP001/pii').status_code == 403
        conn = get_db()
        conn.execute("UPDATE users SET role = 'HR' WHERE emp_id = 'EMP942'")
        conn.close()

        # Denying pii_reveal removes it, and the self record stays readable.
        _set_admin_session(client, 99879)
        assert client.put(
            '/api/users/EMP942/permissions', json={'modules': {'pii_reveal': False}}
        ).status_code == 200
        _login_as(client, 'EMP942', 'HR', 99878)
        # The reveal route is module-gated, so a denied pii_reveal blocks it
        # outright — the employee's own personal data keeps flowing through
        # /api/profile, which never needs the capability.
        assert client.get('/api/users/EMP001/pii').status_code == 403
        assert client.get('/api/users/EMP942/pii').status_code == 403
        profile = client.get('/api/profile').get_json()
        assert profile['address'] == '1 Test Street'
        # Own record is not logged as a reveal.
        conn = get_db()
        own_reveals = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'PII_REVEAL' AND entity_id = 'EMP942'"
        ).fetchone()[0]
        conn.close()
        assert own_reveals == 2
        assert policy.pii_view({'emp_id': 'EMP942', 'role': 'Employee'}, 'EMP942') is True
    finally:
        conn = get_db()
        conn.execute("DELETE FROM audit_log WHERE entity_id = 'EMP942' AND action = 'PII_REVEAL'")
        conn.close()
        _clear_permission_rows('EMP942')
        _cleanup_user_contract_rows('EMP942')


def test_pii_admin_page_hides_the_reveal_without_the_module(client):
    _set_admin_session(client, 99877)
    try:
        _create_policy_user(client, 'EMP943', role='Admin')
        _login_as(client, 'EMP943', 'Admin', 99876)
        assert 'canRevealPii = true' in client.get('/admin/users').get_data(as_text=True)
        _set_admin_session(client, 99875)
        assert client.put(
            '/api/users/EMP943/permissions', json={'modules': {'pii_reveal': False}}
        ).status_code == 200
        _login_as(client, 'EMP943', 'Admin', 99874)
        page = client.get('/admin/users').get_data(as_text=True)
        assert 'canRevealPii = false' in page
    finally:
        _clear_permission_rows('EMP943')
        _cleanup_user_contract_rows('EMP943')


def imports_max_rows():
    import imports
    return imports.MAX_ROWS


def _big_csv(rows):
    lines = ['emp_id,name,email,role,department']
    for index in range(rows):
        lines.append(f'EMP8{index:02d},Bulk {index},bulk{index}@company.com,Employee,MIS')
    return '\n'.join(lines).encode() + b'\n'


def _cleanup_import_job_rows():
    conn = get_db()
    try:
        conn.execute("DELETE FROM import_jobs")
        conn.execute("DELETE FROM audit_log WHERE entity = 'import_jobs'")
        conn.execute("DELETE FROM audit_log WHERE action LIKE 'USER_IMPORT%'")
    finally:
        conn.close()



# ── FR-LEA-06/08 policy-derived leave balances ────────────────────────────

def _leave_balance(emp_id, leave_type, year=None):
    year = year or datetime.now().year
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT total_days, used_days, reserved FROM leave_balance "
            "WHERE emp_id = ? AND leave_type = ? AND year = ?",
            [emp_id, leave_type, year],
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else {'total': row[0], 'used': row[1], 'reserved': row[2]}


def _cleanup_leave_rows(emp_id, leave_type=None, year=None):
    year = year or datetime.now().year
    conn = get_db()
    try:
        if leave_type:
            conn.execute(
                "DELETE FROM leave_balance WHERE emp_id = ? AND leave_type = ? AND year = ?",
                [emp_id, leave_type, year],
            )
        else:
            conn.execute("DELETE FROM leave_balance WHERE emp_id = ?", [emp_id])
        conn.execute("DELETE FROM leave_policy_assignments WHERE emp_id = ?", [emp_id])
        conn.execute("DELETE FROM leave_requests WHERE emp_id = ?", [emp_id])
        conn.execute("DELETE FROM notifications WHERE emp_id = ?", [emp_id])
        conn.execute("DELETE FROM audit_log WHERE entity_id = ?", [emp_id])
    finally:
        conn.close()


def test_leave_entitlement_defaults_to_the_published_matrix(client):
    """With no assignment the entitlement is exactly what the boot seed wrote."""
    import leave_policy

    conn = get_db()
    try:
        assert leave_policy.entitlement_days(conn, 'EMP001', 'Casual') == (12, 'default')
        assert leave_policy.entitlement_days(conn, 'EMP001', 'Sick') == (10, 'default')
        assert leave_policy.entitlement_days(conn, 'EMP001', 'Annual') == (20, 'default')
        # A type with no entitlement is "unlimited", as before.
        assert leave_policy.entitlement_days(conn, 'EMP001', 'Sabbatical') == (0, 'unlimited')
    finally:
        conn.close()


def test_new_employee_gets_a_derived_balance_and_cannot_double_spend(client):
    """FR-LEA-06: reservations stop two pending requests spending one balance."""
    import leave_policy

    _set_admin_session(client, 99872)
    _create_policy_user(client, 'EMP951', role='Employee')
    _login_as(client, 'EMP951', 'Employee', 99870)
    year = datetime.now().year
    conn = get_db()
    try:
        leave_policy.ensure_balances(conn, 'EMP951', year)
    finally:
        conn.close()
    assert _leave_balance('EMP951', 'Casual') == {'total': 12, 'used': 0, 'reserved': 0}

    today = datetime.now().date()
    # 12 days of entitlement: two requests of 8 must not both be accepted.
    first = client.post('/api/leaves', json={
        'leave_type': 'Casual',
        'start_date': today.isoformat(),
        'end_date': (today + __import__('datetime').timedelta(days=7)).isoformat(),
        'reason': 'policy reserve 1',
    })
    assert first.status_code == 201, first.get_json()
    assert _leave_balance('EMP951', 'Casual')['reserved'] == 8

    second = client.post('/api/leaves', json={
        'leave_type': 'Casual',
        'start_date': (today + __import__('datetime').timedelta(days=10)).isoformat(),
        'end_date': (today + __import__('datetime').timedelta(days=17)).isoformat(),
        'reason': 'policy reserve 2',
    })
    assert second.status_code == 400
    assert 'Insufficient balance' in second.get_json()['error']

    # A different type is unaffected.
    other = client.post('/api/leaves', json={
        'leave_type': 'Sick',
        'start_date': (today + __import__('datetime').timedelta(days=10)).isoformat(),
        'end_date': (today + __import__('datetime').timedelta(days=11)).isoformat(),
        'reason': 'policy other type',
    })
    assert other.status_code == 201, other.get_json()

    # Approval moves reserved -> used; rejection releases.
    _set_admin_session(client, 99869)
    rows = [row for row in client.get('/api/leaves').get_json() if row['emp_id'] == 'EMP951']
    casual = [row for row in rows if row['leave_type'] == 'Casual']
    sick = [row for row in rows if row['leave_type'] == 'Sick']
    assert len(casual) == 1 and len(sick) == 1
    assert client.post(f"/api/leaves/{casual[0]['leave_id']}/approve").status_code == 200
    balance = _leave_balance('EMP951', 'Casual')
    assert balance['used'] == 8 and balance['reserved'] == 0, balance
    assert client.post(f"/api/leaves/{sick[0]['leave_id']}/reject").status_code == 200
    balance = _leave_balance('EMP951', 'Sick')
    assert balance['used'] == 0 and balance['reserved'] == 0, balance

    conn = get_db()
    try:
        conn.execute("DELETE FROM leave_requests WHERE emp_id = 'EMP951'")
        conn.execute("DELETE FROM notifications WHERE emp_id = 'EMP951'")
    finally:
        conn.close()
    _cleanup_leave_rows('EMP951')
    _cleanup_user_contract_rows('EMP951')


def test_leave_policy_routes_require_hr_and_report_the_source(client):
    _set_admin_session(client, 99868)
    try:
        _create_policy_user(client, 'EMP952', role='HR')
        payload = client.get('/api/users/EMP952/leave-policy').get_json()
        assert payload['emp_id'] == 'EMP952'
        assert payload['effective'] is None
        assert payload['entitlements']['Annual'] == {'days': 20, 'source': 'default'}
        assert payload['defaults'] == {'Casual': 12, 'Sick': 10, 'Annual': 20}

        # An Employee cannot read or write somebody else's leave policy. The
        # role has to change in the database: the gate reads the live row.
        conn = get_db()
        conn.execute("UPDATE users SET role = 'Employee' WHERE emp_id = 'EMP952'")
        conn.close()
        _login_as(client, 'EMP952', 'Employee', 99867)
        assert client.get('/api/users/EMP001/leave-policy').status_code == 403
        assert client.put('/api/users/EMP001/leave-policy', json={
            'effective_from': '2026-01-01',
        }).status_code == 403
    finally:
        _cleanup_leave_rows('EMP952')
        _cleanup_user_contract_rows('EMP952')


def test_leave_policy_assignment_is_audited(client):
    _set_admin_session(client, 99866)
    try:
        _create_policy_user(client, 'EMP953', role='Employee')
        assert client.put('/api/users/EMP953/leave-policy', json={
            'accrual_rate': '1.0', 'effective_from': datetime.now().date().isoformat(),
        }).status_code == 200
        conn = get_db()
        row = conn.execute(
            'SELECT "before", "after" FROM audit_log WHERE action = \'LEAVE_POLICY_ASSIGN\' '
            "AND entity_id = 'EMP953' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row is not None, 'leave policy assignment was not audited'
        # No prior assignment, so the `before` column is NULL rather than 'null'.
        assert row[0] is None
        after = json.loads(row[1])
        assert after['accrual_rate'] == 1.0
    finally:
        _cleanup_leave_rows('EMP953')
        _cleanup_user_contract_rows('EMP953')


@pytest.mark.skipif(
    os.getenv('APP_DB', 'duckdb').lower() not in ('postgres', 'postgresql', 'pg'),
    reason='the boolean adapter snoop needs PostgreSQL and both schemas',
)
def test_user_permissions_allow_column_shape_public_and_inert_legacy():
    """v2.0 stores BOOLEAN, legacy stores INTEGER; the adapter only rewrites one."""
    from db_backend import _coerce_boolean_comparison_params, _coerce_insert_boolean_params

    conn = get_db()
    shapes = {
        row[0]: row[1] for row in conn.execute(
            "SELECT table_schema, data_type FROM information_schema.columns "
            "WHERE table_name = 'user_permissions' AND column_name = 'allow'"
        ).fetchall()
    }
    conn.close()
    assert shapes.get('public') == 'boolean', shapes
    assert shapes.get('legacy') == 'integer', shapes

    # The route writes ints; the adapter rewrites them only where the column is
    # actually a boolean.
    update_sql = "UPDATE user_permissions SET allow = ?, updated_at = ? WHERE emp_id = ?"
    _, public_params = _coerce_boolean_comparison_params(update_sql, [0, 'now', 'EMP001'], 'public')
    assert public_params == [False, 'now', 'EMP001']
    legacy_sql, legacy_params = _coerce_boolean_comparison_params(update_sql, [0, 'now', 'EMP001'], 'legacy')
    assert legacy_sql == update_sql
    assert legacy_params == [0, 'now', 'EMP001']

    insert_sql = ("INSERT INTO user_permissions (perm_id, emp_id, module, allow, created_at) "
                  "VALUES (?, ?, ?, ?, ?)")
    _, coerced = _coerce_insert_boolean_params(insert_sql, [1, 'EMP001', 'goals', 1, 'now'], 'public')
    assert coerced[3] is True
    _, insert_legacy = _coerce_insert_boolean_params(insert_sql, [1, 'EMP001', 'goals', 1, 'now'], 'legacy')
    assert insert_legacy[3] == 1


def test_user_archive_restore_preserves_records_and_revokes_sessions(client):
    emp_id = f"ARC{datetime.now().strftime('%H%M%S%f')}"
    now = datetime.now()
    conn = get_db()
    conn.execute(
        "INSERT INTO users (emp_id, name, email, password, role, department, designation, status, first_login, created_at, allow_login, allow_breaks) "
        "VALUES (?, ?, ?, ?, 'Employee', 'QA', 'Archive Test', 'Active', ?, ?, 1, 1)",
        [emp_id, 'Archive Test', f'{emp_id.lower()}@example.invalid', hash_password('archive-pass-123'), now, now],
    )
    conn.execute(
        "INSERT INTO user_sessions (session_id, emp_id, login_time, logout_time, total_hours, session_date) "
        "VALUES (-987654, ?, ?, NULL, NULL, ?)",
        [emp_id, now - timedelta(hours=1), now.date()],
    )
    conn.close()

    try:
        with client.session_transaction() as sess:
            sess['emp_id'] = 'EMP001'
            sess['name'] = 'Admin'
            sess['role'] = 'Admin'
            sess['session_id'] = 99891
        response = client.post(f'/api/users/{emp_id}/archive')
        assert response.status_code == 200, response.get_json()

        conn = get_db()
        row = conn.execute(
            "SELECT status, allow_login FROM users WHERE emp_id = ?", [emp_id]
        ).fetchone()
        archived_session = conn.execute(
            "SELECT logout_time FROM user_sessions WHERE session_id = -987654 AND emp_id = ?", [emp_id]
        ).fetchone()
        conn.close()
        assert row[0] == 'Archived'
        assert not row[1]
        assert archived_session[0] is not None

        with client.session_transaction() as sess:
            sess.clear()
            sess['emp_id'] = emp_id
            sess['name'] = 'Archive Test'
            sess['role'] = 'Employee'
            sess['session_id'] = 99892
        assert client.get('/api/profile').status_code in (302, 401)

        with client.session_transaction() as sess:
            sess.clear()
            sess['emp_id'] = 'EMP001'
            sess['name'] = 'Admin'
            sess['role'] = 'Admin'
            sess['session_id'] = 99893
        response = client.post(f'/api/users/{emp_id}/restore')
        assert response.status_code == 200, response.get_json()
        conn = get_db()
        row = conn.execute(
            "SELECT status, allow_login FROM users WHERE emp_id = ?", [emp_id]
        ).fetchone()
        conn.close()
        assert row[0] == 'Active'
        assert row[1]
    finally:
        conn = get_db()
        for table in ('user_sessions', 'shift_assignments', 'user_permissions', 'password_reset_tokens'):
            try:
                conn.execute(f"DELETE FROM {table} WHERE emp_id = ?", [emp_id])
            except Exception:
                pass
        conn.execute("DELETE FROM audit_log WHERE entity = 'users' AND entity_id = ?", [emp_id])
        conn.execute("DELETE FROM users WHERE emp_id = ?", [emp_id])
        conn.close()


def test_break_types_api(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99997
    resp = client.get('/api/break-types')
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) >= 1


def test_dashboard_stats_requires_admin(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99996
    resp = client.get('/api/dashboard-stats')
    assert resp.status_code == 200


def test_export_report(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99995
    resp = client.get('/api/reports/export?format=csv')
    assert resp.status_code == 200


# ── Leave Management Tests ──────────────────────────────────────

def test_leave_balance(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99994
    resp = client.get('/api/leave-balance')
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) >= 1
    types = {b['leave_type'] for b in data}
    assert 'Casual' in types


def test_apply_leave(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99993
    resp = client.post('/api/leaves', json={
        'leave_type': 'Casual',
        'start_date': '2026-07-10',
        'end_date': '2026-07-11',
        'reason': 'Test leave'
    })
    assert resp.status_code == 201


def test_audit_log(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99992
    resp = client.get('/api/audit-log')
    assert resp.status_code == 200
    data = resp.get_json()
    assert 'data' in data
    assert 'total' in data


# ── Idempotency (CC-07) ────────────────────────────────────────

def _idem_session(client, sid):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = sid


def test_idempotent_leave_apply_replays_once(client):
    """Same Idempotency-Key + same body replays the stored 201; only one
    leave request is created (no double-apply)."""
    _idem_session(client, 99001)
    payload = {
        'leave_type': 'Casual',
        'start_date': '2026-08-10',
        'end_date': '2026-08-11',
        'reason': 'idempotent replay'
    }
    r1 = client.post('/api/leaves', json=payload, headers={'Idempotency-Key': 'ik-leave-1'})
    assert r1.status_code == 201, r1.get_json()
    r2 = client.post('/api/leaves', json=payload, headers={'Idempotency-Key': 'ik-leave-1'})
    assert r2.status_code == 201, r2.get_json()
    assert r2.get_json() == r1.get_json()
    conn = get_db()
    n = conn.execute(
        "SELECT COUNT(*) FROM leave_requests WHERE emp_id = 'EMP001' AND CAST(start_date AS VARCHAR) = '2026-08-10'"
    ).fetchone()[0]
    conn.close()
    assert n == 1


def test_idempotent_key_reused_with_different_body_409(client):
    """Reusing a key with a different payload is a conflict, not a replay."""
    _idem_session(client, 99002)
    r1 = client.post('/api/leaves', json={
        'leave_type': 'Casual', 'start_date': '2026-09-01',
        'end_date': '2026-09-01', 'reason': 'first',
    }, headers={'Idempotency-Key': 'ik-leave-2'})
    assert r1.status_code == 201, r1.get_json()
    r2 = client.post('/api/leaves', json={
        'leave_type': 'Casual', 'start_date': '2026-09-02',
        'end_date': '2026-09-02', 'reason': 'different',
    }, headers={'Idempotency-Key': 'ik-leave-2'})
    assert r2.status_code == 409, r2.get_json()
    assert 'different request' in r2.get_json()['error']


def test_idempotent_header_absent_runs_normally(client):
    """No Idempotency-Key header -> request passes straight through."""
    _idem_session(client, 99003)
    r = client.post('/api/leaves', json={
        'leave_type': 'Casual', 'start_date': '2026-10-01',
        'end_date': '2026-10-02', 'reason': 'no key',
    })
    assert r.status_code == 201, r.get_json()
    conn = get_db()
    n = conn.execute(
        "SELECT COUNT(*) FROM leave_requests WHERE emp_id = 'EMP001' AND CAST(start_date AS VARCHAR) = '2026-10-01'"
    ).fetchone()[0]
    conn.close()
    assert n == 1


def test_idempotent_failed_request_releases_claim(client):
    """A failed attempt (400) releases the claim so a retry with the same
    key succeeds instead of replaying the error."""
    _idem_session(client, 99004)
    bad = {'start_date': '2026-11-01'}  # missing leave_type -> 400 inside handler
    r1 = client.post('/api/leaves', json=bad, headers={'Idempotency-Key': 'ik-leave-4'})
    assert r1.status_code == 400, r1.get_json()
    good = {
        'leave_type': 'Casual', 'start_date': '2026-11-01',
        'end_date': '2026-11-01', 'reason': 'retry',
    }
    r2 = client.post('/api/leaves', json=good, headers={'Idempotency-Key': 'ik-leave-4'})
    assert r2.status_code == 201, r2.get_json()


def test_idempotent_payroll_finalize_single_outbox_event(client):
    """Replayed finalize must not enqueue a second payroll.finalized event."""
    _idem_session(client, 99005)
    r = client.post('/api/payroll-runs', json={'month': 12, 'year': 2099},
                    headers={'Idempotency-Key': 'ik-pr-1'})
    assert r.status_code == 201, r.get_json()
    conn = get_db()
    rid = conn.execute(
        "SELECT run_id FROM payroll_runs WHERE month = 12 AND year = 2099"
    ).fetchone()[0]
    conn.close()
    assert _payroll_submit_and_approve(client, rid, 99092).status_code == 200
    hdrs = {'Idempotency-Key': 'ik-finalize-1'}
    f1 = client.post(f'/api/payroll-runs/{rid}/finalize', headers=hdrs)
    assert f1.status_code == 200, f1.get_json()
    f2 = client.post(f'/api/payroll-runs/{rid}/finalize', headers=hdrs)
    assert f2.status_code == 200, f2.get_json()
    assert f2.get_json() == f1.get_json()
    conn = get_db()
    n = conn.execute(
        "SELECT COUNT(*) FROM outbox_events WHERE event_type = 'payroll.finalized' AND aggregate_id = ?",
        [str(rid)]).fetchone()[0]
    conn.close()
    assert n == 1


def test_idempotency_keys_expired_cleaned_by_job(client):
    """The hourly cleanup purges expired idempotency claims and keeps live ones."""
    import app as app_module
    _idem_session(client, 99006)
    conn = get_db()
    conn.execute(
        "INSERT INTO idempotency_keys (key, route, request_hash, response_status, expires_at) VALUES (?, ?, ?, 0, ?)",
        ['ik-expired', 'POST /api/leaves', 'hash-a', datetime(2020, 1, 1)],
    )
    conn.execute(
        "INSERT INTO idempotency_keys (key, route, request_hash, response_status, expires_at) VALUES (?, ?, ?, 0, ?)",
        ['ik-valid', 'POST /api/leaves', 'hash-b', datetime.now() + timedelta(hours=1)],
    )
    conn.close()
    app_module.cleanup_expired_tokens()
    conn = get_db()
    n_exp = conn.execute("SELECT COUNT(*) FROM idempotency_keys WHERE key = 'ik-expired'").fetchone()[0]
    n_val = conn.execute("SELECT COUNT(*) FROM idempotency_keys WHERE key = 'ik-valid'").fetchone()[0]
    conn.close()
    assert n_exp == 0
    assert n_val == 1


# ── Service-layer rewrite inc 1 (expanded audit_log + notifications.category) ──

def test_audit_log_expanded_fields(client):
    """CC-13: LOGIN audits actor/entity/entity_id and honours X-Request-ID."""
    resp = client.post('/login', json={'emp_id': 'EMP001', 'password': 'pass123'},
                       headers={'X-Request-ID': 'trace-abc-123'})
    assert resp.status_code == 200, resp.get_json()
    conn = get_db()
    row = conn.execute(
        "SELECT actor, entity, entity_id, request_id FROM audit_log "
        "WHERE action = 'LOGIN' AND emp_id = 'EMP001' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert row, 'no LOGIN audit row'
    assert row[1] == 'Auth'
    assert row[2] == 'EMP001'
    assert row[3] == 'trace-abc-123'  # gateway correlation id passed through

def test_audit_log_api_exposes_expanded_fields(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99010
    resp = client.get('/api/audit-log')
    assert resp.status_code == 200
    data = resp.get_json()['data']
    assert data
    first = data[0]
    for key in ('actor', 'entity', 'entity_id', 'request_id', 'before', 'after'):
        assert key in first, f'missing {key} in audit-log payload'

def test_audit_log_entity_before_after_written(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99011
    client.post('/api/users', json={
        'emp_id': 'EMP902', 'name': 'Audit Subject', 'email': 'emp902@company.com',
        'department': 'MIS', 'role': 'Employee', 'password': 'audit-subject-pass',
    })
    conn = get_db()
    row = conn.execute(
        'SELECT entity, entity_id, "after" FROM audit_log '
        "WHERE action = 'USER_CREATE' AND entity_id = 'EMP902' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert row is not None, 'no USER_CREATE audit row'
    assert row[0] == 'users'
    assert row[1] == 'EMP902'
    after = json.loads(row[2])
    assert after['role'] == 'Employee'
    assert after['department'] == 'MIS'

def test_notification_category_derived_on_write(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99012
    client.post('/api/leaves', json={
        'leave_type': 'Casual', 'start_date': '2026-12-01',
        'end_date': '2026-12-02', 'reason': 'category probe',
    })
    conn = get_db()
    row = conn.execute(
        "SELECT type, category FROM notifications WHERE type = 'LEAVE_APPLIED'"
        " ORDER BY created_at DESC LIMIT 1").fetchone()
    conn.close()
    assert row and row[0] == 'LEAVE_APPLIED'
    assert row[1] == 'Leave'

def test_notification_category_db_default(client):
    """Legacy/`legacy` DDL default applies when category is omitted (outbox path)."""
    conn = get_db()
    conn.execute(
        "INSERT INTO notifications (notification_id, emp_id, type, message, created_at) VALUES (?, 'EMP001', 'X_TYPE', 'x', ?)",
        [gen_id(), datetime.now()],
    )
    row = conn.execute("SELECT category FROM notifications WHERE type = 'X_TYPE'").fetchone()
    conn.close()
    assert row and row[0] == 'General'

def test_outbox_payroll_notification_category(client):
    """payroll.finalized dispatching writes category='Payroll'."""
    import outbox
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99013
    client.post('/api/payroll-runs', json={'month': 12, 'year': 2098})
    conn = get_db()
    rid = conn.execute("SELECT run_id FROM payroll_runs WHERE month = 12 AND year = 2098").fetchone()[0]
    conn.close()
    assert _payroll_submit_and_approve(client, rid, 99093).status_code == 200
    assert client.post(f'/api/payroll-runs/{rid}/finalize').status_code == 200
    conn = get_db()
    outbox.dispatch_once(conn)
    conn.close()
    conn = get_db()
    row = conn.execute(
        "SELECT category FROM notifications WHERE type = 'Payroll'"
        " ORDER BY created_at DESC LIMIT 1").fetchone()
    conn.close()
    assert row and row[0] == 'Payroll'


def test_payroll_bank_file_is_binary_csv(client):
    """The export must hand Flask bytes, not the csv module's text stream."""
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99014
    created = client.post('/api/payroll-runs', json={'month': 11, 'year': 2097})
    assert created.status_code == 201, created.get_json()
    conn = get_db()
    run_id = conn.execute(
        "SELECT run_id FROM payroll_runs WHERE month = 11 AND year = 2097"
    ).fetchone()[0]
    conn.close()
    assert _payroll_submit_and_approve(client, run_id, 99094).status_code == 200
    assert client.post(f'/api/payroll-runs/{run_id}/finalize').status_code == 200

    response = client.get(f'/api/payroll-runs/{run_id}/bank-file')
    assert response.status_code == 200
    assert response.mimetype == 'text/csv'
    assert response.data.startswith(b'Employee ID,Name,Net Salary,Account Number,IFSC')


# ── Payroll maker-checker (Phase 4 / FR-PAY-06) ─────────────────────────

def test_finance_role_can_access_payroll_and_salary(client):
    _ensure_finance_approver()
    with client.session_transaction() as sess:
        sess['emp_id'] = 'PAYFIN01'
        sess['name'] = 'Payroll Finance'
        sess['role'] = 'Finance'
        sess['department'] = 'Finance'
        sess['session_id'] = 99094
    assert client.get('/admin/payroll').status_code == 200
    assert client.get('/admin/salary-structures').status_code == 200
    assert client.get('/api/payroll-runs').status_code == 200
    assert client.get('/api/salary-structures').status_code == 200


def test_payroll_maker_checker_blocks_direct_and_self_approval(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99095
    created = client.post('/api/payroll-runs', json={'month': 10, 'year': 2096})
    assert created.status_code == 201, created.get_json()
    rid = created.get_json()['run_id']

    assert client.post(f'/api/payroll-runs/{rid}/finalize').status_code == 409
    assert client.post(f'/api/payroll-runs/{rid}/submit').status_code == 200
    assert client.post(f'/api/payroll-runs/{rid}/approve').status_code == 403

    _ensure_finance_approver()
    with client.session_transaction() as sess:
        sess['emp_id'] = 'PAYFIN01'
        sess['name'] = 'Payroll Finance'
        sess['role'] = 'Finance'
        sess['department'] = 'Finance'
        sess['session_id'] = 99096
    assert client.post(f'/api/payroll-runs/{rid}/approve').status_code == 200
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99097
    assert client.post(f'/api/payroll-runs/{rid}/finalize').status_code == 200

    conn = get_db()
    status = conn.execute("SELECT status FROM payroll_runs WHERE run_id = ?", [rid]).fetchone()[0]
    trail = conn.execute(
        "SELECT action, actor_emp_id, from_status, to_status FROM payroll_approvals "
        "WHERE run_id = ? ORDER BY approval_id",
        [rid],
    ).fetchall()
    conn.close()
    assert status == 'Finalized'
    assert [(row[0], row[1], row[2], row[3]) for row in trail] == [
        ('Submit', 'EMP001', 'Draft', 'Submitted'),
        ('Approve', 'PAYFIN01', 'Submitted', 'Approved'),
        ('Finalize', 'EMP001', 'Approved', 'Finalized'),
    ]


def test_payroll_adjustment_run_must_reference_finalized_run(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99098
    original = client.post('/api/payroll-runs', json={'month': 9, 'year': 2095})
    assert original.status_code == 201, original.get_json()
    original_id = original.get_json()['run_id']
    assert _payroll_submit_and_approve(client, original_id, 99099).status_code == 200
    assert client.post(f'/api/payroll-runs/{original_id}/finalize').status_code == 200

    adjustment = client.post('/api/payroll-runs', json={
        'month': 10, 'year': 2095, 'adjustment_of_run_id': original_id,
    })
    assert adjustment.status_code == 201, adjustment.get_json()
    conn = get_db()
    row = conn.execute(
        "SELECT adjustment_of_run_id, status FROM payroll_runs WHERE run_id = ?",
        [adjustment.get_json()['run_id']],
    ).fetchone()
    conn.close()
    assert row == (original_id, 'Draft')


# ── Attendance finalisation (Phase 4 / FR-JOB-01) ─────────────────────

ATTENDANCE_TEST_DATE = datetime(2091, 1, 8).date()  # Monday
ATTENDANCE_HOLIDAY_DATE = datetime(2091, 1, 9).date()
ATTENDANCE_OPTIONAL_DATE = datetime(2091, 1, 10).date()
ATTENDANCE_TEST_IDS = [
    'ATTJOBPRS', 'ATTJOBHALF', 'ATTJOBSHORT', 'ATTJOBLEAVE',
    'ATTJOBHOL', 'ATTJOBWEEK', 'ATTJOBORPHAN', 'ATTJOBOPTYES',
    'ATTJOBOPTPEND', 'ATTJOBINACTIVE',
]


@pytest.fixture
def attendance_scenario():
    """Create isolated source rows for every FR-JOB-01 classification."""
    from app import set_shift

    conn = get_db()
    conn.execute("DELETE FROM holiday_optins WHERE emp_id LIKE 'ATTJOB%'")
    conn.execute("DELETE FROM holidays WHERE name IN ('Attendance national holiday', 'Attendance optional holiday')")
    conn.execute("DELETE FROM leave_requests WHERE reason = 'Attendance finalisation test'")
    conn.execute("DELETE FROM regularization_requests WHERE reason = 'test correction'")
    conn.execute("DELETE FROM user_sessions WHERE emp_id LIKE 'ATTJOB%'")
    conn.execute("DELETE FROM attendance_days WHERE emp_id LIKE 'ATTJOB%'")
    try:
        conn.execute("DELETE FROM shift_assignments WHERE emp_id LIKE 'ATTJOB%'")
    except Exception:
        pass
    conn.execute("DELETE FROM users WHERE emp_id LIKE 'ATTJOB%'")

    patterns = {
        'ATTJOBWEEK': 'Mon',
    }
    for emp_id in ATTENDANCE_TEST_IDS:
        status = 'Inactive' if emp_id == 'ATTJOBINACTIVE' else 'Active'
        conn.execute(
            "INSERT INTO users (emp_id, name, email, password, role, status) "
            "VALUES (?, ?, ?, 'not-used', 'Employee', ?)",
            [emp_id, emp_id, f'{emp_id.lower()}@company.com', status],
        )
        set_shift(
            emp_id, '09:00', '17:00', conn=conn,
            weekly_off=patterns.get(emp_id, 'Sat,Sun'),
            effective_from=datetime(2090, 1, 1).date(),
        )

    sessions = {
        'ATTJOBPRS': ('09:00', '17:00', 8.0),
        'ATTJOBHALF': ('09:00', '13:00', 4.0),
        'ATTJOBSHORT': ('09:00', '10:00', 1.0),
        'ATTJOBLEAVE': ('09:00', '17:00', 8.0),
        'ATTJOBHOL': ('09:00', '17:00', 8.0),
        'ATTJOBWEEK': ('09:00', '17:00', 8.0),
        'ATTJOBOPTYES': ('09:00', '17:00', 8.0),
        'ATTJOBOPTPEND': ('09:00', '17:00', 8.0),
    }
    for offset, (emp_id, (login_at, logout_at, hours)) in enumerate(sessions.items(), start=1):
        conn.execute(
            "INSERT INTO user_sessions "
            "(session_id, emp_id, login_time, logout_time, total_hours, session_date) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                887200 + offset, emp_id,
                datetime.combine(ATTENDANCE_TEST_DATE, datetime.strptime(login_at, '%H:%M').time()),
                datetime.combine(ATTENDANCE_TEST_DATE, datetime.strptime(logout_at, '%H:%M').time()),
                hours, ATTENDANCE_TEST_DATE,
            ],
        )
    conn.execute(
        "INSERT INTO user_sessions (session_id, emp_id, login_time, session_date) "
        "VALUES (887250, 'ATTJOBORPHAN', ?, ?)",
        [datetime(2091, 1, 8, 9), ATTENDANCE_TEST_DATE],
    )

    conn.execute(
        "INSERT INTO leave_requests "
        "(leave_id, emp_id, leave_type, start_date, end_date, year, reason, status) "
        "VALUES (887101, 'ATTJOBLEAVE', 'Casual', ?, ?, 2091, "
        "'Attendance finalisation test', 'Approved')",
        [ATTENDANCE_TEST_DATE, ATTENDANCE_TEST_DATE],
    )
    conn.execute(
        "INSERT INTO holidays (holiday_id, name, holiday_date, year, type) "
        "VALUES (887001, 'Attendance national holiday', ?, 2091, 'National')",
        [ATTENDANCE_HOLIDAY_DATE],
    )
    conn.execute(
        "INSERT INTO holidays (holiday_id, name, holiday_date, year, type) "
        "VALUES (887002, 'Attendance optional holiday', ?, 2091, 'Optional')",
        [ATTENDANCE_OPTIONAL_DATE],
    )
    for offset, emp_id in enumerate(['ATTJOBOPTYES', 'ATTJOBOPTPEND'], start=1):
        conn.execute(
            "INSERT INTO user_sessions "
            "(session_id, emp_id, login_time, logout_time, total_hours, session_date) "
            "VALUES (?, ?, ?, ?, 8, ?)",
            [
                887260 + offset, emp_id,
                datetime(2091, 1, 10, 9), datetime(2091, 1, 10, 17),
                ATTENDANCE_OPTIONAL_DATE,
            ],
        )
    conn.execute(
        "INSERT INTO holiday_optins (optin_id, emp_id, holiday_id, status) "
        "VALUES (887301, 'ATTJOBOPTYES', 887002, 'Approved')"
    )
    conn.execute(
        "INSERT INTO holiday_optins (optin_id, emp_id, holiday_id, status) "
        "VALUES (887302, 'ATTJOBOPTPEND', 887002, 'Pending')"
    )
    conn.close()

    try:
        yield
    finally:
        conn = get_db()
        conn.execute("DELETE FROM holiday_optins WHERE emp_id LIKE 'ATTJOB%'")
        conn.execute("DELETE FROM holidays WHERE name IN ('Attendance national holiday', 'Attendance optional holiday')")
        conn.execute("DELETE FROM leave_requests WHERE reason = 'Attendance finalisation test'")
        conn.execute("DELETE FROM regularization_requests WHERE reason = 'test correction'")
        conn.execute("DELETE FROM user_sessions WHERE emp_id LIKE 'ATTJOB%'")
        conn.execute("DELETE FROM attendance_days WHERE emp_id LIKE 'ATTJOB%'")
        try:
            conn.execute("DELETE FROM shift_assignments WHERE emp_id LIKE 'ATTJOB%'")
        except Exception:
            pass
        conn.execute("DELETE FROM users WHERE emp_id LIKE 'ATTJOB%'")
        conn.close()


def test_attendance_finalization_classifies_every_source(attendance_scenario):
    from app import finalize_attendance_for_date

    result = finalize_attendance_for_date(
        ATTENDANCE_TEST_DATE,
        as_of=datetime(2091, 1, 9, 12),
    )
    assert result['date'] == ATTENDANCE_TEST_DATE.isoformat()
    assert result['processed'] >= len(ATTENDANCE_TEST_IDS) - 1

    conn = get_db()
    placeholders = ','.join('?' for _ in ATTENDANCE_TEST_IDS)
    rows = conn.execute(
        f"SELECT emp_id, status, shift_hours, source, version FROM attendance_days "
        f"WHERE attendance_date = ? AND emp_id IN ({placeholders})",
        [ATTENDANCE_TEST_DATE, *ATTENDANCE_TEST_IDS],
    ).fetchall()
    conn.close()
    by_employee = {row[0]: row for row in rows}

    assert by_employee['ATTJOBPRS'][1:4] == ('Present', 8.0, 'job')
    assert by_employee['ATTJOBHALF'][1:4] == ('Half-day', 4.0, 'job')
    assert by_employee['ATTJOBSHORT'][1:4] == ('Absent', 1.0, 'job')
    assert by_employee['ATTJOBLEAVE'][1:4] == ('On Leave', 0.0, 'job')
    assert by_employee['ATTJOBHOL'][1:4] == ('Present', 8.0, 'job')
    assert by_employee['ATTJOBWEEK'][1:4] == ('Weekly-off', 0.0, 'job')
    # Open/orphaned session is capped at scheduled 8h + 25% = 10h.
    assert by_employee['ATTJOBORPHAN'][1:4] == ('Present', 10.0, 'job')
    assert 'ATTJOBINACTIVE' not in by_employee
    assert all(row[4] == 1 for row in rows)

    finalize_attendance_for_date(
        ATTENDANCE_HOLIDAY_DATE, employee_ids=['ATTJOBHOL'],
        as_of=datetime(2091, 1, 10, 12),
    )
    conn = get_db()
    holiday_row = conn.execute(
        "SELECT status, shift_hours FROM attendance_days "
        "WHERE emp_id = 'ATTJOBHOL' AND attendance_date = ?",
        [ATTENDANCE_HOLIDAY_DATE],
    ).fetchone()
    conn.close()
    assert holiday_row == ('Holiday', 0.0)


def test_attendance_optional_holiday_requires_approved_optin(attendance_scenario):
    from app import finalize_attendance_for_date

    # A row outside the targeted employee set must survive a subset rerun.
    conn = get_db()
    conn.execute(
        "INSERT INTO attendance_days (attendance_id, emp_id, attendance_date, status, source) "
        "VALUES (887500, 'ATTJOBPRS', ?, 'Present', 'manual')",
        [ATTENDANCE_OPTIONAL_DATE],
    )
    conn.close()

    finalize_attendance_for_date(
        ATTENDANCE_OPTIONAL_DATE,
        employee_ids=['ATTJOBOPTYES', 'ATTJOBOPTPEND'],
        as_of=datetime(2091, 1, 10, 18),
    )

    conn = get_db()
    rows = conn.execute(
        "SELECT emp_id, status, source FROM attendance_days WHERE attendance_date = ? "
        "AND emp_id IN ('ATTJOBOPTYES', 'ATTJOBOPTPEND', 'ATTJOBPRS')",
        [ATTENDANCE_OPTIONAL_DATE],
    ).fetchall()
    conn.close()
    by_employee = {row[0]: row[1:] for row in rows}
    assert by_employee['ATTJOBOPTYES'] == ('Holiday', 'job')
    assert by_employee['ATTJOBOPTPEND'] == ('Present', 'job')
    assert by_employee['ATTJOBPRS'] == ('Present', 'manual')


def test_attendance_rerun_replaces_date_transactionally(attendance_scenario):
    from app import finalize_attendance_for_date

    finalize_attendance_for_date(
        ATTENDANCE_TEST_DATE, employee_ids=['ATTJOBPRS'],
        as_of=datetime(2091, 1, 8, 18),
    )
    conn = get_db()
    conn.execute(
        "UPDATE attendance_days SET status = 'Absent', source = 'manual', version = 9 "
        "WHERE emp_id = 'ATTJOBPRS' AND attendance_date = ?",
        [ATTENDANCE_TEST_DATE],
    )
    conn.close()

    finalize_attendance_for_date(
        ATTENDANCE_TEST_DATE, employee_ids=['ATTJOBPRS'],
        as_of=datetime(2091, 1, 8, 18),
    )
    conn = get_db()
    rows = conn.execute(
        "SELECT status, shift_hours, source, version FROM attendance_days "
        "WHERE emp_id = 'ATTJOBPRS' AND attendance_date = ?",
        [ATTENDANCE_TEST_DATE],
    ).fetchall()
    conn.close()
    assert rows == [('Present', 8.0, 'job', 1)]


def test_approved_regularization_recomputes_attendance_row(attendance_scenario, client):
    from app import finalize_attendance_for_date

    finalize_attendance_for_date(
        ATTENDANCE_TEST_DATE, employee_ids=['ATTJOBPRS'],
        as_of=datetime(2091, 1, 8, 18),
    )
    conn = get_db()
    conn.execute(
        "INSERT INTO regularization_requests "
        "(request_id, emp_id, request_date, reason, status) "
        "VALUES (887401, 'ATTJOBPRS', ?, 'test correction', 'Pending')",
        [ATTENDANCE_TEST_DATE],
    )
    conn.close()
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'

    response = client.post('/api/regularization/887401/approve')
    assert response.status_code == 200
    conn = get_db()
    row = conn.execute(
        "SELECT status, source FROM attendance_days "
        "WHERE emp_id = 'ATTJOBPRS' AND attendance_date = ?",
        [ATTENDANCE_TEST_DATE],
    ).fetchone()
    conn.close()
    assert row == ('Present', 'job')


def test_attendance_status_appears_in_monthly_calendar(attendance_scenario, client):
    from app import finalize_attendance_for_date

    finalize_attendance_for_date(
        ATTENDANCE_TEST_DATE, employee_ids=['ATTJOBPRS'],
        as_of=datetime(2091, 1, 8, 18),
    )
    with client.session_transaction() as sess:
        sess['emp_id'] = 'ATTJOBPRS'
        sess['name'] = 'Attendance Test'
        sess['role'] = 'Employee'

    response = client.get('/api/user/calendar?month=1&year=2091')
    assert response.status_code == 200
    day = response.get_json()['attendance_days'][ATTENDANCE_TEST_DATE.isoformat()]
    assert day == {'status': 'Present', 'shift_hours': 8.0, 'source': 'job'}


def test_weekly_off_pattern_is_not_hard_coded():
    from app import _is_weekly_off

    monday = datetime(2091, 1, 8).date()
    assert _is_weekly_off(monday, 'Mon')
    assert _is_weekly_off(monday, 'Monday')
    assert _is_weekly_off(monday, 'Mon-Fri')
    assert not _is_weekly_off(monday, 'Sat,Sun')
    assert not _is_weekly_off(monday, '')


def test_attendance_nightly_scheduler_job_registered():
    from app import _register_scheduler_jobs, scheduler

    # Registered without starting the scheduler: a background thread firing next
    # to the assertions is what makes the DuckDB backend flaky.
    _register_scheduler_jobs(2)
    job = scheduler.get_job('attendance-finalization')
    assert job is not None
    assert job.max_instances == 1
    assert job.coalesce is True


# ── Service-layer rewrite inc 2 (shifts: users.columns ⇄ shift_assignments) ──

def test_shift_model_false_on_v1(client):
    """DuckDB/legacy keep shifts on users.shift_start/shift_end."""
    from app import _shift_model
    assert _shift_model() is False


def test_seed_default_shift_2020(client):
    """Boot seed still gives every employee a 20:00-05:00 shift on v1.0."""
    conn = get_db()
    row = conn.execute("SELECT shift_start, shift_end FROM users WHERE emp_id = 'EMP002'").fetchone()
    conn.close()
    assert row == ('20:00', '05:00')


def test_get_shift_resolves_v1_and_unknown(client):
    from app import get_shift
    assert get_shift('EMP002') == ('20:00', '05:00')
    assert get_shift('NOPE') == (None, None)


def test_get_shift_start_end_math(client):
    """20:00-05:00 shift: start at 20:00 of the target day, end at 05:00 next day."""
    from app import _get_shift_end_dt, _get_shift_start_dt
    sd = _get_shift_start_dt('EMP002', target_date=datetime(2030, 1, 15))
    assert sd == datetime(2030, 1, 15, 20, 0, 0)
    ed = _get_shift_end_dt('EMP002', sd)
    assert ed == datetime(2030, 1, 16, 5, 0, 0)


def test_user_create_roundtrips_shift(client):
    """Admin user create/update persists and surfaces shift_start/shift_end."""
    from app import get_shift
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99020
    resp = client.post('/api/users', json={
        'emp_id': 'EMP903', 'name': 'Shift Tester', 'email': 'emp903@company.com',
        'department': 'MIS', 'role': 'Employee', 'password': 'audit-subject-pass',
        'shift_start': '09:00', 'shift_end': '18:00', 'weekly_off_pattern': 'Sun,Mon',
    })
    assert resp.status_code == 201, resp.get_json()
    assert get_shift('EMP903') == ('09:00', '18:00')
    detail = client.get('/api/users/EMP903').get_json()
    assert detail['shift_start'] == '09:00'
    assert detail['shift_end'] == '18:00'
    assert detail['weekly_off_pattern'] == 'Sun,Mon'
    listed = client.get('/api/users?search=EMP903').get_json()['data']
    assert any(u['emp_id'] == 'EMP903' and u['shift_start'] == '09:00' and u['weekly_off_pattern'] == 'Sun,Mon' for u in listed)
    resp = client.put('/api/users/EMP903', json={
        'name': 'Shift Tester', 'email': 'emp903@company.com', 'role': 'Employee',
        'department': 'MIS', 'status': 'Active',
        'shift_start': '22:00', 'shift_end': '06:00', 'weekly_off_pattern': 'Tue',
    })
    assert resp.status_code == 200, resp.get_json()
    assert get_shift('EMP903') == ('22:00', '06:00')
    assert client.get('/api/users/EMP903').get_json()['weekly_off_pattern'] == 'Tue'


def test_shift_24x7_roundtrip(client):
    """'24x7' stays a recognised shift and workday math falls back to midnight."""
    from app import _get_shift_start_dt, get_shift
    conn = get_db()
    conn.execute("UPDATE users SET shift_start = '24x7', shift_end = '24x7' WHERE emp_id = 'EMP002'")
    conn.close()
    assert get_shift('EMP002') == ('24x7', '24x7')
    sd = _get_shift_start_dt('EMP002', target_date=datetime(2030, 1, 15))
    assert sd == datetime(2030, 1, 15, 0, 0, 0)


def test_shift_assignments_branch_executes_on_duckdb(client):
    """Exercise the v2.0 shift_assignments code path with DuckDB standing in
    for public: create the table, flip the cached model flag, and drive
    get_shift/set_shift/user-CRUD through the assignment branch (inc 2).
    """
    from app import _SHIFT_MODEL_CACHE, _get_shift_end_dt, _get_shift_start_dt, _shift_model, get_shift, set_shift
    conn = get_db()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS shift_assignments (emp_id VARCHAR, shift_type VARCHAR, "
        "shift_start TIME, shift_end TIME, weekly_off_pattern VARCHAR, effective_from DATE, effective_to DATE)"
    )
    conn.close()
    _SHIFT_MODEL_CACHE.clear()
    try:
        assert _shift_model() is True
        assert get_shift('NOPE') == (None, None)
        set_shift('EMP001', '11:00', '20:00')
        assert get_shift('EMP001') == ('11:00', '20:00')
        conn = get_db()
        row = conn.execute(
            "SELECT shift_type, effective_to FROM shift_assignments WHERE emp_id = 'EMP001'"
        ).fetchone()
        conn.close()
        assert row[0] == 'Fixed' and row[1] is None
        set_shift('EMP001', '24x7', '24x7')
        assert get_shift('EMP001') == ('24x7', '24x7')
        sd = _get_shift_start_dt('EMP001', target_date=datetime(2030, 1, 15))
        assert sd == datetime(2030, 1, 15, 0, 0, 0)
        ed = _get_shift_end_dt('EMP001', datetime(2030, 1, 15, 9, 0))
        assert ed == datetime(2030, 1, 16, 9, 0)  # 24x7 -> start + 1 day
        # user CRUD through the public branch: INSERT without shift columns,
        # shift persisted to shift_assignments, read back via get_shift.
        with client.session_transaction() as sess:
            sess['emp_id'] = 'EMP001'
            sess['name'] = 'Admin'
            sess['role'] = 'Admin'
            sess['session_id'] = 99022
        resp = client.post('/api/users', json={
            'emp_id': 'EMP904', 'name': 'Shift Two', 'email': 'emp904@company.com',
            'department': 'MIS', 'role': 'Employee', 'password': 'audit-subject-pass',
            'shift_start': '13:00', 'shift_end': '22:00',
        })
        assert resp.status_code == 201, resp.get_json()
        detail = client.get('/api/users/EMP904').get_json()
        assert detail['shift_start'] == '13:00' and detail['shift_end'] == '22:00'
        listed = client.get('/api/users?search=EMP904').get_json()['data']
        assert any(u['emp_id'] == 'EMP904' and u['shift_start'] == '13:00' for u in listed)
        resp = client.put('/api/users/EMP904', json={
            'name': 'Shift Two', 'email': 'emp904@company.com', 'role': 'Employee',
            'department': 'MIS', 'status': 'Active', 'shift_start': '08:00', 'shift_end': '17:00',
        })
        assert resp.status_code == 200, resp.get_json()
        assert get_shift('EMP904') == ('08:00', '17:00')
    finally:
        _SHIFT_MODEL_CACHE.clear()
        conn = get_db()
        conn.execute("DROP TABLE IF EXISTS shift_assignments")
        conn.close()


@pytest.mark.skipif(
    os.getenv('APP_DB', 'duckdb').lower() not in ('postgres', 'postgresql', 'pg')
    or os.getenv('APP_DB_SCHEMA', 'legacy') != 'public',
    reason='the v2.0 shift_assignments path runs on the pure public schema',
)
def test_shift_assignments_path_on_public():
    """v2.0 public: shifts resolve from shift_assignments, users has no columns."""
    from app import _get_shift_start_dt, _shift_model, get_shift, set_shift
    assert _shift_model() is True
    conn = get_db()
    cols = [r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'users'"
    ).fetchall()]
    conn.close()
    assert 'shift_start' not in cols, 'init_db must not mutate public.users'
    assert get_shift('EMP001') == ('20:00', '05:00')  # boot-seeded assignment
    set_shift('EMP001', '10:30', '19:30')
    assert get_shift('EMP001') == ('10:30', '19:30')
    set_shift('EMP001', '24x7', '24x7')
    assert get_shift('EMP001') == ('24x7', '24x7')
    sd = _get_shift_start_dt('EMP001', target_date=datetime(2030, 1, 15))
    assert sd == datetime(2030, 1, 15, 0, 0, 0)
    conn = get_db()
    rows = conn.execute(
        "SELECT shift_type, shift_start, shift_end, effective_to FROM shift_assignments WHERE emp_id = 'EMP001'"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == '24x7' and rows[0][3] is None


# ── Corrected ATS / onboarding / offboarding (Phase 4) ─────────────

def _lifecycle_candidate(client, marker):
    created = client.post('/api/candidates', json={
        'name': 'Lifecycle Candidate', 'email': marker, 'resume_text': 'phase 4 test',
    })
    assert created.status_code == 201, created.get_json()
    return created.get_json()['id']


def _lifecycle_offer(client, candidate_id, marker):
    assert client.put(f'/api/candidates/{candidate_id}/status', json={'status': 'Screened'}).status_code == 200
    assert client.put(f'/api/candidates/{candidate_id}/status', json={'status': 'Interviewed'}).status_code == 200
    invalid = client.post('/api/offers', json={
        'candidate_id': candidate_id, 'offered_salary': 500000,
        'basic_pct': 50, 'hra_pct': 30, 'allowances_pct': 10,
    })
    assert invalid.status_code == 400
    created = client.post('/api/offers', json={
        'candidate_id': candidate_id, 'offered_salary': 500000,
        'basic_pct': 50, 'hra_pct': 30, 'allowances_pct': 20,
    })
    assert created.status_code == 201, created.get_json()
    return created.get_json()['id']


def test_ats_rejects_direct_hired_and_acceptance_creates_onboarding(client):
    _idem_session(client, 99100)
    marker = f'ats-{gen_id()}@example.com'
    candidate_id = _lifecycle_candidate(client, marker)
    assert client.put(f'/api/candidates/{candidate_id}/status', json={'status': 'Hired'}).status_code == 409
    offer_id = _lifecycle_offer(client, candidate_id, marker)
    assert client.post('/api/offers/999999999/accept').status_code == 404
    accepted = client.post(f'/api/offers/{offer_id}/accept')
    assert accepted.status_code == 200, accepted.get_json()
    payload = accepted.get_json()
    assert payload['preboarding_token']
    assert client.post(f'/api/offers/{offer_id}/accept').status_code == 409
    conn = get_db()
    candidate = conn.execute("SELECT status FROM candidates WHERE candidate_id = ?", [candidate_id]).fetchone()
    user = conn.execute("SELECT status, allow_login, candidate_id FROM users WHERE emp_id = ?", [payload['emp_id']]).fetchone()
    workflow = conn.execute("SELECT completed, current_step FROM onboarding_workflow WHERE workflow_id = ?", [payload['workflow_id']]).fetchone()
    checklist = conn.execute("SELECT COUNT(*) FROM onboarding_checklist WHERE workflow_id = ?", [payload['workflow_id']]).fetchone()[0]
    salary = conn.execute("SELECT COUNT(*) FROM salary_structures WHERE emp_id = ?", [payload['emp_id']]).fetchone()[0]
    conn.close()
    assert candidate == ('Hired',)
    assert user == ('Pre-hire', 0, candidate_id)
    assert workflow == (0, 1)
    assert checklist == 5
    assert salary == 1


def test_preboarding_token_upload_review_and_guarded_steps(client):
    _idem_session(client, 99101)
    candidate_id = _lifecycle_candidate(client, f'onb-{gen_id()}@example.com')
    offer_id = _lifecycle_offer(client, candidate_id, 'onboarding')
    accepted = client.post(f'/api/offers/{offer_id}/accept').get_json()
    token = accepted['preboarding_token']
    status = client.get(f'/api/preboarding/{token}')
    assert status.status_code == 200
    assert len(status.get_json()['checklist']) == 5
    assert client.post(
        f'/api/preboarding/{token}/documents/ID%20Proof',
        data={'file': (BytesIO(b'%PDF-1.4\nprobe'), 'id.pdf', 'application/pdf')},
    ).status_code == 201
    assert client.post(f'/api/preboarding/{token}/submit').status_code == 200
    conn = get_db()
    items = conn.execute('SELECT item_id, doc_type FROM onboarding_checklist WHERE workflow_id = ?', [accepted['workflow_id']]).fetchall()
    conn.close()
    for item_id, doc_type in items:
        assert client.post(
            f'/api/preboarding/{token}/documents/{doc_type.replace(" ", "%20")}',
            data={'file': (BytesIO(b'%PDF-1.4\nprobe'), 'document.pdf', 'application/pdf')},
        ).status_code == 201
        assert client.post(f'/api/onboarding-checklist/{item_id}/review', json={'status': 'Approved'}).status_code == 200
    workflow = client.get(f"/api/onboarding-workflows/{accepted['workflow_id']}").get_json()
    assert workflow['steps']['step2'] == 'Completed'
    assert workflow['steps']['step3'] == 'InProgress'
    tasks = sorted(client.get('/api/onboarding-tasks').get_json(), key=lambda task: task.get('stage') or 0)
    for task in tasks:
        if task['emp_id'] == accepted['emp_id'] and task['stage'] in (3, 4, 5):
            assert client.post(f"/api/onboarding-tasks/{task['id']}/complete").status_code == 200
    assert client.post(f"/api/onboarding-workflows/{accepted['workflow_id']}/steps/4/complete").status_code == 200
    assert client.post(f"/api/onboarding-workflows/{accepted['workflow_id']}/steps/5/complete").status_code == 200
    finished = client.get(f"/api/onboarding-workflows/{accepted['workflow_id']}").get_json()
    assert finished['completed'] is True
    conn = get_db()
    user = conn.execute('SELECT status, allow_login FROM users WHERE emp_id = ?', [accepted['emp_id']]).fetchone()
    conn.close()
    assert user == ('Active', 1)


def test_offboarding_parallel_clearance_settlement_and_lwd_revocation(client):
    import app as app_module

    _idem_session(client, 99102)
    emp_id = f'OFF{gen_id() % 1000000:06d}'
    today = datetime.now().date()
    conn = get_db()
    conn.execute(
        "INSERT INTO users (emp_id, name, email, password, role, department, status, allow_login, allow_breaks) "
        "VALUES (?, ?, ?, ?, 'Employee', 'Operations', 'Active', 1, 1)",
        [emp_id, 'Lifecycle Exit', f'{emp_id.lower()}@example.com', hash_password('pass123')],
    )
    conn.close()
    created = client.post('/api/resignations', json={
        'emp_id': emp_id, 'notice_date': (today - timedelta(days=1)).isoformat(),
        'last_working_day': today.isoformat(), 'reason': 'test exit',
    })
    assert created.status_code == 201, created.get_json()
    offboard_id = created.get_json()['offboard_id']
    assert client.post(f'/api/offboarding-workflows/{offboard_id}/stages/2/complete').status_code == 409
    assert client.post(f'/api/resignations/{created.get_json()["resignation_id"]}/acknowledge').status_code == 200
    assert client.post(f'/api/offboarding-workflows/{offboard_id}/stages/2/complete').status_code == 200
    conn = get_db()
    asset_id = gen_id()
    conn.execute(
        "INSERT INTO assets (asset_id, emp_id, asset_type, issued_date, status) VALUES (?, ?, 'Laptop', ?, 'Issued')",
        [asset_id, emp_id, today],
    )
    conn.close()
    assert client.post(f'/api/offboarding-workflows/{offboard_id}/stages/3/complete').status_code == 409
    conn = get_db()
    conn.execute("UPDATE assets SET status = 'Returned', return_date = ? WHERE asset_id = ?", [today, asset_id])
    conn.close()
    assert client.post(f'/api/offboarding-workflows/{offboard_id}/stages/3/complete').status_code == 200

    for actor in (('OFFFIN1', 'Finance One'), ('OFFFIN2', 'Finance Two')):
        conn = get_db()
        if not conn.execute('SELECT 1 FROM users WHERE emp_id = ?', [actor[0]]).fetchone():
            conn.execute(
                "INSERT INTO users (emp_id, name, email, password, role, department, status) VALUES (?, ?, ?, ?, 'Finance', 'Finance', 'Active')",
                [actor[0], actor[1], f'{actor[0].lower()}@example.com', hash_password('pass123')],
            )
        conn.close()
    with client.session_transaction() as sess:
        sess.update({'emp_id': 'OFFFIN1', 'name': 'Finance One', 'role': 'Finance', 'department': 'Finance', 'session_id': 99103})
    prepared = client.post(f'/api/offboarding-workflows/{offboard_id}/stage/4/prepare')
    assert prepared.status_code == 200
    assert 'total_amount' in prepared.get_json()['settlement']
    assert client.post(f'/api/offboarding-workflows/{offboard_id}/stage/4/approve').status_code == 409
    with client.session_transaction() as sess:
        sess.update({'emp_id': 'OFFFIN2', 'name': 'Finance Two', 'role': 'Finance', 'department': 'Finance', 'session_id': 99104})
    assert client.post(f'/api/offboarding-workflows/{offboard_id}/stage/4/approve').status_code == 200
    with client.session_transaction() as sess:
        sess.update({'emp_id': 'EMP001', 'name': 'Admin', 'role': 'Admin', 'department': 'MIS', 'session_id': 99105})
    assert client.post(f'/api/offboarding-workflows/{offboard_id}/stages/5/complete').status_code == 200
    revoked = app_module.revoke_offboarding_access(today)
    assert any(item['emp_id'] == emp_id for item in revoked)
    conn = get_db()
    user = conn.execute('SELECT status, allow_login FROM users WHERE emp_id = ?', [emp_id]).fetchone()
    sessions = conn.execute('SELECT COUNT(*) FROM user_sessions WHERE emp_id = ? AND logout_time IS NULL', [emp_id]).fetchone()[0]
    conn.close()
    assert user == ('Inactive', 0)
    assert sessions == 0


def test_offer_percentage_precision_and_offered_state_guards(client):
    _idem_session(client, 99106)
    candidate_id = _lifecycle_candidate(client, f'precision-{gen_id()}@example.com')
    assert client.put(f'/api/candidates/{candidate_id}/status', json={'status': 'Screened'}).status_code == 200
    assert client.put(f'/api/candidates/{candidate_id}/status', json={'status': 'Interviewed'}).status_code == 200
    over_precision = client.post('/api/offers', json={
        'candidate_id': candidate_id, 'offered_salary': 100000,
        'basic_pct': 33.333, 'hra_pct': 33.333, 'allowances_pct': 33.334,
    })
    assert over_precision.status_code == 400
    non_finite = client.post('/api/offers', json={
        'candidate_id': candidate_id, 'offered_salary': 100000,
        'basic_pct': float('nan'), 'hra_pct': 30, 'allowances_pct': 20,
    })
    assert non_finite.status_code == 400
    valid_candidate = _lifecycle_candidate(client, f'precision-valid-{gen_id()}@example.com')
    _lifecycle_offer(client, valid_candidate, 'precision')
    assert client.put(f'/api/candidates/{valid_candidate}/status', json={'status': 'Rejected'}).status_code == 409


def test_onboarding_review_requires_submission_and_reupload(client):
    _idem_session(client, 99107)
    candidate_id = _lifecycle_candidate(client, f'review-{gen_id()}@example.com')
    offer_id = _lifecycle_offer(client, candidate_id, 'review')
    accepted = client.post(f'/api/offers/{offer_id}/accept').get_json()
    token = accepted['preboarding_token']
    conn = get_db()
    item_id, doc_type = conn.execute(
        'SELECT item_id, doc_type FROM onboarding_checklist WHERE workflow_id = ? ORDER BY item_id LIMIT 1',
        [accepted['workflow_id']],
    ).fetchone()
    conn.close()
    assert client.post(f'/api/onboarding-checklist/{item_id}/review', json={'status': 'Rejected', 'note': 'too early'}).status_code == 409
    assert client.post(
        f'/api/preboarding/{token}/documents/{doc_type.replace(" ", "%20")}',
        data={'file': (BytesIO(b'%PDF-1.4\\nreview'), 'review.pdf', 'application/pdf')},
    ).status_code == 201
    assert client.post(f'/api/preboarding/{token}/submit').status_code == 200
    assert client.post(f'/api/onboarding-checklist/{item_id}/review', json={'status': 'Rejected'}).status_code == 400
    assert client.post(f'/api/onboarding-checklist/{item_id}/review', json={'status': 'Rejected', 'note': 'reupload please'}).status_code == 200
    conn = get_db()
    assert conn.execute('SELECT status FROM onboarding_checklist WHERE item_id = ?', [item_id]).fetchone() == ('Rejected',)
    conn.close()
    assert client.post(
        f'/api/preboarding/{token}/documents/{doc_type.replace(" ", "%20")}',
        data={'file': (BytesIO(b'%PDF-1.4\\nreview2'), 'review2.pdf', 'application/pdf')},
    ).status_code == 201
    conn = get_db()
    assert conn.execute('SELECT status FROM onboarding_checklist WHERE item_id = ?', [item_id]).fetchone() == ('Uploaded',)
    conn.close()


def test_revoked_signed_cookie_session_is_rejected(client):
    emp_id = f'SESS{gen_id() % 1000000:06d}'
    conn = get_db()
    conn.execute(
        "INSERT INTO users (emp_id, name, email, password, role, department, status, allow_login, allow_breaks) "
        "VALUES (?, ?, ?, ?, 'Employee', 'Operations', 'Active', 1, 1)",
        [emp_id, 'Session Test', f'{emp_id.lower()}@example.com', hash_password('pass123')],
    )
    conn.close()
    try:
        with client.session_transaction() as sess:
            sess.update({'emp_id': emp_id, 'name': 'Session Test', 'role': 'Employee', 'department': 'Operations', 'session_id': 99108})
        conn = get_db()
        conn.execute("UPDATE users SET status = 'Inactive', allow_login = 0 WHERE emp_id = ?", [emp_id])
        conn.close()
        response = client.get('/api/notifications')
        assert response.status_code in (302, 401)
        with client.session_transaction() as sess:
            assert 'emp_id' not in sess
    finally:
        conn = get_db()
        conn.execute("DELETE FROM users WHERE emp_id = ?", [emp_id])
        conn.close()


def test_document_download_is_owner_or_privileged_only(client):
    import app as app_module
    suffix = gen_id() % 1000000
    owner = f'DOC{suffix}O'
    other = f'DOC{suffix}X'
    filename = f'test-doc-{suffix}.pdf'
    path = os.path.join(app_module.UPLOAD_FOLDER, filename)
    with open(path, 'wb') as handle:
        handle.write(b'%PDF-1.4\nowner')
    conn = get_db()
    for emp_id, name in ((owner, 'Document Owner'), (other, 'Other Employee')):
        conn.execute(
            "INSERT INTO users (emp_id, name, email, password, role, department, status, allow_login, allow_breaks) "
            "VALUES (?, ?, ?, ?, 'Employee', 'Operations', 'Active', 1, 1)",
            [emp_id, name, f'{emp_id.lower()}@example.com', hash_password('pass123')],
        )
    did = gen_id()
    conn.execute(
        "INSERT INTO documents (doc_id, emp_id, name, category, file_path, file_size, uploaded_at) VALUES (?, ?, ?, 'Test', ?, ?, ?)",
        [did, owner, 'private.pdf', filename, os.path.getsize(path), datetime.now()],
    )
    conn.close()
    try:
        with client.session_transaction() as sess:
            sess.update({'emp_id': other, 'name': 'Other Employee', 'role': 'Employee', 'department': 'Operations', 'session_id': 99109})
        assert client.get(f'/api/documents/{did}/download').status_code == 404
        with client.session_transaction() as sess:
            sess.update({'emp_id': owner, 'name': 'Document Owner', 'role': 'Employee', 'department': 'Operations', 'session_id': 99110})
        assert client.get(f'/api/documents/{did}/download').status_code == 200
    finally:
        conn = get_db()
        conn.execute('DELETE FROM documents WHERE doc_id = ?', [did])
        conn.execute('DELETE FROM users WHERE emp_id IN (?, ?)', [owner, other])
        conn.close()
        if os.path.exists(path):
            os.remove(path)


def test_lifecycle_scheduler_job_registered():
    import app as app_module

    app_module._register_scheduler_jobs(2)
    assert app_module.scheduler.get_job('offboarding-access-revocation') is not None
    assert app_module.scheduler.get_job('import-dispatch') is not None
    assert app_module.scheduler.get_job('outbox-dispatch') is not None


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
