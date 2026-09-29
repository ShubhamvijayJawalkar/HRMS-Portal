"""The server-side Redis session store (Phase 3a / CC-06).

**Why this is a separate file.** The session backend is chosen when `app` is
imported (``maybe_enable_redis_sessions(app)`` at module level), so it cannot be
switched on from inside a test that has already imported the app. Running this
file as its own pytest process with ``REDIS_URL`` set is the only honest way to
assert it. Every test skips when ``REDIS_URL`` is unset, so a bare
``pytest tests/`` is still safe.

**Why it exists at all.** Until now CI ran the whole unit suite a second time
with ``REDIS_URL`` set — and *not one assertion in that suite referred to the
session store*. So the production session backend, and the four code paths that
claim to revoke a server-side session on block / archive / anonymise / offboard,
were entirely unverified. It was not even hypothetical: this file's first run
found the app silently on cookie sessions because Redis was down, and the old
CI step would still have been green.

Run it with::

    REDIS_URL=redis://localhost:6379/0 python -m pytest tests/test_redis_sessions.py -v
"""

import json
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

os.environ['SECRET_KEY'] = 'test-secret-key'
os.environ['DB_FILE'] = os.path.join(tempfile.gettempdir(), f'hrms_redis_test_{datetime.now().timestamp()}.duckdb')
os.environ['FLASK_DEBUG'] = '0'
os.environ.setdefault('APP_DB', 'duckdb')
os.environ.setdefault('DEFAULT_RATE_LIMIT', '100000 per minute')
# DuckDB attaches a database file once per process; see tests/test_app.py.
os.environ['HRMS_DISABLE_SCHEDULER'] = '1'
os.environ['APP_DB_SCHEMA'] = 'legacy'
if os.getenv('APP_DB', 'duckdb').lower() in ('postgres', 'postgresql', 'pg'):
    import db_backend

    db_backend.reset_schema()

import pytest

from app import app, get_db

REDIS_URL = os.getenv('REDIS_URL')

pytestmark = pytest.mark.skipif(
    not REDIS_URL, reason='server-side sessions are opt-in; set REDIS_URL to exercise them',
)

# `import app` already ran `maybe_enable_redis_sessions`, so importing the app is
# what puts the interface in place. The tests assert it rather than trust it.
from security import RedisSessionInterface, maybe_enable_redis_sessions  # noqa: E402

PREFIX = 'hrms:session:'
TARGET = 'EMP990'
BYSTANDER = 'EMP991'
ADMIN = ('EMP001', 'pass123')
PASSWORD = 'redis-pass-123'


@pytest.fixture
def redis_client():
    import redis as redis_lib

    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.ping()
    return client


@pytest.fixture
def client():
    """A signed-out test client, as a browser would start."""
    app.config['TESTING'] = True
    app.config['SERVER_NAME'] = 'localhost'
    with app.test_client() as c:
        with app.app_context():
            yield c


def _csrf(c, method, url, **kwargs):
    """Issue a state-changing request with a valid CSRF token."""
    token = c.get('/api/csrf-token').get_json()['csrf_token']
    headers = dict(kwargs.pop('headers', {}) or {})
    headers['X-CSRF-Token'] = token
    return getattr(c, method)(url, headers=headers, **kwargs)


def _login(c, emp_id, password=PASSWORD):
    return c.post('/login', json={'emp_id': emp_id, 'password': password})


def _sid(c):
    """The opaque session id the client is holding, or None."""
    cookie = c.get_cookie('session')
    return cookie.value if cookie else None


def _store(redis_client, sid):
    if not sid:
        return None
    raw = redis_client.get(f'{PREFIX}{sid}')
    return json.loads(raw) if raw else None


def _keys(redis_client):
    return set(redis_client.scan_iter(match=f'{PREFIX}*'))


def _make_users():
    """Create the two subjects through the real admin API (CSRF included)."""
    with app.test_client() as admin:
        assert _login(admin, *ADMIN).status_code == 200, 'the seeded admin could not log in'
        for emp_id in (TARGET, BYSTANDER):
            created = _csrf(admin, 'post', '/api/users', json={
                'emp_id': emp_id, 'name': f'Session {emp_id}',
                'email': f'{emp_id.lower()}@company.com',
                'department': 'MIS', 'role': 'Employee', 'password': PASSWORD,
            })
            assert created.status_code == 201, created.get_json()


def _admin_login():
    """A logged-in admin client, in its own context so it can be closed."""
    c = app.test_client()
    assert _login(c, *ADMIN).status_code == 200
    return c


