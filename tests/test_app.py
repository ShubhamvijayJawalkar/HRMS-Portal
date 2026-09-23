import os
import sys
import json
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

os.environ['SECRET_KEY'] = 'test-secret-key'
os.environ['DB_FILE'] = os.path.join(tempfile.gettempdir(), f'hrms_test_{datetime.now().timestamp()}.duckdb')
os.environ['FLASK_DEBUG'] = '0'
os.environ.setdefault('APP_DB', 'duckdb')
if os.getenv('APP_DB', 'duckdb').lower() in ('postgres', 'postgresql', 'pg'):
    import db_backend
    db_backend.reset_schema()

import pytest
from app import app, get_db, hash_password, check_password, gen_id


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
    conn.close()

    natural = {('users', 'emp_id'), ('break_types', 'break_type'), ('idempotency_keys', 'key')}
    framework = {'alembic_version'}
    bad = [
        (t, c) for t, c, ident in rows
        if ident not in ('a', 'd') and (t, c) not in natural and t not in framework
    ]
    assert not bad, f'non-identity surrogate PKs: {bad}'
    assert len(rows) >= 49, f'expected the 49-table target schema, found {len(rows)} PKs'


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
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99998
    resp = client.post('/api/change-password', json={
        'current_password': 'pass123',
        'new_password': 'newpass123'
    })
    assert resp.status_code == 200, f'Expected 200, got {resp.status_code}'
    resp = client.post('/api/change-password', json={
        'current_password': 'newpass123',
        'new_password': 'pass123'
    })
    assert resp.status_code == 200
    resp = client.post('/api/change-password', json={
        'current_password': 'wrong',
        'new_password': 'test123'
    })
    assert resp.status_code == 400


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


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