def _cleanup():
    """Remove the subjects and every row that points at them.

    A login writes a `user_sessions` row and an audit entry, and DuckDB enforces
    the foreign key, so the order matters.
    """
    conn = get_db()
    try:
        for emp_id in (TARGET, BYSTANDER):
            conn.execute('DELETE FROM audit_log WHERE entity_id = ?', [emp_id])
            for table in ('user_sessions', 'shift_assignments', 'user_permissions',
                          'password_reset_tokens', 'leave_balance', 'leave_requests',
                          'notifications', 'dependents'):
                try:
                    conn.execute(f'DELETE FROM {table} WHERE emp_id = ?', [emp_id])
                except Exception:
                    pass  # a table this backend does not have
            conn.execute('DELETE FROM users WHERE emp_id = ?', [emp_id])
    finally:
        conn.close()


@pytest.fixture
def subjects():
    _cleanup()
    _make_users()
    yield
    _cleanup()


# ── the store is actually Redis, not a signed cookie ─────────────────────

def test_the_session_backend_is_redis_not_a_signed_cookie():
    """The fallback to cookies is silent, so the switch itself is asserted."""
    assert isinstance(app.session_interface, RedisSessionInterface), (
        f'session interface is {type(app.session_interface).__name__}: REDIS_URL is set but the '
        'app is still on cookie sessions, so every other assertion in this file is vacuous')
    assert maybe_enable_redis_sessions(app) is True, 're-enabling did not report success'


def test_login_stores_the_session_server_side(client, redis_client, subjects):
    """The cookie is an opaque id; the payload lives in Redis with a TTL."""
    before = _keys(redis_client)
    assert _login(client, TARGET).status_code == 200

    sid = _sid(client)
    assert sid, 'no session cookie was set'
    # Opaque, not a signed payload: itsdangerous separates payload|signature with a dot.
    assert '.' not in sid, f'the cookie still carries a signed payload: {sid[:40]}...'

    stored = _store(redis_client, sid)
    assert stored is not None, 'the session was not written to Redis'
    assert stored.get('emp_id') == TARGET, stored
    assert sid not in json.dumps(stored), 'the sid leaked into the stored payload'
    # The key expires: an abandoned session cannot be replayed for ever.
    ttl = redis_client.ttl(f'{PREFIX}{sid}')
    assert 0 < ttl <= int(app.config['PERMANENT_SESSION_LIFETIME'].total_seconds()), ttl
    # Exactly one new key, for this session.
    assert _keys(redis_client) - before == {f'{PREFIX}{sid}'}


def test_the_session_survives_across_requests(client, redis_client, subjects):
    """A server-side session is loaded from Redis on every request."""
    assert _login(client, TARGET).status_code == 200
    sid = _sid(client)

    for _ in range(3):
        assert client.get('/api/leave-balance').status_code == 200
        assert _sid(client) == sid, 'the session id changed between requests'
    assert _store(redis_client, sid)['emp_id'] == TARGET

    # A client with no cookie sees nothing, which is the point of a server-side store.
    with app.test_client() as anon:
        assert anon.get('/api/leave-balance').status_code in (302, 401)


def test_logout_destroys_the_server_side_session(client, redis_client, subjects):
    assert _login(client, TARGET).status_code == 200
    sid = _sid(client)
    assert _store(redis_client, sid) is not None

    client.get('/logout')
    # The key is gone, so replaying the old cookie cannot resurrect the session.
    assert _store(redis_client, sid) is None, 'logout left the session in Redis'
    assert client.get('/api/leave-balance').status_code in (302, 401)


# ── the revocation paths the docs claim exist ────────────────────────────

def test_blocking_a_user_revokes_their_server_side_session(client, redis_client, subjects):
    """AGENTS.md claims blocking "revokes Redis sessions". Nothing asserted it."""
    assert _login(client, TARGET).status_code == 200
    target_sid = _sid(client)
    assert _store(redis_client, target_sid) is not None

    # A second, unrelated session that must survive the revocation.
    with app.test_client() as other:
        assert _login(other, BYSTANDER).status_code == 200
        bystander_sid = _sid(other)
    assert _store(redis_client, bystander_sid) is not None

    with _admin_login() as admin:
        blocked = _csrf(admin, 'post', f'/api/users/{TARGET}/block', json={})
        assert blocked.status_code == 200, blocked.get_json()

    assert _store(redis_client, target_sid) is None, "the blocked user's session survived"
    assert _store(redis_client, bystander_sid) is not None, 'an unrelated session was revoked'

    # The old cookie really is dead, not merely absent from Redis.
    with app.test_client() as replay:
        replay.set_cookie('session', target_sid, domain='localhost')
        assert replay.get('/api/leave-balance').status_code in (302, 401)


def test_archiving_a_user_revokes_their_server_side_session(client, redis_client, subjects):
    """Archive is a separate call site from block, and has its own status guard."""
    assert _login(client, TARGET).status_code == 200
    target_sid = _sid(client)

    with _admin_login() as admin:
        archived = _csrf(admin, 'post', f'/api/users/{TARGET}/archive', json={})
        assert archived.status_code == 200, archived.get_json()
    assert _store(redis_client, target_sid) is None, "the archived user's session survived"


def test_reblocking_after_an_unblock_revokes_the_new_session(client, redis_client, subjects):
    """A stale revocation is not enough: the *new* session has to go too."""
    assert _login(client, TARGET).status_code == 200
    first_sid = _sid(client)

    with _admin_login() as admin:
        assert _csrf(admin, 'post', f'/api/users/{TARGET}/block', json={}).status_code == 200
        assert _store(redis_client, first_sid) is None
        assert _csrf(admin, 'post', f'/api/users/{TARGET}/unblock', json={}).status_code == 200

    assert _login(client, TARGET).status_code == 200
    revived_sid = _sid(client)
    assert revived_sid != first_sid
    assert _store(redis_client, revived_sid) is not None

    with _admin_login() as admin:
        assert _csrf(admin, 'post', f'/api/users/{TARGET}/block', json={}).status_code == 200
    assert _store(redis_client, revived_sid) is None, 'the new session survived re-blocking'


def test_csrf_is_still_enforced_with_a_server_side_session(client, redis_client, subjects):
    """CC-06 is enforced globally; a Redis-backed session must not bypass it."""
    assert _login(client, TARGET).status_code == 200
    assert _store(redis_client, _sid(client)) is not None

    # No X-CSRF-Token: the write must be refused even with a valid session.
    refused = client.post('/api/leaves', json={
        'leave_type': 'Casual', 'start_date': '2027-01-04', 'end_date': '2027-01-05',
        'reason': 'redis csrf probe',
    })
    assert refused.status_code == 403, refused.get_json()
    assert 'csrf' in str(refused.get_json()).lower(), refused.get_json()
    conn = get_db()
    try:
        assert conn.execute(
            'SELECT COUNT(*) FROM leave_requests WHERE emp_id = ? AND reason = ?',
            [TARGET, 'redis csrf probe'],
        ).fetchone()[0] == 0, 'the write went through without a CSRF token'
    finally:
        conn.close()

    # With the token it goes through.
    accepted = _csrf(client, 'post', '/api/leaves', json={
        'leave_type': 'Casual', 'start_date': '2027-01-04', 'end_date': '2027-01-05',
        'reason': 'redis csrf probe',
    })
    assert accepted.status_code in (200, 201), accepted.get_json()
    conn = get_db()
    try:
        assert conn.execute(
            'SELECT COUNT(*) FROM leave_requests WHERE emp_id = ? AND reason = ?',
            [TARGET, 'redis csrf probe'],
        ).fetchone()[0] == 1
        conn.execute('DELETE FROM leave_requests WHERE emp_id = ?', [TARGET])
        conn.execute('DELETE FROM leave_balance WHERE emp_id = ?', [TARGET])
    finally:
        conn.close()


# ── the guard rails around the store ─────────────────────────────────────

def test_an_unreachable_redis_falls_back_instead_of_taking_the_app_down():
    """`REDIS_URL` pointing at nothing must not break login."""
    from flask import Flask

    probe = Flask(__name__)
    probe.config['SECRET_KEY'] = 'x'
    original = os.environ.get('REDIS_URL')
    os.environ['REDIS_URL'] = 'redis://127.0.0.1:6399/0'  # nothing listens here
    try:
        before = probe.session_interface
        assert maybe_enable_redis_sessions(probe) is False
        assert probe.session_interface is before, 'the interface was swapped despite the failure'
    finally:
        if original is None:
            os.environ.pop('REDIS_URL', None)
        else:
            os.environ['REDIS_URL'] = original


def test_no_redis_url_keeps_cookie_sessions():
    """The default path must be unchanged when the opt-in is absent."""
    from flask import Flask

    probe = Flask(__name__)
    probe.config['SECRET_KEY'] = 'x'
    original = os.environ.pop('REDIS_URL', None)
    try:
        assert maybe_enable_redis_sessions(probe) is False
        assert not isinstance(probe.session_interface, RedisSessionInterface)
    finally:
        if original is not None:
            os.environ['REDIS_URL'] = original
