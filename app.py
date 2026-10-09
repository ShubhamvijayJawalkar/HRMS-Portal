import hashlib
import json
import logging
import math
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from io import BytesIO
from zoneinfo import ZoneInfo

import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from flasgger import Swagger
from flask import (
    Flask,
    Response,
    g,
    has_request_context,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.utils import secure_filename

import lockout
import mfa
from security import (
    check_password,
    hash_password,
    init_csrf,
    maybe_enable_redis_sessions,
    needs_rehash,
)

load_dotenv()

import anonymise  # noqa: E402  # two-person anonymisation (FR-USR)
import delegations  # noqa: E402  # approval delegation for a date range (FR-LEA-08a)
import expenses  # noqa: E402  # expense claim state machine (FR-EXP-03)
import goals  # noqa: E402  # goal ownership + rating rules (FR-PERF-01)
import holiday_calendar  # noqa: E402  # holiday calendar maintenance (FR-HOL-01/02)
import holidays_optin  # noqa: E402  # optional-holiday opt-ins (FR-HOL-03)
import imports  # noqa: E402  # background bulk-import jobs (FR-USR-04)
import leave_accrual  # noqa: E402  # monthly accrual from leave_policy.accrual_rate
import leave_grants  # noqa: E402  # manual leave grants (FR-LEA-07)
import leave_policy  # noqa: E402  # policy-derived leave balances (FR-LEA-06/08)
import notifications  # noqa: E402  # per-category notification preferences (FR-NOT-03)
import object_storage  # noqa: E402  # S3/MinIO object storage (FR-PAY-07, FR-DOC-02)
import orphan_breaks  # noqa: E402  # auto-close of breaks left Active (FR-AUTH-14/FR-JOB-02)
import outbox  # noqa: E402
import passwords  # noqa: E402  # FR-AUTH-10 password policy (length + breach corpus)  # CC-09 transactional outbox (dispatcher job + enqueue helper)
import policy  # noqa: E402  # FR-USR-09/15 role + permission matrix (policy.py)
import reviews  # noqa: E402  # performance review + 360 feedback integrity (FR-PERF-02)
import shift_hours  # noqa: E402  # the ONE shift-length rule (FR-ATT-09): shared by the shift summary and payroll finalisation
import tickets  # noqa: E402  # ticket state machine + visibility (FR-TKT-03/04)
import working_days  # noqa: E402  # the ONE working-day function (FR-LEA-09); shared by leave, payroll LOP and reports
from idempotency import idempotent  # noqa: E402  # CC-07 idempotent writes (Idempotency-Key replay)

# ── Logging ───────────────────────────────────────────────────────────
log_level = getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper(), logging.INFO)
logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    level=log_level,
)
logger = logging.getLogger('hrms')

# ── Flask App ──────────────────────────────────────────────────────────
app = Flask(__name__)

_secret = os.getenv('SECRET_KEY', '')
if not _secret or _secret in ('change-me-to-random-string', 'hrms_secret_key_2024', 'change-me-in-production'):
    if os.getenv('FLASK_ENV') == 'production':
        logger.critical("SECRET_KEY is not set or is insecure. Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\"")
        raise RuntimeError("SECRET_KEY must be set to a secure random value in production")
    _secret = secrets.token_hex(32)
    logger.warning("Using auto-generated SECRET_KEY (sessions will not persist across restarts)")
app.secret_key = _secret

app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = (os.getenv('FLASK_ENV') == 'production')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)

if os.getenv('FLASK_ENV') == 'production':
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

# ── Security response headers (SRS §11.3) ───────────────────────────────
# The SRS names Flask-Talisman and five headers explicitly, and the application
# shipped **none** of them: no CSP, no nosniff, no frame-deny, no referrer policy,
# no HSTS. That is invisible in a demo and is a clickjacking and content-injection
# exposure in production, which is why it belongs to the go-live list rather than a
# nice-to-have.
#
# HSTS is the one behaviour difference between environments, and it is deliberate:
# `force_https=False` locally so a browser on http://localhost is not locked out of
# its own development server for a year. It is on in production, which is the only
# place it means anything. `FLASK_ENV=development` disables Talisman entirely,
# because CSP breaks the CDN-loaded Bootstrap/font assets these templates rely on
# and a dev box that logs CSP violations on every page load trains people to ignore
# them.
#: The Content-Security-Policy, as a module constant so the test asserts *this*
#: configuration rather than a hand-copied duplicate of it. A test that re-declares
#: the policy is a test that passes when the real one is wrong.
SECURITY_CSP = {
    # No `unsafe-inline` for scripts: the SRS says "CSP (no inline scripts by
    # default)", and this application's JS lives in external files plus the CSRF
    # fetch patcher. Styles *do* need inline (the templates set element styles
    # directly), so only `style-src` is relaxed — the relaxation is narrow and
    # commented rather than blanket.
    'default-src': "'self'",
    'script-src': "'self'",
    'style-src': "'self' 'unsafe-inline'",
    'img-src': "'self' data:",
    # The CDN the templates load Bootstrap, bootstrap-icons and the webfont from.
    'font-src': "'self' https://cdn.jsdelivr.net https://fonts.gstatic.com",
    'script-src-elem': "'self' https://cdn.jsdelivr.net",
    'connect-src': "'self'",
    # `frame-ancestors: none` is the CSP half of clickjacking defence; the
    # `X-Frame-Options: DENY` header below is the legacy half and both are sent,
    # because CSP is not honoured by every browser the SRS targets.
    'frame-ancestors': "'none'",
    'object-src': "'none'",
    'base-uri': "'self'",
    'form-action': "'self'",
}

_TALISMAN_KWARGS = {
    # TLS terminates at the reverse proxy; HSTS still applies and is what tells the
    # browser to keep asking over https from now on.
    'force_https': False,
    'strict_transport_security': True,
    'strict_transport_security_include_subdomains': True,
    'frame_options': 'DENY',
    'frame_options_allow_from': 'self',
    'referrer_policy': 'strict-origin-when-cross-origin',
    'content_security_policy': SECURITY_CSP,
    'feature_policy': "geolocation 'none', microphone 'none', camera 'none'",
}

_PRODUCTION = os.getenv('FLASK_ENV') == 'production'

if not _PRODUCTION:
    app.config['TALISMAN_ENABLED'] = False
else:
    from flask_talisman import Talisman

    Talisman(app, **_TALISMAN_KWARGS)
    logger.info('Security headers enabled (Talisman): CSP, nosniff, frame-deny, HSTS')

# ── Phase 3a (SRS CC-06): Argon2id hashing, CSRF guard, Redis sessions ─
# Security lives in the request pipeline above the DB layer, so it applies
# to both the DuckDB and PostgreSQL backends unchanged.
init_csrf(app)
maybe_enable_redis_sessions(app)

IST = ZoneInfo('Asia/Kolkata')

# ── Rate Limiter ──────────────────────────────────────────────────────
def rate_limit_key() -> str:
    """Who to charge for this request.

    This was `get_remote_address`, and measuring it against the SRS's own §10
    targets showed the shipped defaults **cannot meet them**:

    * Sustained NFR is 150 req/s = **9,000 req/min**; the default global limit was
      200 req/min, keyed per address — 45× short.
    * 500 concurrent users behind one corporate NAT share a single 200/min bucket,
      so each gets **0.4 requests per minute**. One employee refreshing a dashboard
      every 5 s exhausts the entire company's budget and everybody is 429'd.
    * The burst NFR is 1,000 logins in five minutes = 200/min from one address, and
      `LOGIN_RATE_LIMIT` was 20/min — 10× short.

    Measured: one signed-in user issuing 260 rapid requests got 200 × 200 and then
    60 × 429. Behind shared NAT the second user would never get to be served at all.

    The SRS resolves this itself: the burst target says "without lockouts caused by
    shared-NAT rate limiting (**per-account, not per-IP-only**)". So authenticated
    traffic is charged to the **employee**, and anonymous traffic to the address:

    * **Anonymous** (`/login`, `/api/forgot-password`, `/api/reset-password`) stays
      per-address. That is the surface actually worth rate-limiting, because it is
      the only one an attacker can hammer without credentials.
    * **Authenticated** is per-`emp_id`. Fair to the person browsing, and immune to
      shared NAT. Keyed on the *identity*, not the session, so opening ten browser
      tabs does not buy ten budgets and a session-multiplication evasion gets
      nothing. Behind a proxy this also fixes the case the old key silently got
      wrong: every user looked like one address.
    """
    from flask import session as _session

    emp_id = _session.get('emp_id')
    if emp_id:
        return f'user:{emp_id}'
    return f'ip:{get_remote_address()}'


limiter = Limiter(
    key_func=rate_limit_key,
    app=app,
    # Per employee once authenticated, per address before that. 600/minute is
    # roughly ten requests a second, which a dashboard polling every 5 s plus a few
    # panels stays well inside; the previous 200/minute was not a security posture,
    # it was an unmeasured default that happened to break the NFR.
    #
    # Overridable for test runs: a full suite issues thousands of requests in a
    # couple of minutes, and a 429 on the CSRF-token fetch shows up later as a
    # confusing "CSRF token missing or invalid" on an unrelated assertion.
    default_limits=[os.getenv('DEFAULT_RATE_LIMIT', '600 per minute')],
    storage_uri="memory://",
)

# ── Swagger ───────────────────────────────────────────────────────────
swagger_config = {
    'headers': [],
    'specs': [
        {
            'endpoint': 'apispec',
            'route': '/apispec.json',
            'rule_filter': lambda rule: rule.rule.startswith('/api/'),
            'model_filter': lambda tag: True,
        }
    ],
    'static_url_path': '/flasgger_static',
    'swagger_ui': True,
    'specs_route': '/docs/',
}
swagger = Swagger(app, config=swagger_config, template={
    'info': {
        'title': 'HRMS API',
        'description': 'Human Resource Management System',
        'version': '1.0.0',
    },
    'securityDefinitions': {
        'sessionAuth': {
            'type': 'apiKey',
            'name': 'Cookie',
            'in': 'header',
        }
    }
})

# ── Scheduler ──────────────────────────────────────────────────────────
scheduler = BackgroundScheduler()
STARTED = False


# ══════════════════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════════════════

def get_db():
    """A connection to the PostgreSQL backend.

    DuckDB was the original runtime and was removed at the Phase-6 decommission,
    so this no longer branches. What the branch *was* hiding is worth recording:
    a process-wide shared DuckDB connection was tried and reverted, because the
    threaded server interleaves `execute()` and `fetchall()` across requests and
    one connection means one cursor state, so results got corrupted
    (`ValueError: not enough values to unpack`) under load. DuckDB also attached
    a file only once per process, so a concurrent second connect raised
    "Unique file handle conflict" — which is why the browser suite had to run the
    dev server single-threaded on that backend. PostgreSQL has no such
    constraint, so the suite is threaded again.
    """
    import db_backend
    return db_backend.connect()


def _is_public_target_schema():
    """True when the app is serving the immutable v2.0 ``public`` schema."""
    import db_backend
    return db_backend.app_schema() == 'public'


def _has_column(conn, table, column):
    import db_backend
    return bool(conn.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_schema = ? "
        "AND table_name = ? AND column_name = ?",
        [db_backend.app_schema(), table, column],
    ).fetchone())


def _advance_public_identity_sequences(conn):
    """Keep public identity sequences ahead after boot-time compatibility seeds."""
    if not _is_public_target_schema():
        return
    import db_backend

    schema = db_backend.app_schema()
    rows = conn.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = ? AND is_identity = 'YES'",
        [schema],
    ).fetchall()
    identifier = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
    for table, column in rows:
        if not identifier.fullmatch(table) or not identifier.fullmatch(column):
            raise RuntimeError(f'unsafe public identity identifier: {table}.{column}')
        sequence = conn.execute(
            "SELECT pg_get_serial_sequence(?, ?)",
            [f'{schema}.{table}', column],
        ).fetchone()[0]
        if not sequence:
            raise RuntimeError(f'public identity sequence missing for {table}.{column}')
        last_value = int(conn.execute(f'SELECT last_value FROM {sequence}').fetchone()[0])
        max_value = int(conn.execute(
            f'SELECT COALESCE(MAX({column}), 0) FROM {schema}.{table}'
        ).fetchone()[0])
        if last_value < max_value:
            conn.execute('SELECT setval(?, ?, true)', [sequence, max_value])


# ── Shift model (service-layer rewrite inc 2) ──────────────────────────────
# v1.0 keeps shifts on users.shift_start/shift_end (DuckDB + PG legacy).
# v2.0 (public) moves them to the effective-dated shift_assignments table
# (FR-ATT-17) and users has NO shift columns — init_db must not re-add them.
_SHIFT_MODEL_CACHE: dict = {}


def _shift_model() -> bool:
    """True when the connected schema stores shifts in ``shift_assignments``
    (v2.0 public); False on the v1.0 shape (``users.shift_start/end``).

    Introspected once per process per backend+schema and cached — the same
    pattern the DB adapter uses for boolean columns.
    """
    import db_backend
    schema = db_backend.app_schema()
    key = schema
    if key not in _SHIFT_MODEL_CACHE:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = ? AND table_name = 'shift_assignments'",
                [schema],
            ).fetchone()
            _SHIFT_MODEL_CACHE[key] = bool(row)
        except Exception:
            _SHIFT_MODEL_CACHE[key] = False
        finally:
            conn.close()
    return _SHIFT_MODEL_CACHE[key]


_PAYROLL_MODEL_CACHE: dict = {}


def _payroll_v2_model() -> bool:
    """True when payroll runs use the v2.0 maker-checker columns."""
    import db_backend
    schema = db_backend.app_schema()
    key = schema
    if key not in _PAYROLL_MODEL_CACHE:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = ? AND table_name = 'payroll_runs' "
                "AND column_name = 'submitted_by'",
                [schema],
            ).fetchone()
            _PAYROLL_MODEL_CACHE[key] = bool(row)
        except Exception:
            _PAYROLL_MODEL_CACHE[key] = False
        finally:
            conn.close()
    return _PAYROLL_MODEL_CACHE[key]


_SALARY_MODEL_CACHE: dict = {}


def _salary_v2_model() -> bool:
    """True when salary structures carry effective-dated end dates."""
    import db_backend
    schema = db_backend.app_schema()
    key = schema
    if key not in _SALARY_MODEL_CACHE:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = ? AND table_name = 'salary_structures' "
                "AND column_name = 'effective_to'",
                [schema],
            ).fetchone()
            _SALARY_MODEL_CACHE[key] = bool(row)
        except Exception:
            _SALARY_MODEL_CACHE[key] = False
        finally:
            conn.close()
    return _SALARY_MODEL_CACHE[key]


def _fmt_shift_time(v):
    """Normalise a shift time (str 'HH:MM' or datetime.time) to 'HH:MM'."""
    if v is None:
        return None
    if hasattr(v, 'strftime'):
        return v.strftime('%H:%M')
    s = str(v)
    return s[:5] if len(s) >= 5 else s


def _parse_shift_time(s):
    """'HH:MM' or '24x7' → (datetime.time | None, is_24x7)."""
    if not s:
        return None, False
    s = str(s).strip()
    if s.lower() == '24x7':
        return None, True
    try:
        return datetime.strptime(s[:5], '%H:%M').time(), False
    except Exception:
        return None, False


def get_shift(emp_id, conn=None, on_date=None):
    """Resolve an employee's current shift as ('HH:MM', 'HH:MM') strings, or
    ('24x7', '24x7') for round-the-clock. Returns (None, None) when unset.

    v1.0 shape: static ``users.shift_start/shift_end`` columns.
    v2.0 shape: the effective-dated ``shift_assignments`` row covering
    ``on_date`` (default today), FR-ATT-17.
    """
    own = conn is None
    if own:
        conn = get_db()
    try:
        if on_date is None:
            on_date = datetime.now().date()
        if _shift_model():
            row = conn.execute(
                "SELECT shift_type, shift_start, shift_end FROM shift_assignments "
                "WHERE emp_id = ? AND effective_from <= ? "
                "AND (effective_to IS NULL OR effective_to >= ?) "
                "ORDER BY effective_from DESC LIMIT 1",
                [emp_id, on_date, on_date],
            ).fetchone()
            if not row:
                return None, None
            stype, sstart, send = row[0], row[1], row[2]
            if stype == '24x7':
                return '24x7', '24x7'
            return _fmt_shift_time(sstart), _fmt_shift_time(send)
        row = conn.execute("SELECT shift_start, shift_end FROM users WHERE emp_id = ?", [emp_id]).fetchone()
        if not row:
            return None, None
        return (row[0] or None), (row[1] or None)
    finally:
        if own:
            conn.close()


def set_shift(emp_id, shift_start, shift_end, conn=None, weekly_off=None, effective_from=None):
    """Persist an employee's (static) shift and weekly-off pattern.

    v1.0 shape: ``users.shift_start/shift_end/weekly_off_pattern`` columns.
    v2.0 shape: replace that employee's ``shift_assignments`` rows with one
    open-ended row — the v1.0 one-shift-per-employee equivalent. Effective-dated
    scheduling (multiple periods) is a Phase-4 concern (FR-ATT-17).

    ``weekly_off=None`` preserves the employee's current pattern (or falls
    back to ``WEEKLY_OFF_PATTERN``) so callers that only change shift times do
    not silently reset the employee's non-working days.
    """
    if not shift_start and not shift_end:
        return
    if effective_from is None:
        effective_from = datetime.now().date()
    own = conn is None
    if own:
        conn = get_db()
    try:
        if _shift_model():
            if weekly_off is None:
                current = conn.execute(
                    "SELECT weekly_off_pattern FROM shift_assignments WHERE emp_id = ? "
                    "ORDER BY effective_from DESC LIMIT 1",
                    [emp_id],
                ).fetchone()
                weekly_off = (current[0] if current else None) or os.getenv('WEEKLY_OFF_PATTERN', 'Sat,Sun')
            start_t, start_24x7 = _parse_shift_time(shift_start)
            end_t, end_24x7 = _parse_shift_time(shift_end)
            stype = '24x7' if (start_24x7 or end_24x7) else 'Fixed'
            if stype == '24x7':
                start_t = datetime.strptime('09:00', '%H:%M').time()
                end_t = datetime.strptime('18:00', '%H:%M').time()
            conn.execute("DELETE FROM shift_assignments WHERE emp_id = ?", [emp_id])
            conn.execute(
                "INSERT INTO shift_assignments (emp_id, shift_type, shift_start, shift_end, weekly_off_pattern, effective_from, effective_to) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                [emp_id, stype, start_t, end_t, weekly_off, effective_from],
            )
        elif weekly_off is None:
            conn.execute(
                "UPDATE users SET shift_start = ?, shift_end = ? WHERE emp_id = ?",
                [shift_start, shift_end, emp_id],
            )
        else:
            conn.execute(
                "UPDATE users SET shift_start = ?, shift_end = ?, weekly_off_pattern = ? WHERE emp_id = ?",
                [shift_start, shift_end, weekly_off, emp_id],
            )
    finally:
        if own:
            conn.close()


def get_weekly_off_pattern(emp_id, conn=None, on_date=None):
    """Return the employee's effective weekly-off pattern for ``on_date``.

    v2.0 stores it on ``shift_assignments`` (falling back to the effective
    leave-policy assignment). The v1.0 compatibility shape stores it on
    ``users``. If neither has data, ``WEEKLY_OFF_PATTERN`` (default
    ``Sat,Sun``) is used rather than hard-coding Monday-Friday in the job.
    """
    own = conn is None
    if own:
        conn = get_db()
    if on_date is None:
        on_date = datetime.now().date()
    try:
        if _shift_model():
            row = conn.execute(
                "SELECT weekly_off_pattern FROM shift_assignments "
                "WHERE emp_id = ? AND effective_from <= ? "
                "AND (effective_to IS NULL OR effective_to >= ?) "
                "ORDER BY effective_from DESC LIMIT 1",
                [emp_id, on_date, on_date],
            ).fetchone()
            if row and row[0]:
                return row[0]
            try:
                row = conn.execute(
                    "SELECT weekly_off_pattern FROM leave_policy_assignments "
                    "WHERE emp_id = ? AND effective_from <= ? "
                    "AND (effective_to IS NULL OR effective_to >= ?) "
                    "ORDER BY effective_from DESC LIMIT 1",
                    [emp_id, on_date, on_date],
                ).fetchone()
                if row and row[0]:
                    return row[0]
            except Exception:
                pass
        else:
            row = conn.execute(
                "SELECT weekly_off_pattern FROM users WHERE emp_id = ?", [emp_id]
            ).fetchone()
            if row and row[0]:
                return row[0]
        return os.getenv('WEEKLY_OFF_PATTERN', 'Sat,Sun')
    finally:
        if own:
            conn.close()


def _get_shift_date_for_dt(emp_id, dt, conn):
    shift_start_str, _ = get_shift(emp_id, conn)
    if shift_start_str and shift_start_str != '24x7':
        try:
            parts = shift_start_str.split(':')
            h, m = int(parts[0]), int(parts[1])
            shift_start_today = dt.replace(hour=h, minute=m, second=0, microsecond=0)
            if dt >= shift_start_today:
                return dt.date()
            else:
                return (dt - timedelta(days=1)).date()
        except Exception:
            pass
    return dt.date()


def _fix_seed_shift_dates(conn, now):
    sessions = conn.execute("SELECT session_id, emp_id, login_time FROM user_sessions").fetchall()
    for sid, eid, lt in sessions:
        if lt:
            correct_date = _get_shift_date_for_dt(eid, lt, conn)
            conn.execute("UPDATE user_sessions SET session_date = ? WHERE session_id = ?", [correct_date, sid])
    breaks = conn.execute("SELECT break_id, emp_id, start_time FROM breaks").fetchall()
    for bid, eid, st in breaks:
        if st:
            correct_date = _get_shift_date_for_dt(eid, st, conn)
            conn.execute("UPDATE breaks SET break_date = ? WHERE break_id = ?", [correct_date, bid])
    conn.commit()


def _token_digest(token):
    """The value stored in ``password_reset_tokens.token`` (FR-AUTH-09).

    SHA-256, not a slow password hash, because this is not a password: it is a
    256-bit random value that must be looked up by equality on every reset attempt,
    and Argon2id cannot be indexed. The stored column therefore holds this digest
    and never the token.

    Defined *above* ``init_db`` because the boot seed needs it, and ``init_db``
    runs at import — as a function below this one it would not exist yet.
    """
    return hashlib.sha256(str(token).encode()).hexdigest()


def init_db():
    conn = get_db()

    # ── Users ──────────────────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            emp_id VARCHAR PRIMARY KEY,
            name VARCHAR NOT NULL,
            email VARCHAR NOT NULL,
            password VARCHAR NOT NULL,
            role VARCHAR DEFAULT 'Employee',
            department VARCHAR,
            designation VARCHAR,
            manager_emp_id VARCHAR,
            phone VARCHAR,
            date_of_birth DATE,
            date_of_joining DATE,
            address VARCHAR,
            emergency_contact_name VARCHAR,
            emergency_contact_phone VARCHAR,
            status VARCHAR DEFAULT 'Active',
            allow_login INTEGER DEFAULT 1,
            allow_breaks INTEGER DEFAULT 1,
            first_login TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # FR-ATS-03: the v2.0 public users table owns candidate_id; compatibility
    # schemas receive the same nullable link during boot.
    if not _is_public_target_schema() and not _has_column(conn, 'users', 'candidate_id'):
        conn.execute("ALTER TABLE users ADD COLUMN candidate_id BIGINT")

    # ── User Sessions ──────────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS user_sessions (
            session_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            login_time TIMESTAMP NOT NULL,
            logout_time TIMESTAMP,
            total_hours DECIMAL(10,2),
            session_date DATE,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── RBAC compatibility (public already owns the v2.0 table) ─────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS user_permissions (
            perm_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            module VARCHAR NOT NULL,
            allow INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id),
            UNIQUE (emp_id, module)
        )
    ''')

    # ── Break Types ────────────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS break_types (
            break_type VARCHAR PRIMARY KEY,
            daily_limit_minutes INTEGER,
            description VARCHAR
        )
    ''')

    # ── Break Approvals ───────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS break_approvals (
            approval_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            break_type VARCHAR NOT NULL,
            break_date DATE NOT NULL,
            reason VARCHAR,
            status VARCHAR DEFAULT 'Pending',
            approved_by VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id),
            FOREIGN KEY (break_type) REFERENCES break_types(break_type)
        )
    ''')
    # FR-ATT-05/CC-05: one Pending Lunch approval per employee per shift date,
    # enforced by a unique **partial** index, not just an app check. The canonical
    # schema has had `uq_pending_lunch_approval` since the baseline; the
    # compatibility shape now carries the same index. PostgreSQL is the only
    # backend since the Phase-6 decommission, so the "DuckDB cannot build a
    # partial index" excuse that forced a conditional INSERT for holiday opt-ins
    # no longer applies: the database itself refuses the duplicate under
    # concurrency, and the route translates a race into a 409 rather than a 500.
    if not _is_public_target_schema():
        conn.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_lunch_approval
                ON break_approvals (emp_id, break_type, break_date) WHERE status = 'Pending'
        ''')

    # ── Breaks ─────────────────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS breaks (
            break_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            break_type VARCHAR NOT NULL,
            start_time TIMESTAMP NOT NULL,
            end_time TIMESTAMP,
            duration_minutes INTEGER,
            break_date DATE,
            status VARCHAR DEFAULT 'Active',
            -- FR-AUTH-14/FR-JOB-02. The canonical schema already carries this column
            -- and documents the vocabulary on it
            -- (orphan_timeout|admin_dispose|auto_end_new_break); it had no writer at
            -- all, so a break could be closed for four different reasons with no way to
            -- tell which applied. Added additively rather than by rewriting the CREATE,
            -- because init_db must never reshape a table that already exists.
            ended_reason VARCHAR,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id),
            FOREIGN KEY (break_type) REFERENCES break_types(break_type)
        )
    ''')

    # ── Audit Log (new) ────────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS audit_log (
            log_id INTEGER PRIMARY KEY,
            emp_id VARCHAR,
            actor VARCHAR,
            action VARCHAR NOT NULL,
            entity VARCHAR,
            entity_id VARCHAR,
            details VARCHAR,
            "before" TEXT,
            "after" TEXT,
            ip_address VARCHAR,
            request_id VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # ── Leave Requests (new) ───────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS leave_requests (
            leave_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            leave_type VARCHAR NOT NULL,
            start_date DATE NOT NULL,
            end_date DATE NOT NULL,
            year INTEGER NOT NULL DEFAULT 0,
            reason VARCHAR,
            status VARCHAR DEFAULT 'Pending',
            approved_by VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # FR-LEA-02's `session` (Full | First-half | Second-half) and FR-LEA-09's stored
    # `days`. The canonical table already has `session` and Alembic 0010 adds `days`;
    # the compatibility shape gains both here. Added additively because
    # `CREATE TABLE IF NOT EXISTS` does nothing for a table that already exists.
    #
    # `days` exists so approve and cancel move **exactly** what apply reserved.
    # Recomputing at each step is not equivalent: a holiday added between the request
    # and its approval would make approve release a different number of days than
    # apply reserved, and the balance would drift by the difference with no audit
    # trail — which is the ledger defect FR-LEA-06 was written to prevent, reappearing
    # one layer down.
    for ddl in (
        "ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS session VARCHAR DEFAULT 'Full'",
        'ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS days INTEGER',
    ):
        conn.execute(ddl)

    # ── Migration: add year column to leave_requests ─────────────
    try:
        conn.execute("ALTER TABLE leave_requests ADD COLUMN year INTEGER DEFAULT 0")
    except Exception:
        pass
    conn.execute("UPDATE leave_requests SET year = CAST(strftime('%Y', start_date) AS INTEGER) WHERE year = 0 OR year IS NULL")

    # ── Leave Balance (new) ────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS leave_balance (
            balance_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            leave_type VARCHAR NOT NULL,
            total_days INTEGER DEFAULT 0,
            used_days INTEGER DEFAULT 0,
            reserved INTEGER DEFAULT 0,
            year INTEGER NOT NULL,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')
    # FR-LEA-08a: approval delegation. The canonical schema has carried this table with
    # a `no_overlapping_delegation` GiST exclusion constraint since the baseline and no
    # route read or wrote it. The compatibility shape gains the table here; the
    # exclusion constraint cannot be reproduced portably, so the same predicate runs in
    # `delegations.create` and is asserted by a test.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS approval_delegations (
            delegation_id INTEGER PRIMARY KEY,
            delegator_id VARCHAR NOT NULL,
            delegate_id VARCHAR NOT NULL,
            starts_on DATE NOT NULL,
            ends_on DATE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (delegator_id) REFERENCES users(emp_id),
            FOREIGN KEY (delegate_id) REFERENCES users(emp_id)
        )
    ''')

    # FR-LEA-07: manual leave grants. A separate ledger rather than a write to
    # `leave_balance.total_days`, because that column is DERIVED and `ensure_balances`
    # overwrites it on every read — a grant written straight there would be silently
    # erased the next time anybody looked at the balance. `grant_month`/`grant_year`
    # carry the SRS's "for a type/month/year" even though only the year feeds the
    # entitlement, so the record says which period an administrator adjusted.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS leave_grants (
            grant_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            leave_type VARCHAR NOT NULL,
            days INTEGER NOT NULL,
            grant_month INTEGER,
            grant_year INTEGER NOT NULL,
            granted_by VARCHAR,
            reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')
    if not _is_public_target_schema() and not _has_column(conn, 'leave_balance', 'reserved'):
        conn.execute("ALTER TABLE leave_balance ADD COLUMN reserved INTEGER DEFAULT 0")

    # ── Effective-dated leave policy (FR-LEA-08) ────────────────────
    # The compatibility shape. v2.0 `public` already owns this table with an
    # identity key and the `no_overlapping_policy` exclusion constraint, so
    # `CREATE TABLE IF NOT EXISTS` is a no-op there and the target is never
    # reshaped at boot.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS leave_policy_assignments (
            assignment_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            location VARCHAR,
            grade VARCHAR,
            accrual_rate NUMERIC(6,2),
            carry_forward_cap INTEGER,
            encashment_rule VARCHAR,
            weekly_off_pattern VARCHAR,
            effective_from DATE NOT NULL,
            effective_to DATE,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')


    # ── Two-person anonymisation requests (FR-USR) ──────────────────
    # Compatibility shape; the v2.0 target owns the identity key and JSONB.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS anonymisation_requests (
            request_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            status VARCHAR NOT NULL DEFAULT 'proposed',
            requested_by VARCHAR,
            confirmed_by VARCHAR,
            plan_summary VARCHAR,
            result_summary VARCHAR,
            failure_reason VARCHAR,
            requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            confirmed_at TIMESTAMP,
            applied_at TIMESTAMP
        )
    ''')

    # ── Monthly leave accrual ledger (FR-LEA-08) ───────────────────
    # The compatibility shape. v2.0 `public` owns this table with an identity
    # key, so `CREATE TABLE IF NOT EXISTS` is a no-op there.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS monthly_leave_grants (
            grant_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            leave_type VARCHAR NOT NULL,
            days INTEGER NOT NULL,
            month INTEGER NOT NULL,
            year INTEGER NOT NULL,
            granted_by VARCHAR,
            granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # ── Background import jobs (FR-USR-04) ────────────────────────
    # Compatibility shape: the identity key of the v2.0 target becomes an
    # INTEGER primary key here and is allocated through `_next_generated_id`.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS import_jobs (
            job_id INTEGER PRIMARY KEY,
            job_type VARCHAR NOT NULL DEFAULT 'users',
            status VARCHAR NOT NULL DEFAULT 'pending',
            filename VARCHAR NOT NULL,
            stored_path VARCHAR,
            total_rows INTEGER NOT NULL DEFAULT 0,
            processed_rows INTEGER NOT NULL DEFAULT 0,
            imported INTEGER NOT NULL DEFAULT 0,
            skipped INTEGER NOT NULL DEFAULT 0,
            error_summary VARCHAR,
            created_by VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,
            finished_at TIMESTAMP,
            failure_reason VARCHAR
        )
    ''')
    # ── Password Reset Tokens (new) ────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            token_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            token VARCHAR NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            used INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Idempotency Keys (CC-07) ──────────────────────────────────
    # Mirrors the v2.0 `public` shape (JSONB -> TEXT on DuckDB; no-op on
    # `public`, which owns the JSONB/TIMESTAMPTZ version).
    conn.execute('''
        CREATE TABLE IF NOT EXISTS idempotency_keys (
            key VARCHAR PRIMARY KEY,
            route VARCHAR NOT NULL,
            request_hash VARCHAR NOT NULL,
            response_status INTEGER NOT NULL,
            response_body TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL
        )
    ''')

    # ── Employee Documents ─────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS employee_documents (
            doc_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            doc_type VARCHAR NOT NULL,
            file_name VARCHAR,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Dependents ─────────────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS dependents (
            dependent_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            relationship VARCHAR NOT NULL,
            date_of_birth DATE,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Holidays ───────────────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS holidays (
            holiday_id INTEGER PRIMARY KEY,
            name VARCHAR NOT NULL,
            holiday_date DATE NOT NULL,
            year INTEGER NOT NULL,
            type VARCHAR DEFAULT 'National'
        )
    ''')
    # `location` is per-location applicability (FR-HOL-01) and exists on the v2.0
    # table. It is added here rather than left out because without it a holiday
    # cannot be scoped to a site, the "duplicate (name, date) *per location*"
    # rule of FR-HOL-02 has nothing to key on, and the compatibility schema
    # would silently accept what the target refuses. The ADD is guarded because
    # the table may predate it; the backfill of '' is what makes a pre-existing
    # org-wide holiday compare equal to a new one, matching
    # COALESCE(location, '') in the canonical index.
    try:
        conn.execute("ALTER TABLE holidays ADD COLUMN location VARCHAR")
    except Exception:
        pass  # already present
    conn.execute("UPDATE holidays SET location = '' WHERE location IS NULL")

    # ── Holiday Opt-ins ────────────────────────────────────────────
    # Optional holidays become attendance holidays only for employees with
    # an Approved opt-in (FR-HOL-03 / FR-JOB-01). No-op on v2.0 `public`.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS holiday_optins (
            optin_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            holiday_id INTEGER NOT NULL,
            status VARCHAR DEFAULT 'Pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id),
            FOREIGN KEY (holiday_id) REFERENCES holidays(holiday_id)
        )
    ''')

    # ── Attendance Days (Phase 4 / FR-JOB-01) ──────────────────────
    # Output of the nightly attendance job — one row per employee per day
    # (Present|Half-day|Absent|On Leave|Holiday|Weekly-off); the single
    # source for reports / payroll LOP. No-op on the v2.0 `public` schema
    # (already BIGINT-identity + TIMESTAMPTZ); DuckDB / legacy-PG get the
    # self-serving v1.0 shape.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS attendance_days (
            attendance_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            attendance_date DATE NOT NULL,
            status VARCHAR NOT NULL,
            shift_hours NUMERIC(8,2),
            source VARCHAR DEFAULT 'job',
            version INTEGER DEFAULT 1,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id),
            UNIQUE (emp_id, attendance_date)
        )
    ''')

    # ── Notifications ──────────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS notifications (
            notification_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            type VARCHAR NOT NULL,
            category VARCHAR DEFAULT 'General',
            message VARCHAR NOT NULL,
            related_link VARCHAR,
            is_read INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Notification preferences (FR-NOT-03) ─────────────────────────
    # The `category` column on `notifications` existed for this and nothing read it.
    # A row is optional and its absence means "default true", so adding a category
    # is a no-op for employees already in the system rather than a backfill.
    # No-op on v2.0 `public`, which owns the identity/BOOLEAN version.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS notification_preferences (
            pref_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            category VARCHAR NOT NULL,
            in_app INTEGER DEFAULT 1,
            email INTEGER DEFAULT 1,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id),
            UNIQUE (emp_id, category)
        )
    ''')

    # ── MFA credentials (FR-AUTH-11) ──────────────────────────────────
    # This table was in the canonical schema from the baseline with an encrypted
    # secret column and **no code anywhere that read or wrote it** — the
    # "schema without routes" shape the traceability pass exists to catch. Adding
    # the compat DDL makes the feature runnable on both shapes.
    # `enabled` is INTEGER here and BOOLEAN on v2.0 `public`; the adapter rewrites
    # the flag, so the routes write one value and both backends answer alike.
    # No-op on v2.0 `public`, which owns the identity key and the partial state.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS mfa_credentials (
            cred_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            secret_encrypted VARCHAR NOT NULL,
            enabled INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id),
            UNIQUE (emp_id)
        )
    ''')

    # ── FR-AUTH-03 account lockout state ───────────────────────────
    # Three additive columns on `users`; every existing row is already correct
    # (0 failures, no lock), so this needs no backfill and moves no data. No-op
    # on the v2.0 `public` schema, which gets them from Alembic 0009 — the compat
    # ALTERs exist only so a legacy-shaped database can serve the same login
    # route. `locked_until` is deliberately not a `status` value: a lockout is a
    # temporary consequence of failed sign-ins, not a sanctioned account state,
    # and folding it into `status` would make the two indistinguishable to an
    # administrator and in the audit trail.
    for ddl in (
        'ALTER TABLE users ADD COLUMN IF NOT EXISTS failed_attempts INTEGER DEFAULT 0',
        'ALTER TABLE users ADD COLUMN IF NOT EXISTS last_failed_login TIMESTAMP',
        'ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_until TIMESTAMP',
        # Idempotent add for a `breaks` table that predates the column. `CREATE TABLE
        # IF NOT EXISTS` above does nothing for an existing table, so without this the
        # FR-AUTH-14 sweep would raise "column ended_reason does not exist" on every
        # deployment that already has the v1.0 shape - and the job's own `except` would
        # swallow it, which is how this shipped broken in the first place.
        'ALTER TABLE breaks ADD COLUMN IF NOT EXISTS ended_reason VARCHAR',
    ):
        conn.execute(ddl)

    # ── Outbox (CC-09 transactional outbox) ────────────────────────
    # No-op on the v2.0 `public` schema (already BIGINT-identity + JSONB);
    # DuckDB / legacy-PG get the self-serving v1.0 shape.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS outbox_events (
            event_id INTEGER PRIMARY KEY,
            event_type VARCHAR NOT NULL,
            aggregate VARCHAR,
            aggregate_id VARCHAR,
            payload TEXT,
            status VARCHAR DEFAULT 'pending',
            attempts INTEGER DEFAULT 0,
            next_attempt_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            delivered_at TIMESTAMP
        )
    ''')

    # ── Regularization Requests ────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS regularization_requests (
            request_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            request_date DATE NOT NULL,
            reason VARCHAR NOT NULL,
            status VARCHAR DEFAULT 'Pending',
            approved_by VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Assets (Phase 2) ────────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS assets (
            asset_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            asset_type VARCHAR NOT NULL,
            asset_tag VARCHAR,
            brand VARCHAR,
            model VARCHAR,
            serial_number VARCHAR,
            issued_date DATE NOT NULL,
            return_date DATE,
            status VARCHAR DEFAULT 'Issued',
            notes VARCHAR,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Job Postings (Phase 2) ──────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS job_postings (
            job_id INTEGER PRIMARY KEY,
            title VARCHAR NOT NULL,
            department VARCHAR,
            location VARCHAR,
            description VARCHAR,
            requirements VARCHAR,
            status VARCHAR DEFAULT 'Open',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # ── Candidates (Phase 2) ────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS candidates (
            candidate_id INTEGER PRIMARY KEY,
            job_id INTEGER,
            name VARCHAR NOT NULL,
            email VARCHAR NOT NULL,
            phone VARCHAR,
            resume_text VARCHAR,
            status VARCHAR DEFAULT 'Applied',
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (job_id) REFERENCES job_postings(job_id)
        )
    ''')

    # ── Interviews (Phase 2) ────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS interviews (
            interview_id INTEGER PRIMARY KEY,
            candidate_id INTEGER NOT NULL,
            scheduled_at TIMESTAMP NOT NULL,
            interviewer VARCHAR,
            mode VARCHAR DEFAULT 'In-person',
            feedback VARCHAR,
            status VARCHAR DEFAULT 'Scheduled',
            FOREIGN KEY (candidate_id) REFERENCES candidates(candidate_id)
        )
    ''')

    # ── Offer Letters (Phase 2) ─────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS offer_letters (
            offer_id INTEGER PRIMARY KEY,
            candidate_id INTEGER NOT NULL,
            offered_salary DECIMAL(12,2),
            offer_date DATE NOT NULL,
            status VARCHAR DEFAULT 'Pending',
            accepted_at TIMESTAMP,
            notes VARCHAR,
            FOREIGN KEY (candidate_id) REFERENCES candidates(candidate_id)
        )
    ''')
    # FR-ATS-04: compatibility schemas need the same salary split columns as
    # the canonical public offer_letters table.
    if not _is_public_target_schema():
        for column, definition in {
            'basic_pct': 'DECIMAL(5,2)',
            'hra_pct': 'DECIMAL(5,2)',
            'allowances_pct': 'DECIMAL(5,2)',
        }.items():
            if not _has_column(conn, 'offer_letters', column):
                conn.execute(f"ALTER TABLE offer_letters ADD COLUMN {column} {definition}")

    if not _is_public_target_schema():
        conn.execute(
            """WITH ranked AS (
                   SELECT offer_id,
                          ROW_NUMBER() OVER (
                              PARTITION BY candidate_id
                              ORDER BY CASE WHEN status = 'Accepted' THEN 0 ELSE 1 END, offer_id
                          ) AS rn
                   FROM offer_letters
                   WHERE status IN ('Pending', 'Accepted')
               ) DELETE FROM offer_letters o USING ranked
               WHERE o.offer_id = ranked.offer_id AND ranked.rn > 1"""
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_active_offer_candidate "
            "ON offer_letters (candidate_id) WHERE status IN ('Pending', 'Accepted')"
        )

    # ── Onboarding Tasks (Phase 2 / FR-ONB) ──────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS onboarding_tasks (
            task_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            task_name VARCHAR NOT NULL,
            assigned_to VARCHAR NOT NULL,
            status VARCHAR DEFAULT 'Pending',
            due_date DATE,
            completed_at TIMESTAMP,
            stage INTEGER DEFAULT 1,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')
    if not _is_public_target_schema() and not _has_column(conn, 'onboarding_tasks', 'stage'):
        conn.execute("ALTER TABLE onboarding_tasks ADD COLUMN stage INTEGER DEFAULT 1")

    # ── Corrected onboarding workflow (FR-ONB-01..06) ───────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS onboarding_workflow (
            workflow_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            candidate_id INTEGER,
            current_step INTEGER DEFAULT 1,
            step1_status VARCHAR DEFAULT 'InProgress',
            step2_status VARCHAR DEFAULT 'Pending',
            step3_status VARCHAR DEFAULT 'Pending',
            step4_status VARCHAR DEFAULT 'Pending',
            step5_status VARCHAR DEFAULT 'Pending',
            completed INTEGER DEFAULT 0,
            completed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id),
            FOREIGN KEY (candidate_id) REFERENCES candidates(candidate_id)
        )
    ''')
    if not _is_public_target_schema() and not _has_column(conn, 'onboarding_workflow', 'step_started_at'):
        conn.execute("ALTER TABLE onboarding_workflow ADD COLUMN step_started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    conn.execute('''
        CREATE TABLE IF NOT EXISTS onboarding_checklist (
            item_id INTEGER PRIMARY KEY,
            workflow_id INTEGER NOT NULL,
            doc_type VARCHAR NOT NULL,
            status VARCHAR DEFAULT 'Pending',
            uploaded_at TIMESTAMP,
            reviewed_by VARCHAR,
            review_note VARCHAR,
            reviewed_at TIMESTAMP,
            FOREIGN KEY (workflow_id) REFERENCES onboarding_workflow(workflow_id),
            FOREIGN KEY (reviewed_by) REFERENCES users(emp_id),
            UNIQUE (workflow_id, doc_type)
        )
    ''')

    # ── Offboarding Tasks (Phase 2 / FR-OFF) ─────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS offboarding_tasks (
            task_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            task_name VARCHAR NOT NULL,
            assigned_to VARCHAR NOT NULL,
            status VARCHAR DEFAULT 'Pending',
            due_date DATE,
            completed_at TIMESTAMP,
            stage INTEGER DEFAULT 1,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')
    if not _is_public_target_schema() and not _has_column(conn, 'offboarding_tasks', 'stage'):
        conn.execute("ALTER TABLE offboarding_tasks ADD COLUMN stage INTEGER DEFAULT 1")

    # ── Exit Interviews (Phase 2 / FR-OFF-02) ───────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS exit_interviews (
            interview_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            reason VARCHAR NOT NULL,
            feedback VARCHAR,
            exit_date DATE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            offboard_id INTEGER,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')
    if not _is_public_target_schema() and not _has_column(conn, 'exit_interviews', 'offboard_id'):
        conn.execute("ALTER TABLE exit_interviews ADD COLUMN offboard_id INTEGER")

    # ── Corrected offboarding workflow (FR-OFF-01..03) ──────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS resignations (
            resignation_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            notice_date DATE NOT NULL,
            last_working_day DATE NOT NULL,
            reason VARCHAR,
            initiated_by VARCHAR NOT NULL,
            status VARCHAR DEFAULT 'Pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            version INTEGER DEFAULT 1,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id),
            FOREIGN KEY (initiated_by) REFERENCES users(emp_id)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS offboarding_workflow (
            offboard_id INTEGER PRIMARY KEY,
            resignation_id INTEGER NOT NULL,
            emp_id VARCHAR NOT NULL,
            stage1_status VARCHAR DEFAULT 'Pending',
            stage2_status VARCHAR DEFAULT 'Pending',
            stage3_status VARCHAR DEFAULT 'Pending',
            stage4_status VARCHAR DEFAULT 'Pending',
            stage5_status VARCHAR DEFAULT 'Pending',
            completed INTEGER DEFAULT 0,
            completed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (resignation_id) REFERENCES resignations(resignation_id),
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')
    if not _is_public_target_schema():
        conn.execute('''
            CREATE TABLE IF NOT EXISTS offboarding_approvals (
                approval_id INTEGER PRIMARY KEY,
                offboard_id INTEGER NOT NULL,
                actor_emp_id VARCHAR NOT NULL,
                action VARCHAR NOT NULL,
                from_status VARCHAR NOT NULL,
                to_status VARCHAR NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (offboard_id) REFERENCES offboarding_workflow(offboard_id),
                FOREIGN KEY (actor_emp_id) REFERENCES users(emp_id)
            )
        ''')

    if not _is_public_target_schema():
        conn.execute('''
            CREATE TABLE IF NOT EXISTS offboarding_settlements (
                settlement_id INTEGER PRIMARY KEY,
                offboard_id INTEGER NOT NULL UNIQUE,
                pending_payroll DECIMAL(14,2) DEFAULT 0,
                lop_adjustment DECIMAL(14,2) DEFAULT 0,
                leave_encashment DECIMAL(14,2) DEFAULT 0,
                deductions DECIMAL(14,2) DEFAULT 0,
                asset_damage DECIMAL(14,2) DEFAULT 0,
                total_amount DECIMAL(14,2) DEFAULT 0,
                status VARCHAR DEFAULT 'Prepared',
                prepared_by VARCHAR NOT NULL,
                prepared_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                approved_by VARCHAR,
                approved_at TIMESTAMP,
                FOREIGN KEY (offboard_id) REFERENCES offboarding_workflow(offboard_id),
                FOREIGN KEY (prepared_by) REFERENCES users(emp_id),
                FOREIGN KEY (approved_by) REFERENCES users(emp_id)
            )
        ''')

    # ── Salary Structures (Phase 2) ─────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS salary_structures (
            struct_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            basic DECIMAL(12,2) DEFAULT 0,
            hra DECIMAL(12,2) DEFAULT 0,
            allowances DECIMAL(12,2) DEFAULT 0,
            deductions DECIMAL(12,2) DEFAULT 0,
            effective_from DATE NOT NULL,
            effective_to DATE,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Payroll Runs (Phase 2 / FR-PAY-06) ───────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS payroll_runs (
            run_id INTEGER PRIMARY KEY,
            month INTEGER NOT NULL,
            year INTEGER NOT NULL,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status VARCHAR DEFAULT 'Draft',
            submitted_by VARCHAR,
            submitted_at TIMESTAMP,
            approved_by VARCHAR,
            approved_at TIMESTAMP,
            finalized_at TIMESTAMP,
            adjustment_of_run_id INTEGER
        )
    ''')

    # ── Payroll Items (Phase 2) ─────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS payroll_items (
            item_id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            emp_id VARCHAR NOT NULL,
            gross_salary DECIMAL(12,2) DEFAULT 0,
            deductions_total DECIMAL(12,2) DEFAULT 0,
            net_salary DECIMAL(12,2) DEFAULT 0,
            pf DECIMAL(12,2) DEFAULT 0,
            esi DECIMAL(12,2) DEFAULT 0,
            pt DECIMAL(12,2) DEFAULT 0,
            tds DECIMAL(12,2) DEFAULT 0,
            lop_amount DECIMAL(12,2) DEFAULT 0,
            reimbursements DECIMAL(12,2) DEFAULT 0,
            payslip_generated INTEGER DEFAULT 0,
            FOREIGN KEY (run_id) REFERENCES payroll_runs(run_id),
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Payroll maker-checker trail (FR-PAY-06) ─────────────────────
    # No-op on v2.0 public, which already owns identity + TIMESTAMPTZ.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS payroll_approvals (
            approval_id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            actor_emp_id VARCHAR NOT NULL,
            action VARCHAR NOT NULL,
            from_status VARCHAR NOT NULL,
            to_status VARCHAR NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (run_id) REFERENCES payroll_runs(run_id),
            FOREIGN KEY (actor_emp_id) REFERENCES users(emp_id)
        )
    ''')

    # Existing v1.0 DuckDB/legacy files predate the v2.0 payroll columns.
    # Add them only on the compatibility schema; `public` already has the
    # identity/TIMESTAMPTZ shape and must not be ALTERed at boot.
    if not _salary_v2_model():
        try:
            conn.execute("ALTER TABLE salary_structures ADD COLUMN effective_to DATE")
        except Exception:
            pass
    if not _payroll_v2_model():
        for table, columns in {
            'payroll_runs': [
                ('submitted_by', 'VARCHAR'), ('submitted_at', 'TIMESTAMP'),
                ('approved_by', 'VARCHAR'), ('approved_at', 'TIMESTAMP'),
                ('finalized_at', 'TIMESTAMP'), ('adjustment_of_run_id', 'INTEGER'),
            ],
            'payroll_items': [
                ('tds', 'DECIMAL(12,2) DEFAULT 0'),
                ('lop_amount', 'DECIMAL(12,2) DEFAULT 0'),
                ('reimbursements', 'DECIMAL(12,2) DEFAULT 0'),
            ],
        }.items():
            for column, definition in columns:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                except Exception:
                    pass

    # ── Performance Goals (Phase 3) ─────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS goals (
            goal_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            title VARCHAR NOT NULL,
            description VARCHAR,
            target_date DATE,
            weight INTEGER DEFAULT 1,
            rating INTEGER,
            status VARCHAR DEFAULT 'Active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Performance Reviews (Phase 3) ───────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS performance_reviews (
            review_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            reviewer_id VARCHAR NOT NULL,
            review_period VARCHAR NOT NULL,
            overall_rating REAL,
            comments VARCHAR,
            status VARCHAR DEFAULT 'Draft',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            submitted_at TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id),
            FOREIGN KEY (reviewer_id) REFERENCES users(emp_id)
        )
    ''')

    # ── 360 Feedback (Phase 3) ──────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS feedback_360 (
            feedback_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            reviewer_id VARCHAR NOT NULL,
            category VARCHAR,
            rating INTEGER,
            comment VARCHAR,
            submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id),
            FOREIGN KEY (reviewer_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Expense Categories (Phase 3) ─────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS expense_categories (
            cat_id INTEGER PRIMARY KEY,
            name VARCHAR NOT NULL,
            description VARCHAR
        )
    ''')

    # ── Expense Claims (Phase 3) ─────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS expense_claims (
            claim_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            cat_id INTEGER NOT NULL,
            amount DECIMAL(12,2) NOT NULL,
            description VARCHAR,
            receipt_path VARCHAR,
            status VARCHAR DEFAULT 'Pending',
            approved_by VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id),
            FOREIGN KEY (cat_id) REFERENCES expense_categories(cat_id)
        )
    ''')
    # FR-EXP-03: `Approved -> Paid by Finance only` needs the timestamp the
    # canonical target already carries, and a rejection has to say why. Both are
    # additive on legacy/DuckDB; on v2.0 `public` the columns already exist and
    # the ALTER is skipped, so the boot seed does not reshape the target.
    for _column, _ddl in (('paid_at', 'TIMESTAMP'), ('rejection_reason', 'VARCHAR')):
        try:
            conn.execute(f'ALTER TABLE expense_claims ADD COLUMN {_column} {_ddl}')
        except Exception:
            pass  # already present on this schema

    # ── Help Desk Tickets (Phase 3) ──────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS tickets (
            ticket_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            subject VARCHAR NOT NULL,
            description VARCHAR,
            category VARCHAR,
            priority VARCHAR DEFAULT 'Medium',
            status VARCHAR DEFAULT 'Open',
            assigned_to VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP,
            resolved_at TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Ticket Comments (Phase 3) ────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS ticket_comments (
            comment_id INTEGER PRIMARY KEY,
            ticket_id INTEGER NOT NULL,
            emp_id VARCHAR NOT NULL,
            comment VARCHAR NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (ticket_id) REFERENCES tickets(ticket_id),
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Document Uploads metadata (Phase 3) ─────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS documents (
            doc_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            category VARCHAR DEFAULT 'Other',
            file_path VARCHAR,
            file_size INTEGER,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Seed Data ──────────────────────────────────────────────────
    pwd_hash = hash_password('pass123')
    result = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if (
        result == 0
        and os.getenv('FLASK_ENV', '').lower() == 'production'
        and os.getenv('HRMS_ALLOW_DEMO_SEED') != '1'
    ):
        conn.close()
        raise RuntimeError(
            'Production database is empty; run the approved ETL/cutover before starting HRMS'
        )
    if result == 0:
        conn.execute(
            "INSERT INTO users (emp_id, name, email, password, role, department, designation, phone, date_of_joining, status, allow_login, allow_breaks, first_login, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ['EMP001', 'Shubham Jawalkar', 'shubham@company.com', pwd_hash,
             'Admin', 'MIS', 'Tech Lead', '9876543210', datetime.now().date(),
             'Active', 1, 1, datetime.now(), datetime.now()]
        )
        conn.execute(
            "INSERT INTO users (emp_id, name, email, password, role, department, designation, phone, date_of_joining, manager_emp_id, status, allow_login, allow_breaks, first_login, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ['EMP002', 'Sachin Bhakte', 'sachinbhakte@gmail.com', pwd_hash,
             'Employee', 'Operations', 'Jr Developer', '9876543211', datetime.now().date(),
             'EMP001', 'Active', 1, 1, datetime.now(), datetime.now()]
        )

        # Seed some holidays. The named column list is load-bearing: `location`
        # (FR-HOL-01) now exists on the compatibility table too, so a bare
        # `INSERT INTO holidays VALUES (?, ?, ?, ?, ?)` fails to bind with a column
        # count error on boot. Org-wide, so the location is an empty string rather
        # than NULL — the same spelling the unique index's COALESCE produces.
        year = datetime.now().year
        base = 9000000 + (datetime.now().microsecond % 100000)
        holidays_data = [
            [base + 1, 'New Year', f'{year}-01-01', year, 'National', ''],
            [base + 2, 'Republic Day', f'{year}-01-26', year, 'National', ''],
            [base + 3, 'Independence Day', f'{year}-08-15', year, 'National', ''],
            [base + 4, 'Diwali', f'{year}-11-01', year, 'Optional', ''],
            [base + 5, 'Christmas', f'{year}-12-25', year, 'Optional', ''],
        ]
        conn.executemany(
            'INSERT INTO holidays (holiday_id, name, holiday_date, year, type, location) '
            'VALUES (?, ?, ?, ?, ?, ?)', holidays_data,
        )

    # ── Seed Expense Categories ───────────────────────────────────
    result = conn.execute("SELECT COUNT(*) FROM expense_categories").fetchone()[0]
    if result == 0:
        conn.executemany(
            "INSERT INTO expense_categories VALUES (?, ?, ?)",
            [[1, 'Travel', 'Travel expenses including flights, trains, cabs'],
             [2, 'Food', 'Meals and refreshments'],
             [3, 'Office Supplies', 'Stationery and office consumables'],
             [4, 'Equipment', 'Hardware and equipment purchases'],
             [5, 'Utilities', 'Phone, internet, electricity bills'],
             [6, 'Other', 'Miscellaneous expenses']]
        )

    # ── Normalize any passwords still stored in plain/legacy format ──
    # Prefix check ($2 = bcrypt, $a = Argon2id) — avoids LIKE/% patterns,
    # which the PG adapter would double-escape differently between stacks.
    conn.execute(
        "UPDATE users SET password = ? WHERE SUBSTR(password, 1, 2) NOT IN ('$2', '$a')",
        [pwd_hash]
    )

    # ── Migrate: add new columns if missing ───────────────────────
    for col in ['designation', 'manager_emp_id', 'phone', 'date_of_birth', 'date_of_joining', 'address', 'emergency_contact_name', 'emergency_contact_phone']:
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} VARCHAR")
        except Exception:
            pass

    # shift_start/shift_end live on users ONLY in the v1.0 model. On v2.0
    # public the ALTER must NOT fire — the app must leave the target schema
    # untouched (it would otherwise mutate it at every boot).
    if not _shift_model():
        for col in ['shift_start', 'shift_end']:
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} VARCHAR")
            except Exception:
                pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN weekly_off_pattern VARCHAR DEFAULT 'Sat,Sun'")
        except Exception:
            pass
        conn.execute("UPDATE users SET weekly_off_pattern = 'Sat,Sun' WHERE weekly_off_pattern IS NULL")

    result = conn.execute("SELECT COUNT(*) FROM break_types").fetchone()[0]
    if result == 0:
        conn.executemany(
            "INSERT INTO break_types VALUES (?, ?, ?)",
            [('Tea', 15, 'Tea Break - 15 minutes'),
             ('Lunch', 60, 'Lunch Break - 1 hour'),
             ('Personal', 30, 'Personal Break - 30 minutes')]
        )

    # ── Seed Leave Balance ─────────────────────────────────────────
    result = conn.execute("SELECT COUNT(*) FROM leave_balance").fetchone()[0]
    if result == 0:
        year = datetime.now().year
        bid = int(datetime.now().timestamp() * 1000) % 1000000
        for emp in conn.execute("SELECT emp_id FROM users").fetchall():
            bid += 1
            conn.execute(
                "INSERT INTO leave_balance (balance_id, emp_id, leave_type, total_days, used_days, reserved, year) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [bid, emp[0], 'Casual', 12, 0, 0, year],
            )
            bid += 1
            conn.execute(
                "INSERT INTO leave_balance (balance_id, emp_id, leave_type, total_days, used_days, reserved, year) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [bid, emp[0], 'Sick', 10, 0, 0, year],
            )
            bid += 1
            conn.execute(
                "INSERT INTO leave_balance (balance_id, emp_id, leave_type, total_days, used_days, reserved, year) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [bid, emp[0], 'Annual', 20, 0, 0, year],
            )

    # ── Seed sample rows for all major modules ────────────────────
    now = datetime.now()
    base_id = int(now.timestamp() * 1000) % 1000000

    if conn.execute("SELECT COUNT(*) FROM user_sessions").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO user_sessions (session_id, emp_id, login_time, logout_time, total_hours, session_date) VALUES (?, ?, ?, ?, ?, ?)",
            [base_id + 1, 'EMP001', now - timedelta(hours=8), now, 8.0, now.date()]
        )
        conn.execute(
            "INSERT INTO user_sessions (session_id, emp_id, login_time, logout_time, total_hours, session_date) VALUES (?, ?, ?, ?, ?, ?)",
            [base_id + 2, 'EMP002', now - timedelta(hours=6), now - timedelta(hours=1), 5.0, now.date()]
        )

    if conn.execute("SELECT COUNT(*) FROM breaks").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO breaks (break_id, emp_id, break_type, start_time, end_time, duration_minutes, break_date, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 3, 'EMP001', 'Tea', now - timedelta(minutes=30), now - timedelta(minutes=15), 15, now.date(), 'Completed']
        )
        conn.execute(
            "INSERT INTO breaks (break_id, emp_id, break_type, start_time, end_time, duration_minutes, break_date, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 4, 'EMP002', 'Lunch', now - timedelta(hours=1), now - timedelta(minutes=30), 30, now.date(), 'Completed']
        )

    if conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO audit_log (log_id, emp_id, action, details, ip_address, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            [base_id + 5, 'EMP001', 'LOGIN', 'User signed in', '127.0.0.1', now]
        )
        conn.execute(
            "INSERT INTO audit_log (log_id, emp_id, action, details, ip_address, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            [base_id + 6, 'EMP002', 'PROFILE_UPDATE', 'Updated profile', '127.0.0.1', now - timedelta(hours=1)]
        )

    if conn.execute("SELECT COUNT(*) FROM leave_requests").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO leave_requests (leave_id, emp_id, leave_type, start_date, end_date, year, reason, status, approved_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 7, 'EMP002', 'Casual', (now + timedelta(days=3)).date(), (now + timedelta(days=4)).date(), (now + timedelta(days=3)).year, 'Personal work', 'Pending', None, now, now]
        )
        conn.execute(
            "INSERT INTO leave_requests (leave_id, emp_id, leave_type, start_date, end_date, year, reason, status, approved_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 8, 'EMP002', 'Sick', (now + timedelta(days=10)).date(), (now + timedelta(days=12)).date(), (now + timedelta(days=10)).year, 'Medical appointment', 'Approved', 'EMP001', now, now]
        )

    if conn.execute("SELECT COUNT(*) FROM password_reset_tokens").fetchone()[0] < 2:
        # Digests, not the tokens themselves (FR-AUTH-09). These used to be written
        # in the clear, which is why `/api/reset-password` had to look tokens up as
        # `token IN (raw, digest)` — that accommodation is what kept the hashing half
        # done, and two seeded working credentials in the sample database is exactly
        # the property "hashed at rest" exists to remove. The plaintext tokens
        # `reset-token-001` / `reset-token-002` still verify, because only their
        # digest is stored.
        conn.execute(
            "INSERT INTO password_reset_tokens (token_id, emp_id, token, expires_at, used, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            [base_id + 9, 'EMP002', _token_digest('reset-token-001'), now + timedelta(hours=2), 0, now]
        )
        conn.execute(
            "INSERT INTO password_reset_tokens (token_id, emp_id, token, expires_at, used, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            [base_id + 10, 'EMP002', _token_digest('reset-token-002'), now + timedelta(hours=4), 0, now]
        )

    if conn.execute("SELECT COUNT(*) FROM employee_documents").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO employee_documents (doc_id, emp_id, doc_type, file_name, uploaded_at) VALUES (?, ?, ?, ?, ?)",
            [base_id + 11, 'EMP001', 'Offer Letter', 'offer-letter.pdf', now]
        )
        conn.execute(
            "INSERT INTO employee_documents (doc_id, emp_id, doc_type, file_name, uploaded_at) VALUES (?, ?, ?, ?, ?)",
            [base_id + 12, 'EMP002', 'ID Proof', 'aadhaar.pdf', now - timedelta(days=1)]
        )

    if conn.execute("SELECT COUNT(*) FROM dependents").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO dependents (dependent_id, emp_id, name, relationship, date_of_birth) VALUES (?, ?, ?, ?, ?)",
            [base_id + 13, 'EMP001', 'Ananya', 'Spouse', (now - timedelta(days=365*30)).date()]
        )
        conn.execute(
            "INSERT INTO dependents (dependent_id, emp_id, name, relationship, date_of_birth) VALUES (?, ?, ?, ?, ?)",
            [base_id + 14, 'EMP002', 'Riya', 'Child', (now - timedelta(days=365*7)).date()]
        )

    if conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] < 2:
        conn.execute(
            # `category` is named explicitly: omitting it took the column default
            # 'General', so a seeded leave notification carried a different category
            # from a real one and no preference could ever have matched it.
            "INSERT INTO notifications (notification_id, emp_id, type, category, message, related_link, is_read, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 15, 'EMP001', 'Leave', notifications.category_for('Leave'),
             'Your leave request is pending', '/leaves', 0, now]
        )
        conn.execute(
            "INSERT INTO notifications (notification_id, emp_id, type, message, related_link, is_read, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [base_id + 16, 'EMP002', 'Profile', 'Please update your profile', '/profile', 0, now - timedelta(hours=2)]
        )

    if conn.execute("SELECT COUNT(*) FROM regularization_requests").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO regularization_requests (request_id, emp_id, request_date, reason, status, approved_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 17, 'EMP002', now.date(), 'Late arrival', 'Pending', None, now, now]
        )
        conn.execute(
            "INSERT INTO regularization_requests (request_id, emp_id, request_date, reason, status, approved_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 18, 'EMP002', (now - timedelta(days=1)).date(), 'Forgot punch', 'Approved', 'EMP001', now - timedelta(days=1), now]
        )

    if conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO assets (asset_id, emp_id, asset_type, asset_tag, brand, model, serial_number, issued_date, return_date, status, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 19, 'EMP002', 'Laptop', 'LAP-001', 'Dell', 'Latitude 5430', 'SN-1001', (now - timedelta(days=30)).date(), None, 'Issued', 'Primary workstation']
        )
        conn.execute(
            "INSERT INTO assets (asset_id, emp_id, asset_type, asset_tag, brand, model, serial_number, issued_date, return_date, status, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 20, 'EMP002', 'Phone', 'PH-001', 'Samsung', 'Galaxy S24', 'SN-1002', (now - timedelta(days=10)).date(), None, 'Issued', 'Company phone']
        )

    if conn.execute("SELECT COUNT(*) FROM job_postings").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO job_postings (job_id, title, department, location, description, requirements, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 21, 'Software Engineer', 'Engineering', 'Pune', 'Build scalable apps', 'Python, Flask', 'Open', now]
        )
        conn.execute(
            "INSERT INTO job_postings (job_id, title, department, location, description, requirements, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 22, 'HR Specialist', 'HR', 'Remote', 'Support employee lifecycle', 'People operations', 'Open', now]
        )

    if conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO candidates (candidate_id, job_id, name, email, phone, resume_text, status, applied_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 23, base_id + 21, 'Kavya Rao', 'kavya@example.com', '9999999001', 'Experienced backend engineer', 'Applied', now]
        )
        conn.execute(
            "INSERT INTO candidates (candidate_id, job_id, name, email, phone, resume_text, status, applied_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 24, base_id + 22, 'Mihir Shah', 'mihir@example.com', '9999999002', 'HR operations background', 'Screened', now - timedelta(days=1)]
        )

    if conn.execute("SELECT COUNT(*) FROM interviews").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO interviews (interview_id, candidate_id, scheduled_at, interviewer, mode, feedback, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [base_id + 25, base_id + 23, now + timedelta(days=2), 'EMP001', 'Virtual', 'Strong technical skills', 'Scheduled']
        )
        conn.execute(
            "INSERT INTO interviews (interview_id, candidate_id, scheduled_at, interviewer, mode, feedback, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [base_id + 26, base_id + 24, now + timedelta(days=3), 'EMP002', 'In-person', 'Good fit', 'Scheduled']
        )

    if conn.execute("SELECT COUNT(*) FROM offer_letters").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO offer_letters (offer_id, candidate_id, offered_salary, basic_pct, hra_pct, allowances_pct, offer_date, status, accepted_at, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 27, base_id + 23, 1800000.00, 50, 30, 20, now.date(), 'Pending', None, 'Standard package']
        )
        conn.execute(
            "INSERT INTO offer_letters (offer_id, candidate_id, offered_salary, basic_pct, hra_pct, allowances_pct, offer_date, status, accepted_at, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 28, base_id + 24, 1200000.00, 50, 30, 20, (now - timedelta(days=1)).date(), 'Accepted', now, 'Offer accepted']
        )

    if conn.execute("SELECT COUNT(*) FROM onboarding_tasks").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO onboarding_tasks (task_id, emp_id, task_name, assigned_to, status, due_date, completed_at, stage) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 29, 'EMP002', 'Laptop setup', 'EMP001', 'Pending', (now + timedelta(days=2)).date(), None, 3]
        )
        conn.execute(
            "INSERT INTO onboarding_tasks (task_id, emp_id, task_name, assigned_to, status, due_date, completed_at, stage) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 30, 'EMP002', 'HR paperwork', 'EMP002', 'Completed', (now - timedelta(days=1)).date(), now - timedelta(hours=3), 2]
        )

    if conn.execute("SELECT COUNT(*) FROM offboarding_tasks").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO offboarding_tasks (task_id, emp_id, task_name, assigned_to, status, due_date, completed_at, stage) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 31, 'EMP002', 'Collect company assets', 'EMP001', 'Pending', (now + timedelta(days=5)).date(), None, 3]
        )
        conn.execute(
            "INSERT INTO offboarding_tasks (task_id, emp_id, task_name, assigned_to, status, due_date, completed_at, stage) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 32, 'EMP002', 'Revoke access', 'EMP001', 'Completed', (now - timedelta(days=1)).date(), now - timedelta(days=1), 5]
        )

    if conn.execute("SELECT COUNT(*) FROM exit_interviews").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO exit_interviews (interview_id, emp_id, reason, feedback, exit_date, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            [base_id + 33, 'EMP002', 'Career change', 'Positive experience', (now - timedelta(days=2)).date(), now - timedelta(days=2)]
        )
        conn.execute(
            "INSERT INTO exit_interviews (interview_id, emp_id, reason, feedback, exit_date, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            [base_id + 34, 'EMP002', 'Relocation', 'Clear onboarding', (now - timedelta(days=5)).date(), now - timedelta(days=5)]
        )

    if conn.execute("SELECT COUNT(*) FROM salary_structures").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO salary_structures (struct_id, emp_id, basic, hra, allowances, deductions, effective_from) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [base_id + 35, 'EMP002', 30000.00, 9000.00, 4000.00, 1500.00, (now - timedelta(days=30)).date()]
        )
        # Sample data must satisfy CC-05 (`no_overlapping_structure`): v2.0
        # scopes salary ranges per employee, so two unbounded ranges on the
        # same employee would overlap. Give the older structure to EMP001.
        conn.execute(
            "INSERT INTO salary_structures (struct_id, emp_id, basic, hra, allowances, deductions, effective_from) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [base_id + 36, 'EMP001', 28000.00, 8400.00, 3200.00, 1200.00, (now - timedelta(days=60)).date()]
        )

    if conn.execute("SELECT COUNT(*) FROM payroll_runs").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO payroll_runs (run_id, month, year, processed_at, status) VALUES (?, ?, ?, ?, ?)",
            [base_id + 37, now.month, now.year, now, 'Draft']
        )
        conn.execute(
            "INSERT INTO payroll_runs (run_id, month, year, processed_at, status) VALUES (?, ?, ?, ?, ?)",
            [base_id + 38, now.month - 1 if now.month > 1 else 12, now.year if now.month > 1 else now.year - 1, now - timedelta(days=30), 'Finalized']
        )

    if conn.execute("SELECT COUNT(*) FROM payroll_items").fetchone()[0] < 2:
        runs = conn.execute("SELECT run_id FROM payroll_runs ORDER BY run_id LIMIT 2").fetchall()
        if len(runs) >= 2:
            conn.execute(
                "INSERT INTO payroll_items (item_id, run_id, emp_id, gross_salary, deductions_total, net_salary, pf, esi, pt, payslip_generated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [base_id + 39, runs[0][0], 'EMP002', 50000.00, 5000.00, 45000.00, 2500.00, 1500.00, 200.00, 0]
            )
            conn.execute(
                "INSERT INTO payroll_items (item_id, run_id, emp_id, gross_salary, deductions_total, net_salary, pf, esi, pt, payslip_generated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [base_id + 40, runs[1][0], 'EMP002', 48000.00, 4800.00, 43200.00, 2400.00, 1400.00, 200.00, 1]
            )

    if conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO goals (goal_id, emp_id, title, description, target_date, weight, rating, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 41, 'EMP002', 'Improve delivery', 'Ship one feature per sprint', (now + timedelta(days=30)).date(), 5, 4, 'Active', now]
        )
        conn.execute(
            "INSERT INTO goals (goal_id, emp_id, title, description, target_date, weight, rating, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 42, 'EMP002', 'Customer support excellence', 'Maintain SLA', (now + timedelta(days=45)).date(), 4, 5, 'Active', now]
        )

    if conn.execute("SELECT COUNT(*) FROM performance_reviews").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO performance_reviews (review_id, emp_id, reviewer_id, review_period, overall_rating, comments, status, created_at, submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 43, 'EMP002', 'EMP001', 'Q2 2026', 4.2, 'Strong execution', 'Submitted', now - timedelta(days=5), now - timedelta(days=3)]
        )
        conn.execute(
            "INSERT INTO performance_reviews (review_id, emp_id, reviewer_id, review_period, overall_rating, comments, status, created_at, submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 44, 'EMP002', 'EMP001', 'Q2 2026', 4.6, 'Excellent ownership', 'Draft', now - timedelta(days=2), None]
        )

    if conn.execute("SELECT COUNT(*) FROM feedback_360").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO feedback_360 (feedback_id, emp_id, reviewer_id, category, rating, comment, submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [base_id + 45, 'EMP002', 'EMP002', 'Collaboration', 5, 'Great teammate', now - timedelta(days=1)]
        )
        conn.execute(
            "INSERT INTO feedback_360 (feedback_id, emp_id, reviewer_id, category, rating, comment, submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [base_id + 46, 'EMP002', 'EMP002', 'Communication', 4, 'Clear updates', now - timedelta(days=2)]
        )

    if conn.execute("SELECT COUNT(*) FROM expense_claims").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO expense_claims (claim_id, emp_id, cat_id, amount, description, receipt_path, status, approved_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 47, 'EMP002', 1, 1250.00, 'Mumbai travel', 'travel.pdf', 'Pending', None, now]
        )
        conn.execute(
            "INSERT INTO expense_claims (claim_id, emp_id, cat_id, amount, description, receipt_path, status, approved_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 48, 'EMP002', 2, 850.00, 'Client lunch', 'food.pdf', 'Approved', 'EMP001', now - timedelta(days=2)]
        )

    if conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO tickets (ticket_id, emp_id, subject, description, category, priority, status, assigned_to, created_at, updated_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 49, 'EMP002', 'VPN access issue', 'Unable to connect to VPN', 'IT', 'High', 'Open', 'EMP001', now, now, None]
        )
        conn.execute(
            "INSERT INTO tickets (ticket_id, emp_id, subject, description, category, priority, status, assigned_to, created_at, updated_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 50, 'EMP002', 'Payroll question', 'Need pay slip clarification', 'HR', 'Medium', 'Resolved', 'EMP002', now - timedelta(days=1), now - timedelta(hours=2), now - timedelta(hours=1)]
        )

    if conn.execute("SELECT COUNT(*) FROM ticket_comments").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO ticket_comments (comment_id, ticket_id, emp_id, comment, created_at) VALUES (?, ?, ?, ?, ?)",
            [base_id + 51, base_id + 49, 'EMP001', 'We are looking into it', now]
        )
        conn.execute(
            "INSERT INTO ticket_comments (comment_id, ticket_id, emp_id, comment, created_at) VALUES (?, ?, ?, ?, ?)",
            [base_id + 52, base_id + 50, 'EMP002', 'Shared the payslip details', now - timedelta(hours=1)]
        )

    if conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO documents (doc_id, emp_id, name, category, file_path, file_size, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [base_id + 53, 'EMP001', 'Offer Letter', 'Offer Letter', '/uploads/offer.pdf', 204800, now]
        )
        conn.execute(
            "INSERT INTO documents (doc_id, emp_id, name, category, file_path, file_size, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [base_id + 54, 'EMP002', 'ID Proof', 'ID Proof', '/uploads/id.pdf', 153600, now - timedelta(days=1)]
        )

    # ── Assign default shift 20:00-05:00 to all employees ────────
    if _shift_model():
        for (eid,) in conn.execute("SELECT emp_id FROM users").fetchall():
            s, _e = get_shift(eid, conn)
            if not s:
                set_shift(eid, '20:00', '05:00', conn=conn)
    else:
        conn.execute("UPDATE users SET shift_start = '20:00', shift_end = '05:00' WHERE shift_start IS NULL")

    # ── Fix seed session/break dates to use shift-based dates ─────
    _fix_seed_shift_dates(conn, now)
    _advance_public_identity_sequences(conn)

    conn.close()
    logger.info("Database initialized")


init_db()

if os.getenv('FLASK_ENV') == 'production':
    _conn = get_db()
    _seed_hashes = [r[0] for r in _conn.execute(
        "SELECT password FROM users WHERE emp_id IN ('EMP001', 'EMP002')"
    ).fetchall()]
    _conn.close()
    # Verify rather than string-compare: Argon2id re-hashes on every boot.
    if any(check_password('pass123', h) for h in _seed_hashes):
        logger.warning("⚠️  SEED USERS WITH DEFAULT PASSWORDS DETECTED — Change all passwords before use!")


# ══════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════

def audit_log(emp_id, action, details=None, *, actor=None, entity=None, entity_id=None, before=None, after=None):
    """Write an audit trail row (v2.0 shape: actor/entity/entity_id/before/after/request_id).

    CC-13: every request gets a trace ``request_id`` (honours an inbound
    ``X-Request-ID`` header so gateways can correlate; otherwise a fresh
    ``req-`` token is minted per request). ``actor`` defaults to the session
    user's name. ``before``/``after`` accept dicts and are stored as JSON.
    """
    conn = None
    try:
        conn = get_db()
        log_id = (
            _next_generated_id(conn, 'audit_log', 'log_id')
            if _is_public_target_schema()
            else int(datetime.now().timestamp() * 1_000_000) % 2_147_483_647
        )
        # Background jobs (scheduler threads) have no request context, so the
        # actor and the request metadata must degrade instead of raising: an
        # audit row that is silently dropped is worse than one without a
        # request id, and `except` below used to swallow exactly that.
        in_request = has_request_context()
        if actor is None:
            actor = (session.get('name') or session.get('emp_id') or emp_id) if in_request else 'SYSTEM'
        request_id = getattr(g, '_hrms_request_id', None) if in_request else None
        if request_id is None:
            request_id = (
                request.headers.get('X-Request-ID') or f"req-{secrets.token_hex(8)}"
            ) if in_request else f"job-{secrets.token_hex(8)}"
            g._hrms_request_id = request_id

        def _json(v):
            if v is None:
                return None
            if isinstance(v, dict):
                return json.dumps(v, default=str)
            return str(v) if not isinstance(v, str) else v

        conn.execute(
            'INSERT INTO audit_log (log_id, emp_id, actor, action, entity, entity_id, details, '
            '"before", "after", ip_address, request_id, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            [log_id, emp_id, actor, action, entity, entity_id, details,
             _json(before), _json(after),
             request.remote_addr if in_request else None, request_id, datetime.now()]
        )
    except Exception as e:
        logger.warning("audit_log failed: %s", e)
    finally:
        if conn:
            conn.close()


def parse_date(date_string, default=None):
    if not date_string:
        return default
    try:
        return datetime.strptime(date_string, '%Y-%m-%d').date()
    except ValueError:
        return default


def parse_datetime(value, default=None):
    if not value:
        return default
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).replace(tzinfo=None)
    except (TypeError, ValueError):
        return default


def gen_id():
    return int(datetime.now().timestamp() * 1_000_000) % 2_147_483_647


def _get_shift_start_dt(emp_id, conn=None, target_date=None):
    now = datetime.now()
    shift_start_str, _ = get_shift(emp_id, conn)
    if shift_start_str and shift_start_str != '24x7':
        try:
            parts = shift_start_str.split(':')
            h, m = int(parts[0]), int(parts[1])
            if target_date:
                dt = datetime(target_date.year, target_date.month, target_date.day, h, m, 0, 0)
            else:
                dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if dt > now:
                    dt -= timedelta(days=1)
            return dt
        except Exception:
            pass
    if target_date:
        return datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0, 0)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _get_shift_end_dt(emp_id, shift_start_dt, conn=None):
    shift_start_str, shift_end_str = get_shift(emp_id, conn)
    if shift_start_str and shift_start_str != '24x7' and shift_end_str:
        try:
            sp = shift_start_str.split(':')
            ep = shift_end_str.split(':')
            sh, sm = int(sp[0]), int(sp[1])
            eh, em = int(ep[0]), int(ep[1])
            end_dt = shift_start_dt.replace(hour=eh, minute=em, second=0, microsecond=0)
            if eh < sh or (eh == sh and em < sm):
                end_dt += timedelta(days=1)
            return end_dt
        except Exception:
            pass
    return shift_start_dt + timedelta(days=1)


# ══════════════════════════════════════════════════════════════════════
#  ATTENDANCE FINALISATION (Phase 4 / FR-JOB-01)
# ══════════════════════════════════════════════════════════════════════

ATTENDANCE_STATUS_PRESENT = 'Present'
ATTENDANCE_STATUS_HALF_DAY = 'Half-day'
ATTENDANCE_STATUS_ABSENT = 'Absent'
ATTENDANCE_STATUS_ON_LEAVE = 'On Leave'
ATTENDANCE_STATUS_HOLIDAY = 'Holiday'
ATTENDANCE_STATUS_WEEKLY_OFF = 'Weekly-off'

_WEEKDAY_NAMES = (
    'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'
)
_ATTENDANCE_IDENTITY_CACHE: dict = {}


def _attendance_id_is_identity() -> bool:
    """True when the target schema generates ``attendance_id`` itself.

    PostgreSQL ``public`` uses the CC-01 identity key. The compatibility
    ``legacy`` PostgreSQL schema uses the v1.0 integer key and needs a
    caller-supplied ID.
    """
    import db_backend
    schema = db_backend.app_schema()
    key = schema
    if key not in _ATTENDANCE_IDENTITY_CACHE:
        conn = db_backend.connect()
        try:
            row = conn.execute(
                "SELECT is_identity FROM information_schema.columns "
                "WHERE table_schema = ? AND table_name = 'attendance_days' "
                "AND column_name = 'attendance_id'",
                [schema],
            ).fetchone()
            _ATTENDANCE_IDENTITY_CACHE[key] = bool(row and str(row[0]).upper() == 'YES')
        except Exception:
            _ATTENDANCE_IDENTITY_CACHE[key] = False
        finally:
            conn.close()
    return _ATTENDANCE_IDENTITY_CACHE[key]


def _attendance_date(value):
    """Coerce a date/datetime/string to ``datetime.date``."""
    if isinstance(value, datetime):
        return value.date()
    if hasattr(value, 'year') and hasattr(value, 'month') and hasattr(value, 'day'):
        return value
    return datetime.strptime(str(value), '%Y-%m-%d').date()


def _attendance_table_exists(conn, table_name):
    try:
        conn.execute(f"SELECT 1 FROM {table_name} LIMIT 1").fetchone()
        return True
    except Exception:
        return False


def _attendance_ratio(env_name, default):
    try:
        value = float(os.getenv(env_name, default))
    except (TypeError, ValueError):
        value = default
    return min(max(value, 0.0), 2.0)


def _is_weekly_off(target_date, pattern):
    """Check a day against a per-employee ``Sat,Sun``-style pattern.

    Full weekday names, three-letter abbreviations, numeric weekdays, and a
    simple ``Mon-Fri`` range are accepted. A blank pattern means no configured
    weekly off, rather than a hard-coded Monday-Friday working week.
    """
    if not pattern:
        return False
    normalised = str(pattern).lower().replace('|', ',').replace(';', ',').replace('/', ',')
    configured = set()
    for token in (part.strip() for part in normalised.split(',')):
        if not token:
            continue
        bounds = [part.strip() for part in token.split('-') if part.strip()]
        indices = []
        for bound in bounds:
            for index, name in enumerate(_WEEKDAY_NAMES):
                if bound in (name, name[:3], str(index + 1)):
                    indices.append(index)
                    break
        if len(indices) == 1:
            configured.add(indices[0])
        elif len(indices) == 2 and indices[0] <= indices[1]:
            configured.update(range(indices[0], indices[1] + 1))
    return target_date.weekday() in configured


def _attendance_shift_window(emp_id, target_date, conn):
    """Resolve the employee's scheduled window and credited shift length."""
    start_value, end_value = get_shift(emp_id, conn, on_date=target_date)
    if start_value == '24x7' and end_value == '24x7':
        start_dt = datetime.combine(target_date, datetime.min.time())
        end_dt = start_dt + timedelta(days=1)
        return start_dt, end_dt

    start_t, start_24x7 = _parse_shift_time(start_value)
    end_t, end_24x7 = _parse_shift_time(end_value)
    if start_t and end_t and not start_24x7 and not end_24x7:
        start_dt = datetime.combine(target_date, start_t)
        end_dt = datetime.combine(target_date, end_t)
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
        return start_dt, end_dt

    # Employees without a configured shift still receive a deterministic
    # attendance row; use an 8-hour default rather than treating 24x7 as a
    # full-day requirement by accident.
    start_dt = datetime.combine(target_date, datetime.min.time())
    return start_dt, start_dt + timedelta(hours=float(os.getenv('ATTENDANCE_DEFAULT_SHIFT_HOURS', '8')))


def _attendance_worked_hours(emp_id, target_date, shift_start_dt, shift_end_dt, conn, as_of=None):
    """Calculate credited hours from first login through last logout.

    Multiple sessions are deliberately not summed: the SRS uses the elapsed
    shift window, consistent with FR-ATT-09. Open/orphaned sessions are capped
    at the scheduled length plus 25%; all values are capped at that same
    payroll-safe ceiling before being written to ``attendance_days``.
    """
    rows = conn.execute(
        "SELECT login_time, logout_time FROM user_sessions "
        "WHERE emp_id = ? AND session_date = ? ORDER BY login_time",
        [emp_id, target_date],
    ).fetchall()
    if not rows:
        return 0.0

    login_times = [row[0] for row in rows if row[0]]
    if not login_times:
        return 0.0
    first_login = min(login_times)
    open_shift = any(row[1] is None for row in rows)
    if open_shift:
        # The clock-time bound and the duration cap both come from the shared rule
        # (FR-ATT-09), so the figure an employee sees on the shift summary and the one
        # written to `attendance_days` are computed the same way. They used to be two
        # implementations and only this one had the cap — so a forgotten logout showed
        # 30 hours on the dashboard and the capped figure on the payslip.
        as_of = as_of or datetime.now()
        last_event = min(as_of, shift_hours.clock_cap(shift_start_dt, shift_end_dt)
                         or as_of)
    else:
        logout_times = [row[1] for row in rows if row[1]]
        last_event = max(logout_times) if logout_times else first_login
    if last_event < first_login:
        return 0.0

    raw_hours = max(0.0, (last_event - first_login).total_seconds() / 3600)
    # The **payroll** ceiling, deliberately kept on top of the shared rule and not
    # folded into it: it bounds what a long-but-legitimate day is worth, which is a
    # payroll policy question rather than a data-quality one. FR-ATT-09's cap is about
    # an unreliable figure; this one is about entitlement.
    credit_cap = shift_hours.scheduled_hours(shift_start_dt, shift_end_dt) * 1.25
    return round(min(raw_hours, credit_cap), 2)


def _has_approved_attendance_leave(emp_id, target_date, conn):
    return bool(conn.execute(
        "SELECT 1 FROM leave_requests WHERE emp_id = ? "
        "AND status = 'Approved' AND start_date <= ? AND end_date >= ? LIMIT 1",
        [emp_id, target_date, target_date],
    ).fetchone())


def _attendance_employee_location(emp_id, target_date, conn):
    """Resolve an employee's effective leave-policy location when available."""
    if not _shift_model():
        return None
    try:
        row = conn.execute(
            "SELECT location FROM leave_policy_assignments "
            "WHERE emp_id = ? AND effective_from <= ? "
            "AND (effective_to IS NULL OR effective_to >= ?) "
            "ORDER BY effective_from DESC LIMIT 1",
            [emp_id, target_date, target_date],
        ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _is_attendance_holiday(emp_id, target_date, conn):
    """National holidays, plus location-matched optional holidays with opt-in."""
    if _shift_model():
        try:
            holidays = conn.execute(
                "SELECT holiday_id, type, location FROM holidays WHERE holiday_date = ?",
                [target_date],
            ).fetchall()
        except Exception:
            # A lightweight v2.0 stand-in may not carry location yet.
            holidays = conn.execute(
                "SELECT holiday_id, type FROM holidays WHERE holiday_date = ?", [target_date]
            ).fetchall()
    else:
        # The v1.0 compatibility table predates the location column.
        holidays = conn.execute(
            "SELECT holiday_id, type FROM holidays WHERE holiday_date = ?", [target_date]
        ).fetchall()
    if not holidays:
        return False

    has_national = any(str(row[1] or '').lower() == 'national' for row in holidays)
    if has_national:
        return True
    optional_ids = {row[0] for row in holidays if str(row[1] or '').lower() == 'optional'}
    if not optional_ids or not _attendance_table_exists(conn, 'holiday_optins'):
        return False

    placeholders = ','.join('?' for _ in optional_ids)
    approved = {
        row[0] for row in conn.execute(
            f"SELECT holiday_id FROM holiday_optins WHERE emp_id = ? "
            f"AND status = 'Approved' AND holiday_id IN ({placeholders})",
            [emp_id, *sorted(optional_ids)],
        ).fetchall()
    }
    if not approved:
        return False
    employee_location = _attendance_employee_location(emp_id, target_date, conn)
    for row in holidays:
        holiday_id, holiday_type = row[0], str(row[1] or '').lower()
        if holiday_id not in approved or holiday_type != 'optional':
            continue
        # A NULL location is an organisation-wide holiday. When location data
        # is unavailable (the v1.0 shape), retain the safe legacy behaviour of
        # applying an approved optional holiday rather than silently dropping it.
        holiday_location = row[2] if len(row) > 2 else None
        if not holiday_location or not employee_location or str(holiday_location).lower() == str(employee_location).lower():
            return True
    return False


def _classify_attendance(emp_id, target_date, conn, as_of=None):
    """Return ``(status, credited_hours)`` for one employee/date (FR-JOB-01)."""
    shift_start_dt, shift_end_dt = _attendance_shift_window(emp_id, target_date, conn)
    if _is_attendance_holiday(emp_id, target_date, conn):
        return ATTENDANCE_STATUS_HOLIDAY, 0.0
    if _has_approved_attendance_leave(emp_id, target_date, conn):
        return ATTENDANCE_STATUS_ON_LEAVE, 0.0

    pattern = get_weekly_off_pattern(emp_id, conn, on_date=target_date)
    if _is_weekly_off(target_date, pattern):
        return ATTENDANCE_STATUS_WEEKLY_OFF, 0.0

    worked_hours = _attendance_worked_hours(
        emp_id, target_date, shift_start_dt, shift_end_dt, conn, as_of=as_of
    )
    scheduled_hours = max(0.0, (shift_end_dt - shift_start_dt).total_seconds() / 3600)
    full_threshold = scheduled_hours * _attendance_ratio('ATTENDANCE_FULL_DAY_RATIO', 1.0)
    half_threshold = scheduled_hours * _attendance_ratio('ATTENDANCE_HALF_DAY_RATIO', 0.5)
    if worked_hours >= full_threshold and full_threshold > 0:
        return ATTENDANCE_STATUS_PRESENT, worked_hours
    if worked_hours >= half_threshold and half_threshold > 0:
        return ATTENDANCE_STATUS_HALF_DAY, worked_hours
    return ATTENDANCE_STATUS_ABSENT, worked_hours


def finalize_attendance_for_date(target_date, employee_ids=None, as_of=None):
    """Replace one shift date's attendance rows in a single transaction.

    With no ``employee_ids`` every currently active employee is finalised,
    which is the normal nightly path. A subset is used when employees have
    different shift dates at the scheduler's run time (or for a targeted
    recompute after a source record changes).
    """
    target_date = _attendance_date(target_date)
    as_of = as_of or datetime.now()
    if getattr(as_of, 'tzinfo', None) is not None:
        as_of = as_of.replace(tzinfo=None)
    counts = {}
    processed = 0

    with outbox.transaction() as conn:
        all_employees = employee_ids is None
        if all_employees:
            employee_ids = [row[0] for row in conn.execute(
                "SELECT emp_id FROM users WHERE status = 'Active' ORDER BY emp_id"
            ).fetchall()]
        else:
            if isinstance(employee_ids, str):
                employee_ids = [employee_ids]
            employee_ids = list(dict.fromkeys(employee_ids))
            employee_ids = [
                emp_id for emp_id in employee_ids
                if conn.execute(
                    "SELECT 1 FROM users WHERE emp_id = ? AND status = 'Active'", [emp_id]
                ).fetchone()
            ]

        classified = [
            (emp_id, *_classify_attendance(emp_id, target_date, conn, as_of=as_of))
            for emp_id in employee_ids
        ]

        if all_employees:
            # Normal all-employee run: one predicate keeps the replacement
            # statement efficient while still removing stale inactive rows,
            # including the edge case where no users remain active.
            conn.execute("DELETE FROM attendance_days WHERE attendance_date = ?", [target_date])
        else:
            for emp_id in employee_ids:
                conn.execute(
                    "DELETE FROM attendance_days WHERE emp_id = ? AND attendance_date = ?",
                    [emp_id, target_date],
                )

        now = datetime.now()
        identity_model = _attendance_id_is_identity()
        next_attendance_id = None
        if not identity_model:
            next_attendance_id = int(conn.execute(
                "SELECT COALESCE(MAX(attendance_id), 0) + 1 FROM attendance_days"
            ).fetchone()[0])
        for emp_id, status, shift_hours in classified:
            if identity_model:
                conn.execute(
                    "INSERT INTO attendance_days "
                    "(emp_id, attendance_date, status, shift_hours, source, version, updated_at) "
                    "VALUES (?, ?, ?, ?, 'job', 1, ?)",
                    [emp_id, target_date, status, shift_hours, now],
                )
            else:
                conn.execute(
                    "INSERT INTO attendance_days "
                    "(attendance_id, emp_id, attendance_date, status, shift_hours, source, version, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'job', 1, ?)",
                    [next_attendance_id, emp_id, target_date, status, shift_hours, now],
                )
                next_attendance_id += 1
            counts[status] = counts.get(status, 0) + 1
        processed = len(classified)

    return {
        'date': target_date.isoformat(),
        'processed': processed,
        'counts': counts,
    }


def run_attendance_finalization():
    """Nightly scheduler entry: finalise each employee's current shift date."""
    conn = get_db()
    now = datetime.now()
    by_date = {}
    try:
        active_ids = [row[0] for row in conn.execute(
            "SELECT emp_id FROM users WHERE status = 'Active' ORDER BY emp_id"
        ).fetchall()]
        for emp_id in active_ids:
            shift_date = _get_shift_date_for_dt(emp_id, now, conn)
            by_date.setdefault(shift_date, []).append(emp_id)
    finally:
        conn.close()

    results = [
        finalize_attendance_for_date(shift_date, employee_ids=emp_ids, as_of=now)
        for shift_date, emp_ids in sorted(by_date.items())
    ]
    return {
        'processed': sum(result['processed'] for result in results),
        'dates': results,
    }


def get_user(emp_id):
    conn = get_db()
    u = conn.execute(
        "SELECT emp_id, name, email, role, status, department, allow_login, allow_breaks, designation, manager_emp_id, phone, date_of_birth, date_of_joining, address, emergency_contact_name, emergency_contact_phone FROM users WHERE emp_id = ?",
        [emp_id]
    ).fetchone()
    conn.close()
    return u


# ══════════════════════════════════════════════════════════════════════
#  DECORATORS
# ══════════════════════════════════════════════════════════════════════

def _session_user_active(conn=None):
    emp_id = session.get('emp_id')
    if not emp_id:
        return False
    own_conn = conn is None
    conn = conn or get_db()
    try:
        row = conn.execute(
            "SELECT status, allow_login FROM users WHERE emp_id = ?", [emp_id]
        ).fetchone()
        return bool(row and row[0] in ('Active', 'Onboarding') and row[1])
    finally:
        if own_conn:
            conn.close()


# ── Role/permission gates (the role check is the outer gate; policy.can() can
#    only narrow it further with an explicit per-user deny) ─────────────────
# Module per gated view. The admin-only operations endpoints that have no SRS
# module of their own (dashboard stats, outbox monitor, break monitoring,
# notification mail, the directory itself) fall under `users`, the umbrella for
# "admin operations". `tests/test_app.py` fails if a gated view is missing here.
_ROUTE_MODULES = {
    # Directory
    'admin_users': 'users', 'get_users': 'users', 'add_user': 'users',
    'get_user_route': 'users', 'update_user': 'users', 'block_user': 'users',
    'unblock_user': 'users', 'archive_user': 'users', 'restore_user': 'users',
    'delete_user': 'users', 'get_user_permissions': 'users',
    'update_user_permissions': 'users', 'get_user_pii': 'pii_reveal',
    'get_leave_policy': 'leaves', 'update_leave_policy': 'leaves',
    'run_leave_accrual_route': 'leaves',
    'propose_anonymisation': 'policy_admin', 'anonymisation_status': 'policy_admin',
    'anonymisation_list': 'policy_admin', 'confirm_anonymisation': 'policy_admin',
    'cancel_anonymisation': 'policy_admin',
    'import_users_csv': 'import_users', 'import_users_page': 'import_users',
    'import_job_status': 'import_users', 'import_job_list': 'import_users',
    'cancel_import_job': 'import_users', 'run_import_job': 'import_users',
    # Attendance / time off
    'add_holiday': 'holidays', 'delete_holiday': 'holidays', 'admin_holidays': 'holidays',
    'update_holiday': 'holidays', 'copy_holiday_year': 'holidays',
    'import_holidays': 'holidays',
    'holiday_optin_queue': 'holidays', 'approve_holiday_optin': 'holidays',
    'reject_holiday_optin': 'holidays',
    'approve_regularization': 'regularization', 'reject_regularization': 'regularization',
    'admin_leaves_page': 'leaves', 'export_leaves': 'leaves',
    'approve_leave': 'leaves', 'reject_leave': 'leaves', 'cancel_leave': 'leaves',
    'live_monitoring': 'breaks', 'get_break_summary': 'breaks',
    'get_disposed_breaks': 'breaks', 'admin_breaks': 'breaks',
    'admin_dispose_break': 'breaks', 'approve_break': 'breaks', 'reject_break': 'breaks',
    # ATS
    'admin_jobs': 'jobs', 'jobs_api': 'jobs', 'close_job': 'jobs',
    'job_detail': 'jobs', 'recruitment_pipeline': 'jobs',
    'admin_candidates': 'candidates', 'candidates_api': 'candidates',
    'candidate_detail': 'candidates', 'update_candidate_status': 'candidates',
    'interviews_api': 'candidates', 'interview_feedback': 'candidates',
    'offers_api': 'offers', 'accept_offer': 'offers', 'reject_offer': 'offers',
    # Lifecycle
    'review_onboarding_document': 'onboarding',
    'exit_interviews_api': 'offboarding',
    'revoke_offboarding_workflow_access': 'offboarding',
    'admin_revoke_offboarding_access': 'offboarding',
    # Payroll
    'admin_payroll': 'payroll', 'payroll_runs_api': 'payroll', 'submit_payroll': 'payroll',
    'finalize_payroll': 'payroll', 'payroll_items': 'payroll',
    'bank_file_export': 'payroll', 'tds_report': 'payroll',
    'approve_payroll': 'payroll_approve',
    'admin_salary': 'salary_structures', 'salary_api': 'salary_structures',
    # Workplace
    'admin_goals': 'goals', 'rate_goal': 'goals',
    'admin_reviews': 'performance', 'reviews_api': 'performance',
    'admin_expenses': 'expenses', 'update_expense_status': 'expenses',
    'admin_tickets': 'tickets', 'assign_ticket': 'tickets',
    'admin_assets': 'assets', 'assets_api': 'assets', 'return_asset': 'assets',
    'admin_documents': 'documents',
    # Assurance
    'admin_analytics': 'analytics', 'analytics_headcount': 'analytics',
    'analytics_leave_trends': 'analytics', 'analytics_attrition': 'analytics',
    'analytics_expense_summary': 'analytics', 'analytics_performance': 'analytics',
    'audit_page': 'audit', 'get_audit_log': 'audit',
    'admin_reports': 'reports', 'get_reports': 'reports', 'export_report': 'reports',
    'export_report_pdf': 'reports', 'get_department_summary': 'reports',
    'admin_outbox_list': 'audit', 'admin_outbox_dispatch': 'audit',
    'get_dashboard_stats': 'reports',
    'send_notification_email': 'users',
}

# Module used when a gated view has no explicit entry above. The directory is
# the umbrella for admin operations, so denying it removes the whole surface.
_DEFAULT_GATED_MODULE = 'users'


def _gate_passes(actor, gate, departments=()):
    """Role/department half of a gate (unchanged semantics per gate)."""
    role = str(actor.get('role') or '')
    department = actor.get('department')
    if gate == 'admin':
        return role in policy.ADMIN_ROLES
    if gate == 'hr_or_admin':
        return role in policy.ADMIN_ROLES or role == 'HR' or department == 'HR'
    if gate == 'finance_or_admin':
        return role in policy.ADMIN_ROLES or role == 'Finance'
    if gate == 'department':
        return role in policy.ADMIN_ROLES or department in departments
    return True


def _gate_module(f):
    return _ROUTE_MODULES.get(f.__name__, _DEFAULT_GATED_MODULE)


def expense_actor_required(f):
    """Gate for the expense state machine (FR-EXP-03).

    The SRS transition table has three kinds of actor — the claim owner's
    manager (approve/reject), HR (approve/reject), and Finance (pay) — and none of
    the existing role gates is that set. `admin_required` excluded the manager and
    Finance entirely, which is why `Approved -> Paid` was unreachable by the role
    the SRS names for it.

    The gate only decides *whether* the caller may attempt a transition.
    `expenses.check_transition` still decides *which*, and re-reads the actor from
    the database, so a stale session copy cannot widen this.
    """
    def denial():
        if _wants_json():
            return jsonify({'error': 'Forbidden'}), 403
        return redirect(url_for('dashboard'))

    @wraps(f)
    def decorated(*args, **kwargs):
        if 'emp_id' not in session or not _session_user_active():
            session.clear()
            if _wants_json():
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('login'))
        conn = get_db()
        try:
            actor = policy.current_actor(conn)
            role = str(actor.get('role') or '')
            if role in policy.ADMIN_ROLES or role in ('HR', 'Finance'):
                pass
            elif actor.get('department') == 'HR':
                pass
            elif not _manages_any_employee(conn, actor.get('emp_id')):
                return denial()
            if not policy.can(actor, 'expenses', conn=conn) and role not in policy.ADMIN_ROLES:
                # The matrix says an explicit override may deny the module, and
                # an Admin is not narrowed by it either (same invariant as
                # `_gated`: the matrix can revoke, never grant).
                return denial()
        finally:
            conn.close()
        return f(*args, **kwargs)
    return _tag_gate(decorated, 'expense_actor', _gate_module(f))


def _manages_any_employee(conn, manager_emp_id):
    """Does this employee manage at least one other employee?

    Checked against the database rather than the session, so a re-org takes
    effect immediately.
    """
    if not manager_emp_id:
        return False
    try:
        return bool(conn.execute(
            'SELECT 1 FROM users WHERE manager_emp_id = ? LIMIT 1', [manager_emp_id]
        ).fetchone())
    except Exception:
        return False


def reporting_line_required(f=None, *, module=None):
    """Gate for an action the SRS assigns to a *manager* rather than a role.

    FR-PERF-01 says a goal is "rated 1-5 by manager (not self)", but the rating route
    was `@admin_required`, so a Team Leader who actually manages people could not rate
    their reports' goals — the requirement was unreachable for the role it names, the
    same way `Approved -> Paid` was unreachable for Finance before the expense gate.
    FR-LEA-04 (leave approve/reject) and FR-REG-03 (regularization approve/reject) had
    the identical gate, and this is also FR-ATT-06's break-approval gate: four
    handlers the SRS gives to "the employee's manager or HR/Admin", three of them
    written as admin-only while the matrix recorded them as done.

    The gate is deliberately coarse: it admits an administrator, HR, or anyone who
    manages at least one employee — or stands in for a manager today *as an active
    delegate* (FR-LEA-08a) — so a plain-employee delegate who manages nobody can
    reach the route their delegation is for. *Which* employee, whether the caller is
    the applicant, and whether an active delegate stands in for the manager are
    decided per request by `_approval_denial`, which re-reads the actor from the
    database.

    The matrix is consulted on **the view's own module** (`module=` overrides it). It
    used to hard-code `performance`, so the module the navbar reads off
    `__hrms_module__` and the module the gate actually checked disagreed: an explicit
    deny on `breaks` left break approval reachable, and a deny on `performance`
    removed a route that has nothing to do with performance reviews. Deriving it from
    the route is what keeps the two from drifting apart again.
    """
    def decorator(view):
        gate_module = module or _gate_module(view)

        def denial():
            if _wants_json():
                return jsonify({'error': 'Forbidden'}), 403
            return redirect(url_for('dashboard'))

        @wraps(view)
        def decorated(*args, **kwargs):
            if 'emp_id' not in session or not _session_user_active():
                session.clear()
                if _wants_json():
                    return jsonify({'error': 'Authentication required'}), 401
                return redirect(url_for('login'))
            conn = get_db()
            try:
                actor = policy.current_actor(conn)
                role = str(actor.get('role') or '')
                on_the_line = (
                    role in policy.ADMIN_ROLES
                    or role == 'HR'
                    or actor.get('department') == 'HR'
                    or _manages_any_employee(conn, actor.get('emp_id'))
                    or delegations.delegate_is_standing_in(conn, actor.get('emp_id'))
                )
                if not on_the_line:
                    return denial()
                # The matrix may revoke, never grant — the same invariant as `_gated`.
                if not policy.can(actor, gate_module, conn=conn) and role not in policy.ADMIN_ROLES:
                    return denial()
            finally:
                conn.close()
            return view(*args, **kwargs)
        return _tag_gate(decorated, 'reporting_line', gate_module)

    if f is None:
        return decorator
    return decorator(f)


def _approval_denial(conn, actor, target_emp_id):
    """A ready refusal when the caller may not decide *this* employee's request, else ``None``.

    The coarse gate above only establishes that the caller is a manager, HR or an
    administrator *somewhere*. This says they may act on this employee, and it is one
    function because it is one question with one answer (FR-LEA-04's "actor is manager
    (or delegate) or HR/Admin, **not the applicant**" and FR-ATT-06's "actor is the
    employee's manager (including an active delegate, FR-LEA-08a) or has role
    HR/Admin"). Leave and regularization used to answer it by not asking; a fourth
    approval path later would have made it four slightly different answers.

    Self-decision is **409**, not 403: this codebase already answers a self-action
    (self-archive, self-anonymise, self-delegation) with 409, and 403 here would read
    as "you lack the module" when the real fact is that the applicant and the approver
    are the same person, which no permission change can fix.
    """
    if actor == target_emp_id:
        return jsonify({
            'error': 'You cannot decide your own request',
            'target_emp_id': target_emp_id,
        }), 409
    if not delegations.can_approve_for(conn, actor, target_emp_id):
        return jsonify({
            'error': 'Only this employee\u2019s manager, an active delegate, or '
                     'HR/Admin may decide this request',
            'target_emp_id': target_emp_id,
        }), 403
    return None


def _pending_my_approval_clause(conn, actor, prefix=''):
    """The ``pending_my_approval`` list filter (FR-LEA-01, FR-REG-01, FR-LEA-08a).

    Returns ``(condition, params)``, or ``(None, None)`` when the caller can decide
    nothing at all. An employee with no reports and no live delegation has an empty
    queue, and answering that with every pending row would be the exact opposite of
    what the filter's name promises.

    ``None`` from `approvable_employees` means HR/Admin: every request but their own,
    because `can_approve_for` refuses the applicant whatever their role. The role half
    of the rule stays in `delegations` rather than being re-derived here, so the list
    and the approve route cannot disagree about who is an approver.
    """
    targets = delegations.approvable_employees(conn, actor)
    if targets is None:
        return f"{prefix}status = 'Pending' AND {prefix}emp_id != ?", [actor]
    if not targets:
        return None, None
    placeholders = ','.join('?' for _ in targets)
    return (
        f"{prefix}status = 'Pending' AND {prefix}emp_id IN ({placeholders})",
        list(targets),
    )


def _tag_gate(decorated, gate, module):
    """Record the gate on the view so the navbar can reuse it verbatim."""
    decorated.__hrms_gate__ = gate
    decorated.__hrms_module__ = module
    return decorated


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'emp_id' not in session or not _session_user_active():
            session.clear()
            if _wants_json():
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return _tag_gate(decorated, 'login', None)


def _wants_json():
    """Should a gate denial be JSON, or an HTML redirect?

    ``request.is_json`` alone is wrong for an upload. A ``multipart/form-data``
    POST carries no JSON body, so ``request.is_json`` is False and an
    admin-gated upload route answered a non-admin with a **302 to the dashboard
    HTML** instead of a 403 - the client followed the redirect and received a page
    it cannot parse. ``POST /api/users/import`` and ``POST /api/holidays/import``
    are both reachable that way.

    A path under ``/api/`` is an API call whatever it carries, and no page route
    lives under that prefix (checked: every ``/api/`` rule returns JSON or a
    file), so widening the test to the path is safe and makes the answer depend on
    *what was asked for* rather than on *how it was encoded*.

    The ``Accept`` header is honoured too, and that is the SRS's own wording for
    FR-AUTH-07: "unauthenticated requests get 302 (page) or 401 (API, **Accept:
    application/json**)". It was missing, so ``GET /dashboard`` with
    ``Accept: application/json`` answered **302 to the login page** — an HTML body
    for a client that asked for JSON. That is the identical failure the multipart
    case above was fixed for: the client follows the redirect and receives a page it
    cannot parse, with a success-looking status. The matrix row claimed the correct
    behaviour while the handler did the opposite for the exact request the
    requirement names.

    Order matters: an explicit request for anything other than HTML wins, so a
    browser sending a combined ``Accept`` header that happens to mention JSON among
    other types is still treated as a page request.
    """
    accept = (request.headers.get('Accept') or '').lower()
    if accept and 'text/html' not in accept:
        if 'application/json' in accept or accept.strip() == '':
            return True
    return bool(request.is_json) or request.path.startswith('/api/')


def _gated(f, gate, denial, module, departments=()):
    """Shared body of every role gate.

    The role/department check stays the outer gate, exactly as before;
    ``policy.can(actor, module)`` is an additional *narrowing* check, so a user
    with no override rows behaves identically to this commit's parent while an
    explicit deny removes access. The gate and module are tagged onto the view
    so ``navigation_for()`` can reuse the identical predicate for the navbar.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'emp_id' not in session or not _session_user_active():
            session.clear()
            if _wants_json():
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('login'))
        conn = get_db()
        try:
            actor = policy.current_actor(conn)
            if not _gate_passes(actor, gate, departments) or not policy.can(actor, module, conn=conn):
                return denial()
        finally:
            conn.close()
        return f(*args, **kwargs)
    return _tag_gate(decorated, gate, module)


def _forbidden_json(message):
    return jsonify({'error': message}), 403


def admin_required(f):
    def denial():
        if _wants_json():
            return jsonify({'error': 'Forbidden'}), 403
        return redirect(url_for('dashboard'))
    return _gated(f, 'admin', denial, _gate_module(f))


def hr_or_admin_required(f):
    return _gated(
        f, 'hr_or_admin', lambda: _forbidden_json('Forbidden - HR access required'),
        _gate_module(f),
    )


def finance_or_admin_required(f):
    return _gated(
        f, 'finance_or_admin', lambda: _forbidden_json('Forbidden - Finance access required'),
        _gate_module(f),
    )


def department_required(*depts):
    """Require specific department(s) or Admin role"""
    def decorator(f):
        return _gated(
            f, 'department', lambda: _forbidden_json('Forbidden - insufficient department access'),
            _gate_module(f), departments=depts,
        )
    return decorator



def _page_rules():
    """Map a page href to its Flask endpoint (first non-API GET rule wins)."""
    cache = getattr(app, '_hrms_page_endpoints', None)
    if cache is None:
        cache = {}
        for rule in app.url_map.iter_rules():
            if rule.rule.startswith('/api/') or 'GET' not in (rule.methods or set()):
                continue
            cache.setdefault(rule.rule, rule.endpoint)
        app._hrms_page_endpoints = cache
    return cache


def navigation_for(actor, conn=None) -> list[dict]:
    """Navbar entries for ``actor``, filtered by the *routes' own* gate.

    The navigation is derived from ``policy.NAV_ENTRIES`` and the gate/module
    tags the decorators stamped on each view, so a link is shown exactly when
    the linked route would let the actor through (FR-USR-15).
    """
    own_conn = conn is None
    conn = conn or get_db()
    try:
        endpoints = _page_rules()

        def permitted(href):
            endpoint = endpoints.get(href)
            if endpoint is None:
                # A nav entry with no matching page route: trust the spec.
                return True
            view = app.view_functions[endpoint]
            gate = getattr(view, '__hrms_gate__', 'login')
            module = getattr(view, '__hrms_module__', None)
            if gate == 'login' or not module:
                return True
            return _gate_passes(actor, gate) and policy.can(actor, module, conn=conn)

        entries = []
        for entry in policy.NAV_ENTRIES:
            if entry.get('always'):
                entries.append(entry)
                continue
            if entry.get('group') == 'modules':
                children = [
                    child for child in entry.get('children', ())
                    if child.get('divider') or permitted(child['href'])
                ]
                if not any(child.get('href') for child in children):
                    continue
                entries.append({**entry, 'children': children})
                continue
            if permitted(entry['href']):
                entries.append(entry)
        return entries
    finally:
        if own_conn:
            conn.close()


@app.context_processor
def inject_navigation():
    """Give every template the same policy-filtered navbar (FR-USR-15).

    Also exposes `current_emp_id`, taken from the **database** actor rather than the
    session copy, because that is the identity the authorization decisions are made
    against and a stale session copy is what a role change leaves behind. The admin
    user panel needs it to tell "setting someone else's password" apart from
    "setting my own", which has to supply the current password.
    """
    if 'emp_id' not in session:
        return {'nav_entries': (), 'current_emp_id': None}
    conn = get_db()
    try:
        actor = policy.current_actor(conn)
        return {
            'nav_entries': navigation_for(actor, conn=conn),
            'current_emp_id': actor.get('emp_id'),
        }
    except Exception:
        logger.warning('navigation_for failed; rendering an empty navbar', exc_info=True)
        return {'nav_entries': (), 'current_emp_id': None}
    finally:
        conn.close()

# ══════════════════════════════════════════════════════════════════════
#  AUTH ROUTES
# ══════════════════════════════════════════════════════════════════════

@app.route('/api/credentials')
@login_required
def get_credentials():
    """Return known user credentials for demo purposes (admin only)"""
    conn = get_db()
    try:
        if not policy.can(policy.current_actor(conn), 'users', conn=conn):
            return jsonify({'error': 'Admin access required'}), 403
        rows = conn.execute("SELECT emp_id, name, role, department FROM users ORDER BY emp_id").fetchall()
    finally:
        conn.close()
    result = [{'emp_id': r[0], 'name': r[1], 'role': r[2], 'department': r[3] or '-'} for r in rows]
    return jsonify(result), 200


def _invalid_credentials():
    """The one response every failed sign-in gets (FR-AUTH-02).

    The body is deliberately the SRS's own ``invalid_credentials`` string and not a
    sentence about passwords: a message that says "invalid password" confirms the
    account exists to anyone who gets the "unknown employee" case to differ from
    it. It is a function rather than a literal repeated at each `return` because the
    route has six refusal paths and the whole control is that they agree.
    """
    return jsonify({'error': 'invalid_credentials'}), 401


def _count_failed_login(conn, emp_id, name, email):
    """Record a failed sign-in, and lock + notify on the attempt that tips it.

    Called only for a genuine wrong password against an account that could
    otherwise have signed in — not for a blocked account, and not for one already
    locked out, because counting those would grow a counter behind a refusal that
    has already happened.
    """
    attempts, just_locked = lockout.register_failure(conn, emp_id)
    audit_log(
        emp_id, 'LOGIN_FAILED',
        f'Failed sign-in {attempts}/{lockout.MAX_FAILED_ATTEMPTS} within '
        f'{int(lockout.FAILURE_WINDOW.total_seconds() // 60)} minutes',
        entity='Auth', entity_id=emp_id,
    )
    if not just_locked:
        return
    audit_log(
        emp_id, 'ACCOUNT_LOCKED',
        f'Account locked for {int(lockout.LOCK_DURATION.total_seconds() // 60)} '
        f'minutes after {attempts} consecutive failed sign-ins',
        entity='Auth', entity_id=emp_id,
    )
    # The SRS asks for an email, and it matters more than usual here: without it a
    # lockout is a silent denial, and the account's owner has no way to tell their
    # own mistyped password from someone else guessing at it. The in-app
    # notification is not redundant — `send_email` returns success and only logs
    # when no SMTP host is configured, so on a deployment without mail this email
    # is never sent at all, and a lockout the account owner cannot see is a
    # denial of service with no explanation.
    add_notification(
        emp_id, 'ACCOUNT_LOCKED',
        f'Your account is locked for '
        f'{int(lockout.LOCK_DURATION.total_seconds() // 60)} minutes after '
        f'{attempts} consecutive failed sign-in attempts. It unlocks on its own. '
        'If you do not recognise these attempts, contact your administrator and '
        'change your password.',
    )
    if email:
        # **Queued, not sent** (FR-NOT-01). This is the sign-in path, so an SMTP call
        # here is the worst possible place for one: a slow provider would hold the
        # worker that is meant to be refusing the attempt, and the old code had no
        # timeout. `force=True` because this is an account-security notice — the SRS
        # pairs the lock with a notification precisely so the login response cannot
        # become a status oracle, and a notification an employee could have muted is
        # not the control the requirement describes.
        try:
            enqueue_notification_email(
                conn, emp_id, email,
                'Your HRMS account has been temporarily locked',
                f'<p>Hello {name or emp_id},</p>'
                f'<p>There were {attempts} consecutive failed sign-in attempts '
                f'against your account within '
                f'{int(lockout.FAILURE_WINDOW.total_seconds() // 60)} minutes, so it '
                f'is locked for '
                f'{int(lockout.LOCK_DURATION.total_seconds() // 60)} minutes as a '
                'protection against a password-guessing attack.</p>'
                '<p>The lock expires on its own. If you do not recognise these '
                'attempts, contact your administrator and change your password.</p>',
                category=notifications.category_for('ACCOUNT_LOCKED'),
                force=True,
            )
        except Exception:  # a failed notification must not fail the sign-in path
            logger.exception('Could not queue the account-lockout email to %s', emp_id)


# The SRS's shift-start burst target is 1,000 logins inside a five-minute window,
# which is 200/minute from whatever egress address a corporate NAT shares — and it
# qualifies it precisely: "without lockouts caused by shared-NAT rate limiting
# (**per-account, not per-IP-only**)". At 20/minute this route refused 90% of a
# legitimate shift start before anybody had typed a wrong password.
#
# Raising it is only safe because the *per-account* control now exists properly:
# `lockout.py` locks an employee after 10 consecutive failures inside 15 minutes
# and emails them. That is the defence against guessing, and this limit was
# standing in for it before FR-AUTH-03 landed. What remains here is a bound on
# volumetric abuse from one address, which 200/minute still is.
@limiter.limit(os.getenv('LOGIN_RATE_LIMIT', '200 per minute'))
@app.route('/login', methods=['GET', 'POST'])
def login():
    """User login
    ---
    post:
      tags: [Auth]
      parameters:
        - in: body
          name: body
          schema:
            type: object
            properties:
              emp_id: {type: string}
              password: {type: string}
      responses:
        200: {description: Login success}
        401: {description: Invalid credentials}
    """
    if request.method == 'GET':
        return render_template('login.html')

    data = request.get_json(silent=True) or {}
    emp_id = data.get('emp_id', '').strip().upper()
    password = data.get('password', '')

    if not emp_id or not password:
        return jsonify({'error': 'Missing credentials'}), 400

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT emp_id, name, role, password, status, allow_login, department "
            "FROM users WHERE emp_id = ?",
            [emp_id],
        ).fetchone()

        # FR-AUTH-02 (enumeration) and FR-AUTH-03 (lockout) in one pass.
        #
        # *Every* refusal below is `_invalid_credentials()`: one body, one 401, no
        # matter whether the account is unknown, the password is wrong, the account
        # is blocked, archived, pre-hire, has `allow_login` off, or is locked out.
        # This used to answer two distinct 401 messages plus two **403s** ("Account
        # is blocked", "Login is not allowed"), which is exactly the state leak
        # FR-AUTH-02 forbids — and the traceability matrix recorded the row as
        # IMPLEMENTED, so the gap was invisible. Adding a lockout without fixing
        # this would have added a *third* distinguishable state.
        #
        # The password is always verified before anything else is considered, so
        # an unknown account and a locked account cost the same time and the
        # response time says nothing either.
        if not row:
            return _invalid_credentials()

        stored_hash = row[3]
        password_ok = check_password(password, stored_hash)

        remaining = lockout.lock_remaining(conn, row[0])
        # A locked account is refused *after* the password check, so a wrong
        # password on a locked account is refused for being wrong — no state is
        # disclosed by which reason won, because there is only one reason given.
        if not password_ok or remaining is not None or not row[5] \
                or lockout.refusal_state(row[4]):
            if not password_ok and remaining is None and row[5] \
                    and not lockout.refusal_state(row[4]):
                # A real attempt at an account that could otherwise have signed
                # in: count it, and lock + notify on the one that tips it over.
                _count_failed_login(conn, row[0], row[1], row[6])
            return _invalid_credentials()

        # Phase 3a (CC-06): transparently upgrade legacy bcrypt hashes to Argon2id
        if needs_rehash(stored_hash):
            conn.execute(
                "UPDATE users SET password = ? WHERE emp_id = ?",
                [hash_password(password), emp_id],
            )

        # A successful sign-in breaks the failure streak — this is what
        # "consecutive" means. Without it an employee who typos twice, signs in,
        # and typos twice more would be locked out by four mistakes spread over a
        # week.
        lockout.register_success(conn, row[0])
    finally:
        conn.close()

    # FR-AUTH-11: for a role in mfa.MANDATORY_ROLES, and for anyone who has
    # opted in, the password is only half the credential. From here login is a
    # two-step flow: the password step parks the identity in
    # `session['mfa_pending']` and deliberately does NOT set `session['emp_id']`,
    # so `login_required` and every role gate refuse this session everywhere
    # until the second factor is presented. Nothing else has to remember to
    # check, which is the point.
    conn = get_db()
    try:
        enrolled = mfa.is_enrolled(conn, row[0])
    finally:
        conn.close()
    mandatory = mfa.requires_enrolment(row[2])
    if enrolled or mandatory:
        # Signing in as a second account has to end the first one. The browser
        # keeps the session cookie across a sign-in, so without this the previous
        # employee's identity is *still* in the session while the second factor is
        # being asked for — and `_mfa_subject()` prefers `session['emp_id']`, so the
        # enrolment and the challenge would both act on the wrong person. It is
        # also what "log in" means: signing in as someone else is not additive.
        #
        # This is not hypothetical. The browser suite signs in as EMP001, then as a
        # second administrator, in the same browser context; the second sign-in
        # tried to enrol the *first* employee and was refused with a 409, which
        # surfaced as a timeout on a wait for a secret that was never going to
        # appear.
        _end_current_session()
        step = 'challenge' if enrolled else 'enrol'
        mfa.start_pending(
            session, row[0], row[1], row[2], step, row[6] or '',
        )
        audit_log(
            row[0], 'MFA_REQUIRED',
            f'Password accepted for {row[1]}; awaiting {step}',
            entity='Auth', entity_id=row[0],
        )
        return jsonify({
            'message': 'Password accepted; a second factor is required',
            'mfa_required': 'challenge_required' if enrolled else 'enrol_required',
            'mandatory': mandatory,
            'redirect': '/dashboard',
        }), 200

    return _complete_login(row[0], row[1], row[2], row[6] or '')


def _complete_login(emp_id, name, role, department):
    """Establish the authenticated session and record the attendance row.

    Shared by the one-step login and by the two-step MFA path, so the second
    factor cannot quietly skip anything the first step does — the session id
    allocation, the shift-date resolution and the audit row are all here
    exactly once.
    """
    conn = get_db()
    session_id = _next_generated_id(conn, 'user_sessions', 'session_id')
    session['emp_id'] = emp_id
    session['name'] = name
    session['role'] = role
    session['department'] = department
    session['session_id'] = session_id

    now = datetime.now()
    shift_date = _get_shift_date_for_dt(emp_id, now, conn)
    conn.execute(
        "INSERT INTO user_sessions (session_id, emp_id, login_time, session_date) VALUES (?, ?, ?, ?)",
        [session_id, emp_id, now, shift_date]
    )
    conn.close()

    audit_log(emp_id, 'LOGIN', f'User {name} logged in', entity='Auth', entity_id=emp_id)
    return jsonify({'message': 'Login successful', 'redirect': '/dashboard'}), 200


@app.route('/logout')
def logout():
    emp_id = session.get('emp_id')
    session_id = session.get('session_id')
    if emp_id:
        conn = get_db()
        if session_id:
            sess = conn.execute(
                "SELECT login_time FROM user_sessions WHERE session_id = ? AND emp_id = ? AND logout_time IS NULL",
                [session_id, emp_id]
            ).fetchone()
        else:
            sess = conn.execute(
                "SELECT session_id, login_time FROM user_sessions WHERE emp_id = ? AND logout_time IS NULL ORDER BY login_time DESC LIMIT 1",
                [emp_id]
            ).fetchone()
        if sess:
            if session_id:
                login_time, curr_sid = sess[0], session_id
            else:
                curr_sid, login_time = sess[0], sess[1]
            logout_time = datetime.now()
            hours = round((logout_time - login_time).total_seconds() / 3600, 2)
            conn.execute(
                "UPDATE user_sessions SET logout_time = ?, total_hours = ? WHERE session_id = ?",
                [logout_time, hours, curr_sid]
            )
        conn.close()
        audit_log(emp_id, 'LOGOUT', 'User logged out', entity='Auth', entity_id=emp_id)
    session.clear()
    return redirect(url_for('login'))


# ── FR-AUTH-11 multi-factor authentication ──────────────────────────────────
# The rules that decide *whether* a code is acceptable live in `mfa.py`; these
# are the routes. `_mfa_subject()` is the one place that resolves "who is this
# request acting as", because a user reaching /api/mfa/enrol may be fully logged
# in (opting in) or half-authenticated (a mandatory role being made to enrol
# before they get a session) and both are legitimate.

#: The session keys that constitute an authenticated identity. Popped by
#: `_end_current_session` so that signing in as somebody else does not leave the
#: previous employee's session in place. `csrf_token` is deliberately not here:
#: it is not an identity, and dropping it would make the very next write — the
#: challenge itself — fail CSRF for no reason.
_IDENTITY_SESSION_KEYS = ('emp_id', 'name', 'role', 'department', 'session_id')


def _end_current_session():
    """Drop the current identity, closing its `user_sessions` row first.

    Called when a new sign-in replaces an existing one. The row is closed with
    its `logout_time` so the abandoned session's hours are recorded rather than
    left open forever, which is the same accounting `/logout` performs — an
    abandoned browser tab is a logout for attendance purposes either way.
    """
    emp_id = session.get('emp_id')
    session_id = session.get('session_id')
    if emp_id and session_id:
        conn = get_db()
        try:
            row = conn.execute(
                'SELECT login_time FROM user_sessions WHERE session_id = ? '
                'AND emp_id = ? AND logout_time IS NULL',
                [session_id, emp_id],
            ).fetchone()
            if row:
                logout_time = datetime.now()
                hours = round((logout_time - row[0]).total_seconds() / 3600, 2)
                conn.execute(
                    'UPDATE user_sessions SET logout_time = ?, total_hours = ? '
                    'WHERE session_id = ?',
                    [logout_time, hours, session_id],
                )
        finally:
            conn.close()
        audit_log(emp_id, 'LOGOUT', 'Session replaced by a new sign-in',
                  entity='Auth', entity_id=emp_id)
    for key in _IDENTITY_SESSION_KEYS:
        session.pop(key, None)
    session.pop('mfa_pending', None)


def _mfa_subject():
    """(emp_id, name, pending_state) for an MFA route, or (None, None, None).

    A parked login wins over a full session. It should be impossible for both to
    be set — `_end_current_session` clears the old identity before parking the new
    one — but if both were ever present, the identity *under authentication* is
    the one whose second factor is being presented, and the other would let a
    request answer for an employee who is not the one signing in.
    """
    state = mfa.pending(session)
    if state:
        return state['emp_id'], state.get('name') or '', state
    if 'emp_id' in session:
        return session['emp_id'], session.get('name') or '', None
    state = mfa.pending(session)
    if state:
        return state['emp_id'], state.get('name') or '', state
    return None, None, None


def _mfa_unauthorised():
    return jsonify({
        'error': 'Authentication required',
        'mfa_required': 'login',
    }), 401


@app.route('/api/mfa/status')
def mfa_status():
    """Is the caller enrolled, and is it compulsory for their role?"""
    emp_id, _, _ = _mfa_subject()
    if not emp_id:
        return _mfa_unauthorised()
    conn = get_db()
    try:
        return jsonify(mfa.status_for(conn, emp_id)), 200
    finally:
        conn.close()


@app.route('/api/mfa/enrol', methods=['POST'])
def mfa_enrol():
    """Step 1 of enrolment: mint a secret and store it *disabled*.

    The secret is returned once and is not usable until `confirm` proves the
    authenticator holds it.
    """
    emp_id, name, _ = _mfa_subject()
    if not emp_id:
        return _mfa_unauthorised()
    conn = get_db()
    try:
        try:
            out = mfa.begin_enrolment(conn, emp_id, name)
        except mfa.EnrolmentConflict as exc:
            return jsonify({'error': str(exc)}), 409
        except RuntimeError as exc:  # no/unusable MFA_ENCRYPTION_KEY
            logger.error('MFA enrolment refused for %s: %s', emp_id, exc)
            return jsonify({'error': str(exc)}), 503
    finally:
        conn.close()
    audit_log(
        emp_id, 'MFA_ENROL_STARTED',
        f'Unconfirmed MFA enrolment created for {emp_id}', entity='Auth', entity_id=emp_id,
    )
    return jsonify({
        'secret': out['secret'],
        'uri': out['uri'],
        'qr_url': '/api/mfa/qr',
        'message': 'Scan the code, then confirm with the 6-digit code your app shows.',
    }), 200


@app.route('/api/mfa/qr')
def mfa_qr():
    """The provisioning URI as a PNG.

    Rendered from the stored (encrypted) secret rather than from the enrolment
    response so that reloading the enrolment page works, and so the URI is not
    duplicated into the page twice.
    """
    emp_id, _, _ = _mfa_subject()
    if not emp_id:
        return _mfa_unauthorised()
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT secret_encrypted, enabled FROM mfa_credentials WHERE emp_id = ?",
            [emp_id],
        ).fetchone()
    finally:
        conn.close()
    if not row or row[1]:
        # Already confirmed: there is nothing to enrol into, and re-serving the
        # QR would put a live bearer secret back on the wire for no reason.
        return jsonify({'error': 'No enrolment in progress'}), 404
    name = session.get('name') or emp_id
    try:
        uri = mfa.provisioning_uri(emp_id, name, mfa.decrypt_secret(row[0]))
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 503
    return send_file(
        BytesIO(mfa.qr_png(uri)), mimetype='image/png',
        max_age=0, download_name=f'mfa-{emp_id}.png',
    )


@app.route('/api/mfa/confirm', methods=['POST'])
def mfa_confirm():
    """Step 2 of enrolment: prove possession, then go live."""
    emp_id, _, state = _mfa_subject()
    if not emp_id:
        return _mfa_unauthorised()
    code = (request.get_json(silent=True) or {}).get('code', '')
    conn = get_db()
    try:
        ok = mfa.confirm_enrolment(conn, emp_id, code)
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 503
    finally:
        conn.close()
    if not ok:
        attempts, locked = mfa.note_wrong_code(session)
        if locked:
            return jsonify({
                'error': 'Too many incorrect codes. Start the sign-in again.',
                'mfa_required': 'login',
            }), 429
        logger.info(
            'MFA confirmation refused for %s (attempt %d of %d)',
            emp_id, attempts, mfa.MAX_CHALLENGE_ATTEMPTS,
        )
        return jsonify({'error': 'That code is not correct'}), 401

    mfa.clear_pending(session)
    audit_log(emp_id, 'MFA_ENABLED', 'MFA enrolment confirmed', entity='Auth', entity_id=emp_id)

    # A mandatory role enrols *during* login, so confirming possession of both
    # factors completes that sign-in. An already-logged-in optional user is
    # simply told they are done.
    if state and state.get('step') == 'enrol':
        return _complete_login(emp_id, state.get('name'), state.get('role'),
                               state.get('department') or '')
    return jsonify({'message': 'Multi-factor authentication is now active'}), 200


@app.route('/api/mfa/challenge', methods=['POST'])
def mfa_challenge():
    """Second step for an enrolled user: the code from their authenticator."""
    state = mfa.pending(session)
    if not state:
        return _mfa_unauthorised()
    if state.get('step') != 'challenge':
        return jsonify({'error': 'No second-factor challenge is pending'}), 409
    code = (request.get_json(silent=True) or {}).get('code', '')
    emp_id = state['emp_id']
    conn = get_db()
    try:
        ok = mfa.verify_challenge(conn, emp_id, code)
    finally:
        conn.close()
    if not ok:
        attempts, locked = mfa.note_wrong_code(session)
        if locked:
            audit_log(emp_id, 'MFA_CHALLENGE_LOCKED',
                      'MFA challenge abandoned after too many incorrect codes',
                      entity='Auth', entity_id=emp_id)
            return jsonify({
                'error': 'Too many incorrect codes. Start the sign-in again.',
                'mfa_required': 'login',
            }), 429
        logger.info(
            'MFA challenge refused for %s (attempt %d of %d)',
            emp_id, attempts, mfa.MAX_CHALLENGE_ATTEMPTS,
        )
        return jsonify({'error': 'That code is not correct'}), 401

    mfa.clear_pending(session)
    audit_log(emp_id, 'MFA_VERIFIED', 'Second factor accepted', entity='Auth', entity_id=emp_id)
    return _complete_login(emp_id, state.get('name'), state.get('role'),
                           state.get('department') or '')


@app.route('/api/mfa/disable', methods=['POST'])
def mfa_disable():
    """Turn one's own second factor off.

    Refused for a role where MFA is mandatory: an Admin could otherwise make
    themselves unbreakable-to-themselves and permanently outside the control
    the policy exists to impose. Those users go through the Admin reset, which
    is audited.
    """
    emp_id, _, _ = _mfa_subject()
    if not emp_id:
        return _mfa_unauthorised()
    conn = get_db()
    try:
        st = mfa.status_for(conn, emp_id)
        if st['mandatory']:
            return jsonify({
                'error': 'Multi-factor authentication is required for your role '
                         'and can only be reset by an administrator.',
            }), 409
        removed = mfa.disable(conn, emp_id)
    finally:
        conn.close()
    if removed:
        audit_log(emp_id, 'MFA_DISABLED', 'MFA turned off by the user',
                  entity='Auth', entity_id=emp_id)
    return jsonify({'message': 'Multi-factor authentication is off', 'removed': removed}), 200


@app.route('/api/admin/users/<emp_id>/mfa/reset', methods=['POST'])
@admin_required
def admin_reset_mfa(emp_id):
    """Support recovery: drop an employee's second factor so they can re-enrol.

    This is a social-engineering target by construction — "I lost my phone" is
    what an attacker asks for right after stealing a password. Two things follow
    and are implemented rather than assumed: the response is identical whether or
    not the target had MFA enabled, so it cannot be used to discover who is
    protected; and the action is audited with the before/after state.
    """
    conn = get_db()
    try:
        target = conn.execute(
            "SELECT role, status FROM users WHERE emp_id = ?", [emp_id]
        ).fetchone()
        if not target:
            return jsonify({'error': 'Employee not found'}), 404
        # A blocked or archived account is not an MFA question; the block already
        # decides who may sign in, and clearing the factor here would suggest
        # otherwise to an admin reading the audit trail.
        if target[1] in ('Blocked', 'Inactive', 'Archived'):
            return jsonify({'error': 'Account is not active'}), 409
        before = mfa.is_enrolled(conn, emp_id)
        mfa.reset(conn, emp_id)
    finally:
        conn.close()
    audit_log(
        emp_id, 'MFA_RESET',
        f'Admin reset MFA (was_enrolled={before}) for {emp_id}',
        entity='Auth', entity_id=emp_id,
        before={'mfa_enrolled': before}, after={'mfa_enrolled': False},
    )
    add_notification(
        emp_id, 'MFA_RESET',
        'Your multi-factor authentication was reset by an administrator. '
        'You will be asked to enrol again the next time you sign in.',
    )
    return jsonify({
        'message': f'MFA reset for {emp_id}. They will re-enrol at next sign-in.',
        'emp_id': emp_id,
    }), 200


@app.route('/api/admin/users/<emp_id>/unlock', methods=['POST'])
@admin_required
def admin_unlock_account(emp_id):
    """Clear a FR-AUTH-03 lockout now, without waiting for it to expire.

    A lock expires by itself, so this is a convenience — but it is a real one.
    "Locked until 23:14" is useless to someone whose shift ended at 18:00, and an
    administrator who cannot clear it will be tempted to reach for `status =
    Blocked`, which is a *sanctioned* account state. That would turn a fifteen-minute
    nuisance into an HR record, so the two are kept apart here.

    `200` even when the account was not locked: the admin asked for the account to
    be unlocked, and it is. `removed` reports whether there was anything to do, so a
    UI can distinguish "fixed" from "was never locked" without guessing.
    """
    conn = get_db()
    try:
        target = conn.execute(
            'SELECT status FROM users WHERE emp_id = ?', [emp_id]
        ).fetchone()
        if not target:
            return jsonify({'error': 'Employee not found'}), 404
        before = lockout.status_for(conn, emp_id)
        was_locked = lockout.unlock(conn, emp_id)
    finally:
        conn.close()
    if was_locked:
        audit_log(
            emp_id, 'ACCOUNT_UNLOCKED',
            f'Admin cleared the sign-in lockout for {emp_id}',
            entity='Auth', entity_id=emp_id,
            before=before, after={'locked': False, 'attempts': 0, 'locked_until': None},
        )
        add_notification(
            emp_id, 'ACCOUNT_UNLOCKED',
            'An administrator cleared the temporary sign-in lock on your account. '
            'If you do not recognise the failed attempts, please change your password.',
        )
    return jsonify({
        'message': f'{emp_id} is not locked out.',
        'emp_id': emp_id,
        'removed': was_locked,
    }), 200


@app.route('/api/admin/users/<emp_id>/password', methods=['POST'])
@admin_required
def admin_set_user_password(emp_id):
    """An administrator sets a colleague's password.

    This exists because **`/api/forgot-password` cannot work without an email
    server.** The reset link's only delivery is the outbox, and every delivery path
    goes through `send_email`, so on a deployment with no SMTP configured a
    forgotten password is *unrecoverable*: the employee is told, truthfully and
    uniformly, that "if the account exists, a reset link has been sent" — and then
    waits for a message that will never arrive. An administrator is the recovery
    path, so it belongs in the user-management panel rather than being improvised
    over a database console.

    Four decisions that are not obvious from the signature:

    **Sessions are closed, and this is the point of the route.** A password reset
    that leaves existing sessions alive is not a reset — the usual reason an admin
    resets a password is that it may have been exposed, and the exposed session
    would survive it. Both the database sessions and the Redis-backed ones are
    revoked.

    **The lockout is cleared.** FR-AUTH-03 locks an employee for 15 minutes after 10
    consecutive failures. Without this, an admin sets a new password for a locked
    account, tells the employee "try again", and they are still locked out — an
    admin action that appears to work and does not.

    **Setting your own password this way needs your current password.** Otherwise a
    hijacked admin *session* becomes permanent account ownership: the attacker sets
    a password only they know and the account is theirs after the session ends.
    Self-service already requires the current password; this route must not be the
    way around it.

    **The password is never written to the audit row.** `audit_log` is retained for
    years; a copy of a credential in it would outlive the account.

    Blocked, archived and inactive accounts are refused with 409 rather than
    silently accepted: they cannot sign in anyway, and the honest response names
    the action that would help — restore or unblock first, then set the password.
    """
    data = request.get_json(silent=True) or {}
    new_password = (data.get('password') or '').strip()
    actor = session['emp_id']
    generated = False

    if not new_password:
        # Same affordance as user creation: a deployment with no mail server has no
        # way to deliver a reset link, so "leave it blank and hand over the printed
        # password" is a first-class path rather than a workaround.
        new_password = _generate_initial_password()
        generated = True
    else:
        # FR-AUTH-10, the same check every other password-setting route uses, so the
        # answer for "too short" is identical whichever route the admin reaches for.
        problem = _password_problem(new_password, 'password')
        if problem:
            return jsonify(problem[0]), problem[1]

    conn = get_db()
    try:
        target = conn.execute(
            'SELECT emp_id, name, status FROM users WHERE emp_id = ?', [emp_id]
        ).fetchone()
        if not target:
            return jsonify({'error': 'Employee not found'}), 404

        if target[2] != 'Active':
            # The advice has to use the *same word as the button that does it*, or
            # the admin is left hunting for an "Activate" control that does not
            # exist. This one is "Unblock" — deliberately distinct from "Unlock",
            # which clears a temporary lockout and is a different action with a
            # different meaning (FR-AUTH-03).
            next_step = {
                'Archived': 'Restore the account first, then set the password.',
                'Blocked': 'Unblock the account first, then set the password.',
                'Inactive': 'Set the status back to Active, then set the password.',
                'Pre-hire': 'Complete the hire (accept the offer), then set the password.',
            }.get(target[2], 'Set the status back to Active, then set the password.')
            return jsonify({
                'error': f'{emp_id} is {target[2]}, so a password would not let them sign in',
                'status': target[2],
                'next_step': next_step,
            }), 409

        if emp_id == actor:
            # See the docstring: without this a stolen admin session becomes a
            # permanent takeover.
            current = data.get('current_password', '')
            stored = conn.execute(
                'SELECT password FROM users WHERE emp_id = ?', [emp_id]
            ).fetchone()[0]
            if not check_password(current, stored):
                return jsonify({
                    'error': 'Your own password change needs your current password',
                    'hint': 'Use the change-password form on your profile, or supply '
                            'current_password here.',
                }), 400
            if new_password == current:
                return jsonify({
                    'error': 'The new password must differ from the current one',
                }), 400

        before = lockout.status_for(conn, emp_id)
        was_locked = lockout.unlock(conn, emp_id)
        conn.execute(
            'UPDATE users SET password = ?, failed_attempts = 0, locked_until = NULL '
            'WHERE emp_id = ?',
            [hash_password(new_password), emp_id],
        )
        revoked = _close_active_user_sessions(conn, emp_id)
        conn.commit()
    finally:
        conn.close()

    # Redis-backed sessions live outside the database, so closing the rows above is
    # not enough when REDIS_URL is set — a session that survives here would keep its
    # access after a password reset.
    _revoke_redis_sessions(emp_id)

    audit_log(
        actor, 'ADMIN_PASSWORD_SET',
        f'{actor} set a new password for {emp_id}',
        entity='Auth', entity_id=emp_id,
        before={'locked': before.get('locked'), 'attempts': before.get('attempts')},
        # Deliberately no password, and no hash: this table is kept for years.
        after={'password': 'set', 'locked_cleared': was_locked,
               'sessions_closed': revoked},
    )
    add_notification(
        emp_id, 'ADMIN_PASSWORD_SET',
        'An administrator set a new password for your account and signed you out of '
        'your other sessions. If you did not expect this, contact your administrator.',
    )

    body = {
        'message': f'Password set for {emp_id}.'
                   + (f' Cleared a lockout and closed {revoked} active session(s).'
                      if revoked else ''),
        'emp_id': emp_id,
        'sessions_closed': revoked,
        'lockout_cleared': was_locked,
    }
    if generated:
        # The only copy of the plaintext. Returned once, never stored, never emailed
        # — a deployment with no SMTP has no other way to hand it over.
        body['generated_password'] = new_password
        body['password_source'] = 'generated'
        body['notice'] = ('Shown once and not emailed — no SMTP server is configured. '
                          'Copy it now and hand it over; it cannot be retrieved later.')
    return jsonify(body), 200


@app.route('/dashboard')
@login_required
def dashboard():
    conn = get_db()
    try:
        # The admin variant is a presentation choice; it follows the policy
        # ("administers something") instead of a hard-coded role list.
        if policy.sees_admin_surface(policy.current_actor(conn), conn=conn):
            return render_template('admin_dashboard.html')
    finally:
        conn.close()
    return render_template('user_dashboard.html')


@app.route('/profile')
@login_required
def profile_page():
    return render_template('profile.html')


@app.route('/api/profile', methods=['GET', 'PUT'])
@login_required
def profile_api():
    """Employee self-service: view / update profile
    ---
    get:
      tags: [Profile]
      responses:
        200:
          description: Profile data
    put:
      tags: [Profile]
      parameters:
        - in: body
          name: body
          schema:
            type: object
            properties:
              name: {type: string}
              email: {type: string}
              department: {type: string}
      responses:
        200:
          description: Updated
    """
    emp_id = session['emp_id']
    if request.method == 'GET':
        u = get_user(emp_id)
        if not u:
            return jsonify({'error': 'Not found'}), 404
        # Own record: `pii_view` always allows it, so this documents the rule
        # rather than changing it. Another employee's PII is /api/users/<id>/pii.
        conn = get_db()
        try:
            if not policy.pii_view(policy.current_actor(conn), emp_id, conn=conn):
                return jsonify({'error': 'Forbidden'}), 403
        finally:
            conn.close()
        return jsonify({
            'emp_id': u[0], 'name': u[1], 'email': u[2],
            'role': u[3], 'department': u[5],
            'allow_login': u[6], 'allow_breaks': u[7],
            'designation': u[8], 'manager_emp_id': u[9],
            'phone': u[10],
            'date_of_birth': u[11].isoformat() if u[11] else None,
            'date_of_joining': u[12].isoformat() if u[12] else None,
            'address': u[13], 'emergency_contact_name': u[14],
            'emergency_contact_phone': u[15]
        }), 200

    data = request.get_json(silent=True) or {}
    conn = get_db()
    conn.execute(
        "UPDATE users SET name = ?, email = ?, department = ?, phone = ?, address = ?, emergency_contact_name = ?, emergency_contact_phone = ? WHERE emp_id = ?",
        [data.get('name'), data.get('email'), data.get('department', ''),
         data.get('phone'), data.get('address'), data.get('emergency_contact_name'),
         data.get('emergency_contact_phone'), emp_id]
    )
    conn.close()
    session['name'] = data.get('name')
    audit_log(emp_id, 'PROFILE_UPDATE', 'Profile updated', entity='users', entity_id=emp_id)
    return jsonify({'message': 'Profile updated'}), 200


@app.route('/api/change-password', methods=['POST'])
@limiter.limit("10 per minute")
@login_required
def change_password():
    """Change own password
    ---
    post:
      tags: [Profile]
      parameters:
        - in: body
          name: body
          schema:
            type: object
            properties:
              current_password: {type: string}
              new_password: {type: string}
      responses:
        200: {description: Password changed}
        400: {description: Validation error}
    """
    data = request.get_json(silent=True) or {}
    emp_id = session['emp_id']
    current = data.get('current_password', '')
    new_pwd = data.get('new_password', '')

    # FR-AUTH-10: the 6-character minimum the SRS calls a defect is gone, and
    # the breach corpus is now consulted. The current password is still checked
    # first below, so an unauthenticated guesser learns nothing from the policy.
    problem = _password_problem(new_pwd, 'new_password')
    if problem:
        return jsonify(problem[0]), problem[1]

    conn = get_db()
    row = conn.execute("SELECT password FROM users WHERE emp_id = ?", [emp_id]).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'User not found'}), 404

    stored = row[0]
    if not check_password(current, stored):
        conn.close()
        return jsonify({'error': 'Current password is incorrect'}), 400

    conn.execute("UPDATE users SET password = ? WHERE emp_id = ?", [hash_password(new_pwd), emp_id])
    conn.close()
    audit_log(emp_id, 'PASSWORD_CHANGE', 'Password changed', entity='users', entity_id=emp_id)
    return jsonify({'message': 'Password changed successfully'}), 200


#: The single body every ``forgot-password`` request gets, whatever it matched.
#: The SRS's own wording, and identical for a real account, a wrong email, a
#: malformed id and a request with no body at all — FR-AUTH-08's whole control is
#: that there is nothing here to compare.
FORGOT_PASSWORD_RESPONSE = {
    'message': 'If the account exists, a reset link has been sent.',
}

#: FR-AUTH-09. One hour, per the SRS (the 24 h token in the SRS belongs to
#: FR-USR-02's welcome email, a different flow with a different table row).
RESET_TOKEN_TTL = timedelta(hours=1)


def reset_link_for(token: str) -> str:
    """The absolute URL the emailed link points at.

    Built at **enqueue** time, in the request that asked for the reset, and carried
    in the outbox payload. The obvious alternative — building it in the dispatcher —
    has no request context to work from, so `_external=True` would either raise or
    silently produce a relative link that is useless in an email. Building it once,
    from the user's own request, also means the link cannot drift from the route
    that serves it.
    """
    return url_for('reset_password_page', token=token, _external=True)


@app.route('/reset-password')
def reset_password_page():
    """The page the emailed reset link points at.

    It did not exist. `/api/forgot-password` minted a token and returned it, so
    nothing ever needed a page — which meant the URL in the SRS's own flow
    ("email {host}/reset-password?token=...") was a 404, and the entire
    forgot-password journey was reachable only by calling the API and reading the
    response. Now that the token goes out of band, the link has to land somewhere,
    and the token stays in the URL fragment/query of a page that never sends it
    anywhere.
    """
    return render_template('reset_password.html')


@app.route('/api/forgot-password', methods=['POST'])
# The SRS says "Rate limit 5/min per IP and per email". Env-overridable because a
# hardcoded literal is a deployment knob nobody can turn, and because the test
# suites have to lift it — the same reasoning as LOGIN_RATE_LIMIT.
@limiter.limit(os.getenv('FORGOT_PASSWORD_RATE_LIMIT', '5 per minute'))
def forgot_password():
    """Request a password reset (FR-AUTH-08).

    **Always 202, always this body, and never the token.** This used to answer 404
    ``{"error": "No matching user found"}`` for an unknown account and 200 *carrying
    the token* for a real one — which is not a weakened version of the control, it
    is the whole enumeration oracle in one endpoint: a caller could confirm any
    employee ID and, if the email matched, obtain a working credential without ever
    touching the account. The traceability matrix recorded the requirement as
    IMPLEMENTED.

    Delivery moved to the outbox, which is where the SRS puts it ("email
    {host}/reset-password?token=... (queued via outbox)"). That is not decoration:
    the token was in the response precisely because nothing else carried it.
    """
    data = request.get_json(silent=True) or {}
    emp_id = data.get('emp_id', '').strip().upper()
    email = data.get('email', '')

    # (body, status) — Flask reads a two-tuple as (body, status), so the order is
    # not cosmetic. Getting it backwards returns a bare 202 with an integer where
    # the body should be.
    accepted = (jsonify(dict(FORGOT_PASSWORD_RESPONSE)), 202)
    if not emp_id or not email:
        return accepted

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT emp_id, name FROM users WHERE emp_id = ? AND email = ?",
            [emp_id, email]
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return accepted

    token = secrets.token_urlsafe(32)
    # The token row and its delivery event commit together (CC-09). A token with no
    # queued email is a token nobody can use, and a queued email for a token that
    # was rolled back is a link that fails on arrival; either way the user is stuck
    # with no explanation, so they are one atomic unit.
    with outbox.transaction() as tconn:
        tconn.execute(
            "INSERT INTO password_reset_tokens (token_id, emp_id, token, expires_at) "
            "VALUES (?, ?, ?, ?)",
            [_next_generated_id(tconn, 'password_reset_tokens', 'token_id'), row[0],
             _token_digest(token), datetime.now() + RESET_TOKEN_TTL],
        )
        outbox.enqueue(
            tconn, 'password.reset', aggregate='users', aggregate_id=row[0],
            payload={
                'emp_id': row[0],
                # Encrypted, not plaintext: `password_reset_tokens` holds a SHA-256
                # digest, so this encrypted copy is the only way the dispatcher can
                # mail a link the reset endpoint can verify. Without the app's
                # Fernet key the queue is unreadable, so a database read still
                # cannot mint a reset — see outbox._handle_password_reset.
                'reset_token_encrypted': _encrypt_lifecycle_secret(token),
                # Built here, where there is a request context; the dispatcher has
                # none and would have to guess the host.
                'reset_url': reset_link_for(token),
                'expires_in_minutes': int(RESET_TOKEN_TTL.total_seconds() // 60),
            },
        )
    audit_log(row[0], 'PASSWORD_RESET_REQUESTED',
              'Password reset requested', entity='users', entity_id=row[0])
    return accepted


@app.route('/api/reset-password', methods=['POST'])
# A 6-digit-free secret is guessable in principle, so the reset endpoint gets the
# same treatment as the request endpoint — and the same env override.
@limiter.limit(os.getenv('RESET_PASSWORD_RATE_LIMIT', '5 per minute'))
def reset_password():
    """Reset password using token
    ---
    post:
      tags: [Auth]
      parameters:
        - in: body
          name: body
          schema:
            type: object
            properties:
              token: {type: string}
              new_password: {type: string}
      responses:
        200:
          description: Password reset
    """
    data = request.get_json(silent=True) or {}
    token = data.get('token', '')
    new_pwd = data.get('new_password', '')

    # FR-AUTH-10: checked before the token is consumed, so a rejected password
    # can be retried with the same token instead of stranding the user and burning
    # a single-use credential on a typo.
    problem = _password_problem(new_pwd, 'new_password')
    if problem:
        return jsonify(problem[0]), problem[1]

    conn = get_db()
    try:
        # Digests only. The lookup used to be `token IN (?, ?)` with the raw token
        # *and* its digest, because the boot seed wrote two plaintext tokens — so
        # the hashing was half-done and a database read still yielded two working
        # credentials. The seed now writes digests (see `init_db`), and accepting
        # the plaintext spelling would put that hole straight back.
        row = conn.execute(
            "SELECT token_id, emp_id FROM password_reset_tokens "
            "WHERE token = ? AND used = 0 AND expires_at > ?",
            [_token_digest(token), datetime.now()]
        ).fetchone()
        if not row:
            return jsonify({'error': 'Invalid or expired token'}), 400

        # Conditional on `used = 0`, so two concurrent replays of the same token
        # give one winner and one 400. An unconditional write let both succeed, which
        # is the opposite of "single use" for the only race that matters.
        consumed = conn.execute(
            "UPDATE password_reset_tokens SET used = 1 "
            "WHERE token_id = ? AND used = 0", [row[0]]
        )
        if not getattr(consumed, 'rowcount', 1):
            return jsonify({'error': 'Invalid or expired token'}), 400

        # The SRS: "ALL other reset tokens for this user invalidated". Without
        # this, an attacker who requested their own reset while a legitimate one was
        # still live keeps a working credential after the legitimate user resets.
        conn.execute(
            "UPDATE password_reset_tokens SET used = 1 "
            "WHERE emp_id = ? AND token_id <> ? AND used = 0",
            [row[1], row[0]],
        )
        conn.execute(
            "UPDATE users SET password = ? WHERE emp_id = ?", [hash_password(new_pwd), row[1]]
        )
    finally:
        conn.close()
    audit_log(row[1], 'PASSWORD_RESET', 'Password reset via token', entity='users', entity_id=row[1])
    return jsonify({'message': 'Password reset successfully'}), 200


# ══════════════════════════════════════════════════════════════════════
#  EMPLOYEE MASTER EXTENSIONS
# ══════════════════════════════════════════════════════════════════════

@app.route('/api/v1/dependents', methods=['GET', 'POST'])
@app.route('/api/dependents', methods=['GET', 'POST'])
@login_required
def dependents_api():
    emp_id = session['emp_id']
    if request.method == 'GET':
        conn = get_db()
        rows = conn.execute("SELECT dependent_id, name, relationship, date_of_birth FROM dependents WHERE emp_id = ?", [emp_id]).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'name': r[1], 'relationship': r[2], 'date_of_birth': r[3].isoformat() if r[3] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('name') or not data.get('relationship'):
        return jsonify({'error': 'name and relationship required'}), 400
    conn = get_db()
    did = _next_generated_id(conn, 'dependents', 'dependent_id')
    conn.execute(
        # Explicit columns for the same reason as `documents_api` above.
        'INSERT INTO dependents (dependent_id, emp_id, name, relationship, '
        'date_of_birth) VALUES (?, ?, ?, ?, ?)',
        [did, emp_id, data['name'], data['relationship'],
         parse_date(data.get('date_of_birth'))],
    )
    conn.close()
    # `dependents` is PII by `policy.PII_FIELDS` — a third party with no statutory
    # retention of their own — so recording one is the other half of the pair the
    # delete route now records.
    audit_log(
        emp_id, 'DEPENDENT_CREATE',
        f'Added dependent {data["name"]} ({data["relationship"]})',
        entity='dependents', entity_id=did,
        after={'name': data['name'], 'relationship': data['relationship']},
    )
    return jsonify({'message': 'Dependent added', 'id': did}), 201


@app.route('/api/v1/dependents/<int:did>', methods=['DELETE'])
@app.route('/api/dependents/<int:did>', methods=['DELETE'])
@login_required
def delete_dependent(did):
    # The name is read before the delete so the audit row can name *whose* record
    # went. `dependents` is classified as PII by `policy.PII_FIELDS` — a third party
    # with no statutory retention of their own — so erasing one is exactly the kind
    # of irreversible change that must not be invisible. It did not audit, and the
    # row is gone immediately after, so there was no way to find out afterwards.
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT name, relationship FROM dependents "
            "WHERE dependent_id = ? AND emp_id = ?", [did, session['emp_id']],
        ).fetchone()
        if not row:
            return jsonify({'error': 'Dependent not found'}), 404
        conn.execute(
            "DELETE FROM dependents WHERE dependent_id = ? AND emp_id = ?",
            [did, session['emp_id']],
        )
    finally:
        conn.close()
    audit_log(
        session['emp_id'], 'DEPENDENT_DELETE',
        f'Deleted dependent {row[0]} ({row[1]}) of {session["emp_id"]}',
        entity='dependents', entity_id=did,
        before={'name': row[0], 'relationship': row[1]},
    )
    return jsonify({'message': 'Deleted'}), 200


@app.route('/api/v1/documents', methods=['GET', 'POST'])
@app.route('/api/documents', methods=['GET', 'POST'])
@login_required
def documents_api():
    emp_id = session['emp_id']
    if request.method == 'GET':
        conn = get_db()
        rows = conn.execute("SELECT doc_id, doc_type, file_name, uploaded_at FROM employee_documents WHERE emp_id = ?", [emp_id]).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'doc_type': r[1], 'file_name': r[2], 'uploaded_at': r[3].isoformat() if r[3] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('doc_type'):
        return jsonify({'error': 'doc_type required'}), 400
    conn = get_db()
    did = _next_generated_id(conn, 'employee_documents', 'doc_id')
    conn.execute(
        # An explicit column list, not a bare `VALUES`. Both schemas have five
        # columns in this order *today*, so this is not a live failure — it is the
        # latent version of the defect that made `POST /api/goals` return 500 on
        # every backend and `add_holiday` mis-target every value against v2.0's
        # sixth column. The seed two functions below already writes the column list
        # out, which is exactly why the seed kept working when the create path would
        # not have.
        'INSERT INTO employee_documents (doc_id, emp_id, doc_type, file_name, '
        'uploaded_at) VALUES (?, ?, ?, ?, ?)',
        [did, emp_id, data['doc_type'], data.get('file_name', ''), datetime.now()],
    )
    conn.close()
    audit_log(
        emp_id, 'DOCUMENT_RECORD',
        f'Recorded {data["doc_type"]} document'
        + (f' ({data["file_name"]})' if data.get('file_name') else ''),
        entity='employee_documents', entity_id=did,
        after={'doc_type': data['doc_type'], 'file_name': data.get('file_name') or None},
    )
    return jsonify({'message': 'Document recorded', 'id': did}), 201


# ══════════════════════════════════════════════════════════════════════
#  HOLIDAY CALENDAR
# ══════════════════════════════════════════════════════════════════════

# The unique index that makes "duplicate (name, date) per location" a constraint
# rather than a check in a handler. Named here because the error mapping below
# matches on it: a database that refuses the duplicate is the authority, and the
# pre-check exists only to give a readable message.
_HOLIDAY_UNIQUE_INDEX = 'uq_holiday_name_date_location'
_HOLIDAY_UNIQUE_COLUMNS = 3


def _holiday_rows(conn, year=None, htype=None, location=None, name=None):
    """The calendar, with the filters FR-HOL-01 asks for."""
    query = 'SELECT holiday_id, name, holiday_date, type, location FROM holidays WHERE 1 = 1'
    params = []
    if year is not None:
        query += ' AND year = ?'
        params.append(year)
    if htype is not None:
        query += ' AND type = ?'
        params.append(htype)
    if location is not None:
        # An org-wide holiday (location NULL or empty) is part of every location's
        # calendar, so filtering for a site includes it. Filtering the column
        # directly would hide Republic Day from the Mumbai office, which is
        # exactly the "stored but not filtered on" gap FR-HOL-01 records.
        query += " AND (location IS NULL OR location = '' OR LOWER(location) = ?)"
        params.append(location.strip().lower())
    if name is not None:
        query += ' AND LOWER(name) LIKE ?'
        params.append(f'%{name.strip().lower()}%')
    query += ' ORDER BY holiday_date, name'
    return conn.execute(query, params).fetchall()


def _holiday_json(row):
    holiday_id, name, when, htype, location = row
    payload = {
        'id': holiday_id,
        'name': name,
        'date': when.isoformat() if hasattr(when, 'isoformat') else str(when),
        'type': htype,
    }
    # `location` is reported as null rather than '' for an org-wide holiday, so a
    # client does not have to know which empty spelling the backend stores.
    if location:
        payload['location'] = location
    return payload


def _is_holiday_duplicate(exc):
    """Did the database refuse this insert as a duplicate holiday?"""
    constraint = getattr(getattr(exc, 'diag', None), 'constraint_name', None) or ''
    if constraint == _HOLIDAY_UNIQUE_INDEX:
        return True
    text = str(exc).lower()
    return 'unique' in text or 'duplicate key' in text


def _holiday_duplicate_exists(conn, name, when, location, exclude_id=None):
    """The readable pre-check, using the same normalisation as the index.

    It exists so the caller gets "a holiday named X already exists on that date
    for <location>" instead of a constraint name. The index is still what makes it
    true under concurrency; this is a message, not the rule.
    """
    target = holiday_calendar.duplicate_key(name, when, location)
    for row in _holiday_rows(conn):
        if row[0] == exclude_id:
            continue
        if holiday_calendar.duplicate_key(row[1], row[2], row[4]) == target:
            return row
    return None


def _holiday_filter_error(kind, value):
    if kind == 'type' and value not in holiday_calendar.TYPES:
        return jsonify({'error': f'type must be one of {", ".join(holiday_calendar.TYPES)}'}), 400
    if kind == 'year' and (value is None or not 1970 <= value <= 2200):
        return jsonify({'error': 'year must be between 1970 and 2200'}), 400
    return None


@app.route('/api/v1/holidays', methods=['GET'])
@app.route('/api/holidays', methods=['GET'])
@login_required
def get_holidays():
    """The calendar, with search and filter (FR-HOL-01)."""
    year = request.args.get('year', type=int)
    htype = request.args.get('type')
    location = request.args.get('location')
    name = request.args.get('q') or request.args.get('name')
    if year is None and request.args.get('year') is not None:
        return _holiday_filter_error('year', 0)
    if year is not None:
        bad = _holiday_filter_error('year', year)
        if bad:
            return bad
    if htype is not None:
        bad = _holiday_filter_error('type', htype)
        if bad:
            return bad
    conn = get_db()
    try:
        rows = _holiday_rows(conn, year=year, htype=htype, location=location, name=name)
    finally:
        conn.close()
    return jsonify({
        'holidays': [_holiday_json(r) for r in rows],
        'filters': {
            'year': year, 'type': htype, 'location': location, 'q': name,
        },
    }), 200


@app.route('/api/v1/holidays', methods=['POST'])
@app.route('/api/holidays', methods=['POST'])
@admin_required
@idempotent
def add_holiday():
    """Add a holiday (FR-HOL-02). Duplicate (name, date) per location is a 409."""
    try:
        name, when, htype, location = holiday_calendar.check_payload(
            request.get_json(silent=True) or {})
    except holiday_calendar.HolidayError as exc:
        return jsonify({'error': str(exc)}), exc.status
    conn = get_db()
    try:
        existing = _holiday_duplicate_exists(conn, name, when, location)
        if existing is not None:
            return jsonify({'error': (
                f'A holiday named {name} already exists on {when.isoformat()}'
                + (f' for {existing[4]}' if existing[4] else ' (organisation-wide)')
            )}), 409
        hid = _next_generated_id(conn, 'holidays', 'holiday_id')
        # An explicit column list, not `INSERT INTO holidays VALUES (...)`. v2.0
        # `holidays` has a sixth column (`location`, FR-HOL-01), so a bare VALUES
        # with five placeholders mis-targets every value and raises — the same
        # shape of defect that made `POST /api/goals` return 500 on every backend.
        try:
            conn.execute(
                'INSERT INTO holidays (holiday_id, name, holiday_date, year, type, location) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                [hid, name, when, when.year, htype, location],
            )
        except Exception as exc:
            if _is_holiday_duplicate(exc):
                # The index refused a concurrent duplicate. The pre-check cannot
                # see that, which is the whole reason the rule is a constraint.
                return jsonify({'error': 'A holiday with this name and date already '
                                         'exists for that location'}), 409
            raise
        audit_log(
            session['emp_id'], 'HOLIDAY_CREATE',
            f'Added {htype} holiday {name} on {when.isoformat()}'
            + (f' for {location}' if location else ' (org-wide)'),
            entity='holidays', entity_id=hid,
            after={'name': name, 'date': when.isoformat(), 'type': htype,
                   'location': location or None},
        )
        return jsonify({'message': 'Holiday added', 'id': hid, **_holiday_json(
            (hid, name, when, htype, location))}), 201
    finally:
        conn.close()


@app.route('/api/v1/holidays/<int:hid>', methods=['PUT'])
@app.route('/api/holidays/<int:hid>', methods=['PUT'])
@admin_required
def update_holiday(hid):
    """Edit a holiday. The "C" and "U" of CRUD, which did not exist.

    A partial update reads the existing row and validates the merged result, so
    omitting a field does not null it — the same contract `PUT /api/users` follows
    (see the FR-USR directory section). Changing a holiday's date re-derives
    ``year``, and an edited holiday is audited because attendance may have been
    finalised against the old date.
    """
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict) or not data:
        return jsonify({'error': 'A JSON object with at least one field is required'}), 400
    # Reject an unknown field on the *raw* body. Merging into a fixed dict first
    # would strip the offending key before check_payload ever saw it, so a typo
    # like `dat:` would be accepted and silently ignored - the same failure mode
    # as an edit that "succeeds" while changing nothing.
    allowed = {'name', 'date', 'type', 'location'}
    unknown = set(data) - allowed
    if unknown:
        return jsonify({'error': f'Unknown field(s): {", ".join(sorted(unknown))}'}), 400
    conn = get_db()
    try:
        current = conn.execute(
            'SELECT holiday_id, name, holiday_date, type, location FROM holidays '
            'WHERE holiday_id = ?', [hid],
        ).fetchone()
        if not current:
            return jsonify({'error': 'Holiday not found'}), 404
        merged = {
            'name': data.get('name', current[1]),
            'date': data.get('date', current[2]),
            'type': data.get('type', current[3]),
            'location': data.get('location', current[4]),
        }
        try:
            name, when, htype, location = holiday_calendar.check_payload(merged)
        except holiday_calendar.HolidayError as exc:
            return jsonify({'error': str(exc)}), exc.status
        existing = _holiday_duplicate_exists(conn, name, when, location, exclude_id=hid)
        if existing is not None:
            return jsonify({'error': 'A holiday with this name and date already exists '
                                     'for that location'}), 409
        try:
            conn.execute(
                'UPDATE holidays SET name = ?, holiday_date = ?, year = ?, type = ?, '
                'location = ? WHERE holiday_id = ?',
                [name, when, when.year, htype, location, hid],
            )
        except Exception as exc:
            if _is_holiday_duplicate(exc):
                return jsonify({'error': 'A holiday with this name and date already exists '
                                         'for that location'}), 409
            raise
    finally:
        conn.close()
    audit_log(session['emp_id'], 'HOLIDAY_UPDATE', f'Holiday {name} updated',
              entity='holidays', entity_id=hid,
              before={'name': current[1], 'date': current[2].isoformat(),
                      'type': current[3], 'location': current[4] or None},
              after={'name': name, 'date': when.isoformat(), 'type': htype,
                     'location': location})
    return jsonify({'message': 'Holiday updated', **_holiday_json(
        (hid, name, when, htype, location))}), 200


@app.route('/api/v1/holidays/<int:hid>', methods=['DELETE'])
@app.route('/api/holidays/<int:hid>', methods=['DELETE'])
@admin_required
def delete_holiday(hid):
    """Delete a holiday, refusing while employees have an opt-in for it.

    The opt-ins carry a foreign key to the holiday, so deleting one that has been
    opted into would raise — and the two ways out are both bad: silently deleting
    the opt-ins removes the evidence that an employee asked to take that day off,
    and a raw 500 tells the admin nothing. The refusal names the count so the next
    step is obvious.
    """
    conn = get_db()
    try:
        row = conn.execute(
            'SELECT holiday_id, name, holiday_date, type, location FROM holidays '
            'WHERE holiday_id = ?', [hid],
        ).fetchone()
        if not row:
            return jsonify({'error': 'Holiday not found'}), 404
        linked = 0
        if _attendance_table_exists(conn, 'holiday_optins'):
            linked = conn.execute(
                'SELECT COUNT(*) FROM holiday_optins WHERE holiday_id = ?', [hid],
            ).fetchone()[0]
        if linked:
            return jsonify({
                'error': f'{linked} employee(s) have an opt-in for {row[1]}, which is '
                         'the basis of their attendance for that day',
                'optins': linked,
                'hint': 'reject or let the employees withdraw the opt-ins first',
            }), 409
        conn.execute('DELETE FROM holidays WHERE holiday_id = ?', [hid])
    finally:
        conn.close()
    audit_log(session['emp_id'], 'HOLIDAY_DELETE', f'Holiday {row[1]} deleted',
              entity='holidays', entity_id=hid,
              before={'name': row[1], 'date': row[2].isoformat(), 'type': row[3]})
    return jsonify({'message': 'Deleted'}), 200


@app.route('/api/v1/holidays/copy-year', methods=['POST'])
@app.route('/api/holidays/copy-year', methods=['POST'])
@admin_required
def copy_holiday_year():
    """Copy one year's calendar into another (FR-HOL-02).

    The Feb-29 rule and the idempotency rule are `holiday_calendar.copy_plan`'s,
    not this route's: a 29 February holiday is **skipped and named in the
    response** rather than shifted onto another day, because silently moving a
    company holiday to a date nobody agreed to is worse than leaving it out, and
    "the 29th of February" means nothing on the 1st of March. A re-run skips what
    is already there and reports it, so a retried request converges.
    """
    data = request.get_json(silent=True) or {}
    source_year = data.get('from_year')
    target_year = data.get('to_year')
    for label, value in (('from_year', source_year), ('to_year', target_year)):
        if value is None:
            return jsonify({'error': f'{label} is required'}), 400
        bad = _holiday_filter_error('year', value)
        if bad:
            return bad
    if source_year == target_year:
        return jsonify({'error': 'from_year and to_year must differ'}), 400

    conn = get_db()
    try:
        source = conn.execute(
            'SELECT name, holiday_date, type, location FROM holidays WHERE year = ? '
            'ORDER BY holiday_date', [source_year],
        ).fetchall()
        if not source:
            return jsonify({'error': f'There are no holidays in {source_year} to copy'}), 404
        plan, skipped = holiday_calendar.copy_plan(source, target_year)
        created, already = 0, 0
        for name, when, htype, location in plan:
            if _holiday_duplicate_exists(conn, name, when, location) is not None:
                already += 1
                continue
            conn.execute(
                'INSERT INTO holidays (holiday_id, name, holiday_date, year, type, location) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                [_next_generated_id(conn, 'holidays', 'holiday_id'), name, when,
                 target_year, htype, location],
            )
            created += 1
    finally:
        conn.close()
    audit_log(session['emp_id'], 'HOLIDAY_COPY_YEAR',
              f'Copied {created} holiday(s) from {source_year} to {target_year}',
              entity='holidays', entity_id=None,
              after={'from_year': source_year, 'to_year': target_year, 'created': created,
                     'skipped': len(skipped), 'already_present': already})
    return jsonify({
        'message': f'Copied {created} holiday(s) from {source_year} to {target_year}',
        'from_year': source_year, 'to_year': target_year,
        'created': created,
        'already_present': already,
        # Named, not counted: "2 holidays were not copied" sends the admin back
        # to the calendar to work out which two.
        'skipped': [{'name': n, 'reason': r, 'detail': d} for n, r, d in skipped],
    }), 200


@app.route('/api/v1/holidays/export', methods=['GET'])
@app.route('/api/holidays/export', methods=['GET'])
@login_required
def export_holidays():
    """Export the calendar as CSV, in the shape `POST /api/holidays/import` reads."""
    year = request.args.get('year', type=int)
    conn = get_db()
    try:
        rows = _holiday_rows(conn, year=year, htype=request.args.get('type'),
                            location=request.args.get('location'))
    finally:
        conn.close()
    body = holiday_calendar.to_csv([(r[1], r[2], r[3], r[4]) for r in rows])
    return Response(
        body,
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="holidays_{year or "all"}.csv"'},
    )


@app.route('/api/v1/holidays/import', methods=['POST'])
@app.route('/api/holidays/import', methods=['POST'])
@admin_required
def import_holidays():
    """Import a holiday calendar from CSV.

    Per-row validation with a per-row reason and the spreadsheet row number, the
    same contract the user CSV import uses (FR-USR-04): a calendar is usually
    mostly good, and an admin needs to know which lines failed rather than a
    single abort that loses the rest. Rows that duplicate an existing holiday are
    reported as skipped rather than inserted, so re-running a corrected file
    converges instead of erroring.
    """
    upload = request.files.get('file')
    if upload is None:
        return jsonify({'error': 'A CSV file part named "file" is required'}), 400
    raw = upload.read(holiday_calendar.MAX_IMPORT_BYTES + 1)
    if len(raw) > holiday_calendar.MAX_IMPORT_BYTES:
        return jsonify({'error': f'The file is larger than '
                                 f'{holiday_calendar.MAX_IMPORT_BYTES // (1024 * 1024)} MB'}), 400
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        return jsonify({'error': 'The file must be UTF-8 encoded'}), 400
    try:
        rows, errors = holiday_calendar.parse_csv(text)
    except holiday_calendar.HolidayError as exc:
        return jsonify({'error': str(exc)}), exc.status

    conn = get_db()
    try:
        imported, skipped = 0, []
        for number, (name, when, htype, location) in rows:
            if _holiday_duplicate_exists(conn, name, when, location) is not None:
                skipped.append((number, f'{name} on {when.isoformat()} already exists'
                                       + (f' for {location}' if location else '')))
                continue
            conn.execute(
                'INSERT INTO holidays (holiday_id, name, holiday_date, year, type, location) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                [_next_generated_id(conn, 'holidays', 'holiday_id'), name, when,
                 when.year, htype, location],
            )
            imported += 1
    finally:
        conn.close()
    audit_log(session['emp_id'], 'HOLIDAY_IMPORT',
              f'Imported {imported} holiday(s), {len(skipped)} skipped, '
              f'{len(errors)} rejected', entity='holidays', entity_id=None,
              after={'imported': imported, 'skipped': len(skipped), 'errors': len(errors)})
    return jsonify({
        'message': f'Imported {imported} holiday(s)', 'imported': imported,
        'skipped': [{'row': n, 'reason': r} for n, r in skipped],
        'errors': [{'row': n, 'reason': r} for n, r in errors],
    }), 200


@app.route('/api/v1/holidays/ical', methods=['GET'])
@app.route('/api/holidays/ical', methods=['GET'])
@login_required
def holiday_ical_feed():
    """The calendar as an iCalendar feed (FR-HOL-02).

    A subscribeable URL, which is the point: an employee's own calendar shows the
    company's holidays without anyone maintaining a second copy.
    """
    year = request.args.get('year', datetime.now().year, type=int)
    bad = _holiday_filter_error('year', year)
    if bad:
        return bad
    conn = get_db()
    try:
        rows = _holiday_rows(conn, year=year, location=request.args.get('location'))
    finally:
        conn.close()
    name = request.args.get('calendar_name')
    body = holiday_calendar.to_ics(
        [(r[1], r[2], r[3], r[4]) for r in rows], name or f'HRMS Holidays {year}'
    )
    return Response(body, mimetype='text/calendar', headers={
        'Content-Disposition': f'inline; filename="holidays_{year}.ics"',
    })


# ── Optional-holiday opt-ins (FR-HOL-03) ───────────────────────────────
# `holiday_optins` exists in the canonical schema and `init_db` creates it on the
# compatibility shape, but nothing ever wrote to it. The consequence was not
# theoretical: the boot seed creates two Optional holidays (Diwali, Christmas),
# `_is_attendance_holiday` counts an Optional holiday only for an employee with an
# Approved opt-in, and with no route no employee could ever have one — so the
# nightly FR-JOB-01 finalisation classified a company holiday as `Weekly-off` on
# Diwali and would have said `Absent` on Christmas. A High-priority implemented
# requirement was producing a wrong answer because a Medium one had no route.


def _optin_row(conn, optin_id):
    """One opt-in with its holiday, for the approval queue and the owner's view."""
    return conn.execute(
        'SELECT o.optin_id, o.emp_id, o.holiday_id, o.status, o.created_at, '
        'h.name, h.holiday_date, h.type FROM holiday_optins o '
        'JOIN holidays h ON h.holiday_id = o.holiday_id WHERE o.optin_id = ?',
        [optin_id],
    ).fetchone()


def _optin_json(row):
    optin_id, emp_id, holiday_id, status, created_at, name, when, htype = row
    payload = {
        'optin_id': optin_id,
        'emp_id': emp_id,
        'holiday_id': holiday_id,
        'holiday': name,
        'holiday_date': when.isoformat() if hasattr(when, 'isoformat') else str(when),
        'holiday_type': htype,
        'status': status,
        'created_at': created_at.isoformat() if hasattr(created_at, 'isoformat') else created_at,
    }
    return payload


def _recompute_optin_attendance(emp_id, holiday_date):
    """Correct an already-finalised day whose classification just changed.

    Normally a no-op: opt-ins are refused for a holiday that has passed, so the
    nightly job has not run for that date yet and will pick the approval up by
    itself. It fires when an attendance row for the date already exists — for
    example a re-finalisation, or an admin editing the calendar around the nightly
    cut-off — and returning a stale `Absent` there would be a wrong record that
    nothing else would revisit.
    """
    conn = get_db()
    try:
        present = conn.execute(
            'SELECT 1 FROM attendance_days WHERE emp_id = ? AND attendance_date = ?',
            [emp_id, holiday_date],
        ).fetchone()
    finally:
        conn.close()
    if not present:
        return False
    finalize_attendance_for_date(holiday_date, employee_ids=[emp_id])
    return True


@app.route('/api/v1/holidays/<int:hid>/opt-in', methods=['POST'])
@app.route('/api/holidays/<int:hid>/opt-in', methods=['POST'])
@login_required
@idempotent
def request_holiday_optin(hid):
    """Ask to take an Optional holiday off (FR-HOL-03).

    The eligibility rules — Optional only, not already passed, no second active
    opt-in — are `holidays_optin.check_request`'s, so the queue and the tests get
    the same answer.
    """
    conn = get_db()
    try:
        holiday = conn.execute(
            'SELECT holiday_id, name, holiday_date, type FROM holidays WHERE holiday_id = ?',
            [hid],
        ).fetchone()
        if not holiday:
            return jsonify({'error': 'Holiday not found'}), 404
        emp_id = session['emp_id']
        existing = conn.execute(
            'SELECT optin_id, status FROM holiday_optins WHERE emp_id = ? AND holiday_id = ? '
            'ORDER BY created_at DESC, optin_id DESC',
            [emp_id, hid],
        ).fetchall()
        try:
            holidays_optin.check_request(holiday, existing[0] if existing else None)
        except holidays_optin.HolidayOptInError as exc:
            return jsonify({'error': str(exc)}), exc.status
        optin_id = _next_generated_id(conn, 'holiday_optins', 'optin_id')
        now = datetime.now()
        # The insert is conditional rather than checked-then-inserted, and mirrors
        # the canonical partial unique index exactly: `Rejected` is not active, so a
        # declined employee may ask again. DuckDB cannot build a partial index, so
        # on the compatibility shape this *is* the constraint.
        result = conn.execute(
            "INSERT INTO holiday_optins (optin_id, emp_id, holiday_id, status, created_at) "
            'SELECT ?, ?, ?, ?, ? WHERE NOT EXISTS ('
            "SELECT 1 FROM holiday_optins WHERE emp_id = ? AND holiday_id = ? "
            "AND status IN ('Pending', 'Approved'))",
            [optin_id, emp_id, hid, 'Pending', now, emp_id, hid],
        )
        if result.rowcount == 0:
            return jsonify({'error': f'You already have an active opt-in for {holiday[1]}'}), 409
    finally:
        conn.close()
    audit_log(session['emp_id'], 'HOLIDAY_OPTIN_REQUEST',
              f'Opt-in requested for {holiday[1]}', entity='holiday_optins', entity_id=optin_id)
    add_notification(
        session['emp_id'], 'HOLIDAY_OPTIN_REQUESTED',
        f'Your opt-in request for {holiday[1]} is waiting for HR approval.', '/leaves',
    )
    return jsonify({
        'message': 'Opt-in requested', 'optin_id': optin_id, 'status': 'Pending',
        'holiday': holiday[1],
    }), 201


@app.route('/api/v1/holidays/opt-ins/mine', methods=['GET'])
@app.route('/api/holidays/opt-ins/mine', methods=['GET'])
@login_required
def my_holiday_optins():
    conn = get_db()
    try:
        rows = conn.execute(
            'SELECT o.optin_id, o.emp_id, o.holiday_id, o.status, o.created_at, '
            'h.name, h.holiday_date, h.type FROM holiday_optins o '
            'JOIN holidays h ON h.holiday_id = o.holiday_id WHERE o.emp_id = ? '
            'ORDER BY h.holiday_date',
            [session['emp_id']],
        ).fetchall()
        return jsonify([_optin_json(r) for r in rows]), 200
    finally:
        conn.close()


@app.route('/api/v1/holidays/opt-ins/<int:oid>/cancel', methods=['POST'])
@app.route('/api/holidays/opt-ins/<int:oid>/cancel', methods=['POST'])
@login_required
def cancel_holiday_optin(oid):
    """Withdraw an opt-in before the holiday; the attendance is corrected."""
    conn = get_db()
    try:
        row = _optin_row(conn, oid)
        if not row:
            return jsonify({'error': 'Opt-in not found'}), 404
        emp_id, when, holiday_name = row[1], row[6], row[5]
        if emp_id != session['emp_id']:
            return jsonify({'error': 'You can only withdraw your own opt-in request'}), 403
        try:
            holidays_optin.check_cancel((row[0], row[3], when))
        except holidays_optin.HolidayOptInError as exc:
            return jsonify({'error': str(exc)}), exc.status
        was_approved = row[3] == 'Approved'
        result = conn.execute(
            "UPDATE holiday_optins SET status = 'Cancelled' WHERE optin_id = ? AND status = ?",
            [oid, row[3]],
        )
        if result.rowcount == 0:
            return jsonify({'error': 'That opt-in changed while you were withdrawing it'}), 409
    finally:
        conn.close()
    if was_approved:
        _recompute_optin_attendance(emp_id, when)
    audit_log(session['emp_id'], 'HOLIDAY_OPTIN_CANCEL',
              f'Opt-in for {holiday_name} withdrawn', entity='holiday_optins', entity_id=oid,
              before={'status': row[3]}, after={'status': 'Cancelled'})
    return jsonify({'message': 'Opt-in withdrawn', 'status': 'Cancelled'}), 200


@app.route('/api/v1/holidays/opt-ins', methods=['GET'])
@app.route('/api/holidays/opt-ins', methods=['GET'])
@hr_or_admin_required
def holiday_optin_queue():
    """The HR approval queue (FR-HOL-03). Pending by default."""
    status = request.args.get('status', 'Pending')
    if status not in holidays_optin.STATUSES:
        return jsonify({'error': f'status must be one of {", ".join(holidays_optin.STATUSES)}'}), 400
    conn = get_db()
    try:
        rows = conn.execute(
            'SELECT o.optin_id, o.emp_id, o.holiday_id, o.status, o.created_at, '
            'h.name, h.holiday_date, h.type FROM holiday_optins o '
            'JOIN holidays h ON h.holiday_id = o.holiday_id WHERE o.status = ? '
            'ORDER BY h.holiday_date, o.optin_id',
            [status],
        ).fetchall()
        return jsonify({'status': status, 'optins': [_optin_json(r) for r in rows]}), 200
    finally:
        conn.close()


@app.route('/api/v1/holidays/opt-ins/<int:oid>/approve', methods=['POST'])
@app.route('/api/holidays/opt-ins/<int:oid>/approve', methods=['POST'])
@hr_or_admin_required
def approve_holiday_optin(oid):
    return _review_holiday_optin(oid, 'Approved')


@app.route('/api/v1/holidays/opt-ins/<int:oid>/reject', methods=['POST'])
@app.route('/api/holidays/opt-ins/<int:oid>/reject', methods=['POST'])
@hr_or_admin_required
def reject_holiday_optin(oid):
    return _review_holiday_optin(oid, 'Rejected')


def _review_holiday_optin(oid, target):
    """Shared approve/reject body, so the two routes cannot drift apart."""
    conn = get_db()
    try:
        row = _optin_row(conn, oid)
        if not row:
            return jsonify({'error': 'Opt-in request not found'}), 404
        try:
            holidays_optin.check_review((row[0], row[3]), target)
        except holidays_optin.HolidayOptInError as exc:
            return jsonify({'error': str(exc)}), exc.status
        # Conditional on the state it was decided on, so two reviewers racing give
        # one winner and one 409 rather than a silent overwrite.
        result = conn.execute(
            'UPDATE holiday_optins SET status = ? WHERE optin_id = ? AND status = ?',
            [target, oid, row[3]],
        )
        if result.rowcount == 0:
            return jsonify({'error': 'That request was reviewed by someone else'}), 409
        emp_id, when, holiday_name = row[1], row[6], row[5]
    finally:
        conn.close()
    if target == 'Approved':
        _recompute_optin_attendance(emp_id, when)
    audit_log(session['emp_id'], f'HOLIDAY_OPTIN_{target.upper()}',
              f'Opt-in for {holiday_name} {target.lower()}', entity='holiday_optins',
              entity_id=oid, before={'status': 'Pending'}, after={'status': target})
    add_notification(
        emp_id, f'HOLIDAY_OPTIN_{target.upper()}',
        f'Your opt-in request for {holiday_name} was {target.lower()}.', '/leaves',
    )
    return jsonify({'message': f'Opt-in {target.lower()}', 'optin_id': oid, 'status': target}), 200


# ══════════════════════════════════════════════════════════════════════
#  ORG CHART
# ══════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════
#  NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════

def _notification_category(ntype):
    """FR-NOT-03 preference category for a notification type.

    Delegates to `notifications.category_for`. The previous implementation derived
    the category with substring tests, and the two never met the SRS's taxonomy:
    every leave notification was stored as `Leave` where the SRS says `Leaves`, so
    a preference keyed on `Leaves` would never have matched one, and tickets, goals,
    reviews and holiday opt-ins all fell through to `General` - which is to say
    `Tickets` had no producer at all, and a preference screen built on this would
    have been a set of switches that did nothing.
    """
    return notifications.category_for(ntype)


def _effective_notification_preferences(conn, emp_id):
    """The employee's effective preferences, defaults applied."""
    try:
        rows = conn.execute(
            'SELECT category, in_app, email FROM notification_preferences WHERE emp_id = ?',
            [emp_id],
        ).fetchall()
    except Exception:
        # A backend without the table answers with the defaults rather than failing
        # every notification in the system.
        return notifications.effective_for([])
    return notifications.effective_for(rows)


def add_notification(emp_id, ntype, message, link=None, category=None):
    """Record an in-app notification, unless the employee switched that category off.

    FR-NOT-03: `{in_app, email}` per category, default true. A category the employee
    has turned off in-app produces no row - the *fact* is unaffected, it is only the
    in-app delivery that is suppressed, and whatever raised the notification owns
    the record of it.

    Returns True when a row was written, so a caller can tell a suppressed
    notification from a delivered one instead of inferring it from silence.
    """
    conn = None
    try:
        conn = get_db()
        if category is None:
            category = _notification_category(ntype)
        if not notifications.wants_in_app(
            _effective_notification_preferences(conn, emp_id), category
        ):
            return False
        conn.execute(
            "INSERT INTO notifications (notification_id, emp_id, type, category, message, related_link, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [_next_generated_id(conn, 'notifications', 'notification_id'), emp_id, ntype, category, message, link, datetime.now()]
        )
        return True
    except Exception as e:
        logger.warning("Notification failed: %s", e)
    finally:
        if conn:
            conn.close()
    return False


@app.route('/api/v1/notifications', methods=['GET'])
@app.route('/api/notifications', methods=['GET'])
@login_required

def get_notifications():
    conn = get_db()
    rows = conn.execute(
        "SELECT notification_id, type, category, message, related_link, is_read, created_at FROM notifications WHERE emp_id = ? ORDER BY created_at DESC LIMIT 50",
        [session['emp_id']]
    ).fetchall()
    unread = conn.execute("SELECT COUNT(*) FROM notifications WHERE emp_id = ? AND is_read = 0", [session['emp_id']]).fetchone()[0]
    conn.close()
    return jsonify({
        'unread': unread,
        'data': [{'id': r[0], 'type': r[1], 'category': r[2], 'message': r[3], 'link': r[4], 'is_read': bool(r[5]), 'created_at': r[6].isoformat() if r[6] else None} for r in rows]
    }), 200


@app.route('/api/v1/notifications/read', methods=['POST'])
@app.route('/api/notifications/read', methods=['POST'])
@login_required
def mark_notifications_read():
    # Deliberately unaudited, and one of only two exemptions on the FR-AUD-01
    # known-gap list. It is a read receipt on the caller's **own** notifications: a
    # self-service action with no forensic value, and auditing it would write a row
    # per click for as long as the table exists. The SRS's "every mutating action"
    # is not a reason to log a user clearing their own badge — a row here would be
    # noise that makes the real entries harder to find, which is the opposite of what
    # an audit trail is for.
    conn = get_db()
    conn.execute("UPDATE notifications SET is_read = 1 WHERE emp_id = ?", [session['emp_id']])
    conn.close()
    return jsonify({'message': 'Marked read'}), 200


# ── Notification preferences (FR-NOT-03) ─────────────────────────────────
# "Preferences per category (Onboarding, Leaves, Expenses, Tickets, Payroll,
# Tickets-SLA), {in_app, email} each, default true."
#
# The routes are deliberately thin: `notifications.check_payload` validates and
# merges, so a partial body answers the same as a full one and an absent category
# keeps its stored value rather than reverting to the default. That matters for the
# same reason it does on `PUT /api/users` — a client sending one switch must not
# reset the other seven.

@app.route('/api/v1/notification-preferences', methods=['GET'])
@app.route('/api/notification-preferences', methods=['GET'])
@login_required
def get_notification_preferences():
    """The caller's effective preferences, with the taxonomy described.

    `has_producer` is reported per category so an employee toggling `Tickets-SLA` —
    which FR-TKT-01 will populate and nothing does today — is told that, rather
    than left to conclude the switch is broken. The same honesty applies to
    `Expenses`, which has no notification producer yet.
    """
    conn = get_db()
    try:
        effective = _effective_notification_preferences(conn, session['emp_id'])
        stored = {
            row[0] for row in conn.execute(
                'SELECT category FROM notification_preferences WHERE emp_id = ?',
                [session['emp_id']],
            ).fetchall()
        }
    finally:
        conn.close()
    preferences = notifications.ordered(effective)
    return jsonify({
        'preferences': preferences,
        'channels': list(notifications.CHANNELS),
        'default': 'true - a category with no stored row is on',
        'taxonomy': notifications.describe(),
        # The categories the employee has actually touched, so a client can
        # distinguish "I set this to the default" from "I never touched this".
        'customised': [c for c in preferences if c in stored],
    }), 200


@app.route('/api/v1/notification-preferences', methods=['PUT'])
@app.route('/api/notification-preferences', methods=['PUT'])
@login_required
def update_notification_preferences():
    """Set one or more categories. A full replace of the categories named, not of
    the whole set — see the note above."""
    data = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        current = _effective_notification_preferences(conn, session['emp_id'])
        try:
            effective = notifications.check_payload(data, current)
        except notifications.PreferenceError as exc:
            return jsonify({'error': str(exc)}), exc.status
        changed = []
        for category, channels in effective.items():
            if channels == current.get(category):
                continue
            conn.execute(
                'DELETE FROM notification_preferences WHERE emp_id = ? AND category = ?',
                [session['emp_id'], category],
            )
            conn.execute(
                'INSERT INTO notification_preferences '
                '(pref_id, emp_id, category, in_app, email, updated_at) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                [_next_generated_id(conn, 'notification_preferences', 'pref_id'),
                 session['emp_id'], category, int(channels['in_app']),
                 int(channels['email']), datetime.now()],
            )
            changed.append(category)
    finally:
        conn.close()
    audit_log(session['emp_id'], 'NOTIFICATION_PREFERENCES_UPDATE',
              f'Notification preferences updated: {", ".join(changed) or "none"}',
              entity='notification_preferences', entity_id=session['emp_id'],
              before={c: current.get(c) for c in (changed or [])},
              after={c: effective.get(c) for c in (changed or [])})
    return jsonify({
        'message': 'Preferences updated',
        'preferences': notifications.ordered(effective),
        'changed': changed,
        # Stated plainly rather than left for someone to discover: the email channel
        # is stored and reported, and nothing consumes it yet, because the app has
        # no automatic email delivery path (POST /api/send-notification-email is a
        # manual admin endpoint). A client must not promise a user an email that no
        # code will send.
        'email_delivery': 'stored only — this build has no automatic email delivery',
    }), 200


# ══════════════════════════════════════════════════════════════════════
#  REGULARIZATION
# ══════════════════════════════════════════════════════════════════════

@app.route('/api/v1/regularization', methods=['GET', 'POST'])
@app.route('/api/regularization', methods=['GET', 'POST'])
@login_required
@idempotent
def regularization_api():
    emp_id = session['emp_id']
    if request.method == 'GET':
        # FR-REG-01 — "list with pending_my_approval, status, month filters; manager
        # view includes delegated reports (FR-LEA-08a)". None of the three existed: the
        # route answered either *every* request for a `can_view_all` actor or the
        # caller's own, and read no query argument at all, while the matrix note said
        # "filters and the company-wide/self split ship". The manager view is the same
        # `_pending_my_approval_clause` the leave list uses, so "who may decide" has one
        # answer on both surfaces — a delegate sees their delegator's reports' requests
        # here and on `/api/leaves`, or neither.
        status_filter = request.args.get('status')
        month_filter = request.args.get('month', type=int)
        year_filter = request.args.get('year', type=int)
        pending_my_approval = request.args.get(
            'pending_my_approval', '').lower() in ('1', 'true', 'yes')
        conn = get_db()
        conditions, params = [], []
        if pending_my_approval:
            clause, extra = _pending_my_approval_clause(conn, emp_id)
            if clause is None:
                conn.close()
                return jsonify([]), 200
            conditions.append(clause)
            params.extend(extra)
        elif not policy.can_view_all(
            policy.current_actor(conn), 'regularization', conn=conn
        ):
            conditions.append('emp_id = ?')
            params.append(emp_id)
        if status_filter:
            conditions.append('status = ?')
            params.append(status_filter)
        if month_filter:
            conditions.append("CAST(strftime('%m', request_date) AS INTEGER) = ?")
            params.append(month_filter)
        if year_filter:
            conditions.append("CAST(strftime('%Y', request_date) AS INTEGER) = ?")
            params.append(year_filter)
        query = ('SELECT request_id, emp_id, request_date, reason, status, '
                 'approved_by, created_at FROM regularization_requests')
        if conditions:
            query += ' WHERE ' + ' AND '.join(conditions)
        query += ' ORDER BY created_at DESC'
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return jsonify([{
            'id': r[0], 'emp_id': r[1], 'date': r[2].isoformat(),
            'reason': r[3], 'status': r[4], 'approved_by': r[5],
            'created_at': r[6].isoformat() if r[6] else None
        } for r in rows]), 200

    data = request.get_json(silent=True) or {}
    d = parse_date(data.get('date'))
    if not d or not data.get('reason'):
        return jsonify({'error': 'date and reason required'}), 400
    conn = get_db()
    if conn.execute(
        "SELECT 1 FROM regularization_requests WHERE emp_id = ? AND request_date = ? AND status = 'Pending'",
        [emp_id, d]
    ).fetchone():
        conn.close()
        return jsonify({'error': 'A pending request already exists for this date'}), 409
    rid = _next_generated_id(conn, 'regularization_requests', 'request_id')
    conn.execute(
        "INSERT INTO regularization_requests (request_id, emp_id, request_date, reason, status) VALUES (?, ?, ?, ?, 'Pending')",
        [rid, emp_id, d, data['reason']]
    )
    conn.close()
    # The employee-side half of the pair the approve/reject audit rows belong to. A
    # regularization request is what authorises an attendance correction, so the
    # request and the decision both belong on the record.
    audit_log(
        emp_id, 'REGULARIZATION_REQUEST',
        f'Requested a correction for {d}',
        entity='regularization_requests', entity_id=rid,
        after={'emp_id': emp_id, 'request_date': str(d), 'status': 'Pending'},
    )
    return jsonify({'message': 'Request submitted', 'id': rid}), 201


@app.route('/api/v1/regularization/<int:rid>/approve', methods=['POST'])
@app.route('/api/regularization/<int:rid>/approve', methods=['POST'])
@reporting_line_required
def approve_regularization(rid):
    """Approve a regularization request (FR-REG-03) on behalf of the right actor.

    The gate was `@admin_required` — the same defect FR-LEA-04 has on leave. FR-REG-01
    already describes a *manager* acting on this queue ("manager view includes
    delegated reports (FR-LEA-08a)"), so keeping the route admin-only made that view a
    dead end and would have left the delegation consultation unreachable behind an
    admin door. Admitting a manager, HR or an administrator, then deciding **which**
    employee per request, matches what the list already says about who sees it.

    Attendance correction is a deliberate widening and it is recorded as one: a Team
    Leader may now correct their own report's timesheet, which is what "manager view"
    implies, and `_approval_denial` still refuses their own request.
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT emp_id, request_date, status FROM regularization_requests "
            "WHERE request_id = ?", [rid],
        ).fetchone()
        if not row:
            return jsonify({'error': 'Regularization request not found'}), 404
        if row[2] != 'Pending':
            # The decision has already been made, so there is nothing to approve.
            # This used to fall through to `200 {"message": "Approved"}`, which
            # made a no-op indistinguishable from a real approval — and the same
            # shape on the reject route answered "Rejected" for a request that was
            # still sitting there as Approved. A client checking `status_code`
            # would have believed a decision it had not made.
            return jsonify({
                'error': f'Request is already {row[2].lower()}',
                'status': row[2],
            }), 409
        denial = _approval_denial(conn, session['emp_id'], row[0])
        if denial is not None:
            return denial
        note = delegations.approval_note(conn, session['emp_id'], row[0])
        # Conditional on `status = 'Pending'` (CC-04), so two approvers racing give one
        # winner and one 409 rather than two successes. The UPDATE has been conditional
        # since the always-200 fix but nobody looked at the rowcount, so the race it
        # guarded against was still two successes.
        claimed = conn.execute(
            "UPDATE regularization_requests SET status = 'Approved', approved_by = ?, "
            "updated_at = ? WHERE request_id = ? AND status = 'Pending'",
            [session['emp_id'], datetime.now(), rid],
        )
        if not getattr(claimed, 'rowcount', 1):
            return jsonify({'error': 'Request was reviewed by someone else'}), 409
    finally:
        conn.close()
    audit_log(
        session['emp_id'], 'REGULARIZATION_APPROVE',
        f'Approved regularization request {rid} for {row[0]}'
        + (f' ({note})' if note else ''), entity='regularization_requests',
        entity_id=rid, before={'status': 'Pending'}, after={'status': 'Approved'},
    )
    # FR-JOB-01/FR-REG-03: a later approved correction recomputes only
    # the affected employee/date, rather than waiting for the next night.
    try:
        finalize_attendance_for_date(row[1], employee_ids=[row[0]])
    except Exception as exc:
        logger.warning('attendance recompute after regularization failed: %s', exc)
    return jsonify({'message': 'Approved', 'status': 'Approved'}), 200


@app.route('/api/v1/regularization/<int:rid>/reject', methods=['POST'])
@app.route('/api/regularization/<int:rid>/reject', methods=['POST'])
@reporting_line_required
def reject_regularization(rid):
    """Reject a regularization request. The same actor rule as approve — a queue whose
    two decisions are reachable by different people is a queue where rejecting is
    harder than approving, and FR-REG-03 gives both to one actor."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT emp_id, request_date, status FROM regularization_requests "
            "WHERE request_id = ?", [rid],
        ).fetchone()
        if not row:
            return jsonify({'error': 'Regularization request not found'}), 404
        if row[2] != 'Pending':
            return jsonify({
                'error': f'Request is already {row[2].lower()}',
                'status': row[2],
            }), 409
        denial = _approval_denial(conn, session['emp_id'], row[0])
        if denial is not None:
            return denial
        note = delegations.approval_note(conn, session['emp_id'], row[0])
        claimed = conn.execute(
            "UPDATE regularization_requests SET status = 'Rejected', approved_by = ?, "
            "updated_at = ? WHERE request_id = ? AND status = 'Pending'",
            [session['emp_id'], datetime.now(), rid],
        )
        if not getattr(claimed, 'rowcount', 1):
            return jsonify({'error': 'Request was reviewed by someone else'}), 409
    finally:
        conn.close()
    audit_log(
        session['emp_id'], 'REGULARIZATION_REJECT',
        f'Rejected regularization request {rid} for {row[0]}'
        + (f' ({note})' if note else ''), entity='regularization_requests',
        entity_id=rid, before={'status': 'Pending'}, after={'status': 'Rejected'},
    )
    return jsonify({'message': 'Rejected', 'status': 'Rejected'}), 200



@app.route('/api/users/<emp_id>/anonymise', methods=['POST'])
@admin_required
def propose_anonymisation(emp_id):
    """Propose the anonymisation of an archived employee (FR-USR, first of two).

    Nothing is erased here. ``?dry_run=1`` returns the plan without even
    creating a request, which is the safety net that makes the operation
    reviewable before a second person is asked to approve it.
    """
    dry_run = request.args.get('dry_run', '').lower() in ('1', 'true', 'yes', 'on')
    conn = get_db()
    try:
        if dry_run:
            return jsonify({'dry_run': True, 'plan': anonymise.plan(conn, emp_id)}), 200
        request_row = anonymise.create_request(
            conn, emp_id, session['emp_id'],
            detail=(request.get_json(silent=True) or {}).get('reason'),
        )
    except anonymise.AnonymisationError as exc:
        return jsonify({'error': str(exc)}), exc.status
    finally:
        conn.close()
    audit_log(
        session['emp_id'], 'USER_ANONYMISATION_REQUESTED',
        f"Anonymisation proposed for {request_row['emp_id']} (request {request_row['request_id']})",
        entity='anonymisation_requests', entity_id=request_row['request_id'],
        after={'status': request_row['status']},
    )
    return jsonify(request_row), 201


@app.route('/api/anonymisation/<int:request_id>', methods=['GET'])
@admin_required
def anonymisation_status(request_id):
    conn = get_db()
    try:
        row = anonymise.get_request(conn, request_id)
    finally:
        conn.close()
    if not row:
        return jsonify({'error': 'Anonymisation request not found'}), 404
    return jsonify(row), 200


@app.route('/api/anonymisation', methods=['GET'])
@admin_required
def anonymisation_list():
    """Recent anonymisation requests (the audit view an admin needs)."""
    limit = request.args.get('limit', 20, type=int)
    conn = get_db()
    try:
        return jsonify({'requests': anonymise.list_requests(conn, limit)}), 200
    finally:
        conn.close()


@app.route('/api/anonymisation/<int:request_id>/confirm', methods=['POST'])
@admin_required
def confirm_anonymisation(request_id):
    """Second approver. The system applies the erasure from here on.

    The confirmer must be a different person from the requester — that is the
    two-person control, and it is enforced in the module, not the UI. Applying is
    idempotent, so a retry after a crash converges instead of double-erasing.
    """
    conn = get_db()
    try:
        subject = conn.execute(
            'SELECT emp_id FROM anonymisation_requests WHERE request_id = ?', [request_id]
        ).fetchone()
        try:
            row = anonymise.confirm_and_apply(conn, request_id, session['emp_id'])
        except anonymise.AnonymisationError as exc:
            return jsonify({'error': str(exc)}), exc.status
    finally:
        conn.close()
    # The audit row records the *fact* of the erasure and the counts. It must
    # never carry an erased value, or the control would defeat itself.
    audit_log(
        session['emp_id'], 'USER_ANONYMISED',
        f"Anonymised {row['emp_id']} (request {request_id}, confirmed by "
        f"{row['confirmed_by']}); audit history of the subject was value-scrubbed",
        entity='anonymisation_requests', entity_id=request_id,
        before={'status': 'archived'},
        after={
            'emp_id': row['emp_id'],
            'status': 'anonymised',
            'rows': (row.get('result') or {}).get('rows'),
            'erased_fields': (row.get('result') or {}).get('erased_fields'),
            'kept_fields': (row.get('result') or {}).get('kept_fields'),
            'unsrubbed_free_text': (row.get('result') or {}).get('unsrubbed_free_text'),
        },
    )
    if subject:
        _revoke_redis_sessions(subject[0])
    return jsonify(row), 200


@app.route('/api/anonymisation/<int:request_id>/cancel', methods=['POST'])
@admin_required
def cancel_anonymisation(request_id):
    conn = get_db()
    try:
        row = anonymise.cancel_request(conn, request_id, session['emp_id'])
    except anonymise.AnonymisationError as exc:
        return jsonify({'error': str(exc)}), exc.status
    finally:
        conn.close()
    audit_log(
        session['emp_id'], 'USER_ANONYMISATION_CANCELLED',
        f'Anonymisation request {request_id} cancelled',
        entity='anonymisation_requests', entity_id=request_id,
        after={'status': row['status']},
    )
    return jsonify(row), 200

# ══════════════════════════════════════════════════════════════════════
#  CSV IMPORT
# ══════════════════════════════════════════════════════════════════════

def _csv_value(row, key, default=''):
    """Read a CSV cell as text, mapping missing/NaN cells to the default."""
    value = row.get(key, default)
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return default
    return str(value).strip()


@app.route('/api/v1/users/import', methods=['POST'])
@app.route('/api/users/import', methods=['POST'])
@admin_required
@idempotent
def import_users_csv():
    """Queue a CSV user import (FR-USR-04 / FR-USR-10).

    The upload is validated (header, size, row count) and stored, a job row is
    recorded and the caller gets ``202`` with the job id. A dispatcher processes
    one job at a time, so a large file no longer holds a web worker open, and
    the outcome stays inspectable at the progress endpoint the SRS names,
    ``GET /api/imports/<job_id>``.
    """
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    try:
        job = imports.create_job(request.files['file'], session['emp_id'])
    except imports.ImportError_ as exc:
        return jsonify({'error': str(exc)}), exc.status
    audit_log(
        session['emp_id'], 'USER_IMPORT_QUEUED',
        f"Queued CSV import {job['job_id']} ({job['filename']}, {job['total_rows']} rows)",
        entity='import_jobs', entity_id=job['job_id'],
        after={'total_rows': job['total_rows'], 'filename': job['filename']},
    )
    return jsonify({
        'message': f"Import queued: {job['total_rows']} rows",
        'job_id': job['job_id'],
        'status': job['status'],
        'total_rows': job['total_rows'],
        # The SRS names the progress endpoint `GET /api/imports/<job_id>`, so
        # that is what the response advertises. The older
        # `/api/users/import/<job_id>` path stays as an alias for existing
        # pollers rather than being removed under them.
        'poll': f"/api/imports/{job['job_id']}",
    }), 202


@app.route('/api/v1/imports/<int:job_id>', methods=['GET'])
@app.route('/api/imports/<int:job_id>', methods=['GET'])
@app.route('/api/v1/users/import/<int:job_id>', methods=['GET'])
@app.route('/api/users/import/<int:job_id>', methods=['GET'])
@admin_required
def import_job_status(job_id):
    """Progress and outcome of one import job.

    FR-USR-10 names this as ``GET /api/imports/<job_id>``; the older
    ``/api/users/import/<job_id>`` path is kept as an alias so a client that
    polls the previous URL keeps working.
    """
    job = imports.get_job(job_id)
    if not job:
        return jsonify({'error': 'Import job not found'}), 404
    return jsonify(job), 200


@app.route('/api/v1/users/import', methods=['GET'])
@app.route('/api/users/import', methods=['GET'])
@admin_required
def import_job_list():
    """Most recent import jobs, newest first."""
    limit = request.args.get('limit', 20, type=int)
    return jsonify({'jobs': imports.list_jobs(limit)}), 200


@app.route('/api/v1/users/import/<int:job_id>/cancel', methods=['POST'])
@app.route('/api/users/import/<int:job_id>/cancel', methods=['POST'])
@admin_required
def cancel_import_job(job_id):
    """Cancel a job that has not been picked up by the dispatcher yet."""
    job = imports.cancel_job(job_id, session['emp_id'])
    if job is None:
        existing = imports.get_job(job_id)
        if not existing:
            return jsonify({'error': 'Import job not found'}), 404
        return jsonify({
            'error': f"Job is {existing['status']} and can no longer be cancelled",
        }), 409
    audit_log(
        session['emp_id'], 'IMPORT_JOB_CANCEL',
        f'Cancelled queued import job {job_id}',
        entity='import_jobs', entity_id=job_id,
        before={'status': 'pending'}, after={'status': job.get('status')},
    )
    return jsonify(job), 200


@app.route('/api/v1/users/import/<int:job_id>/run', methods=['POST'])
@app.route('/api/users/import/<int:job_id>/run', methods=['POST'])
@admin_required
def run_import_job(job_id):
    """Process one queued import now instead of waiting for the next tick.

    The claim is the same conditional transition the dispatcher uses, so this is
    safe to press twice and safe against the scheduler running concurrently.
    """
    job = imports.dispatch_job(job_id)
    if not job:
        return jsonify({'error': 'Import job not found'}), 404
    audit_log(
        session['emp_id'], 'IMPORT_JOB_RUN',
        f'Ran queued import job {job_id} on demand',
        entity='import_jobs', entity_id=job_id,
        after={'status': job.get('status'), 'imported': job.get('imported'),
               'skipped': job.get('skipped')},
    )
    return jsonify(job), 200


@app.route('/api/v1/accrual/run', methods=['POST'])
@app.route('/api/accrual/run', methods=['POST'])
@admin_required
def run_leave_accrual_route():
    """Accrue leave for every policy holder now (FR-LEA-08).

    Idempotent per (employee, leave type, year, month), so this is the same
    safe operation the monthly job runs — useful after a policy is assigned
    mid-year, and safe to press twice.
    """
    conn = get_db()
    try:
        result = leave_accrual.run_accrual(conn)
    finally:
        conn.close()
    audit_log(
        session['emp_id'], 'LEAVE_ACCRUAL_RUN',
        f"Leave accrual: {result['grants']} grant(s), {result['days']} day(s) "
        f"across {result['employees']} employee(s)",
        entity='monthly_leave_grants', entity_id=None,
        after=result,
    )
    return jsonify(result), 200


def run_leave_accrual():
    """Scheduler job: credit the months that have happened (day 1, 00:30 IST)."""
    conn = get_db()
    try:
        return leave_accrual.run_accrual(conn)
    except Exception as exc:
        logger.warning('leave accrual failed: %s', exc)
        return None
    finally:
        conn.close()


def run_import_dispatch():
    """Scheduler job: process one queued import job (FR-USR-04)."""
    try:
        return imports.dispatch_once()
    except Exception as exc:
        logger.warning('import dispatch failed: %s', exc)
        return None



# ══════════════════════════════════════════════════════════════════════
#  PHASE 2 — ASSET MANAGEMENT
# ══════════════════════════════════════════════════════════════════════

@app.route('/admin/assets')
@hr_or_admin_required
def admin_assets():
    return render_template('assets.html')


@app.route('/api/v1/my-assets')
@app.route('/api/my-assets')
@login_required
def my_assets():
    conn = get_db()
    rows = conn.execute("SELECT asset_id, asset_type, asset_tag, brand, model, serial_number, issued_date, return_date, status, notes FROM assets WHERE emp_id = ? ORDER BY issued_date DESC", [session['emp_id']]).fetchall()
    conn.close()
    return jsonify([{'id': r[0], 'type': r[1], 'tag': r[2], 'brand': r[3], 'model': r[4], 'serial': r[5], 'issued': r[6].isoformat() if r[6] else None, 'returned': r[7].isoformat() if r[7] else None, 'status': r[8], 'notes': r[9]} for r in rows]), 200


@app.route('/api/v1/assets', methods=['GET', 'POST'])
@app.route('/api/assets', methods=['GET', 'POST'])
@admin_required
def assets_api():
    if request.method == 'GET':
        emp = request.args.get('emp_id')
        conn = get_db()
        if emp:
            rows = conn.execute("SELECT a.asset_id, a.emp_id, u.name, a.asset_type, a.asset_tag, a.brand, a.model, a.serial_number, a.issued_date, a.return_date, a.status, a.notes FROM assets a JOIN users u ON a.emp_id = u.emp_id WHERE a.emp_id = ? ORDER BY a.issued_date DESC", [emp]).fetchall()
        else:
            rows = conn.execute("SELECT a.asset_id, a.emp_id, u.name, a.asset_type, a.asset_tag, a.brand, a.model, a.serial_number, a.issued_date, a.return_date, a.status, a.notes FROM assets a JOIN users u ON a.emp_id = u.emp_id ORDER BY a.issued_date DESC").fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'type': r[3], 'tag': r[4], 'brand': r[5], 'model': r[6], 'serial': r[7], 'issued': r[8].isoformat() if r[8] else None, 'returned': r[9].isoformat() if r[9] else None, 'status': r[10], 'notes': r[11]} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('emp_id') or not data.get('asset_type'):
        return jsonify({'error': 'emp_id and asset_type required'}), 400
    conn = get_db()
    employee = conn.execute("SELECT 1 FROM users WHERE emp_id = ? AND status = 'Active'", [data['emp_id']]).fetchone()
    if not employee:
        conn.close()
        return jsonify({'error': 'Employee not found or inactive'}), 400
    aid = _next_generated_id(conn, 'assets', 'asset_id')
    conn.execute(
        # An explicit column list. `assets` has eleven columns in this order on both
        # schemas today, so a bare VALUES would work right up until someone adds one —
        # the latent version of the goals and add_holiday defects.
        'INSERT INTO assets (asset_id, emp_id, asset_type, asset_tag, brand, model, '
        'serial_number, issued_date, return_date, status, notes) '
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Issued', ?)",
        # Ten parameters for eleven columns: `status` is the SQL literal above, so
        # it takes no parameter, but `return_date` is still a placeholder and gets
        # its explicit None. Getting that count wrong is the very class of defect
        # this edit exists to remove — the first version of this line had nine
        # parameters for ten placeholders and raised on every call.
        [aid, data['emp_id'], data['asset_type'], data.get('asset_tag'),
         data.get('brand'), data.get('model'), data.get('serial_number'),
         parse_date(data.get('issued_date'), datetime.now().date()),
         None, data.get('notes')],
    )
    conn.close()
    # `status` is a literal rather than a parameter: an asset is created Issued, and
    # saying so in the SQL is clearer than passing `'Issued'` and hoping the caller's
    # dict cannot drift.
    audit_log(
        session['emp_id'], 'ASSET_ISSUE',
        f'Issued {data["asset_type"]} asset {aid} to {data["emp_id"]}'
        + (f' (tag {data["asset_tag"]})' if data.get('asset_tag') else ''),
        entity='assets', entity_id=aid,
        after={'emp_id': data['emp_id'], 'asset_type': data['asset_type'],
               'asset_tag': data.get('asset_tag'), 'serial_number': data.get('serial_number'),
               'status': 'Issued'},
    )
    return jsonify({'message': 'Asset issued', 'id': aid}), 201


@app.route('/api/v1/assets/<int:aid>/return', methods=['POST'])
@app.route('/api/assets/<int:aid>/return', methods=['POST'])
@admin_required
def return_asset(aid):
    conn = get_db()
    info = conn.execute(
        "SELECT emp_id, asset_type, asset_tag, status, return_date FROM assets "
        "WHERE asset_id = ?", [aid],
    ).fetchone()
    if not info:
        conn.close()
        return jsonify({'error': 'Asset not found'}), 404
    if info[3] == 'Returned':
        # Not a success. This used to update unconditionally and answer 200
        # {"message": "Asset returned"} whether or not it returned anything — the
        # always-200 lie, third sighting. A returned asset is a custody record:
        # telling an admin it came back when it did not is how a laptop goes
        # missing quietly.
        conn.close()
        returned_on = info[4].isoformat() if info[4] else 'an earlier date'
        return jsonify({
            'error': f'Asset was already returned on {returned_on}',
            'status': 'Returned', 'return_date': returned_on,
        }), 409
    conn.execute(
        "UPDATE assets SET return_date = ?, status = 'Returned' "
        "WHERE asset_id = ? AND status <> 'Returned'",
        [datetime.now().date(), aid],
    )
    conn.close()
    audit_log(
        session['emp_id'], 'ASSET_RETURN',
        f'Recorded return of {info[1]} asset {aid} from {info[0]}'
        + (f' (tag {info[2]})' if info[2] else ''),
        entity='assets', entity_id=aid,
        before={'status': info[3], 'return_date': None},
        after={'status': 'Returned'},
    )
    return jsonify({'message': 'Asset returned'}), 200


# ══════════════════════════════════════════════════════════════════════
#  CORRECTED LIFECYCLE HELPERS (FR-ATS / FR-ONB / FR-OFF)
# ══════════════════════════════════════════════════════════════════════

ONBOARDING_REQUIRED_DOCS = (
    'ID Proof',
    'Address Proof',
    'Education',
    'Certification',
    'Bank Details',
)
PREBOARDING_TOKEN_MAX_AGE = 14 * 24 * 60 * 60
_PREBOARDING_SALT = 'hrms-preboarding-v1'


class LifecycleError(Exception):
    """Domain error raised inside a lifecycle transaction."""

    def __init__(self, status_code, message, **details):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.details = details


def _lifecycle_error_payload(error):
    payload = {'error': error.message}
    if error.details:
        payload['details'] = error.details
    return payload


def _iso(value):
    return value.isoformat() if hasattr(value, 'isoformat') else value


def _preboarding_serializer():
    return URLSafeTimedSerializer(app.secret_key, salt=_PREBOARDING_SALT)


def _issue_preboarding_token(workflow_id):
    return _preboarding_serializer().dumps({'workflow_id': int(workflow_id)})


def _decode_preboarding_token(token):
    try:
        payload = _preboarding_serializer().loads(token, max_age=PREBOARDING_TOKEN_MAX_AGE)
    except SignatureExpired as exc:
        raise LifecycleError(410, 'Pre-boarding link has expired') from exc
    except BadSignature as exc:
        raise LifecycleError(401, 'Invalid pre-boarding link') from exc
    if not isinstance(payload, dict) or not payload.get('workflow_id'):
        raise LifecycleError(401, 'Invalid pre-boarding link')
    try:
        return int(payload['workflow_id'])
    except (TypeError, ValueError) as exc:
        raise LifecycleError(401, 'Invalid pre-boarding link') from exc


def _candidate_stage(value):
    """Normalise the legacy seed spelling without weakening the state machine."""
    return 'Screened' if value == 'Screening' else value


def _validate_candidate_transition(current, target):
    current = _candidate_stage(current)
    if target in ('Hired', 'Offered'):
        raise LifecycleError(409, 'Hired and Offered are reached through their guarded workflows')
    if target not in ('Screened', 'Interviewed', 'Rejected', 'Withdrawn'):
        raise LifecycleError(400, 'Invalid candidate status')
    if current in ('Hired', 'Rejected', 'Withdrawn'):
        raise LifecycleError(409, 'Candidate is already in a terminal state')
    if current == 'Offered':
        raise LifecycleError(409, 'Use the offer accept/reject endpoint to decide an Offered candidate')
    if target in ('Rejected', 'Withdrawn'):
        return target
    expected = {'Applied': 'Screened', 'Screened': 'Interviewed'}.get(current)
    if expected != target:
        raise LifecycleError(409, f'Invalid candidate transition: {current} -> {target}')
    return target


def _next_generated_id(conn, table, column):
    """Allocate an ID without regressing a public identity sequence.

    Legacy schemas need explicit IDs. On v2.0 ``public``, consume the
    PostgreSQL identity sequence directly; this is concurrency-safe and keeps
    CC-01 true even when compatibility routes still pass the returned value
    back in an INSERT column list.
    """
    if _is_public_target_schema():
        try:
            import db_backend
            ident = conn.execute(
                "SELECT is_identity FROM information_schema.columns "
                "WHERE table_schema = ? AND table_name = ? AND column_name = ?",
                [db_backend.app_schema(), table, column],
            ).fetchone()
            if not ident or str(ident[0]).upper() != 'YES':
                raise RuntimeError(f'{table}.{column} is not a public identity key')
            sequence = conn.execute(
                "SELECT pg_get_serial_sequence(?, ?)",
                [f"{db_backend.app_schema()}.{table}", column],
            ).fetchone()
            if not sequence or not sequence[0]:
                raise RuntimeError(f'{table}.{column} has no public identity sequence')
            return int(conn.execute("SELECT nextval(?::regclass)", [sequence[0]]).fetchone()[0])
        except Exception as exc:
            logger.error('could not allocate identity ID for %s.%s', table, column)
            raise RuntimeError(f'identity allocation failed for {table}.{column}') from exc
    while True:
        value = gen_id()
        if not conn.execute(f"SELECT 1 FROM {table} WHERE {column} = ?", [value]).fetchone():
            return value


def _onboarding_workflow_row(conn, workflow_id):
    return conn.execute(
        "SELECT w.workflow_id, w.emp_id, w.candidate_id, w.current_step, "
        "w.step1_status, w.step2_status, w.step3_status, w.step4_status, w.step5_status, "
        "w.completed, w.completed_at, w.created_at, u.name, u.email, u.status, u.allow_login "
        "FROM onboarding_workflow w JOIN users u ON u.emp_id = w.emp_id WHERE w.workflow_id = ?",
        [workflow_id],
    ).fetchone()


def _onboarding_workflow_summary(conn, row):
    checklist = conn.execute(
        "SELECT item_id, doc_type, status, uploaded_at, reviewed_by, review_note, reviewed_at "
        "FROM onboarding_checklist WHERE workflow_id = ? ORDER BY item_id",
        [row[0]],
    ).fetchall()
    tasks = conn.execute(
        "SELECT task_id, task_name, assigned_to, status, due_date, completed_at, stage "
        "FROM onboarding_tasks WHERE emp_id = ? ORDER BY COALESCE(stage, 1), task_id",
        [row[1]],
    ).fetchall()
    step_started = conn.execute(
        "SELECT step_started_at FROM onboarding_workflow WHERE workflow_id = ?", [row[0]]
    ).fetchone()
    step_base = step_started[0] if step_started and step_started[0] else row[11]
    created = step_base.date() if hasattr(step_base, 'date') else None
    days_in_step = max((datetime.now(IST).date() - created).days, 0) if created else 0
    return {
        'workflow_id': row[0],
        'emp_id': row[1],
        'candidate_id': row[2],
        'employee': row[12],
        'email': row[13],
        'user_status': row[14],
        'allow_login': bool(row[15]),
        'current_step': row[3],
        'steps': {
            'step1': row[4],
            'step2': row[5],
            'step3': row[6],
            'step4': row[7],
            'step5': row[8],
        },
        'completed': bool(row[9]),
        'completed_at': _iso(row[10]),
        'created_at': _iso(row[11]),
        'days_in_step': days_in_step,
        'checklist': [{
            'id': item[0], 'doc_type': item[1], 'status': item[2],
            'uploaded_at': _iso(item[3]), 'reviewed_by': item[4],
            'review_note': item[5], 'reviewed_at': _iso(item[6]),
        } for item in checklist],
        'tasks': [{
            'id': task[0], 'task': task[1], 'assigned_to': task[2],
            'status': task[3], 'due_date': _iso(task[4]),
            'completed_at': _iso(task[5]), 'stage': task[6],
        } for task in tasks],
    }


def _set_onboarding_step(conn, workflow_id, step, status, current_step=None):
    columns = {
        1: 'step1_status', 2: 'step2_status', 3: 'step3_status',
        4: 'step4_status', 5: 'step5_status',
    }
    if step not in columns:
        raise LifecycleError(400, 'Invalid onboarding step')
    conn.execute(
        f"UPDATE onboarding_workflow SET {columns[step]} = ?, current_step = ?, step_started_at = ? WHERE workflow_id = ?",
        [status, current_step if current_step is not None else step, datetime.now(), workflow_id],
    )


def _create_onboarding_workflow(conn, offer_row, candidate_row, percentages):
    """Create the pre-hire, salary structure, checklist and guarded tasks.

    Called inside the same transaction as offer acceptance.  The explicit
    column lists also make this safe on the reshaped v2.0 public tables.
    """
    candidate_id = int(candidate_row[0])
    existing = conn.execute(
        "SELECT workflow_id FROM onboarding_workflow WHERE candidate_id = ? AND completed = 0 LIMIT 1",
        [candidate_id],
    ).fetchone()
    if existing:
        raise LifecycleError(409, 'An active onboarding workflow already exists for this candidate',
                             workflow_id=existing[0])

    job = conn.execute(
        "SELECT title, department FROM job_postings WHERE job_id = ?", [candidate_row[3]]
    ).fetchone()
    department = job[1] if job else None
    designation = job[0] if job else None
    manager = conn.execute(
        "SELECT emp_id FROM users WHERE department = ? AND role IN ('Team Leader', 'Admin') "
        "ORDER BY emp_id LIMIT 1", [department]
    ).fetchone()
    manager_id = manager[0] if manager else None

    if conn.execute(
        "SELECT emp_id FROM users WHERE LOWER(email) = LOWER(?)", [candidate_row[2]]
    ).fetchone():
        raise LifecycleError(409, 'Candidate email is already assigned to a user')

    base_emp_id = f"PRE{candidate_id}"
    emp_id = base_emp_id
    suffix = 1
    while conn.execute("SELECT 1 FROM users WHERE emp_id = ?", [emp_id]).fetchone():
        suffix += 1
        emp_id = f'{base_emp_id}-{suffix}'

    now = datetime.now()
    offer_date = offer_row[2] if offer_row[2] else now.date()
    conn.execute(
        "INSERT INTO users (emp_id, name, email, password, role, department, designation, "
        "manager_emp_id, phone, date_of_joining, status, allow_login, allow_breaks, "
        "first_login, created_at, candidate_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [emp_id, candidate_row[1], candidate_row[2], hash_password(secrets.token_urlsafe(24)),
         'Employee', department, designation, manager_id, candidate_row[4], offer_date,
         'Pre-hire', 0, 0, None, now, candidate_id],
    )
    workflow_id = _next_generated_id(conn, 'onboarding_workflow', 'workflow_id')
    conn.execute(
        "INSERT INTO onboarding_workflow (workflow_id, emp_id, candidate_id, current_step, "
        "step1_status, step2_status, step3_status, step4_status, step5_status, completed, created_at) "
        "VALUES (?, ?, ?, 1, 'InProgress', 'Pending', 'Pending', 'Pending', 'Pending', 0, ?)",
        [workflow_id, emp_id, candidate_id, now],
    )
    for doc_type in ONBOARDING_REQUIRED_DOCS:
        conn.execute(
            "INSERT INTO onboarding_checklist (item_id, workflow_id, doc_type, status) VALUES (?, ?, ?, 'Pending')",
            [_next_generated_id(conn, 'onboarding_checklist', 'item_id'), workflow_id, doc_type],
        )

    # IT/physical/orientation work is assigned to owners, never to the new hire.
    task_specs = (
        ('Provision accounts & equipment', manager_id or 'IT', 3),
        ('Allocate workstation and issue ID card', 'IT', 4),
        ('Orientation and buddy assignment', manager_id or 'HR', 5),
    )
    for task_name, assigned_to, stage in task_specs:
        conn.execute(
            "INSERT INTO onboarding_tasks (task_id, emp_id, task_name, assigned_to, status, due_date, completed_at, stage) "
            "VALUES (?, ?, ?, ?, 'Pending', ?, NULL, ?)",
            [_next_generated_id(conn, 'onboarding_tasks', 'task_id'), emp_id, task_name,
             assigned_to, now.date() + timedelta(days=stage), stage],
        )

    offered_salary = float(offer_row[1] or 0)
    basic_pct, hra_pct, allowances_pct = percentages
    conn.execute(
        "INSERT INTO salary_structures (struct_id, emp_id, basic, hra, allowances, deductions, effective_from, effective_to) "
        "VALUES (?, ?, ?, ?, ?, 0, ?, NULL)",
        [_next_generated_id(conn, 'salary_structures', 'struct_id'), emp_id,
         round(offered_salary * basic_pct / 100, 2), round(offered_salary * hra_pct / 100, 2),
         round(offered_salary * allowances_pct / 100, 2), offer_date],
    )
    return {
        'workflow_id': workflow_id,
        'emp_id': emp_id,
        'preboarding_token': _issue_preboarding_token(workflow_id),
    }


def _workflow_for_token(conn, token):
    workflow_id = _decode_preboarding_token(token)
    row = _onboarding_workflow_row(conn, workflow_id)
    if not row:
        raise LifecycleError(404, 'Onboarding workflow not found')
    return row


def _lifecycle_fernet():
    import base64

    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64encode(hashlib.sha256(app.secret_key.encode()).digest())
    return Fernet(key)


def _encrypt_lifecycle_secret(value):
    return _lifecycle_fernet().encrypt(str(value).encode()).decode()


def _decrypt_lifecycle_secret(value):
    return _lifecycle_fernet().decrypt(str(value).encode()).decode()


def _issue_lifecycle_reset_token(conn, emp_id):
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO password_reset_tokens (token_id, emp_id, token, expires_at, used, created_at) "
        "VALUES (?, ?, ?, ?, 0, ?)",
        [_next_generated_id(conn, 'password_reset_tokens', 'token_id'), emp_id, _token_digest(token),
         datetime.now() + timedelta(hours=24), datetime.now()],
    )
    return token


def _insert_lifecycle_notification(conn, emp_id, message, link, category):
    conn.execute(
        "INSERT INTO notifications (notification_id, emp_id, type, category, message, related_link, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [_next_generated_id(conn, 'notifications', 'notification_id'), emp_id, category, category,
         message, link, datetime.now()],
    )


def _generate_initial_password():
    """A policy-compliant password for an admin-created user with none supplied.

    Generated rather than defaulted, because a default is a shared secret: every
    employee created without a password would otherwise have the same one, and
    the seed's demo password is on the breach corpus by design. The plaintext is
    returned in the response body exactly once, so the administrator can hand it
    over; the reset flow is the better route and this exists so the create call
    cannot fail for want of one.
    """
    while True:
        candidate = (
            secrets.token_urlsafe(9)[:4] + '-'
            + secrets.choice(('harbour', 'meadow', 'lantern', 'quartz', 'cobalt', 'thicket'))
            + '-' + secrets.token_urlsafe(6)[:4]
        )
        if passwords.is_acceptable(candidate):
            return candidate


def _password_problem(password, field='password'):
    """The policy verdict for ``password``: ``None`` if acceptable, else (body, 400).

    FR-AUTH-10. Every route that sets a password goes through here, so the
    answer a user gets for "too short" is the same whether they were created by
    an admin, changed their own, or reset via a token.
    """
    try:
        passwords.check(password)
    except passwords.PasswordPolicyError as exc:
        return exc.payload(), 400
    return None


def _revoke_redis_sessions(emp_id):
    """Best-effort invalidation of server-side Flask sessions for an exit."""
    redis_url = os.getenv('REDIS_URL')
    if not redis_url:
        return
    try:
        import redis
        client = redis.from_url(redis_url, decode_responses=True)
        prefix = 'hrms:session:'
        for key in client.scan_iter(match=f'{prefix}*'):
            raw = client.get(key)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if data.get('emp_id') == emp_id:
                client.delete(key)
    except Exception as exc:
        logger.warning('Redis session revocation failed for %s: %s', emp_id, exc)


def _close_active_user_sessions(conn, emp_id):
    """Close database sessions for a user and return the number revoked."""
    now = datetime.now()
    rows = conn.execute(
        "SELECT session_id, login_time FROM user_sessions "
        "WHERE emp_id = ? AND logout_time IS NULL",
        [emp_id],
    ).fetchall()
    for session_id, login_time in rows:
        if login_time and getattr(login_time, 'tzinfo', None) is not None:
            login_time = login_time.replace(tzinfo=None)
        hours = max((now - login_time).total_seconds() / 3600, 0) if login_time else 0
        conn.execute(
            "UPDATE user_sessions SET logout_time = ?, total_hours = ? WHERE session_id = ?",
            [now, round(hours, 2), session_id],
        )
    return len(rows)


def revoke_offboarding_access(target_date=None, conn=None, offboard_id=None):
    """Revoke LWD access atomically through the backend transaction helper."""
    if conn is not None:
        return _revoke_offboarding_access_impl(target_date, conn, offboard_id)
    with outbox.transaction() as tx:
        return _revoke_offboarding_access_impl(target_date, tx, offboard_id)


def _revoke_offboarding_access_impl(target_date, conn, offboard_id=None):
    """Revoke access for every resignation whose LWD has arrived (FR-OFF-03).

    The function is deliberately callable with a date so tests and the
    nightly scheduler exercise the same idempotent implementation.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_db()
    if target_date is None:
        target_date = datetime.now(IST).date()
    elif isinstance(target_date, datetime):
        target_date = target_date.date()
    revoked = []
    try:
        query = (
            "SELECT r.resignation_id, r.emp_id, w.offboard_id FROM resignations r "
            "JOIN offboarding_workflow w ON w.resignation_id = r.resignation_id "
            "WHERE r.last_working_day <= ? AND r.status NOT IN ('Cancelled', 'Revoked')"
        )
        params = [target_date]
        if offboard_id is not None:
            query += " AND w.offboard_id = ?"
            params.append(offboard_id)
        rows = conn.execute(query, params).fetchall()
        now = datetime.now()
        for resignation_id, emp_id, offboard_id in rows:
            active_sessions = conn.execute(
                "SELECT session_id, login_time FROM user_sessions "
                "WHERE emp_id = ? AND logout_time IS NULL", [emp_id]
            ).fetchall()
            for session_id, login_time in active_sessions:
                hours = max((now - login_time).total_seconds() / 3600, 0) if login_time else 0
                conn.execute(
                    "UPDATE user_sessions SET logout_time = ?, total_hours = ? WHERE session_id = ?",
                    [now, round(hours, 2), session_id],
                )
            conn.execute(
                "UPDATE users SET allow_login = 0, status = 'Inactive' WHERE emp_id = ?", [emp_id]
            )
            try:
                conn.execute("DELETE FROM user_permissions WHERE emp_id = ?", [emp_id])
            except Exception:
                # Older compatibility schemas may not have the optional RBAC table.
                pass
            completed_true = 'TRUE' if _is_public_target_schema() else '1'
            completed_false = 'FALSE' if _is_public_target_schema() else '0'
            conn.execute(
                "UPDATE offboarding_workflow SET "
                "stage5_status = CASE WHEN stage1_status = 'Completed' AND stage2_status = 'Completed' "
                "AND stage3_status = 'Completed' AND stage4_status = 'Completed' "
                "THEN 'Completed' ELSE 'AccessRevoked' END, "
                f"completed = CASE WHEN stage1_status = 'Completed' AND stage2_status = 'Completed' "
                f"AND stage3_status = 'Completed' AND stage4_status = 'Completed' THEN {completed_true} ELSE {completed_false} END, "
                "completed_at = CASE WHEN stage1_status = 'Completed' AND stage2_status = 'Completed' "
                "AND stage3_status = 'Completed' AND stage4_status = 'Completed' THEN ? ELSE NULL END "
                "WHERE offboard_id = ?",
                [now, offboard_id],
            )
            conn.execute("UPDATE resignations SET status = 'Revoked' WHERE resignation_id = ?", [resignation_id])
            admins = conn.execute(
                "SELECT emp_id FROM users WHERE role IN ('Admin', 'Super Admin')"
            ).fetchall()
            for admin in admins:
                _insert_lifecycle_notification(
                    conn, admin[0], f'Access revoked for {emp_id} on last working day',
                    '/offboarding', notifications.category_for('Offboarding')
                )
            _revoke_redis_sessions(emp_id)
            revoked.append({'emp_id': emp_id, 'resignation_id': resignation_id, 'offboard_id': offboard_id})
        return revoked
    finally:
        if own_conn:
            conn.close()


def run_offboarding_access_revocation():
    try:
        with app.app_context():
            revoked = revoke_offboarding_access(datetime.now(IST).date())
            for item in revoked:
                audit_log(item['emp_id'], 'ACCESS_REVOKED', 'Last working day reached',
                          actor='SYSTEM:LWD', entity='resignations', entity_id=item['resignation_id'],
                          after={'allow_login': False, 'status': 'Inactive'})
        return revoked
    except Exception as exc:
        logger.warning('offboarding access revocation failed: %s', exc)
        return []


# ══════════════════════════════════════════════════════════════════════
#  PHASE 2 — RECRUITMENT / ATS
# ══════════════════════════════════════════════════════════════════════

@app.route('/admin/jobs')
@hr_or_admin_required
def admin_jobs():
    return render_template('jobs.html')


@app.route('/admin/candidates')
@hr_or_admin_required
def admin_candidates():
    return render_template('candidates.html')


# ── Job Postings ──────────────────────────────────────────────────

@app.route('/api/v1/jobs', methods=['GET', 'POST'])
@app.route('/api/jobs', methods=['GET', 'POST'])
@hr_or_admin_required
def jobs_api():
    if request.method == 'GET':
        conn = get_db()
        rows = conn.execute("SELECT job_id, title, department, location, description, requirements, status, created_at FROM job_postings ORDER BY created_at DESC").fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'title': r[1], 'department': r[2], 'location': r[3], 'description': r[4], 'requirements': r[5], 'status': r[6], 'created_at': r[7].isoformat() if r[7] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('title'):
        return jsonify({'error': 'title required'}), 400
    conn = get_db()
    jid = _next_generated_id(conn, 'job_postings', 'job_id')
    conn.execute("INSERT INTO job_postings VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 [jid, data['title'], data.get('department'), data.get('location'), data.get('description'), data.get('requirements'), 'Open', datetime.now()])
    conn.close()
    audit_log(session['emp_id'], 'JOB_CREATE', f'Created job {data["title"]}', entity='job_postings', entity_id=jid)
    return jsonify({'message': 'Job created', 'id': jid}), 201


@app.route('/api/v1/jobs/<int:jid>/close', methods=['POST'])
@app.route('/api/jobs/<int:jid>/close', methods=['POST'])
@hr_or_admin_required
def close_job(jid):
    conn = get_db()
    result = conn.execute("UPDATE job_postings SET status = 'Closed' WHERE job_id = ?", [jid])
    conn.close()
    if result.rowcount == 0:
        return jsonify({'error': 'Job not found'}), 404
    audit_log(session['emp_id'], 'JOB_CLOSE', f'Closed job {jid}', entity='job_postings', entity_id=jid)
    return jsonify({'message': 'Job closed'}), 200


@app.route('/api/v1/jobs/<int:jid>', methods=['PUT', 'DELETE'])
@app.route('/api/jobs/<int:jid>', methods=['PUT', 'DELETE'])
@hr_or_admin_required
def job_detail(jid):
    conn = get_db()
    row = conn.execute("SELECT job_id FROM job_postings WHERE job_id = ?", [jid]).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Job not found'}), 404
    if request.method == 'DELETE':
        active = conn.execute(
            "SELECT 1 FROM candidates WHERE job_id = ? AND status NOT IN ('Rejected', 'Withdrawn')", [jid]
        ).fetchone()
        if active:
            conn.close()
            return jsonify({'error': 'Close the job before deleting it'}), 409
        conn.execute("DELETE FROM job_postings WHERE job_id = ?", [jid])
        conn.close()
        audit_log(session['emp_id'], 'JOB_DELETE', f'Deleted job {jid}', entity='job_postings', entity_id=jid)
        return jsonify({'message': 'Job deleted'}), 200
    data = request.get_json(silent=True) or {}
    if not data.get('title'):
        conn.close()
        return jsonify({'error': 'title required'}), 400
    if data.get('status', 'Open') not in ('Open', 'Closed'):
        conn.close()
        return jsonify({'error': 'Invalid job status'}), 400
    conn.execute(
        "UPDATE job_postings SET title = ?, department = ?, location = ?, description = ?, requirements = ?, status = ? WHERE job_id = ?",
        [data['title'], data.get('department'), data.get('location'), data.get('description'),
         data.get('requirements'), data.get('status', 'Open'), jid],
    )
    conn.close()
    audit_log(session['emp_id'], 'JOB_UPDATE', f'Updated job {jid}', entity='job_postings', entity_id=jid)
    return jsonify({'message': 'Job updated'}), 200


@app.route('/api/v1/pipeline', methods=['GET'])
@app.route('/api/pipeline', methods=['GET'])
@hr_or_admin_required
def recruitment_pipeline():
    """Aggregate ATS stage counts and accepted-offer conversions per job."""
    conn = get_db()
    jobs = conn.execute("SELECT job_id, title, status FROM job_postings ORDER BY created_at DESC").fetchall()
    result = []
    stages = ('Applied', 'Screened', 'Interviewed', 'Offered', 'Hired', 'Rejected', 'Withdrawn')
    for job_id, title, job_status in jobs:
        counts = {stage: 0 for stage in stages}
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM candidates WHERE job_id = ? GROUP BY status", [job_id]
        ).fetchall()
        for status, count in rows:
            counts[_candidate_stage(status)] = int(count)
        converted = {'pre_hire': 0, 'onboarding': 0, 'active': 0}
        converted_rows = conn.execute(
            "SELECT u.status, w.completed FROM users u LEFT JOIN onboarding_workflow w ON w.emp_id = u.emp_id "
            "WHERE u.candidate_id IN (SELECT candidate_id FROM candidates WHERE job_id = ?)", [job_id]
        ).fetchall()
        for user_status, completed in converted_rows:
            if user_status in ('Inactive', 'Blocked'):
                continue
            if completed:
                converted['active'] += 1
            elif user_status == 'Pre-hire':
                converted['pre_hire'] += 1
            else:
                converted['onboarding'] += 1
        result.append({'job_id': job_id, 'title': title, 'status': job_status,
                       'stages': counts, 'converted': converted})
    conn.close()
    return jsonify({'jobs': result, 'pipeline': result}), 200


# ── Candidates ────────────────────────────────────────────────────

def _reveals_candidate_pii(conn) -> bool:
    """May the session read a candidate's contact bundle on this connection?"""
    return policy.can(policy.current_actor(conn), 'pii_reveal', conn=conn)


@app.route('/api/v1/candidates', methods=['GET', 'POST'])
@app.route('/api/candidates', methods=['GET', 'POST'])
@hr_or_admin_required
def candidates_api():
    if request.method == 'GET':
        conn = get_db()
        job_filter = request.args.get('job_id')
        if job_filter:
            rows = conn.execute("SELECT c.candidate_id, c.job_id, j.title, c.name, c.email, c.phone, c.status, c.applied_at FROM candidates c LEFT JOIN job_postings j ON c.job_id = j.job_id WHERE c.job_id = ? ORDER BY c.applied_at DESC", [job_filter]).fetchall()
        else:
            rows = conn.execute("SELECT c.candidate_id, c.job_id, j.title, c.name, c.email, c.phone, c.status, c.applied_at FROM candidates c LEFT JOIN job_postings j ON c.job_id = j.job_id ORDER BY c.applied_at DESC").fetchall()
        # FR-USR-15: a candidate's contact bundle is the personal data of
        # somebody who is not an employee. The name stays (an interviewer has
        # to know who they are meeting, HR has to know whose record they are
        # editing); the email and phone are withheld unless the actor holds
        # `pii_reveal`, and exposing them is audited.
        revealed = _reveals_candidate_pii(conn)
        conn.close()
        records = []
        for r in rows:
            record = policy.redact_pii({
                'id': r[0], 'job_id': r[1], 'job_title': r[2] or 'N/A', 'name': r[3],
                'email': r[4], 'phone': r[5], 'status': r[6],
                'applied_at': r[7].isoformat() if r[7] else None,
            }, revealed, 'candidates')
            record['pii_revealed'] = revealed
            records.append(record)
        if revealed and records:
            audit_log(
                session['emp_id'], 'PII_REVEAL',
                f"Revealed contact details of {len(records)} candidate record(s)",
                entity='candidates',
                after={'records': len(records),
                       'fields': list(policy.pii_fields_for('candidates'))},
            )
        return jsonify(records), 200
    data = request.get_json(silent=True) or {}
    if not data.get('name') or not data.get('email'):
        return jsonify({'error': 'name and email required'}), 400
    conn = get_db()
    try:
        cid = _next_generated_id(conn, 'candidates', 'candidate_id')
        if data.get('job_id') and not conn.execute("SELECT 1 FROM job_postings WHERE job_id = ?", [data['job_id']]).fetchone():
            return jsonify({'error': 'Job not found'}), 400
        conn.execute("INSERT INTO candidates (candidate_id, job_id, name, email, phone, resume_text, status, applied_at) VALUES (?, ?, ?, ?, ?, ?, 'Applied', ?)",
                     [cid, data.get('job_id'), data['name'], data['email'], data.get('phone'), data.get('resume_text', ''), datetime.now()])
    finally:
        conn.close()
    audit_log(session['emp_id'], 'CANDIDATE_CREATE', f'Added candidate {data["name"]}', entity='candidates', entity_id=cid)
    return jsonify({'message': 'Candidate added', 'id': cid}), 201


@app.route('/api/v1/candidates/<int:cid>', methods=['PUT', 'DELETE'])
@app.route('/api/candidates/<int:cid>', methods=['PUT', 'DELETE'])
@hr_or_admin_required
def candidate_detail(cid):
    conn = get_db()
    row = conn.execute("SELECT name, email, phone, job_id, status FROM candidates WHERE candidate_id = ?", [cid]).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Candidate not found'}), 404
    if request.method == 'DELETE':
        if row[4] in ('Hired', 'Offered'):
            conn.close()
            return jsonify({'error': 'Hired or offered candidates cannot be deleted'}), 409
        conn.execute("DELETE FROM candidates WHERE candidate_id = ?", [cid])
        conn.close()
        audit_log(session['emp_id'], 'CANDIDATE_DELETE', f'Deleted candidate {cid}', entity='candidates', entity_id=cid)
        return jsonify({'message': 'Candidate deleted'}), 200
    data = request.get_json(silent=True) or {}
    if not data.get('name') or not data.get('email'):
        conn.close()
        return jsonify({'error': 'name and email required'}), 400
    if data.get('job_id') and not conn.execute("SELECT 1 FROM job_postings WHERE job_id = ?", [data['job_id']]).fetchone():
        conn.close()
        return jsonify({'error': 'Job not found'}), 400
    conn.execute(
        "UPDATE candidates SET name = ?, email = ?, phone = ?, job_id = ?, resume_text = ? WHERE candidate_id = ?",
        [data['name'], data['email'], data.get('phone'), data.get('job_id'), data.get('resume_text', ''), cid],
    )
    conn.close()
    audit_log(session['emp_id'], 'CANDIDATE_UPDATE', f'Updated candidate {cid}', entity='candidates', entity_id=cid)
    return jsonify({'message': 'Candidate updated'}), 200


@app.route('/api/v1/candidates/<int:cid>/status', methods=['PUT'])
@app.route('/api/candidates/<int:cid>/status', methods=['PUT'])
@hr_or_admin_required
def update_candidate_status(cid):
    data = request.get_json(silent=True) or {}
    target = data.get('status')
    if target in ('Hired', 'Offered'):
        return jsonify({'error': 'Hired and Offered are reached through their guarded workflows'}), 409
    conn = get_db()
    try:
        row = conn.execute("SELECT status FROM candidates WHERE candidate_id = ?", [cid]).fetchone()
        if not row:
            return jsonify({'error': 'Candidate not found'}), 404
        try:
            target = _validate_candidate_transition(row[0], target)
        except LifecycleError as exc:
            return _lifecycle_error_payload(exc), exc.status_code
        result = conn.execute(
            "UPDATE candidates SET status = ? WHERE candidate_id = ? AND status = ?",
            [target, cid, row[0]],
        )
        if result.rowcount == 0:
            return jsonify({'error': 'Candidate changed concurrently; retry the transition'}), 409
    finally:
        conn.close()
    audit_log(session['emp_id'], 'CANDIDATE_STATUS', f'Candidate {cid} moved to {target}', entity='candidates', entity_id=cid, after={'status': target})
    return jsonify({'message': f'Status updated to {target}', 'status': target}), 200


# ── Interviews ────────────────────────────────────────────────────

@app.route('/api/v1/interviews', methods=['GET', 'POST'])
@app.route('/api/interviews', methods=['GET', 'POST'])
@hr_or_admin_required
def interviews_api():
    if request.method == 'GET':
        conn = get_db()
        cid = request.args.get('candidate_id')
        if cid:
            rows = conn.execute("SELECT i.interview_id, i.candidate_id, c.name, i.scheduled_at, i.interviewer, i.mode, i.feedback, i.status FROM interviews i JOIN candidates c ON i.candidate_id = c.candidate_id WHERE i.candidate_id = ? ORDER BY i.scheduled_at DESC", [cid]).fetchall()
        else:
            rows = conn.execute("SELECT i.interview_id, i.candidate_id, c.name, i.scheduled_at, i.interviewer, i.mode, i.feedback, i.status FROM interviews i JOIN candidates c ON i.candidate_id = c.candidate_id ORDER BY i.scheduled_at DESC").fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'candidate_id': r[1], 'candidate_name': r[2], 'scheduled_at': r[3].isoformat() if r[3] else None, 'interviewer': r[4], 'mode': r[5], 'feedback': r[6], 'status': r[7]} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    scheduled_at = parse_datetime(data.get('scheduled_at'))
    if not data.get('candidate_id') or not scheduled_at:
        return jsonify({'error': 'candidate_id and a valid scheduled_at are required'}), 400
    conn = get_db()
    try:
        candidate = conn.execute("SELECT status FROM candidates WHERE candidate_id = ?", [data['candidate_id']]).fetchone()
        if not candidate:
            return jsonify({'error': 'Candidate not found'}), 404
        if _candidate_stage(candidate[0]) not in ('Screened', 'Interviewed'):
            return jsonify({'error': 'Candidate must be screened before scheduling an interview'}), 409
        iid = _next_generated_id(conn, 'interviews', 'interview_id')
        conn.execute(
            "INSERT INTO interviews (interview_id, candidate_id, scheduled_at, interviewer, mode, feedback, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'Scheduled')",
            [iid, data['candidate_id'], scheduled_at, data.get('interviewer'), data.get('mode', 'In-person'), data.get('feedback')],
        )
    finally:
        conn.close()
    audit_log(session['emp_id'], 'INTERVIEW_SCHEDULE', f'Scheduled interview {iid}', entity='interviews', entity_id=iid)
    return jsonify({'message': 'Interview scheduled', 'id': iid}), 201


@app.route('/api/v1/interviews/<int:iid>/feedback', methods=['PUT'])
@app.route('/api/interviews/<int:iid>/feedback', methods=['PUT'])
@hr_or_admin_required
def interview_feedback(iid):
    data = request.get_json(silent=True) or {}
    feedback = (data.get('feedback') or '').strip()
    if not feedback:
        return jsonify({'error': 'feedback is required'}), 400
    conn = get_db()
    try:
        row = conn.execute("SELECT status, candidate_id FROM interviews WHERE interview_id = ?", [iid]).fetchone()
        if not row:
            return jsonify({'error': 'Interview not found'}), 404
        if row[0] == 'Completed':
            return jsonify({'error': 'Interview feedback is already completed'}), 409
        conn.execute("UPDATE interviews SET feedback = ?, status = 'Completed' WHERE interview_id = ?", [feedback, iid])
        conn.execute("UPDATE candidates SET status = 'Interviewed' WHERE candidate_id = ? AND status = 'Screened'", [row[1]])
    finally:
        conn.close()
    audit_log(session['emp_id'], 'INTERVIEW_FEEDBACK', f'Saved interview feedback {iid}', entity='interviews', entity_id=iid)
    return jsonify({'message': 'Feedback saved'}), 200


# ── Offer Letters ─────────────────────────────────────────────────

@app.route('/api/v1/offers', methods=['GET', 'POST'])
@app.route('/api/offers', methods=['GET', 'POST'])
@hr_or_admin_required
@idempotent
def offers_api():
    if request.method == 'GET':
        conn = get_db()
        rows = conn.execute(
            "SELECT o.offer_id, o.candidate_id, c.name, c.email, o.offered_salary, "
            "o.basic_pct, o.hra_pct, o.allowances_pct, o.offer_date, o.status, "
            "o.accepted_at, o.notes FROM offer_letters o JOIN candidates c "
            "ON o.candidate_id = c.candidate_id ORDER BY o.offer_date DESC"
        ).fetchall()
        # Same contact bundle as the candidate list, under the same rule.
        revealed = _reveals_candidate_pii(conn)
        conn.close()
        records = []
        for r in rows:
            record = policy.redact_pii(
                {'id': r[0], 'candidate_id': r[1], 'candidate_name': r[2], 'email': r[3]},
                revealed, 'candidates',
            )
            record.update({
                'salary': float(r[4]) if r[4] else 0,
                'basic_pct': float(r[5]) if r[5] is not None else None,
                'hra_pct': float(r[6]) if r[6] is not None else None,
                'allowances_pct': float(r[7]) if r[7] is not None else None,
                'offer_date': r[8].isoformat() if r[8] else None,
                'status': r[9],
                'accepted_at': r[10].isoformat() if r[10] else None,
                'notes': r[11],
                'pii_revealed': revealed,
            })
            records.append(record)
        return jsonify(records), 200

    data = request.get_json(silent=True) or {}
    try:
        candidate_id = int(data.get('candidate_id'))
        offered_salary = float(data.get('offered_salary'))
        basic_decimal = Decimal(str(data.get('basic_pct')))
        hra_decimal = Decimal(str(data.get('hra_pct')))
        allowances_decimal = Decimal(str(data.get('allowances_pct')))
    except (TypeError, ValueError, InvalidOperation):
        return jsonify({'error': 'candidate_id, offered_salary and all three percentages are required'}), 400
    if not math.isfinite(offered_salary) or offered_salary <= 0:
        return jsonify({'error': 'offered_salary must be a finite positive number'}), 400
    decimal_percentages = (basic_decimal, hra_decimal, allowances_decimal)
    if any(not pct.is_finite() for pct in decimal_percentages):
        return jsonify({'error': 'offer percentages must be finite numbers'}), 400
    if any(pct.as_tuple().exponent < -2 for pct in decimal_percentages):
        return jsonify({'error': 'offer percentages may have at most two decimal places'}), 400
    if any(pct < 0 or pct > 100 for pct in decimal_percentages) or sum(decimal_percentages) != Decimal('100'):
        return jsonify({'error': 'basic_pct + hra_pct + allowances_pct must equal 100'}), 400
    percentages = tuple(float(pct) for pct in decimal_percentages)
    basic_pct, hra_pct, allowances_pct = percentages

    result = None
    try:
        with outbox.transaction() as conn:
            candidate = conn.execute(
                "SELECT candidate_id, name, email, job_id, phone, status FROM candidates WHERE candidate_id = ?",
                [candidate_id],
            ).fetchone()
            if not candidate:
                raise LifecycleError(404, 'Candidate not found')
            if _candidate_stage(candidate[5]) != 'Interviewed':
                raise LifecycleError(409, 'Candidate must be Interviewed before an offer is created')
            if conn.execute(
                "SELECT 1 FROM offer_letters WHERE candidate_id = ? AND status IN ('Pending', 'Accepted')",
                [candidate_id],
            ).fetchone():
                raise LifecycleError(409, 'Candidate already has an active offer')
            oid = _next_generated_id(conn, 'offer_letters', 'offer_id')
            conn.execute(
                "INSERT INTO offer_letters (offer_id, candidate_id, offered_salary, basic_pct, hra_pct, "
                "allowances_pct, offer_date, status, accepted_at, notes) VALUES (?, ?, ?, ?, ?, ?, ?, 'Pending', NULL, ?)",
                [oid, candidate_id, offered_salary, basic_pct, hra_pct, allowances_pct,
                 datetime.now().date(), data.get('notes')],
            )
            conn.execute("UPDATE candidates SET status = 'Offered' WHERE candidate_id = ?", [candidate_id])
            outbox.enqueue(
                conn, 'offer.created', 'offer_letters', str(oid),
                {'offer_id': oid, 'candidate_id': candidate_id, 'name': candidate[1],
                 'email': candidate[2], 'salary': offered_salary,
                 'basic_pct': basic_pct, 'hra_pct': hra_pct, 'allowances_pct': allowances_pct},
            )
            result = oid
    except LifecycleError as exc:
        return _lifecycle_error_payload(exc), exc.status_code
    except Exception as exc:
        if 'uq_active_offer_candidate' in str(exc) or 'unique' in str(exc).lower():
            return jsonify({'error': 'Candidate already has an active offer'}), 409
        logger.warning('create offer failed: %s', exc)
        return jsonify({'error': 'Failed to create offer'}), 500
    audit_log(session['emp_id'], 'OFFER_CREATE', f'Created offer {result}', entity='offer_letters', entity_id=result)
    return jsonify({'message': 'Offer sent', 'id': result, 'status': 'Pending'}), 201


@app.route('/api/v1/offers/<int:oid>/accept', methods=['POST'])
@app.route('/api/offers/<int:oid>/accept', methods=['POST'])
@hr_or_admin_required
@idempotent
def accept_offer(oid):
    result = None
    try:
        with outbox.transaction() as conn:
            offer = conn.execute(
                "SELECT offer_id, offered_salary, offer_date, status, basic_pct, hra_pct, allowances_pct, candidate_id "
                "FROM offer_letters WHERE offer_id = ?", [oid]
            ).fetchone()
            if not offer:
                raise LifecycleError(404, 'Offer not found')
            if offer[3] != 'Pending':
                raise LifecycleError(409, 'Offer has already been decided')
            candidate = conn.execute(
                "SELECT candidate_id, name, email, job_id, phone, status FROM candidates WHERE candidate_id = ?",
                [offer[7]],
            ).fetchone()
            if not candidate:
                raise LifecycleError(404, 'Candidate not found')
            if _candidate_stage(candidate[5]) != 'Offered':
                raise LifecycleError(409, 'Candidate is not in the Offered stage')
            percentages = (float(offer[4] or 0), float(offer[5] or 0), float(offer[6] or 0))
            if round(sum(percentages), 2) != 100:
                raise LifecycleError(409, 'Offer salary split is invalid')
            onboarding = _create_onboarding_workflow(conn, (oid, offer[1], offer[2]), candidate, percentages)
            now = datetime.now()
            accepted = conn.execute(
                "UPDATE offer_letters SET status = 'Accepted', accepted_at = ? "
                "WHERE offer_id = ? AND status = 'Pending'", [now, oid]
            )
            if accepted.rowcount == 0:
                raise LifecycleError(409, 'Offer was decided concurrently; retry the request')
            conn.execute("UPDATE candidates SET status = 'Hired' WHERE candidate_id = ?", [candidate[0]])
            outbox.enqueue(
                conn, 'offer.accepted', 'offer_letters', str(oid),
                {'offer_id': oid, 'candidate_id': candidate[0], 'emp_id': onboarding['emp_id'],
                 'workflow_id': onboarding['workflow_id']},
            )
            outbox.enqueue(
                conn, 'candidate.hired', 'candidates', str(candidate[0]),
                {'candidate_id': candidate[0], 'offer_id': oid, 'emp_id': onboarding['emp_id'],
                 'workflow_id': onboarding['workflow_id']},
            )
            result = {**onboarding, 'candidate_id': candidate[0], 'offer_id': oid}
    except LifecycleError as exc:
        return _lifecycle_error_payload(exc), exc.status_code
    except Exception as exc:
        logger.warning('accept_offer failed: %s', exc)
        return jsonify({'error': 'Failed to accept offer'}), 500
    result['preboarding_url'] = url_for('preboarding_page', token=result['preboarding_token'], _external=True)
    audit_log(session['emp_id'], 'OFFER_ACCEPT', f'Accepted offer {oid}', entity='offer_letters', entity_id=oid,
              after={'candidate_id': result['candidate_id'], 'workflow_id': result['workflow_id']})
    return jsonify({'message': 'Offer accepted', **result}), 200


@app.route('/api/v1/offers/<int:oid>/reject', methods=['POST'])
@app.route('/api/offers/<int:oid>/reject', methods=['POST'])
@hr_or_admin_required
@idempotent
def reject_offer(oid):
    data = request.get_json(silent=True) or {}
    candidate_status = data.get('candidate_status', 'Rejected')
    if candidate_status not in ('Rejected', 'Interviewed'):
        return jsonify({'error': 'candidate_status must be Rejected or Interviewed'}), 400
    try:
        with outbox.transaction() as conn:
            offer = conn.execute("SELECT status, candidate_id FROM offer_letters WHERE offer_id = ?", [oid]).fetchone()
            if not offer:
                raise LifecycleError(404, 'Offer not found')
            if offer[0] != 'Pending':
                raise LifecycleError(409, 'Offer has already been decided')
            rejected = conn.execute(
                "UPDATE offer_letters SET status = 'Rejected' WHERE offer_id = ? AND status = 'Pending'", [oid]
            )
            if rejected.rowcount == 0:
                raise LifecycleError(409, 'Offer was decided concurrently; retry the request')
            conn.execute("UPDATE candidates SET status = ? WHERE candidate_id = ?", [candidate_status, offer[1]])
    except LifecycleError as exc:
        return _lifecycle_error_payload(exc), exc.status_code
    except Exception as exc:
        logger.warning('reject_offer failed: %s', exc)
        return jsonify({'error': 'Failed to reject offer'}), 500
    audit_log(session['emp_id'], 'OFFER_REJECT', f'Rejected offer {oid}', entity='offer_letters', entity_id=oid)
    return jsonify({'message': 'Offer rejected', 'candidate_status': candidate_status}), 200


# ══════════════════════════════════════════════════════════════════════
#  PHASE 2 — ONBOARDING & OFFBOARDING
# ══════════════════════════════════════════════════════════════════════

@app.route('/onboarding')
@login_required
def onboarding_page():
    return render_template('onboarding.html')


def _lifecycle_actor(conn=None):
    own = conn is None
    if own:
        conn = get_db()
    try:
        return conn.execute(
            "SELECT emp_id, role, department, manager_emp_id FROM users WHERE emp_id = ?",
            [session.get('emp_id')],
        ).fetchone()
    finally:
        if own:
            conn.close()


def _task_owner_valid(conn, assigned_to):
    if not assigned_to:
        return False
    if str(assigned_to).upper() in ('HR', 'IT', 'FINANCE', 'MANAGER', 'ADMIN'):
        return True
    return bool(conn.execute("SELECT 1 FROM users WHERE emp_id = ?", [assigned_to]).fetchone())


def _can_manage_lifecycle(actor, include_it=False, include_finance=False):
    if not actor:
        return False
    role = str(actor[1] or '')
    return (
        role in ('Admin', 'Super Admin')
        or actor[2] == 'HR'
        or (include_it and role == 'IT')
        or (include_finance and role == 'Finance')
    )


def _required_docs_approved(conn, workflow_id):
    placeholders = ','.join('?' for _ in ONBOARDING_REQUIRED_DOCS)
    row = conn.execute(
        f"SELECT COUNT(*) FROM onboarding_checklist WHERE workflow_id = ? "
        f"AND doc_type IN ({placeholders}) AND status = 'Approved'",
        [workflow_id, *ONBOARDING_REQUIRED_DOCS],
    ).fetchone()
    return int(row[0] or 0) == len(ONBOARDING_REQUIRED_DOCS)


def _onboarding_docs_uploaded(conn, workflow_id):
    placeholders = ','.join('?' for _ in ONBOARDING_REQUIRED_DOCS)
    row = conn.execute(
        f"SELECT COUNT(*) FROM onboarding_checklist WHERE workflow_id = ? "
        f"AND doc_type IN ({placeholders}) AND status IN ('Uploaded', 'Approved')",
        [workflow_id, *ONBOARDING_REQUIRED_DOCS],
    ).fetchone()
    return int(row[0] or 0) >= 1


def _complete_onboarding_stage_tx(conn, workflow_id, step, actor):
    row = _onboarding_workflow_row(conn, workflow_id)
    if not row:
        raise LifecycleError(404, 'Onboarding workflow not found')
    if step < 1 or step > 5:
        raise LifecycleError(400, 'Invalid onboarding step')
    if row[9]:
        return row
    statuses = {1: row[4], 2: row[5], 3: row[6], 4: row[7], 5: row[8]}
    if statuses[step] == 'Completed':
        return row
    if step == 1 and not _onboarding_docs_uploaded(conn, workflow_id):
        raise LifecycleError(409, 'Upload at least one required document before submitting pre-boarding')
    if step == 2 and row[4] != 'Completed':
        raise LifecycleError(409, 'Pre-boarding must be submitted before document verification')
    if step == 2 and not _required_docs_approved(conn, workflow_id):
        raise LifecycleError(409, 'All required documents must be approved')
    if step == 3 and row[5] != 'Completed':
        raise LifecycleError(409, 'Document verification must complete before provisioning')
    if step == 4 and row[6] != 'Completed':
        raise LifecycleError(409, 'System and access provisioning must complete first')
    if step == 5 and row[7] != 'Completed':
        raise LifecycleError(409, 'Workstation and ID card clearance must complete first')

    now = datetime.now()
    next_status = 'Completed' if step == 5 else 'InProgress'
    _set_onboarding_step(conn, workflow_id, step, 'Completed', current_step=min(step + 1, 5))
    if step < 5:
        _set_onboarding_step(conn, workflow_id, step + 1, next_status, current_step=step + 1)
    else:
        conn.execute(
            "UPDATE onboarding_workflow SET completed = 1, completed_at = ? WHERE workflow_id = ?",
            [now, workflow_id],
        )
    if step == 3:
        joining = conn.execute("SELECT date_of_joining FROM users WHERE emp_id = ?", [row[1]]).fetchone()
        target_status = 'Active'
        if joining and joining[0] and joining[0] > datetime.now(IST).date():
            target_status = os.getenv('PREHIRE_STATUS_BEFORE_DAY1', 'Onboarding')
        conn.execute(
            "UPDATE users SET allow_login = 1, status = ? WHERE emp_id = ?", [target_status, row[1]]
        )
    elif step == 5:
        conn.execute(
            "UPDATE users SET allow_login = 1, status = 'Active' WHERE emp_id = ?", [row[1]]
        )
    if step == 3:
        reset_token = _issue_lifecycle_reset_token(conn, row[1])
        outbox.enqueue(
            conn, 'credentials.issued', 'users', row[1],
            {'emp_id': row[1], 'reset_token_encrypted': _encrypt_lifecycle_secret(reset_token), 'workflow_id': workflow_id},
        )
    conn.execute(
        "UPDATE onboarding_tasks SET status = 'Completed', completed_at = ? WHERE emp_id = ? AND stage = ? AND status <> 'Completed'",
        [now, row[1], step],
    )
    return _onboarding_workflow_row(conn, workflow_id)


@app.route('/preboarding/<token>')
def preboarding_page(token):
    conn = get_db()
    try:
        row = _workflow_for_token(conn, token)
        summary = _onboarding_workflow_summary(conn, row)
    except LifecycleError as exc:
        return jsonify(_lifecycle_error_payload(exc)), exc.status_code
    finally:
        conn.close()
    return render_template('preboarding.html', workflow=summary, token=token)


@app.route('/api/v1/preboarding/<token>', methods=['GET'])
@app.route('/api/preboarding/<token>', methods=['GET'])
def preboarding_status(token):
    conn = get_db()
    try:
        row = _workflow_for_token(conn, token)
        return jsonify(_onboarding_workflow_summary(conn, row)), 200
    except LifecycleError as exc:
        return _lifecycle_error_payload(exc), exc.status_code
    finally:
        conn.close()


@app.route('/api/v1/preboarding/<token>/documents/<doc_type>', methods=['POST'])
@app.route('/api/preboarding/<token>/documents/<doc_type>', methods=['POST'])
def upload_preboarding_document(token, doc_type):
    if doc_type not in ONBOARDING_REQUIRED_DOCS:
        return jsonify({'error': 'Unknown required document type'}), 400
    conn = get_db()
    stored_path = None
    try:
        row = _workflow_for_token(conn, token)
        if row[9] or row[5] == 'Completed':
            raise LifecycleError(409, 'Pre-boarding document upload is closed')
        item = conn.execute(
            "SELECT item_id FROM onboarding_checklist WHERE workflow_id = ? AND doc_type = ?",
            [row[0], doc_type],
        ).fetchone()
        if not item:
            raise LifecycleError(404, 'Checklist item not found')
        uploaded = request.files.get('file')
        if uploaded is None or not uploaded.filename:
            raise LifecycleError(400, 'A real document file is required')
        filename = secure_filename(uploaded.filename)
        extension = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        if extension not in ('pdf', 'jpg', 'jpeg', 'png'):
            raise LifecycleError(400, 'Only PDF, JPG, JPEG and PNG documents are accepted')
        uploaded.stream.seek(0)
        content = uploaded.stream.read(10 * 1024 * 1024 + 1)
        size = len(content)
        if size <= 0 or size > 10 * 1024 * 1024:
            raise LifecycleError(400, 'Document must be between 1 byte and 10 MB')
        signatures = {
            'pdf': content.startswith(b'%PDF-'),
            'jpg': content.startswith(b'\xff\xd8\xff'),
            'jpeg': content.startswith(b'\xff\xd8\xff'),
            'png': content.startswith(b'\x89PNG\r\n\x1a\n'),
        }
        if not signatures.get(extension):
            raise LifecycleError(400, 'Document content does not match its extension')
        declared_type = (uploaded.mimetype or '').lower()
        allowed_types = {
            'pdf': ('application/pdf', 'application/octet-stream'),
            'jpg': ('image/jpeg',), 'jpeg': ('image/jpeg',), 'png': ('image/png',),
        }
        if declared_type and declared_type not in allowed_types[extension]:
            raise LifecycleError(400, 'Document MIME type does not match its extension')
        if b'EICAR-STANDARD-ANTIVIRUS-TEST-FILE' in content:
            raise LifecycleError(400, 'Document failed the malware safety check')
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        stored_name = f'preboarding_{row[0]}_{secrets.token_hex(8)}_{filename}'
        stored_path = os.path.join(UPLOAD_FOLDER, stored_name)
        with open(stored_path, 'wb') as handle:
            handle.write(content)
        name, file_size = filename, size
        now = datetime.now()
        conn.execute(
            "INSERT INTO documents (doc_id, emp_id, name, category, file_path, file_size, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [_next_generated_id(conn, 'documents', 'doc_id'), row[1], name, doc_type,
             stored_name if stored_path else None, file_size, now],
        )
        conn.execute(
            "UPDATE onboarding_checklist SET status = 'Uploaded', uploaded_at = ?, reviewed_by = NULL, review_note = NULL, reviewed_at = NULL "
            "WHERE item_id = ?", [now, item[0]]
        )
    except LifecycleError as exc:
        if stored_path and os.path.exists(stored_path):
            os.remove(stored_path)
        return _lifecycle_error_payload(exc), exc.status_code
    except Exception as exc:
        if stored_path and os.path.exists(stored_path):
            os.remove(stored_path)
        logger.warning('preboarding upload failed: %s', exc)
        return jsonify({'error': 'Failed to store document'}), 500
    finally:
        conn.close()
    audit_log(row[1], 'ONBOARDING_DOCUMENT_UPLOAD', f'Uploaded {doc_type}', entity='onboarding_checklist', entity_id=item[0])
    return jsonify({'message': 'Document uploaded', 'doc_type': doc_type}), 201


@app.route('/api/v1/preboarding/<token>/submit', methods=['POST'])
@app.route('/api/preboarding/<token>/submit', methods=['POST'])
def submit_preboarding(token):
    try:
        with outbox.transaction() as conn:
            row = _workflow_for_token(conn, token)
            if row[9] or row[4] == 'Completed':
                return jsonify({'message': 'Pre-boarding already submitted', 'workflow_id': row[0]}), 200
            _complete_onboarding_stage_tx(conn, row[0], 1, None)
    except LifecycleError as exc:
        return _lifecycle_error_payload(exc), exc.status_code
    except Exception as exc:
        logger.warning('preboarding submit failed: %s', exc)
        return jsonify({'error': 'Failed to submit pre-boarding'}), 500
    audit_log(row[1], 'ONBOARDING_STEP_COMPLETE', 'Pre-boarding submitted', entity='onboarding_workflow', entity_id=row[0])
    return jsonify({'message': 'Pre-boarding submitted', 'workflow_id': row[0]}), 200


@app.route('/api/v1/onboarding-workflows', methods=['GET'])
@app.route('/api/onboarding-workflows', methods=['GET'])
@login_required
def onboarding_workflows_api():
    conn = get_db()
    try:
        actor = _lifecycle_actor(conn)
        if not _can_manage_lifecycle(actor):
            rows = conn.execute(
                "SELECT DISTINCT w.workflow_id FROM onboarding_workflow w "
                "LEFT JOIN users u ON u.emp_id = w.emp_id "
                "WHERE w.emp_id = ? OR u.manager_emp_id = ? OR EXISTS ("
                "SELECT 1 FROM onboarding_tasks t WHERE t.emp_id = w.emp_id AND t.assigned_to = ?"
                ") ORDER BY w.created_at DESC",
                [session['emp_id'], session['emp_id'], session['emp_id']],
            ).fetchall()
        else:
            rows = conn.execute("SELECT workflow_id FROM onboarding_workflow ORDER BY created_at DESC").fetchall()
        data = []
        for (workflow_id,) in rows:
            row = _onboarding_workflow_row(conn, workflow_id)
            if row:
                data.append(_onboarding_workflow_summary(conn, row))
        return jsonify(data), 200
    finally:
        conn.close()


@app.route('/api/v1/onboarding-workflows/<int:workflow_id>', methods=['GET'])
@app.route('/api/onboarding-workflows/<int:workflow_id>', methods=['GET'])
@login_required
def onboarding_workflow_detail(workflow_id):
    conn = get_db()
    try:
        row = _onboarding_workflow_row(conn, workflow_id)
        if not row:
            return jsonify({'error': 'Onboarding workflow not found'}), 404
        actor = _lifecycle_actor(conn)
        assigned = conn.execute(
            "SELECT 1 FROM onboarding_tasks WHERE emp_id = ? AND assigned_to = ? LIMIT 1",
            [row[1], session.get('emp_id')],
        ).fetchone()
        manager = conn.execute(
            "SELECT 1 FROM users WHERE emp_id = ? AND manager_emp_id = ? LIMIT 1",
            [row[1], session.get('emp_id')],
        ).fetchone()
        if not (_can_manage_lifecycle(actor) or row[1] == session.get('emp_id') or assigned or manager):
            return jsonify({'error': 'Forbidden'}), 403
        return jsonify(_onboarding_workflow_summary(conn, row)), 200
    finally:
        conn.close()


@app.route('/api/v1/onboarding-workflows/<int:workflow_id>/steps/<int:step>/complete', methods=['POST'])
@app.route('/api/onboarding-workflows/<int:workflow_id>/steps/<int:step>/complete', methods=['POST'])
@login_required
def complete_onboarding_step(workflow_id, step):
    data = request.get_json(silent=True) or {}
    try:
        with outbox.transaction() as conn:
            actor = _lifecycle_actor(conn)
            row = _onboarding_workflow_row(conn, workflow_id)
            if not row:
                raise LifecycleError(404, 'Onboarding workflow not found')
            if step in (1, 2) and not _can_manage_lifecycle(actor):
                raise LifecycleError(403, 'HR/Admin access required')
            if step == 3 and not _can_manage_lifecycle(actor, include_it=True):
                raise LifecycleError(403, 'IT/Admin access required')
            if step == 4 and not _can_manage_lifecycle(actor, include_it=True):
                raise LifecycleError(403, 'IT/Admin access required')
            if step == 5:
                target_manager = conn.execute(
                    "SELECT manager_emp_id FROM users WHERE emp_id = ?", [row[1]]
                ).fetchone()
                assigned_buddy = conn.execute(
                    "SELECT assigned_to FROM onboarding_tasks WHERE emp_id = ? AND stage = 5 "
                    "ORDER BY task_id LIMIT 1", [row[1]]
                ).fetchone()
                allowed_owners = {target_manager[0] if target_manager else None, assigned_buddy[0] if assigned_buddy else None}
                if not (_can_manage_lifecycle(actor) or actor[0] in allowed_owners):
                    raise LifecycleError(403, 'Buddy/manager access required')
            if step in (3, 4, 5):
                pending_tasks = conn.execute(
                    "SELECT COUNT(*) FROM onboarding_tasks WHERE emp_id = ? AND stage = ? AND status <> 'Completed'",
                    [row[1], step],
                ).fetchone()[0]
                if pending_tasks:
                    raise LifecycleError(409, f'All onboarding tasks for step {step} must be completed first',
                                         pending_tasks=int(pending_tasks))
            _complete_onboarding_stage_tx(conn, workflow_id, step, actor)
            if step == 5 and data.get('notify') is not False:
                _insert_lifecycle_notification(conn, row[1], 'Onboarding completed', '/onboarding', 'Onboarding')
    except LifecycleError as exc:
        return _lifecycle_error_payload(exc), exc.status_code
    except Exception as exc:
        logger.warning('onboarding step completion failed: %s', exc)
        return jsonify({'error': 'Failed to complete onboarding step'}), 500
    audit_log(session['emp_id'], 'ONBOARDING_STEP_COMPLETE', f'Completed onboarding step {step}', entity='onboarding_workflow', entity_id=workflow_id)
    return jsonify({'message': f'Onboarding step {step} completed', 'workflow_id': workflow_id}), 200


@app.route('/api/v1/onboarding-checklist/<int:item_id>/review', methods=['POST'])
@app.route('/api/onboarding-checklist/<int:item_id>/review', methods=['POST'])
@hr_or_admin_required
def review_onboarding_document(item_id):
    data = request.get_json(silent=True) or {}
    status = data.get('status')
    if status not in ('Approved', 'Rejected'):
        return jsonify({'error': 'status must be Approved or Rejected'}), 400
    note = (data.get('note') or '').strip()
    if status == 'Rejected' and not note:
        return jsonify({'error': 'A rejection note is required'}), 400
    try:
        with outbox.transaction() as conn:
            item = conn.execute(
                "SELECT c.workflow_id, c.doc_type, w.emp_id, w.step1_status, c.status "
                "FROM onboarding_checklist c JOIN onboarding_workflow w ON w.workflow_id = c.workflow_id "
                "WHERE c.item_id = ?",
                [item_id],
            ).fetchone()
            if not item:
                raise LifecycleError(404, 'Checklist item not found')
            if item[3] != 'Completed':
                raise LifecycleError(409, 'Pre-boarding must be submitted before document review')
            if item[4] != 'Uploaded':
                raise LifecycleError(409, 'Only an uploaded document can be reviewed')
            now = datetime.now()
            conn.execute(
                "UPDATE onboarding_checklist SET status = ?, reviewed_by = ?, review_note = ?, reviewed_at = ? "
                "WHERE item_id = ?",
                ['Approved' if status == 'Approved' else 'Rejected', session['emp_id'], note, now, item_id],
            )
            if status == 'Approved' and _required_docs_approved(conn, item[0]):
                _complete_onboarding_stage_tx(conn, item[0], 2, session['emp_id'])
            _insert_lifecycle_notification(
                conn, item[2], f'Document {item[1]} {status.lower()}', '/onboarding', 'Onboarding'
            )
    except LifecycleError as exc:
        return _lifecycle_error_payload(exc), exc.status_code
    except Exception as exc:
        logger.warning('onboarding checklist review failed: %s', exc)
        return jsonify({'error': 'Failed to review document'}), 500
    audit_log(session['emp_id'], 'ONBOARDING_DOCUMENT_REVIEW', f'{status} checklist item {item_id}', entity='onboarding_checklist', entity_id=item_id)
    return jsonify({'message': f'Document {status.lower()}', 'workflow_id': item[0]}), 200


@app.route('/api/v1/onboarding-tasks', methods=['GET', 'POST'])
@app.route('/api/onboarding-tasks', methods=['GET', 'POST'])
@login_required
def onboarding_api():
    if request.method == 'GET':
        conn = get_db()
        actor = _lifecycle_actor(conn)
        if _can_manage_lifecycle(actor, include_it=True):
            rows = conn.execute(
                "SELECT t.task_id, t.emp_id, u.name, t.task_name, t.assigned_to, t.status, t.due_date, t.completed_at, t.stage "
                "FROM onboarding_tasks t JOIN users u ON t.emp_id = u.emp_id ORDER BY t.task_id DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT t.task_id, t.emp_id, u.name, t.task_name, t.assigned_to, t.status, t.due_date, t.completed_at, t.stage "
                "FROM onboarding_tasks t JOIN users u ON t.emp_id = u.emp_id "
                "WHERE t.emp_id = ? OR t.assigned_to = ? ORDER BY t.task_id DESC",
                [session['emp_id'], session['emp_id']],
            ).fetchall()
        conn.close()
        return jsonify([{
            'id': r[0], 'emp_id': r[1], 'employee': r[2], 'task': r[3], 'assigned_to': r[4],
            'status': r[5], 'due_date': r[6].isoformat() if r[6] else None,
            'completed_at': r[7].isoformat() if r[7] else None, 'stage': r[8],
        } for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('emp_id') or not data.get('task_name'):
        return jsonify({'error': 'emp_id and task_name required'}), 400
    conn = get_db()
    try:
        actor = _lifecycle_actor(conn)
        if not _can_manage_lifecycle(actor, include_it=True):
            return jsonify({'error': 'HR/Admin/IT access required'}), 403
        if not conn.execute("SELECT 1 FROM users WHERE emp_id = ?", [data['emp_id']]).fetchone():
            return jsonify({'error': 'Employee not found'}), 404
        if not _task_owner_valid(conn, data.get('assigned_to', 'HR')):
            return jsonify({'error': 'assigned_to must be an owner employee or HR/IT/Finance/Manager'}), 400
        try:
            stage = int(data.get('stage', 1))
        except (TypeError, ValueError):
            return jsonify({'error': 'stage must be an integer from 1 to 5'}), 400
        if not 1 <= stage <= 5:
            return jsonify({'error': 'stage must be an integer from 1 to 5'}), 400
        tid = _next_generated_id(conn, 'onboarding_tasks', 'task_id')
        conn.execute(
            "INSERT INTO onboarding_tasks (task_id, emp_id, task_name, assigned_to, status, due_date, completed_at, stage) "
            "VALUES (?, ?, ?, ?, 'Pending', ?, NULL, ?)",
            [tid, data['emp_id'], data['task_name'], data.get('assigned_to', 'HR'),
             parse_date(data.get('due_date')), stage],
        )
    finally:
        conn.close()
    audit_log(session['emp_id'], 'ONBOARDING_TASK_CREATE', f'Created onboarding task {tid}', entity='onboarding_tasks', entity_id=tid)
    return jsonify({'message': 'Task added', 'id': tid}), 201


@app.route('/api/v1/onboarding-tasks/<int:tid>/complete', methods=['POST'])
@app.route('/api/onboarding-tasks/<int:tid>/complete', methods=['POST'])
@login_required
def complete_onboarding_task(tid):
    try:
        with outbox.transaction() as conn:
            task = conn.execute("SELECT emp_id, stage, assigned_to FROM onboarding_tasks WHERE task_id = ?", [tid]).fetchone()
            if not task:
                raise LifecycleError(404, 'Onboarding task not found')
            actor = _lifecycle_actor(conn)
            if not (_can_manage_lifecycle(actor, include_it=True) or task[2] == session.get('emp_id')):
                raise LifecycleError(403, 'Assigned owner or HR/Admin/IT access required')
            conn.execute("UPDATE onboarding_tasks SET status = 'Completed', completed_at = ? WHERE task_id = ?", [datetime.now(), tid])
            workflow = conn.execute(
                "SELECT workflow_id FROM onboarding_workflow WHERE emp_id = ? AND completed = 0 ORDER BY workflow_id DESC LIMIT 1",
                [task[0]],
            ).fetchone()
            if workflow and task[1] in (3, 4, 5):
                pending = conn.execute(
                    "SELECT COUNT(*) FROM onboarding_tasks WHERE emp_id = ? AND stage = ? AND status <> 'Completed'",
                    [task[0], task[1]],
                ).fetchone()[0]
                if not pending:
                    _complete_onboarding_stage_tx(conn, workflow[0], task[1], actor)
    except LifecycleError as exc:
        return _lifecycle_error_payload(exc), exc.status_code
    except Exception as exc:
        logger.warning('onboarding task completion failed: %s', exc)
        return jsonify({'error': 'Failed to complete onboarding task'}), 500
    audit_log(session['emp_id'], 'ONBOARDING_TASK_COMPLETE', f'Completed onboarding task {tid}', entity='onboarding_tasks', entity_id=tid)
    return jsonify({'message': 'Task completed'}), 200


@app.route('/offboarding')
@login_required
def offboarding_page():
    return render_template('offboarding.html')


@app.route('/api/v1/offboarding-tasks', methods=['GET', 'POST'])
@app.route('/api/offboarding-tasks', methods=['GET', 'POST'])
@login_required
def offboarding_api():
    if request.method == 'GET':
        conn = get_db()
        actor = _lifecycle_actor(conn)
        if _can_manage_lifecycle(actor, include_it=True, include_finance=True):
            rows = conn.execute(
                "SELECT t.task_id, t.emp_id, u.name, t.task_name, t.assigned_to, t.status, t.due_date, t.completed_at, t.stage "
                "FROM offboarding_tasks t JOIN users u ON t.emp_id = u.emp_id ORDER BY t.task_id DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT t.task_id, t.emp_id, u.name, t.task_name, t.assigned_to, t.status, t.due_date, t.completed_at, t.stage "
                "FROM offboarding_tasks t JOIN users u ON t.emp_id = u.emp_id "
                "WHERE t.emp_id = ? OR t.assigned_to = ? ORDER BY t.task_id DESC",
                [session['emp_id'], session['emp_id']],
            ).fetchall()
        conn.close()
        return jsonify([{
            'id': r[0], 'emp_id': r[1], 'employee': r[2], 'task': r[3], 'assigned_to': r[4],
            'status': r[5], 'due_date': r[6].isoformat() if r[6] else None,
            'completed_at': r[7].isoformat() if r[7] else None, 'stage': r[8],
        } for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('emp_id') or not data.get('task_name'):
        return jsonify({'error': 'emp_id and task_name required'}), 400
    conn = get_db()
    try:
        actor = _lifecycle_actor(conn)
        if not _can_manage_lifecycle(actor, include_it=True, include_finance=True):
            return jsonify({'error': 'HR/Admin/IT/Finance access required'}), 403
        if not conn.execute("SELECT 1 FROM users WHERE emp_id = ?", [data['emp_id']]).fetchone():
            return jsonify({'error': 'Employee not found'}), 404
        if not _task_owner_valid(conn, data.get('assigned_to', 'HR')):
            return jsonify({'error': 'assigned_to must be an owner employee or HR/IT/Finance/Manager'}), 400
        try:
            stage = int(data.get('stage', 1))
        except (TypeError, ValueError):
            return jsonify({'error': 'stage must be an integer from 1 to 5'}), 400
        if not 1 <= stage <= 5:
            return jsonify({'error': 'stage must be an integer from 1 to 5'}), 400
        tid = _next_generated_id(conn, 'offboarding_tasks', 'task_id')
        conn.execute(
            "INSERT INTO offboarding_tasks (task_id, emp_id, task_name, assigned_to, status, due_date, completed_at, stage) "
            "VALUES (?, ?, ?, ?, 'Pending', ?, NULL, ?)",
            [tid, data['emp_id'], data['task_name'], data.get('assigned_to', 'HR'),
             parse_date(data.get('due_date')), stage],
        )
    finally:
        conn.close()
    audit_log(session['emp_id'], 'OFFBOARDING_TASK_CREATE', f'Created offboarding task {tid}', entity='offboarding_tasks', entity_id=tid)
    return jsonify({'message': 'Task added', 'id': tid}), 201


@app.route('/api/v1/offboarding-tasks/<int:tid>/complete', methods=['POST'])
@app.route('/api/offboarding-tasks/<int:tid>/complete', methods=['POST'])
@login_required
def complete_offboarding_task(tid):
    conn = get_db()
    try:
        task = conn.execute("SELECT emp_id, stage, assigned_to FROM offboarding_tasks WHERE task_id = ?", [tid]).fetchone()
        if not task:
            return jsonify({'error': 'Offboarding task not found'}), 404
        actor = _lifecycle_actor(conn)
        if not (_can_manage_lifecycle(actor, include_it=True, include_finance=True) or task[2] == session.get('emp_id')):
            return jsonify({'error': 'Assigned owner or HR/Admin/IT/Finance access required'}), 403
        conn.execute("UPDATE offboarding_tasks SET status = 'Completed', completed_at = ? WHERE task_id = ?", [datetime.now(), tid])
    finally:
        conn.close()
    audit_log(session['emp_id'], 'OFFBOARDING_TASK_COMPLETE', f'Completed offboarding task {tid}', entity='offboarding_tasks', entity_id=tid)
    return jsonify({'message': 'Task completed'}), 200


@app.route('/api/v1/exit-interviews', methods=['GET', 'POST'])
@app.route('/api/exit-interviews', methods=['GET', 'POST'])
@hr_or_admin_required
def exit_interviews_api():
    if request.method == 'GET':
        conn = get_db()
        rows = conn.execute(
            "SELECT ei.interview_id, ei.emp_id, u.name, ei.reason, ei.feedback, ei.exit_date, ei.created_at "
            "FROM exit_interviews ei JOIN users u ON ei.emp_id = u.emp_id ORDER BY ei.created_at DESC"
        ).fetchall()
        conn.close()
        return jsonify([{
            'id': r[0], 'emp_id': r[1], 'employee': r[2], 'reason': r[3], 'feedback': r[4],
            'exit_date': r[5].isoformat() if r[5] else None, 'created_at': r[6].isoformat() if r[6] else None,
        } for r in rows]), 200
    data = request.get_json(silent=True) or {}
    exit_date = parse_date(data.get('exit_date'))
    if not data.get('emp_id') or not data.get('reason') or not exit_date:
        return jsonify({'error': 'emp_id, reason, exit_date required'}), 400
    conn = get_db()
    try:
        eid = _next_generated_id(conn, 'exit_interviews', 'interview_id')
        columns = 'interview_id, emp_id, reason, feedback, exit_date, created_at'
        values = [eid, data['emp_id'], data['reason'], data.get('feedback'), exit_date, datetime.now()]
        try:
            conn.execute(f"INSERT INTO exit_interviews ({columns}, offboard_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         [*values, data.get('offboard_id')])
        except Exception:
            conn.execute(f"INSERT INTO exit_interviews ({columns}) VALUES (?, ?, ?, ?, ?, ?)", values)
    finally:
        conn.close()
    audit_log(session['emp_id'], 'EXIT_INTERVIEW_CREATE', f'Recorded exit interview for {data["emp_id"]}', entity='exit_interviews', entity_id=eid)
    return jsonify({'message': 'Exit interview recorded', 'id': eid}), 201


def _offboarding_workflow_row(conn, offboard_id):
    return conn.execute(
        "SELECT w.offboard_id, w.resignation_id, w.emp_id, w.stage1_status, w.stage2_status, "
        "w.stage3_status, w.stage4_status, w.stage5_status, w.completed, w.completed_at, w.created_at, "
        "r.notice_date, r.last_working_day, r.reason, r.status, u.name, u.manager_emp_id "
        "FROM offboarding_workflow w JOIN resignations r ON r.resignation_id = w.resignation_id "
        "JOIN users u ON u.emp_id = w.emp_id WHERE w.offboard_id = ?",
        [offboard_id],
    ).fetchone()


def _calculate_offboarding_settlement(conn, emp_id):
    """Calculate the F&F components required by FR-OFF-03."""
    salary = conn.execute(
        "SELECT basic, hra, allowances FROM salary_structures WHERE emp_id = ? "
        "ORDER BY effective_from DESC LIMIT 1", [emp_id]
    ).fetchone()
    daily_salary = sum(float(value or 0) for value in salary) / 30 if salary else 0
    pending_payroll = float(conn.execute(
        "SELECT COALESCE(SUM(p.net_salary), 0) FROM payroll_items p "
        "JOIN payroll_runs r ON r.run_id = p.run_id "
        "WHERE p.emp_id = ? AND r.status NOT IN ('Finalized', 'Cancelled')", [emp_id]
    ).fetchone()[0] or 0)
    # FR-LEA-09 / FR-PAY-04: this counted `attendance_days` rows, so an employee marked
    # half-present lost a **full** day's pay — the classification FR-JOB-01 already
    # made was thrown away by the money. `lop_days` reads that classification and
    # weights Half-day as 0.5, through the same module leave uses.
    # Unbounded window on purpose: this settlement previously counted every recorded
    # `Absent`/`Half-day` row for the employee with no date filter, so the window is
    # left alone and only the **rule** is corrected. A half-day used to cost a full
    # day's pay because the row count discarded FR-JOB-01's own classification.
    lop_day_count = working_days.lop_days(conn, emp_id)
    # A Fraction would serialise as "5/2" in JSON, so it is converted here rather than
    # left to reach the response.
    lop_adjustment = round(daily_salary * float(lop_day_count), 2)
    reserved_expr = 'reserved' if _has_column(conn, 'leave_balance', 'reserved') else '0'
    leave_encashment = round(daily_salary * float(conn.execute(
        f"SELECT COALESCE(SUM(GREATEST(total_days - used_days - {reserved_expr}, 0)), 0) "
        "FROM leave_balance WHERE emp_id = ?",
        [emp_id],
    ).fetchone()[0] or 0), 2)
    deductions = float(conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM expense_claims WHERE emp_id = ? AND status = 'Approved'",
        [emp_id],
    ).fetchone()[0] or 0)
    asset_damage = 0.0
    total_amount = round(pending_payroll + leave_encashment - lop_adjustment - deductions + asset_damage, 2)
    return {
        'pending_payroll': round(pending_payroll, 2),
        'lop_adjustment': lop_adjustment,
        'leave_encashment': leave_encashment,
        'deductions': round(deductions, 2),
        'asset_damage': asset_damage,
        'total_amount': total_amount,
    }


def _offboarding_workflow_summary(conn, row):
    tasks = conn.execute(
        "SELECT task_id, task_name, assigned_to, status, due_date, completed_at, stage "
        "FROM offboarding_tasks WHERE emp_id = ? ORDER BY COALESCE(stage, 1), task_id",
        [row[2]],
    ).fetchall()
    approvals = conn.execute(
        "SELECT actor_emp_id, action, from_status, to_status, created_at "
        "FROM offboarding_approvals WHERE offboard_id = ? ORDER BY approval_id",
        [row[0]],
    ).fetchall()
    settlement = conn.execute(
        "SELECT pending_payroll, lop_adjustment, leave_encashment, deductions, asset_damage, "
        "total_amount, status, prepared_by, prepared_at, approved_by, approved_at "
        "FROM offboarding_settlements WHERE offboard_id = ?", [row[0]]
    ).fetchone()
    settlement_data = None
    if settlement:
        settlement_data = {
            'pending_payroll': float(settlement[0]), 'lop_adjustment': float(settlement[1]),
            'leave_encashment': float(settlement[2]), 'deductions': float(settlement[3]),
            'asset_damage': float(settlement[4]), 'total_amount': float(settlement[5]),
            'status': settlement[6], 'prepared_by': settlement[7], 'prepared_at': _iso(settlement[8]),
            'approved_by': settlement[9], 'approved_at': _iso(settlement[10]),
        }
    return {
        'offboard_id': row[0], 'resignation_id': row[1], 'emp_id': row[2], 'employee': row[15],
        'notice_date': _iso(row[11]), 'last_working_day': _iso(row[12]), 'reason': row[13],
        'resignation_status': row[14],
        'stages': {
            'stage1': row[3], 'stage2': row[4], 'stage3': row[5],
            'stage4': row[6], 'stage5': row[7],
        },
        'completed': bool(row[8]), 'completed_at': _iso(row[9]), 'created_at': _iso(row[10]),
        'tasks': [{
            'id': task[0], 'task': task[1], 'assigned_to': task[2], 'status': task[3],
            'due_date': _iso(task[4]), 'completed_at': _iso(task[5]), 'stage': task[6],
        } for task in tasks],
        'approvals': [{
            'actor_emp_id': approval[0], 'action': approval[1],
            'from_status': approval[2], 'to_status': approval[3], 'created_at': _iso(approval[4]),
        } for approval in approvals],
        'settlement': settlement_data,
    }


def _offboarding_actor_allowed(conn, actor, stage, workflow):
    if not actor:
        return False
    role = str(actor[1] or '')
    if role in ('Admin', 'Super Admin') or actor[2] == 'HR':
        return True
    target = conn.execute(
        "SELECT manager_emp_id FROM users WHERE emp_id = ?", [workflow[2]]
    ).fetchone()
    manager_id = target[0] if target else None
    if stage in (1, 2) and role == 'Team Leader' and actor[0] == manager_id:
        return True
    if stage == 3 and role == 'IT':
        return True
    if stage == 4 and role == 'Finance':
        return True
    return False


def _set_offboarding_stage(conn, offboard_id, stage, status):
    columns = {
        1: 'stage1_status', 2: 'stage2_status', 3: 'stage3_status',
        4: 'stage4_status', 5: 'stage5_status',
    }
    if stage not in columns:
        raise LifecycleError(400, 'Invalid offboarding stage')
    conn.execute(
        f"UPDATE offboarding_workflow SET {columns[stage]} = ? WHERE offboard_id = ?",
        [status, offboard_id],
    )


def _record_offboarding_approval(conn, offboard_id, actor, action, from_status, to_status):
    conn.execute(
        "INSERT INTO offboarding_approvals (approval_id, offboard_id, actor_emp_id, action, from_status, to_status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [_next_generated_id(conn, 'offboarding_approvals', 'approval_id'), offboard_id,
         actor, action, from_status, to_status, datetime.now()],
    )


def _complete_offboarding_stage_tx(conn, offboard_id, stage, actor, action='complete'):
    workflow = _offboarding_workflow_row(conn, offboard_id)
    if not workflow:
        raise LifecycleError(404, 'Offboarding workflow not found')
    if stage < 1 or stage > 5:
        raise LifecycleError(400, 'Invalid offboarding stage')
    statuses = {1: workflow[3], 2: workflow[4], 3: workflow[5], 4: workflow[6], 5: workflow[7]}
    if statuses[stage] == 'Completed':
        raise LifecycleError(409, f'Offboarding stage {stage} is already complete')
    if stage in (2, 3, 4, 5) and workflow[3] != 'Completed':
        raise LifecycleError(409, 'Stage 1 must be acknowledged first')
    if stage == 5 and workflow[6] != 'Completed':
        raise LifecycleError(409, 'Full and final settlement must be approved before exit clearance')
    if stage == 4:
        prepared = conn.execute(
            "SELECT actor_emp_id FROM offboarding_approvals WHERE offboard_id = ? AND action = 'Prepare' "
            "ORDER BY approval_id DESC LIMIT 1", [offboard_id]
        ).fetchone()
        if not prepared:
            raise LifecycleError(409, 'Finance must prepare the settlement before approval')
        if workflow[4] != 'Completed' or workflow[5] != 'Completed':
            raise LifecycleError(409, 'Stages 2 and 3 must both be complete before settlement approval')
        if prepared[0] == actor[0]:
            raise LifecycleError(409, 'Settlement preparer cannot approve their own calculation')
    if stage == 3:
        outstanding = conn.execute(
            "SELECT COUNT(*) FROM assets WHERE emp_id = ? AND (status IS NULL OR status <> 'Returned')",
            [workflow[2]],
        ).fetchone()[0]
        if outstanding:
            raise LifecycleError(409, 'All issued assets must be returned before IT clearance', outstanding_assets=int(outstanding))
    now = datetime.now()
    _set_offboarding_stage(conn, offboard_id, stage, 'Completed')
    if stage == 1:
        _set_offboarding_stage(conn, offboard_id, 2, 'InProgress')
        _set_offboarding_stage(conn, offboard_id, 3, 'InProgress')
        conn.execute("UPDATE resignations SET status = 'Accepted' WHERE resignation_id = ?", [workflow[1]])
    elif stage in (2, 3):
        if workflow[4] == 'Completed' and workflow[5] == 'Completed':
            _set_offboarding_stage(conn, offboard_id, 4, 'InProgress')
    elif stage == 4:
        _record_offboarding_approval(conn, offboard_id, actor[0], 'Approve', 'InProgress', 'Completed')
        conn.execute(
            "UPDATE offboarding_settlements SET status = 'Approved', approved_by = ?, approved_at = ? WHERE offboard_id = ?",
            [actor[0], now, offboard_id],
        )
        _set_offboarding_stage(conn, offboard_id, 5, 'InProgress')
    elif stage == 5:
        conn.execute(
            "UPDATE offboarding_workflow SET completed = 1, completed_at = ? WHERE offboard_id = ?",
            [now, offboard_id],
        )
    if stage != 4:
        _record_offboarding_approval(conn, offboard_id, actor[0], f'Stage{stage}', statuses[stage], 'Completed')
    conn.execute(
        "UPDATE offboarding_tasks SET status = 'Completed', completed_at = ? WHERE emp_id = ? AND stage = ? AND status <> 'Completed'",
        [now, workflow[2], stage],
    )
    return _offboarding_workflow_row(conn, offboard_id)


@app.route('/api/v1/resignations', methods=['GET', 'POST'])
@app.route('/api/resignations', methods=['GET', 'POST'])
@login_required
@idempotent
def resignations_api():
    if request.method == 'GET':
        conn = get_db()
        actor = _lifecycle_actor(conn)
        if _can_manage_lifecycle(actor, include_it=True, include_finance=True):
            rows = conn.execute("SELECT offboard_id FROM offboarding_workflow ORDER BY created_at DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT w.offboard_id FROM offboarding_workflow w "
                "LEFT JOIN users u ON u.emp_id = w.emp_id "
                "WHERE w.emp_id = ? OR u.manager_emp_id = ? OR EXISTS ("
                "SELECT 1 FROM offboarding_tasks t WHERE t.emp_id = w.emp_id AND t.assigned_to = ?"
                ") ORDER BY w.created_at DESC",
                [session['emp_id'], session['emp_id'], session['emp_id']],
            ).fetchall()
        data = []
        for (offboard_id,) in rows:
            row = _offboarding_workflow_row(conn, offboard_id)
            if row:
                data.append(_offboarding_workflow_summary(conn, row))
        conn.close()
        return jsonify(data), 200

    data = request.get_json(silent=True) or {}
    notice_date = parse_date(data.get('notice_date'))
    last_working_day = parse_date(data.get('last_working_day'))
    if not notice_date or not last_working_day or last_working_day < notice_date:
        return jsonify({'error': 'notice_date and a valid last_working_day are required'}), 400
    actor = _lifecycle_actor()
    if not actor:
        return jsonify({'error': 'Authentication required'}), 401
    target_emp_id = session['emp_id'] if actor[1] not in ('Admin', 'Super Admin') and actor[2] != 'HR' else data.get('emp_id', session['emp_id'])
    if not target_emp_id:
        return jsonify({'error': 'emp_id is required for HR/Admin'}), 400
    result = None
    try:
        with outbox.transaction() as conn:
            target = conn.execute("SELECT emp_id, name, status FROM users WHERE emp_id = ?", [target_emp_id]).fetchone()
            if not target:
                raise LifecycleError(404, 'Employee not found')
            if target[2] in ('Inactive', 'Blocked'):
                raise LifecycleError(409, 'Inactive employees cannot start offboarding')
            if conn.execute(
                "SELECT 1 FROM resignations WHERE emp_id = ? AND status NOT IN ('Cancelled', 'Revoked')",
                [target_emp_id],
            ).fetchone():
                raise LifecycleError(409, 'An active resignation already exists for this employee')
            rid = _next_generated_id(conn, 'resignations', 'resignation_id')
            oid = _next_generated_id(conn, 'offboarding_workflow', 'offboard_id')
            now = datetime.now()
            conn.execute(
                "INSERT INTO resignations (resignation_id, emp_id, notice_date, last_working_day, reason, initiated_by, status, created_at, version) "
                "VALUES (?, ?, ?, ?, ?, ?, 'Pending', ?, 1)",
                [rid, target_emp_id, notice_date, last_working_day, data.get('reason'), session['emp_id'], now],
            )
            conn.execute(
                "INSERT INTO offboarding_workflow (offboard_id, resignation_id, emp_id, stage1_status, stage2_status, stage3_status, stage4_status, stage5_status, completed, created_at) "
                "VALUES (?, ?, ?, 'InProgress', 'Pending', 'Pending', 'Pending', 'Pending', 0, ?)",
                [oid, rid, target_emp_id, now],
            )
            task_specs = (
                ('Accept resignation', 'HR', 1),
                ('Knowledge transfer and manager clearance', data.get('manager_emp_id') or 'Manager', 2),
                ('Return assets and IT clearance', 'IT', 3),
                ('Full and final settlement', 'Finance', 4),
                ('Exit interview and access revocation', 'HR', 5),
            )
            for task_name, assigned_to, stage in task_specs:
                conn.execute(
                    "INSERT INTO offboarding_tasks (task_id, emp_id, task_name, assigned_to, status, due_date, completed_at, stage) "
                    "VALUES (?, ?, ?, ?, 'Pending', ?, NULL, ?)",
                    [_next_generated_id(conn, 'offboarding_tasks', 'task_id'), target_emp_id, task_name,
                     assigned_to, last_working_day, stage],
                )
            result = {'resignation_id': rid, 'offboard_id': oid, 'emp_id': target_emp_id}
    except LifecycleError as exc:
        return _lifecycle_error_payload(exc), exc.status_code
    except Exception as exc:
        logger.warning('create resignation failed: %s', exc)
        return jsonify({'error': 'Failed to create resignation'}), 500
    audit_log(session['emp_id'], 'RESIGNATION_CREATE', f'Created resignation for {result["emp_id"]}', entity='resignations', entity_id=result['resignation_id'])
    return jsonify({'message': 'Resignation recorded', **result}), 201


@app.route('/api/v1/resignations/<int:resignation_id>/acknowledge', methods=['POST'])
@app.route('/api/resignations/<int:resignation_id>/acknowledge', methods=['POST'])
@login_required
def acknowledge_resignation(resignation_id):
    try:
        with outbox.transaction() as conn:
            row = conn.execute(
                "SELECT w.offboard_id, w.emp_id, w.stage1_status FROM offboarding_workflow w "
                "JOIN resignations r ON r.resignation_id = w.resignation_id WHERE r.resignation_id = ?",
                [resignation_id],
            ).fetchone()
            if not row:
                raise LifecycleError(404, 'Resignation not found')
            if row[2] == 'Completed':
                raise LifecycleError(409, 'Resignation is already acknowledged')
            workflow = _offboarding_workflow_row(conn, row[0])
            actor = _lifecycle_actor(conn)
            if not _offboarding_actor_allowed(conn, actor, 1, workflow):
                raise LifecycleError(403, 'Only the employee\'s manager or HR/Admin may acknowledge resignation')
            _complete_offboarding_stage_tx(conn, row[0], 1, actor)
    except LifecycleError as exc:
        return _lifecycle_error_payload(exc), exc.status_code
    except Exception as exc:
        logger.warning('acknowledge resignation failed: %s', exc)
        return jsonify({'error': 'Failed to acknowledge resignation'}), 500
    audit_log(session['emp_id'], 'RESIGNATION_ACKNOWLEDGE', f'Acknowledged resignation {resignation_id}', entity='resignations', entity_id=resignation_id)
    return jsonify({'message': 'Resignation acknowledged'}), 200


@app.route('/api/v1/offboarding-workflows', methods=['GET'])
@app.route('/api/offboarding-workflows', methods=['GET'])
@login_required
def offboarding_workflows_api():
    return resignations_api()


@app.route('/api/v1/offboarding-workflows/<int:offboard_id>', methods=['GET'])
@app.route('/api/offboarding-workflows/<int:offboard_id>', methods=['GET'])
@login_required
def offboarding_workflow_detail(offboard_id):
    conn = get_db()
    try:
        row = _offboarding_workflow_row(conn, offboard_id)
        if not row:
            return jsonify({'error': 'Offboarding workflow not found'}), 404
        actor = _lifecycle_actor(conn)
        assigned = conn.execute(
            "SELECT 1 FROM offboarding_tasks WHERE emp_id = ? AND assigned_to = ? LIMIT 1",
            [row[2], session.get('emp_id')],
        ).fetchone()
        manager = conn.execute(
            "SELECT 1 FROM users WHERE emp_id = ? AND manager_emp_id = ? LIMIT 1",
            [row[2], session.get('emp_id')],
        ).fetchone()
        if not (_can_manage_lifecycle(actor, include_it=True, include_finance=True)
                or row[2] == session.get('emp_id') or assigned or manager):
            return jsonify({'error': 'Forbidden'}), 403
        return jsonify(_offboarding_workflow_summary(conn, row)), 200
    finally:
        conn.close()


@app.route('/api/v1/offboarding-workflows/<int:offboard_id>/stages/<int:stage>/complete', methods=['POST'])
@app.route('/api/offboarding-workflows/<int:offboard_id>/stages/<int:stage>/complete', methods=['POST'])
@login_required
def complete_offboarding_stage(offboard_id, stage):
    data = request.get_json(silent=True) or {}
    try:
        with outbox.transaction() as conn:
            actor = _lifecycle_actor(conn)
            workflow = _offboarding_workflow_row(conn, offboard_id)
            if not workflow:
                raise LifecycleError(404, 'Offboarding workflow not found')
            if not _offboarding_actor_allowed(conn, actor, stage, workflow):
                raise LifecycleError(403, 'You are not an owner for this offboarding stage')
            _complete_offboarding_stage_tx(conn, offboard_id, stage, actor, data.get('action', 'complete'))
    except LifecycleError as exc:
        return _lifecycle_error_payload(exc), exc.status_code
    except Exception as exc:
        logger.warning('offboarding stage completion failed: %s', exc)
        return jsonify({'error': 'Failed to complete offboarding stage'}), 500
    audit_log(session['emp_id'], 'OFFBOARDING_STAGE_COMPLETE', f'Completed offboarding stage {stage}', entity='offboarding_workflow', entity_id=offboard_id)
    return jsonify({'message': f'Offboarding stage {stage} completed', 'offboard_id': offboard_id}), 200


@app.route('/api/v1/offboarding-workflows/<int:offboard_id>/stage/<int:stage>/prepare', methods=['POST'])
@app.route('/api/offboarding-workflows/<int:offboard_id>/stage/<int:stage>/prepare', methods=['POST'])
@login_required
def prepare_offboarding_settlement(offboard_id, stage):
    if stage != 4:
        return jsonify({'error': 'Only stage 4 has a prepare/approve split'}), 400
    data = request.get_json(silent=True) or {}
    try:
        asset_damage_override = float(data['asset_damage']) if data.get('asset_damage') is not None else None
    except (TypeError, ValueError):
        return jsonify({'error': 'asset_damage must be numeric'}), 400
    if asset_damage_override is not None and asset_damage_override < 0:
        return jsonify({'error': 'asset_damage cannot be negative'}), 400
    try:
        with outbox.transaction() as conn:
            actor = _lifecycle_actor(conn)
            workflow = _offboarding_workflow_row(conn, offboard_id)
            if not workflow:
                raise LifecycleError(404, 'Offboarding workflow not found')
            if not _offboarding_actor_allowed(conn, actor, stage, workflow):
                raise LifecycleError(403, 'Finance/Admin access required')
            if workflow[4] != 'Completed' or workflow[5] != 'Completed':
                raise LifecycleError(409, 'Stages 2 and 3 must both be complete before settlement preparation')
            if workflow[6] == 'Completed':
                raise LifecycleError(409, 'Settlement is already approved')
            previous = conn.execute(
                "SELECT actor_emp_id FROM offboarding_approvals WHERE offboard_id = ? AND action = 'Prepare' "
                "ORDER BY approval_id DESC LIMIT 1", [offboard_id]
            ).fetchone()
            if previous:
                raise LifecycleError(409, 'Settlement was already prepared; a different approver must complete it')
            settlement = _calculate_offboarding_settlement(conn, workflow[2])
            if asset_damage_override is not None:
                settlement['asset_damage'] = round(asset_damage_override, 2)
                settlement['total_amount'] = round(
                    settlement['pending_payroll'] + settlement['leave_encashment']
                    - settlement['lop_adjustment'] - settlement['deductions']
                    + settlement['asset_damage'], 2
                )
            conn.execute(
                "INSERT INTO offboarding_settlements (settlement_id, offboard_id, pending_payroll, "
                "lop_adjustment, leave_encashment, deductions, asset_damage, total_amount, status, "
                "prepared_by, prepared_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Prepared', ?, ?)",
                [_next_generated_id(conn, 'offboarding_settlements', 'settlement_id'), offboard_id,
                 settlement['pending_payroll'], settlement['lop_adjustment'], settlement['leave_encashment'],
                 settlement['deductions'], settlement['asset_damage'], settlement['total_amount'], actor[0], datetime.now()],
            )
            _set_offboarding_stage(conn, offboard_id, 4, 'InProgress')
            _record_offboarding_approval(conn, offboard_id, actor[0], 'Prepare', workflow[6], 'InProgress')
    except LifecycleError as exc:
        return _lifecycle_error_payload(exc), exc.status_code
    except Exception as exc:
        logger.warning('settlement preparation failed: %s', exc)
        return jsonify({'error': 'Failed to prepare settlement'}), 500
    audit_log(session['emp_id'], 'OFFBOARDING_SETTLEMENT_PREPARE', f'Prepared settlement {offboard_id}', entity='offboarding_workflow', entity_id=offboard_id)
    return jsonify({'message': 'Settlement prepared; a different Finance/Admin user must approve', 'settlement': settlement}), 200


@app.route('/api/v1/offboarding-workflows/<int:offboard_id>/stage/<int:stage>/approve', methods=['POST'])
@app.route('/api/offboarding-workflows/<int:offboard_id>/stage/<int:stage>/approve', methods=['POST'])
@login_required
def approve_offboarding_settlement(offboard_id, stage):
    if stage != 4:
        return jsonify({'error': 'Only stage 4 has a prepare/approve split'}), 400
    try:
        with outbox.transaction() as conn:
            actor = _lifecycle_actor(conn)
            workflow = _offboarding_workflow_row(conn, offboard_id)
            if not workflow:
                raise LifecycleError(404, 'Offboarding workflow not found')
            if not _offboarding_actor_allowed(conn, actor, stage, workflow):
                raise LifecycleError(403, 'Finance/Admin access required')
            _complete_offboarding_stage_tx(conn, offboard_id, 4, actor, 'approve')
    except LifecycleError as exc:
        return _lifecycle_error_payload(exc), exc.status_code
    except Exception as exc:
        logger.warning('settlement approval failed: %s', exc)
        return jsonify({'error': 'Failed to approve settlement'}), 500
    audit_log(session['emp_id'], 'OFFBOARDING_SETTLEMENT_APPROVE', f'Approved settlement {offboard_id}', entity='offboarding_workflow', entity_id=offboard_id)
    return jsonify({'message': 'Settlement approved', 'offboard_id': offboard_id}), 200


@app.route('/api/v1/offboarding-workflows/<int:offboard_id>/revoke-access', methods=['POST'])
@app.route('/api/offboarding-workflows/<int:offboard_id>/revoke-access', methods=['POST'])
@admin_required
def revoke_offboarding_workflow_access(offboard_id):
    conn = get_db()
    try:
        row = conn.execute("SELECT emp_id, resignation_id FROM offboarding_workflow WHERE offboard_id = ?", [offboard_id]).fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({'error': 'Offboarding workflow not found'}), 404
    revoked = revoke_offboarding_access(datetime.now(IST).date(), offboard_id=offboard_id)
    # The nightly job does this on its own schedule; this route is the manual path,
    # and a manual revocation of someone's access is exactly the event an
    # investigation needs to place on a timeline.
    audit_log(
        session['emp_id'], 'OFFBOARDING_ACCESS_REVOKE',
        f'Manual access-revocation pass for offboarding {offboard_id}: '
        f'{revoked} employee(s) revoked',
        entity='offboarding_workflow', entity_id=offboard_id,
        after={'revoked': revoked, 'trigger': 'manual'},
    )
    return jsonify({'message': 'Access revocation pass completed', 'revoked': revoked}), 200


@app.route('/api/v1/admin/offboarding/revoke', methods=['POST'])
@app.route('/api/admin/offboarding/revoke', methods=['POST'])
@admin_required
def admin_revoke_offboarding_access():
    target = parse_date((request.get_json(silent=True) or {}).get('date'))
    revoked = revoke_offboarding_access(target or datetime.now(IST).date())
    for item in revoked:
        audit_log(item['emp_id'], 'ACCESS_REVOKED', 'Administrative LWD revocation pass', entity='resignations', entity_id=item['resignation_id'])
    return jsonify({'revoked': revoked, 'count': len(revoked)}), 200


# ══════════════════════════════════════════════════════════════════════
#  PHASE 2 — PAYROLL ENGINE
# ══════════════════════════════════════════════════════════════════════

@app.route('/admin/payroll')
@finance_or_admin_required
def admin_payroll():
    return render_template('payroll.html')


@app.route('/admin/salary-structures')
@finance_or_admin_required
def admin_salary():
    return render_template('salary.html')


# ── Salary Structures ────────────────────────────────────────────

@app.route('/api/v1/salary-structures', methods=['GET', 'POST'])
@app.route('/api/salary-structures', methods=['GET', 'POST'])
@finance_or_admin_required
def salary_api():
    if request.method == 'GET':
        conn = get_db()
        rows = conn.execute("SELECT s.struct_id, s.emp_id, u.name, s.basic, s.hra, s.allowances, s.deductions, s.effective_from, s.effective_to FROM salary_structures s JOIN users u ON s.emp_id = u.emp_id ORDER BY s.effective_from DESC").fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'basic': float(r[3]), 'hra': float(r[4]), 'allowances': float(r[5]), 'deductions': float(r[6]), 'effective_from': r[7].isoformat() if r[7] else None, 'effective_to': r[8].isoformat() if r[8] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('emp_id') or not data.get('basic'):
        return jsonify({'error': 'emp_id and basic required'}), 400
    conn = get_db()
    sid = _next_generated_id(conn, 'salary_structures', 'struct_id')
    conn.execute("INSERT INTO salary_structures (struct_id, emp_id, basic, hra, allowances, deductions, effective_from, effective_to) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 [sid, data['emp_id'], float(data['basic']), float(data.get('hra', 0)), float(data.get('allowances', 0)), float(data.get('deductions', 0)),
                  parse_date(data.get('effective_from'), datetime.now().date()),
                  parse_date(data.get('effective_to'))])
    conn.close()
    # A salary structure is the input to every payroll run, so a change to one is
    # the highest-value thing to be able to reconstruct afterwards. The amounts are
    # deliberately NOT copied into the audit row: audit_log is retained for years and
    # must not become a second, weaker copy of the payroll tables.
    _from = parse_date(data.get('effective_from')) or datetime.now().date()
    _to = parse_date(data.get('effective_to'))
    audit_log(
        session['emp_id'], 'SALARY_STRUCTURE_CREATE',
        f'Created salary structure {sid} for {data["emp_id"]}',
        entity='salary_structures', entity_id=sid,
        after={'emp_id': data['emp_id'], 'effective_from': _from.isoformat(),
               'effective_to': _to.isoformat() if _to else None},
    )
    return jsonify({'message': 'Salary structure saved', 'id': sid}), 201


# ── Payroll Runs ─────────────────────────────────────────────────

def calc_payroll_item(emp_id, basic, hra, allowances, deductions):
    gross = basic + hra + allowances
    pf = min(gross * 0.12, 1800)
    esi = gross * 0.0075 if gross <= 21000 else 0
    pt = 200 if gross > 10000 else 0
    total_ded = deductions + pf + esi + pt
    net = gross - total_ded
    return gross, round(total_ded, 2), round(net, 2), round(pf, 2), round(esi, 2), pt


_PAYROLL_APPROVAL_IDENTITY_CACHE: dict = {}


def _payroll_approval_id_is_identity() -> bool:
    """Detect the v2.0 identity key without mutating either schema."""
    import db_backend
    schema = db_backend.app_schema()
    key = schema
    if key not in _PAYROLL_APPROVAL_IDENTITY_CACHE:
        conn = db_backend.connect()
        try:
            row = conn.execute(
                "SELECT is_identity FROM information_schema.columns "
                "WHERE table_schema = ? AND table_name = 'payroll_approvals' "
                "AND column_name = 'approval_id'",
                [schema],
            ).fetchone()
            _PAYROLL_APPROVAL_IDENTITY_CACHE[key] = bool(row and str(row[0]).upper() == 'YES')
        except Exception:
            _PAYROLL_APPROVAL_IDENTITY_CACHE[key] = False
        finally:
            conn.close()
    return _PAYROLL_APPROVAL_IDENTITY_CACHE[key]


def _record_payroll_approval(conn, run_id, actor_emp_id, action, from_status, to_status):
    """Append one maker-checker transition to payroll_approvals."""
    now = datetime.now()
    if _payroll_approval_id_is_identity():
        conn.execute(
            "INSERT INTO payroll_approvals "
            "(run_id, actor_emp_id, action, from_status, to_status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [run_id, actor_emp_id, action, from_status, to_status, now],
        )
        return
    next_id = int(conn.execute(
        "SELECT COALESCE(MAX(approval_id), 0) + 1 FROM payroll_approvals"
    ).fetchone()[0])
    conn.execute(
        "INSERT INTO payroll_approvals "
        "(approval_id, run_id, actor_emp_id, action, from_status, to_status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [next_id, run_id, actor_emp_id, action, from_status, to_status, now],
    )


def _payroll_period_bounds(month, year):
    start = datetime(int(year), int(month), 1).date()
    if int(month) == 12:
        next_month = datetime(int(year) + 1, 1, 1).date()
    else:
        next_month = datetime(int(year), int(month) + 1, 1).date()
    return start, next_month - timedelta(days=1)


def _payroll_run(rid):
    conn = get_db()
    row = conn.execute(
        "SELECT run_id, month, year, status, submitted_by, submitted_at, "
        "approved_by, approved_at, finalized_at, adjustment_of_run_id "
        "FROM payroll_runs WHERE run_id = ?",
        [rid],
    ).fetchone()
    conn.close()
    return row


@app.route('/api/v1/payroll-runs', methods=['GET', 'POST'])
@app.route('/api/payroll-runs', methods=['GET', 'POST'])
@finance_or_admin_required
@idempotent
def payroll_runs_api():
    if request.method == 'GET':
        conn = get_db()
        rows = conn.execute(
            "SELECT run_id, month, year, processed_at, status, submitted_by, submitted_at, "
            "approved_by, approved_at, finalized_at, adjustment_of_run_id "
            "FROM payroll_runs ORDER BY year DESC, month DESC"
        ).fetchall()
        conn.close()
        return jsonify([{
            'id': r[0], 'month': r[1], 'year': r[2],
            'processed_at': r[3].isoformat() if r[3] else None,
            'status': r[4], 'submitted_by': r[5],
            'submitted_at': r[6].isoformat() if r[6] else None,
            'approved_by': r[7], 'approved_at': r[8].isoformat() if r[8] else None,
            'finalized_at': r[9].isoformat() if r[9] else None,
            'adjustment_of_run_id': r[10],
        } for r in rows]), 200

    data = request.get_json(silent=True) or {}
    try:
        month, year = int(data['month']), int(data['year'])
    except (KeyError, TypeError, ValueError):
        return jsonify({'error': 'month and year are required integers'}), 400
    if month < 1 or month > 12 or year < 2000 or year > 2200:
        return jsonify({'error': 'invalid payroll period'}), 400

    conn = get_db()
    try:
        adjustment_of_run_id = data.get('adjustment_of_run_id')
        if adjustment_of_run_id not in (None, ''):
            try:
                adjustment_of_run_id = int(adjustment_of_run_id)
            except (TypeError, ValueError):
                return jsonify({'error': 'adjustment_of_run_id must be an integer'}), 400
            original = conn.execute(
                "SELECT status FROM payroll_runs WHERE run_id = ?", [adjustment_of_run_id]
            ).fetchone()
            if not original:
                conn.close()
                return jsonify({'error': 'Original payroll run not found'}), 404
            if original[0] != 'Finalized':
                conn.close()
                return jsonify({'error': 'Only a Finalized run can be adjusted'}), 409

        if conn.execute(
            "SELECT 1 FROM payroll_runs WHERE month = ? AND year = ? AND status <> 'Cancelled'",
            [month, year],
        ).fetchone():
            conn.close()
            return jsonify({'error': 'Payroll already processed for this period'}), 409

        period_start, period_end = _payroll_period_bounds(month, year)
        rid = _next_generated_id(conn, 'payroll_runs', 'run_id')
        conn.execute(
            "INSERT INTO payroll_runs "
            "(run_id, month, year, processed_at, status, adjustment_of_run_id) "
            "VALUES (?, ?, ?, ?, 'Draft', ?)",
            [rid, month, year, datetime.now(), adjustment_of_run_id],
        )
        employees = conn.execute(
            "SELECT u.emp_id, s.basic, s.hra, s.allowances, s.deductions "
            "FROM users u JOIN salary_structures s ON s.struct_id = ("
            "SELECT s2.struct_id FROM salary_structures s2 "
            "WHERE s2.emp_id = u.emp_id AND s2.effective_from <= ? "
            "AND (s2.effective_to IS NULL OR s2.effective_to >= ?) "
            "ORDER BY s2.effective_from DESC LIMIT 1"
            ") WHERE u.status IN ('Active', 'Onboarding') ORDER BY u.emp_id",
            [period_end, period_start],
        ).fetchall()
        for emp_id, basic, hra, allowances, deductions in employees:
            gross, total_ded, net, pf, esi, pt = calc_payroll_item(
                emp_id, float(basic), float(hra), float(allowances), float(deductions)
            )
            conn.execute(
                "INSERT INTO payroll_items "
                "(item_id, run_id, emp_id, gross_salary, deductions_total, net_salary, pf, esi, pt) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [_next_generated_id(conn, 'payroll_items', 'item_id'), rid, emp_id, gross, total_ded, net, pf, esi, pt],
            )
        conn.close()
    except Exception:
        conn.close()
        raise
    audit_log(
        session['emp_id'], 'PAYROLL_RUN_CREATE',
        f'Created payroll run {rid} for {month}/{year}'
        + (f' as an adjustment of run {adjustment_of_run_id}'
           if adjustment_of_run_id else '')
        + f' with {len(employees)} employee item(s)',
        entity='payroll_runs', entity_id=rid,
        after={'month': month, 'year': year, 'status': 'Draft',
               'adjustment_of_run_id': adjustment_of_run_id,
               'employee_count': len(employees)},
    )
    return jsonify({
        'message': f'Payroll run created for {month}/{year}',
        'run_id': rid,
        'status': 'Draft',
    }), 201


@app.route('/api/v1/payroll-runs/<int:rid>/submit', methods=['POST'])
@app.route('/api/payroll-runs/<int:rid>/submit', methods=['POST'])
@finance_or_admin_required
@idempotent
def submit_payroll(rid):
    row = _payroll_run(rid)
    if not row:
        return jsonify({'error': 'Payroll run not found'}), 404
    if row[3] != 'Draft':
        return jsonify({'error': f"Payroll run is already {row[3]}"}), 409

    actor = session['emp_id']
    now = datetime.now()
    try:
        with outbox.transaction() as conn:
            conn.execute(
                "UPDATE payroll_runs SET status = 'Submitted', submitted_by = ?, submitted_at = ? "
                "WHERE run_id = ? AND status = 'Draft'",
                [actor, now, rid],
            )
            transitioned = conn.execute(
                "SELECT status, submitted_by FROM payroll_runs WHERE run_id = ?", [rid]
            ).fetchone()
            if not transitioned or transitioned[0] != 'Submitted' or transitioned[1] != actor:
                return jsonify({'error': 'Payroll run state changed; reload and retry'}), 409
            _record_payroll_approval(conn, rid, actor, 'Submit', 'Draft', 'Submitted')
    except Exception as exc:
        logger.warning('submit_payroll failed: %s', exc)
        return jsonify({'error': 'Failed to submit payroll'}), 500
    audit_log(actor, 'PAYROLL_SUBMIT', f'Payroll run {rid} submitted',
              entity='payroll_runs', entity_id=rid, before={'status': 'Draft'}, after={'status': 'Submitted'})
    return jsonify({'message': 'Payroll submitted', 'run_id': rid, 'status': 'Submitted'}), 200


@app.route('/api/v1/payroll-runs/<int:rid>/approve', methods=['POST'])
@app.route('/api/payroll-runs/<int:rid>/approve', methods=['POST'])
@finance_or_admin_required
@idempotent
def approve_payroll(rid):
    row = _payroll_run(rid)
    if not row:
        return jsonify({'error': 'Payroll run not found'}), 404
    if row[3] != 'Submitted':
        return jsonify({'error': f"Payroll run is already {row[3]}"}), 409
    actor = session['emp_id']
    if row[4] == actor:
        return jsonify({'error': 'The submitter cannot approve their own payroll run'}), 403

    now = datetime.now()
    try:
        with outbox.transaction() as conn:
            conn.execute(
                "UPDATE payroll_runs SET status = 'Approved', approved_by = ?, approved_at = ? "
                "WHERE run_id = ? AND status = 'Submitted' AND submitted_by <> ?",
                [actor, now, rid, actor],
            )
            transitioned = conn.execute(
                "SELECT status, approved_by FROM payroll_runs WHERE run_id = ?", [rid]
            ).fetchone()
            if not transitioned or transitioned[0] != 'Approved' or transitioned[1] != actor:
                return jsonify({'error': 'Payroll run state changed; reload and retry'}), 409
            _record_payroll_approval(conn, rid, actor, 'Approve', 'Submitted', 'Approved')
    except Exception as exc:
        logger.warning('approve_payroll failed: %s', exc)
        return jsonify({'error': 'Failed to approve payroll'}), 500
    audit_log(actor, 'PAYROLL_APPROVE', f'Payroll run {rid} approved',
              entity='payroll_runs', entity_id=rid, before={'status': 'Submitted'}, after={'status': 'Approved'})
    return jsonify({'message': 'Payroll approved', 'run_id': rid, 'status': 'Approved'}), 200


@app.route('/api/v1/payroll-runs/<int:rid>/finalize', methods=['POST'])
@app.route('/api/payroll-runs/<int:rid>/finalize', methods=['POST'])
@finance_or_admin_required
@idempotent
def finalize_payroll(rid):
    row = _payroll_run(rid)
    if not row:
        return jsonify({'error': 'Payroll run not found'}), 404
    if row[3] != 'Approved':
        return jsonify({'error': 'Only an Approved payroll run can be finalized'}), 409
    actor = session['emp_id']
    now = datetime.now()
    try:
        with outbox.transaction() as conn:
            conn.execute(
                "UPDATE payroll_runs SET status = 'Finalized', finalized_at = ? "
                "WHERE run_id = ? AND status = 'Approved'",
                [now, rid],
            )
            transitioned = conn.execute(
                "SELECT status, finalized_at FROM payroll_runs WHERE run_id = ?", [rid]
            ).fetchone()
            if not transitioned or transitioned[0] != 'Finalized' or transitioned[1] != now:
                return jsonify({'error': 'Payroll run state changed; reload and retry'}), 409
            _record_payroll_approval(conn, rid, actor, 'Finalize', 'Approved', 'Finalized')
            outbox.enqueue(conn, 'payroll.finalized', 'payroll_runs', str(rid), {'run_id': rid})
    except Exception as exc:
        logger.warning('finalize_payroll failed: %s', exc)
        return jsonify({'error': 'Failed to finalize payroll'}), 500
    audit_log(actor, 'PAYROLL_FINALIZE', f'Payroll run {rid} finalized',
              entity='payroll_runs', entity_id=rid, before={'status': 'Approved'}, after={'status': 'Finalized'})
    return jsonify({'message': 'Payroll finalized', 'run_id': rid, 'status': 'Finalized'}), 200


@app.route('/api/v1/payroll-runs/<int:rid>/items')
@app.route('/api/payroll-runs/<int:rid>/items')
@finance_or_admin_required
def payroll_items(rid):
    conn = get_db()
    rows = conn.execute(
        "SELECT p.item_id, p.emp_id, u.name, p.gross_salary, p.deductions_total, p.net_salary, p.pf, p.esi, p.pt, p.payslip_generated FROM payroll_items p JOIN users u ON p.emp_id = u.emp_id WHERE p.run_id = ? ORDER BY u.name",
        [rid]
    ).fetchall()
    conn.close()
    return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'gross': float(r[3]), 'deductions': float(r[4]), 'net': float(r[5]), 'pf': float(r[6]), 'esi': float(r[7]), 'pt': float(r[8]), 'payslip_generated': bool(r[9])} for r in rows]), 200


def _may_read_payslip(conn, emp_id):
    """Own payslip, or the payroll capability (CC-11 scope, not a role list)."""
    actor = policy.current_actor(conn)
    if actor.get('emp_id') == emp_id:
        return True
    return policy.can(actor, 'payroll', resource={'emp_id': emp_id}, conn=conn)


@app.route('/api/v1/payslip/<int:run_id>/<emp_id>')
@app.route('/api/payslip/<int:run_id>/<emp_id>')
@login_required
def get_payslip(run_id, emp_id):
    conn = get_db()
    if not _may_read_payslip(conn, emp_id):
        conn.close()
        return jsonify({'error': 'Forbidden'}), 403
    row = conn.execute(
        "SELECT p.item_id, r.month, r.year, p.emp_id, u.name, u.department, u.designation, p.gross_salary, p.deductions_total, p.net_salary, p.pf, p.esi, p.pt FROM payroll_items p JOIN payroll_runs r ON p.run_id = r.run_id JOIN users u ON p.emp_id = u.emp_id WHERE p.run_id = ? AND p.emp_id = ?",
        [run_id, emp_id]
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({
        'item_id': row[0], 'month': row[1], 'year': row[2], 'emp_id': row[3], 'employee': row[4],
        'department': row[5], 'designation': row[6], 'gross': float(row[7]), 'deductions': float(row[8]),
        'net': float(row[9]), 'pf': float(row[10]), 'esi': float(row[11]), 'pt': float(row[12])
    }), 200


@app.route('/api/v1/my-payslips')
@app.route('/api/my-payslips')
@login_required
def my_payslips():
    conn = get_db()
    rows = conn.execute(
        "SELECT r.run_id, r.month, r.year, p.net_salary, r.status FROM payroll_items p JOIN payroll_runs r ON p.run_id = r.run_id WHERE p.emp_id = ? ORDER BY r.year DESC, r.month DESC",
        [session['emp_id']]
    ).fetchall()
    conn.close()
    return jsonify([{'run_id': r[0], 'month': r[1], 'year': r[2], 'net': float(r[3]), 'status': r[4]} for r in rows]), 200


# ══════════════════════════════════════════════════════════════════════
#  PHASE 3 — PERFORMANCE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════

@app.route('/admin/goals')
@hr_or_admin_required
def admin_goals():
    return render_template('goals.html')


@app.route('/admin/reviews')
@hr_or_admin_required
def admin_reviews():
    return render_template('reviews.html')


@app.route('/goals')
@login_required
def goals_page():
    return render_template('my_goals.html')


# ── Goals ──────────────────────────────────────────────────────────

@app.route('/api/v1/goals', methods=['GET', 'POST'])
@app.route('/api/goals', methods=['GET', 'POST'])
@login_required
def goals_api():
    if request.method == 'GET':
        conn = get_db()
        if policy.can_view_all(policy.current_actor(conn), 'goals', conn=conn):
            rows = conn.execute("SELECT g.goal_id, g.emp_id, u.name, g.title, g.description, g.target_date, g.weight, g.rating, g.status, g.created_at FROM goals g JOIN users u ON g.emp_id = u.emp_id ORDER BY g.created_at DESC").fetchall()
        else:
            rows = conn.execute("SELECT g.goal_id, g.emp_id, u.name, g.title, g.description, g.target_date, g.weight, g.rating, g.status, g.created_at FROM goals g JOIN users u ON g.emp_id = u.emp_id WHERE g.emp_id = ? ORDER BY g.created_at DESC", [session['emp_id']]).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'title': r[3], 'description': r[4], 'target_date': r[5].isoformat() if r[5] else None, 'weight': r[6], 'rating': r[7], 'status': r[8], 'created_at': r[9].isoformat() if r[9] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    # CC-10: emp_id comes from the session, never the body. The bare `VALUES`
    # insert this replaces had ten placeholders against a nine-column table, so
    # every create returned 500 — the explicit column list is also the v2.0 lesson.
    if data.get('emp_id') not in (None, '', session['emp_id']):
        return jsonify({'error': 'A goal may only be created for yourself'}), 400
    try:
        values = goals.validate_create(data)
    except goals.GoalError as exc:
        return jsonify({'error': str(exc)}), exc.status
    conn = get_db()
    try:
        gid = _next_generated_id(conn, 'goals', 'goal_id')
        conn.execute(
            "INSERT INTO goals (goal_id, emp_id, title, description, target_date, weight, "
            "rating, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [gid, session['emp_id'], values['title'], values['description'],
             values['target_date'], values['weight'], None, 'Active', datetime.now()],
        )
    finally:
        conn.close()
    audit_log(
        session['emp_id'], 'GOAL_CREATE', f'Created goal {gid}',
        entity='goals', entity_id=gid, after=values,
    )
    return jsonify({'message': 'Goal created', 'id': gid}), 201


@app.route('/api/v1/goals/<int:gid>/rate', methods=['PUT'])
@app.route('/api/goals/<int:gid>/rate', methods=['PUT'])
@reporting_line_required
def rate_goal(gid):
    """Rate a goal and complete it (FR-PERF-01: "by manager, not self").

    The write is conditional on the goal still being Active, so two raters
    produce one winner and one 409 rather than a silent overwrite. `status` is set
    here and only here: the edit route refuses to touch it.
    """
    data = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        actor = _lifecycle_actor(conn)
        goal = conn.execute(
            'SELECT emp_id, status, weight FROM goals WHERE goal_id = ?', [gid]
        ).fetchone()
        if not goal:
            return jsonify({'error': 'Goal not found'}), 404
        try:
            rating = goals.check_rating_value(data.get('rating'))
            goals.check_rating(actor, goal)
        except goals.GoalError as exc:
            return jsonify({'error': str(exc)}), exc.status
        if goal[1] != 'Active':
            return jsonify({
                'error': f'This goal is already {goal[1]}',
                'status': goal[1],
            }), 409
        result = conn.execute(
            "UPDATE goals SET rating = ?, status = 'Completed' WHERE goal_id = ? AND status = 'Active'",
            [rating, gid],
        )
        if result.rowcount == 0:
            return jsonify({'error': 'The goal changed while you were rating it; reload and retry'}), 409
    finally:
        conn.close()
    audit_log(
        session['emp_id'], 'GOAL_RATE', f'Rated goal {gid} as {rating}',
        entity='goals', entity_id=gid,
        before={'status': 'Active', 'rating': None},
        after={'status': 'Completed', 'rating': rating},
    )
    add_notification(
        goal[0], 'GOAL_RATED', f'Your goal was rated {rating}/5.', '/goals', 'Performance',
    )
    return jsonify({'message': 'Goal rated', 'rating': rating, 'status': 'Completed'}), 200


@app.route('/api/v1/goals/<int:gid>', methods=['PUT'])
@app.route('/api/goals/<int:gid>', methods=['PUT'])
@login_required
def update_goal(gid):
    """Edit a goal. Owner, their manager, or HR/Admin — and never anyone else.

    This route was `@login_required` with no ownership check, so any authenticated
    user could rewrite any goal in the company by guessing a sequential id, and
    could set `status` to skip rating entirely. `goals.check_edit` owns the
    decision and `goals.EDITABLE_FIELDS` deliberately excludes status and rating.
    """
    data = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        actor = _lifecycle_actor(conn)
        goal = conn.execute(
            'SELECT emp_id, title, description, target_date, weight, status FROM goals '
            'WHERE goal_id = ?', [gid]
        ).fetchone()
        if not goal:
            return jsonify({'error': 'Goal not found'}), 404
        try:
            goals.check_edit(actor, goal)
            cleaned = goals.validate_patch(data)
        except goals.GoalError as exc:
            return jsonify({'error': str(exc)}), exc.status
        assignments = ', '.join(f'{field} = ?' for field in cleaned)
        conn.execute(
            f'UPDATE goals SET {assignments} WHERE goal_id = ?',
            [*cleaned.values(), gid],
        )
        before = {field: goal[1 + i] for i, field in enumerate(goals.EDITABLE_FIELDS)}
    finally:
        conn.close()
    audit_log(
        session['emp_id'], 'GOAL_UPDATE', f'Updated goal {gid}',
        entity='goals', entity_id=gid,
        before={**before, 'status': goal[5]},
        after={**{k: v for k, v in before.items() if k not in cleaned}, **cleaned},
    )
    return jsonify({'message': 'Goal updated', 'fields': sorted(cleaned)}), 200


# ── Performance Reviews ───────────────────────────────────────────

@app.route('/api/v1/performance-reviews', methods=['GET', 'POST'])
@app.route('/api/performance-reviews', methods=['GET', 'POST'])
@hr_or_admin_required
def reviews_api():
    if request.method == 'GET':
        conn = get_db()
        rows = conn.execute(
            "SELECT r.review_id, r.emp_id, u.name, r.reviewer_id, rev.name, r.review_period, r.overall_rating, r.comments, r.status, r.submitted_at FROM performance_reviews r JOIN users u ON r.emp_id = u.emp_id JOIN users rev ON r.reviewer_id = rev.emp_id ORDER BY r.created_at DESC"
        ).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'reviewer_id': r[3], 'reviewer': r[4], 'period': r[5], 'rating': float(r[6]) if r[6] else None, 'comments': r[7], 'status': r[8], 'submitted_at': r[9].isoformat() if r[9] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        # FR-PERF-02: HR opening a review is legitimate; HR opening one whose
        # subject and reviewer are the same person is not, because a self-review
        # has nobody to sign it. Both users must also exist, or a foreign key is
        # the only thing that would notice.
        try:
            values = reviews.validate_assignment(
                conn, data.get('emp_id'), data.get('reviewer_id'), data.get('review_period'),
            )
        except reviews.ReviewError as exc:
            return jsonify({'error': str(exc)}), exc.status
        rid = _next_generated_id(conn, 'performance_reviews', 'review_id')
        conn.execute(
            "INSERT INTO performance_reviews (review_id, emp_id, reviewer_id, review_period, "
            "overall_rating, comments, status, created_at, submitted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [rid, values['emp_id'], values['reviewer_id'], values['review_period'],
             None, None, 'Draft', datetime.now(), None],
        )
    finally:
        conn.close()
    audit_log(
        session['emp_id'], 'REVIEW_CREATE',
        f'Opened review {rid} for {values["emp_id"]} (reviewer {values["reviewer_id"]})',
        entity='performance_reviews', entity_id=rid, after=values,
    )
    return jsonify({'message': 'Review created', 'id': rid}), 201


@app.route('/api/v1/performance-reviews/<int:rid>/submit', methods=['PUT'])
@app.route('/api/performance-reviews/<int:rid>/submit', methods=['PUT'])
@login_required
def submit_review(rid):
    """Submit a performance review (FR-PERF-02).

    The requirement is "submit requires the reviewer to be the assigned reviewer
    for that review", recorded in Appendix A-18 as a v1.0 gap that let any
    authenticated user submit any review. This route was `@login_required` with an
    id from the path, so the gap was still open.

    `reviews.check_submit` is deliberately strict: HR and Admin get no bypass,
    because a review signed by somebody who did not write it is exactly what the
    requirement exists to prevent. The write is also conditional on Draft, so a
    submitted review is final and cannot be quietly rewritten.
    """
    data = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        actor = _lifecycle_actor(conn)
        review = conn.execute(
            'SELECT emp_id, reviewer_id, status, overall_rating, comments '
            'FROM performance_reviews WHERE review_id = ?', [rid],
        ).fetchone()
        if not review:
            return jsonify({'error': 'Review not found'}), 404
        try:
            reviews.check_submit(actor, review)
            rating = reviews.validate_rating(data.get('rating'), 'overall_rating')
            comments = str(data.get('comments') or '').strip() or None
            if comments and len(comments) > reviews.MAX_COMMENTS:
                raise reviews.ReviewError(
                    f'comments must be {reviews.MAX_COMMENTS} characters or fewer')
        except reviews.ReviewError as exc:
            return jsonify({'error': str(exc)}), exc.status
        if review[2] != 'Draft':
            return jsonify({
                'error': f'This review is already {review[2]} and is final',
                'status': review[2],
            }), 409
        result = conn.execute(
            "UPDATE performance_reviews SET overall_rating = ?, comments = ?, "
            "status = 'Submitted', submitted_at = ? WHERE review_id = ? AND status = 'Draft'",
            [rating, comments, datetime.now(), rid],
        )
        if result.rowcount == 0:
            return jsonify({'error': 'The review changed while you were writing it; reload and retry'}), 409
    finally:
        conn.close()
    audit_log(
        session['emp_id'], 'REVIEW_SUBMIT',
        f'Reviewer submitted review {rid} for {review[0]} with {rating}',
        entity='performance_reviews', entity_id=rid,
        before={'status': 'Draft', 'overall_rating': review[3], 'comments': review[4]},
        after={'status': 'Submitted', 'overall_rating': rating, 'comments': comments},
    )
    add_notification(
        review[0], 'REVIEW_SUBMITTED',
        f'Your performance review ({rid}) has been submitted.', '/goals', 'Performance',
    )
    return jsonify({
        'message': 'Review submitted', 'status': 'Submitted', 'overall_rating': rating,
    }), 200


# ── 360 Feedback ──────────────────────────────────────────────────

@app.route('/api/v1/feedback-360', methods=['GET', 'POST'])
@app.route('/api/feedback-360', methods=['GET', 'POST'])
@login_required
def feedback_api():
    if request.method == 'GET':
        conn = get_db()
        if policy.can_view_all(policy.current_actor(conn), 'performance', conn=conn):
            rows = conn.execute("SELECT f.feedback_id, f.emp_id, u.name, f.reviewer_id, rev.name, f.category, f.rating, f.comment, f.submitted_at FROM feedback_360 f JOIN users u ON f.emp_id = u.emp_id JOIN users rev ON f.reviewer_id = rev.emp_id ORDER BY f.submitted_at DESC").fetchall()
        else:
            rows = conn.execute("SELECT f.feedback_id, f.emp_id, u.name, f.reviewer_id, rev.name, f.category, f.rating, f.comment, f.submitted_at FROM feedback_360 f JOIN users u ON f.emp_id = u.emp_id JOIN users rev ON f.reviewer_id = rev.emp_id WHERE f.emp_id = ? ORDER BY f.submitted_at DESC", [session['emp_id']]).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'reviewer_id': r[3], 'reviewer': r[4], 'category': r[5], 'rating': r[6], 'comment': r[7], 'submitted_at': r[8].isoformat() if r[8] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('emp_id') or not data.get('rating'):
        return jsonify({'error': 'emp_id and rating required'}), 400
    conn = get_db()
    try:
        # FR-PERF-02: "360° feedback: reviewer cannot be the subject". The reviewer
        # is the session user, so rating yourself five stars used to be possible.
        try:
            values = reviews.validate_feedback(
                conn, _lifecycle_actor(conn), data.get('emp_id'),
                data.get('rating'), data.get('category'), data.get('comment'),
            )
        except reviews.ReviewError as exc:
            return jsonify({'error': str(exc)}), exc.status
        fid = _next_generated_id(conn, 'feedback_360', 'feedback_id')
        conn.execute(
            "INSERT INTO feedback_360 (feedback_id, emp_id, reviewer_id, category, rating, "
            "comment, submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [fid, values['emp_id'], values['reviewer_id'], values['category'],
             values['rating'], values['comment'], datetime.now()],
        )
    finally:
        conn.close()
    audit_log(
        session['emp_id'], 'FEEDBACK_360',
        f'Gave 360 feedback {fid} to {values["emp_id"]}',
        entity='feedback_360', entity_id=fid,
        after={'rating': values['rating'], 'category': values['category']},
    )
    return jsonify({'message': 'Feedback submitted', 'id': fid}), 201


# ══════════════════════════════════════════════════════════════════════
#  PHASE 3 — EXPENSE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════

@app.route('/admin/expenses')
@hr_or_admin_required
def admin_expenses():
    return render_template('admin_expenses.html')


@app.route('/expenses')
@login_required
def expenses_page():
    return render_template('expenses.html')


@app.route('/api/v1/expense-categories')
@app.route('/api/expense-categories')
@login_required
def expense_categories():
    conn = get_db()
    rows = conn.execute("SELECT cat_id, name, description FROM expense_categories").fetchall()
    conn.close()
    return jsonify([{'id': r[0], 'name': r[1], 'description': r[2]} for r in rows]), 200


@app.route('/api/v1/expenses', methods=['GET', 'POST'])
@app.route('/api/expenses', methods=['GET', 'POST'])
@login_required
def expenses_api():
    if request.method == 'GET':
        conn = get_db()
        actor = _lifecycle_actor(conn)
        viewer = policy.current_actor(conn)
        if policy.can_view_all(viewer, 'expenses', conn=conn):
            rows = conn.execute("SELECT c.claim_id, c.emp_id, u.name, c.cat_id, e.name, c.amount, c.description, c.status, c.created_at FROM expense_claims c JOIN users u ON c.emp_id = u.emp_id JOIN expense_categories e ON c.cat_id = e.cat_id ORDER BY c.created_at DESC").fetchall()
        else:
            # FR-EXP-03: a reviewer has to be able to *find* the claims they may
            # act on, so the list is scoped to "mine, my reports', or anything in
            # a state I could transition" rather than "mine only". Without the
            # third clause Finance could never see an Approved claim to pay,
            # which would make the requirement unreachable through the UI. The
            # company-wide view stays with the roles policy.can_view_all admits.
            reports = {
                r[0] for r in conn.execute(
                    'SELECT emp_id FROM users WHERE manager_emp_id = ?', [session['emp_id']]
                ).fetchall()
            }
            # The states this actor could actually move somebody else's claim out
            # of, derived from the same rules the write enforces, so the list and
            # the state machine cannot disagree about what is reviewable.
            actionable = expenses.actionable_statuses(actor)
            clauses = ['c.emp_id = ?']
            params: list = [session['emp_id']]
            if reports:
                clauses.append(f"c.emp_id IN ({','.join('?' for _ in reports)})")
                params.extend(sorted(reports))
            if actionable:
                clauses.append(f"c.status IN ({','.join('?' for _ in actionable)})")
                params.extend(actionable)
            rows = conn.execute(
                "SELECT c.claim_id, c.emp_id, u.name, c.cat_id, e.name, c.amount, "
                "c.description, c.status, c.created_at "
                "FROM expense_claims c JOIN users u ON c.emp_id = u.emp_id "
                "JOIN expense_categories e ON c.cat_id = e.cat_id "
                f"WHERE {' OR '.join(clauses)} ORDER BY c.created_at DESC",
                params,
            ).fetchall()
        claims = [{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'cat_id': r[3],
                   'category': r[4], 'amount': float(r[5]), 'description': r[6],
                   'status': r[7],
                   # FR-EXP-03: tell the client which transitions are actually
                   # available, rather than letting it guess and get a 403.
                   'allowed_actions': expenses.permitted_targets(actor, (r[1], r[7], r[5])),
                   'created_at': r[8].isoformat() if r[8] else None} for r in rows]
        conn.close()
        return jsonify(claims), 200
    data = request.get_json(silent=True) or {}
    if not data.get('cat_id') or not data.get('amount'):
        return jsonify({'error': 'cat_id and amount required'}), 400
    # CC-10: emp_id comes from the session, never from the body. v1.0 accepted an
    # override, which let any authenticated user file a claim in a colleague's
    # name and, because there was no self-approval block, approve it themselves.
    if data.get('emp_id') not in (None, '', session['emp_id']):
        return jsonify({'error': 'A claim may only be filed for yourself'}), 400
    try:
        amount = float(data['amount'])
    except (TypeError, ValueError):
        return jsonify({'error': 'amount must be a number'}), 400
    if amount <= 0:
        return jsonify({'error': 'amount must be greater than zero'}), 400
    conn = get_db()
    try:
        if not conn.execute('SELECT 1 FROM expense_categories WHERE cat_id = ?', [data['cat_id']]).fetchone():
            return jsonify({'error': 'Unknown expense category'}), 400
        cid = _next_generated_id(conn, 'expense_claims', 'claim_id')
        # Explicit column list: a bare VALUES would mis-target now that the
        # table carries paid_at and rejection_reason (the v2.0 lesson).
        conn.execute(
            "INSERT INTO expense_claims (claim_id, emp_id, cat_id, amount, description, "
            "receipt_path, status, approved_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [cid, session['emp_id'], data['cat_id'], amount, data.get('description'),
             data.get('receipt_path'), 'Pending', None, datetime.now()])
    finally:
        conn.close()
    audit_log(
        session['emp_id'], 'EXPENSE_CLAIM',
        f'Filed expense claim {cid} for {amount}',
        entity='expense_claims', entity_id=cid,
        after={'amount': amount, 'cat_id': data['cat_id']},
    )
    return jsonify({'message': 'Expense claimed', 'id': cid}), 201


@app.route('/api/v1/expenses/<int:eid>/status', methods=['PUT'])
@app.route('/api/expenses/<int:eid>/status', methods=['PUT'])
@expense_actor_required
def update_expense_status(eid):
    """Move an expense claim through the FR-EXP-03 state machine.

    The decision is `expenses.check_transition`'s, not this handler's: it enforces
    the strict transition table, blocks self-approval, requires an actual reason
    for a rejection, and gives ``Paid`` to Finance/Admin only (Appendix A-11
    recorded v1.0 letting any logged-in user pay). The write below is conditional
    on the status the decision was made against, so two approvers racing produce
    one winner and one 409 rather than a silent overwrite.
    """
    data = request.get_json(silent=True) or {}
    target = data.get('status')
    reason = data.get('reason')
    conn = get_db()
    try:
        actor = _lifecycle_actor(conn)
        claim = conn.execute(
            "SELECT emp_id, status, amount FROM expense_claims WHERE claim_id = ?", [eid]
        ).fetchone()
        if not claim:
            return jsonify({'error': 'Expense claim not found'}), 404
        before = claim[1]
        try:
            expenses.check_transition(actor, claim, target, reason)
        except expenses.ExpenseTransitionError as exc:
            return jsonify({'error': str(exc), 'allowed': expenses.permitted_targets(actor, claim)}), exc.status
        if target == 'Paid':
            result = conn.execute(
                "UPDATE expense_claims SET status = ?, approved_by = ?, paid_at = ? "
                "WHERE claim_id = ? AND status = ?",
                [target, session['emp_id'], datetime.now(), eid, before],
            )
        else:
            result = conn.execute(
                "UPDATE expense_claims SET status = ?, approved_by = ?, rejection_reason = ? "
                "WHERE claim_id = ? AND status = ?",
                [target, session['emp_id'], reason if target == 'Rejected' else None,
                 eid, before],
            )
        if result.rowcount == 0:
            return jsonify({
                'error': 'The claim changed while you were reviewing it; reload and retry',
                'allowed': expenses.permitted_targets(actor, claim),
            }), 409
    finally:
        conn.close()
    detail = f' ({reason})' if target == 'Rejected' and reason else ''
    audit_log(
        session['emp_id'], f'EXPENSE_{target.upper()}',
        f'Expense {eid}: {before} -> {target}{detail}',
        entity='expense_claims', entity_id=eid,
        before={'status': before}, after={'status': target, 'reason': reason},
    )
    if target in ('Approved', 'Rejected'):
        add_notification(
            claim[0], f'EXPENSE_{target.upper()}',
            f'Your expense claim of {claim[2]} was {target.lower()}.{detail}',
            '/expenses', 'Expenses',
        )
    return jsonify({'message': f'Expense {target.lower()}', 'status': target}), 200


# ══════════════════════════════════════════════════════════════════════
#  PHASE 3 — HELP DESK / TICKETS
# ══════════════════════════════════════════════════════════════════════

@app.route('/admin/tickets')
@hr_or_admin_required
def admin_tickets():
    return render_template('admin_tickets.html')


@app.route('/tickets')
@login_required
def tickets_page():
    return render_template('tickets.html')


@app.route('/api/v1/tickets', methods=['GET', 'POST'])
@app.route('/api/tickets', methods=['GET', 'POST'])
@login_required
def tickets_api():
    if request.method == 'GET':
        conn = get_db()
        actor = policy.current_actor(conn)
        if policy.can_view_all(actor, 'tickets', conn=conn):
            rows = conn.execute("SELECT t.ticket_id, t.emp_id, u.name, t.subject, t.category, t.priority, t.status, t.assigned_to, t.created_at, t.updated_at FROM tickets t JOIN users u ON t.emp_id = u.emp_id ORDER BY t.created_at DESC").fetchall()
        else:
            # Owned *or* assigned — the same rule the detail view and the write
            # paths use. An assignee who cannot see the ticket they were given
            # cannot work on it.
            rows = conn.execute(
                "SELECT t.ticket_id, t.emp_id, u.name, t.subject, t.category, t.priority, "
                "t.status, t.assigned_to, t.created_at, t.updated_at FROM tickets t "
                "JOIN users u ON t.emp_id = u.emp_id "
                "WHERE t.emp_id = ? OR t.assigned_to = ? ORDER BY t.created_at DESC",
                [session['emp_id'], session['emp_id']],
            ).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'subject': r[3], 'category': r[4], 'priority': r[5], 'status': r[6], 'assigned_to': r[7], 'created_at': r[8].isoformat() if r[8] else None, 'updated_at': r[9].isoformat() if r[9] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('subject'):
        return jsonify({'error': 'subject required'}), 400
    conn = get_db()
    try:
        # FR-TKT-01's SLA table is keyed on priority, so an unrecognised value
        # would silently fall out of every SLA calculation.
        try:
            priority = tickets.validate_priority(data.get('priority'))
        except tickets.TicketError as exc:
            return jsonify({'error': str(exc)}), exc.status
        tid = _next_generated_id(conn, 'tickets', 'ticket_id')
        conn.execute(
            "INSERT INTO tickets (ticket_id, emp_id, subject, description, category, priority, "
            "status, assigned_to, created_at, updated_at, resolved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [tid, session['emp_id'], data['subject'], data.get('description'),
             data.get('category'), priority, 'Open', None, datetime.now(), None, None])
    finally:
        conn.close()
    audit_log(
        session['emp_id'], 'TICKET_CREATE', f'Created ticket {tid}',
        entity='tickets', entity_id=tid,
        after={'priority': priority, 'subject': data['subject']},
    )
    return jsonify({'message': 'Ticket created', 'id': tid}), 201


@app.route('/api/v1/tickets/<int:tid>')
@app.route('/api/tickets/<int:tid>')
@login_required
def ticket_detail(tid):
    conn = get_db()
    row = conn.execute("SELECT t.ticket_id, t.emp_id, u.name, t.subject, t.description, t.category, t.priority, t.status, t.assigned_to, t.created_at, t.updated_at, t.resolved_at FROM tickets t JOIN users u ON t.emp_id = u.emp_id WHERE t.ticket_id = ?", [tid]).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Not found'}), 404
    actor = policy.current_actor(conn)
    # The same rule the write paths use (`tickets.can_view`), so the list, the
    # detail view, commenting and status changes cannot disagree about who can see
    # a ticket. FR-TKT-03 asks for exactly this.
    if not (policy.can_view_all(actor, 'tickets', conn=conn)
            or tickets.can_view(actor, (row[1], row[8]))):
        conn.close()
        return jsonify({'error': 'Forbidden'}), 403
    comments = conn.execute("SELECT c.comment_id, c.emp_id, u.name, c.comment, c.created_at FROM ticket_comments c JOIN users u ON c.emp_id = u.emp_id WHERE c.ticket_id = ? ORDER BY c.created_at", [tid]).fetchall()
    conn.close()
    return jsonify({
        'id': row[0], 'emp_id': row[1], 'employee': row[2], 'subject': row[3], 'description': row[4],
        'category': row[5], 'priority': row[6], 'status': row[7], 'assigned_to': row[8],
        'created_at': row[9].isoformat() if row[9] else None,
        'updated_at': row[10].isoformat() if row[10] else None,
        'resolved_at': row[11].isoformat() if row[11] else None,
        'comments': [{'id': c[0], 'emp_id': c[1], 'name': c[2], 'comment': c[3], 'created_at': c[4].isoformat() if c[4] else None} for c in comments]
    }), 200


@app.route('/api/v1/tickets/<int:tid>/comment', methods=['POST'])
@app.route('/api/tickets/<int:tid>/comment', methods=['POST'])
@login_required
def add_ticket_comment(tid):
    """Append a comment (FR-TKT-03) and reopen the ticket if the rule says so.

    This route had only an existence check, so an employee who is refused the
    detail view with a 403 could still write into the ticket's history. The
    visibility rule is now applied here as well, which is the "defence in depth"
    FR-TKT-03 asks for.

    FR-TKT-04's other half lives here too: a Closed ticket receiving a comment
    **from its reporter** within seven days of closing is Reopened, and nobody
    else can reopen a ticket by commenting on it.
    """
    data = request.get_json(silent=True) or {}
    conn = get_db()
    now = datetime.now()
    try:
        row = conn.execute(
            'SELECT emp_id, assigned_to, status, resolved_at FROM tickets WHERE ticket_id = ?',
            [tid],
        ).fetchone()
        if not row:
            return jsonify({'error': 'Ticket not found'}), 404
        actor = policy.current_actor(conn)
        try:
            tickets.check_visibility(
                actor, (row[0], row[1]),
                can_view_all=policy.can_view_all(actor, 'tickets', conn=conn),
            )
            comment = tickets.validate_comment(data.get('comment'))
        except tickets.TicketError as exc:
            return jsonify({'error': str(exc)}), exc.status
        cid = _next_generated_id(conn, 'ticket_comments', 'comment_id')
        conn.execute(
            'INSERT INTO ticket_comments (comment_id, ticket_id, emp_id, comment, created_at) '
            'VALUES (?, ?, ?, ?, ?)', [cid, tid, session['emp_id'], comment, now],
        )
        reopened = tickets.should_reopen((row[2], row[0], row[3]), session['emp_id'], now)
        if reopened:
            conn.execute(
                "UPDATE tickets SET status = 'Reopened', resolved_at = NULL, updated_at = ? "
                'WHERE ticket_id = ? AND status = ?', [now, tid, 'Closed'],
            )
        else:
            conn.execute('UPDATE tickets SET updated_at = ? WHERE ticket_id = ?', [now, tid])
    finally:
        conn.close()
    if reopened:
        audit_log(
            session['emp_id'], 'TICKET_REOPEN',
            f'Ticket {tid} reopened by the reporter within the 7-day window',
            entity='tickets', entity_id=tid,
            before={'status': 'Closed'}, after={'status': 'Reopened'},
        )
        return jsonify({
            'message': 'Comment added; the ticket has been reopened',
            'id': cid, 'status': 'Reopened', 'reopened': True,
        }), 201
    return jsonify({'message': 'Comment added', 'id': cid, 'reopened': False}), 201


@app.route('/api/v1/tickets/<int:tid>/status', methods=['PUT'])
@app.route('/api/tickets/<int:tid>/status', methods=['PUT'])
@login_required
def update_ticket_status(tid):
    """Move a ticket along the FR-TKT-04 chain.

    This route had neither a visibility check nor a state machine: `@login_required`
    and four accepted strings, so any authenticated user could close anybody's
    ticket. `tickets.check_transition` enforces the chain strictly — the SRS
    writes it as `Open -> In Progress -> Resolved -> Closed` — and the write is
    conditional on the status the decision was made against, so two triagers
    racing give one winner and one 409.
    """
    data = request.get_json(silent=True) or {}
    target = data.get('status')
    conn = get_db()
    now = datetime.now()
    try:
        row = conn.execute(
            'SELECT emp_id, assigned_to, status FROM tickets WHERE ticket_id = ?', [tid],
        ).fetchone()
        if not row:
            return jsonify({'error': 'Ticket not found'}), 404
        actor = policy.current_actor(conn)
        try:
            tickets.check_visibility(
                actor, (row[0], row[1]),
                can_view_all=policy.can_view_all(actor, 'tickets', conn=conn),
            )
            tickets.check_transition(row[2], target)
        except tickets.TicketError as exc:
            payload = {
                'error': str(exc),
                'allowed': sorted(tickets.TRANSITIONS.get(row[2], ())),
            }
            return jsonify(payload), exc.status
        result = conn.execute(
            'UPDATE tickets SET status = ?, updated_at = ?, resolved_at = ? '
            'WHERE ticket_id = ? AND status = ?',
            [target, now, tickets.resolved_at_for(target, now), tid, row[2]],
        )
        if result.rowcount == 0:
            return jsonify({'error': 'The ticket changed while you were triaging it; reload and retry'}), 409
    finally:
        conn.close()
    audit_log(
        session['emp_id'], 'TICKET_STATUS',
        f'Ticket {tid}: {row[2]} -> {target}',
        entity='tickets', entity_id=tid,
        before={'status': row[2]}, after={'status': target},
    )
    if row[0] != session['emp_id']:
        add_notification(
            row[0], 'TICKET_UPDATED',
            f'Your ticket "{tid}" moved to {target}.', '/tickets', 'Tickets',
        )
    return jsonify({'message': f'Status set to {target}', 'status': target}), 200


@app.route('/api/v1/tickets/<int:tid>/assign', methods=['PUT'])
@app.route('/api/tickets/<int:tid>/assign', methods=['PUT'])
@admin_required
def assign_ticket(tid):
    """Assign a ticket. FR-TKT-04: "Assignment audited" — it was not."""
    data = request.get_json(silent=True) or {}
    assignee = data.get('assigned_to') or None
    conn = get_db()
    try:
        row = conn.execute(
            'SELECT emp_id, assigned_to, status FROM tickets WHERE ticket_id = ?', [tid],
        ).fetchone()
        if not row:
            return jsonify({'error': 'Ticket not found'}), 404
        if assignee is not None:
            exists = conn.execute(
                'SELECT 1 FROM users WHERE emp_id = ?', [assignee]
            ).fetchone()
            if not exists:
                return jsonify({'error': f'No such employee: {assignee}'}), 404
        conn.execute(
            'UPDATE tickets SET assigned_to = ?, updated_at = ? WHERE ticket_id = ?',
            [assignee, datetime.now(), tid],
        )
    finally:
        conn.close()
    audit_log(
        session['emp_id'], 'TICKET_ASSIGN',
        f'Ticket {tid} assigned to {assignee or "nobody"}',
        entity='tickets', entity_id=tid,
        before={'assigned_to': row[1]}, after={'assigned_to': assignee},
    )
    if assignee and assignee != row[0]:
        add_notification(
            assignee, 'TICKET_ASSIGNED',
            f'Ticket {tid} was assigned to you.', '/tickets', 'Tickets',
        )
    return jsonify({'message': 'Ticket assigned', 'assigned_to': assignee}), 200


# ══════════════════════════════════════════════════════════════════════
#  PHASE 3 — DOCUMENT MANAGEMENT
# ══════════════════════════════════════════════════════════════════════

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB


@app.route('/admin/documents')
@hr_or_admin_required
def admin_documents():
    return render_template('admin_documents.html')


@app.route('/documents')
@login_required
def documents_page():
    return render_template('documents.html')


@app.route('/api/v1/documents', methods=['GET'])
@app.route('/api/documents', methods=['GET'])
@login_required
def documents_list():
    conn = get_db()
    actor = _lifecycle_actor(conn)
    if actor and (actor[1] in ('Admin', 'Super Admin', 'HR') or actor[2] == 'HR'):
        rows = conn.execute("SELECT d.doc_id, d.emp_id, u.name, d.name, d.category, d.file_path, d.file_size, d.uploaded_at FROM documents d JOIN users u ON d.emp_id = u.emp_id ORDER BY d.uploaded_at DESC").fetchall()
    else:
        rows = conn.execute("SELECT d.doc_id, d.emp_id, u.name, d.name, d.category, d.file_path, d.file_size, d.uploaded_at FROM documents d JOIN users u ON d.emp_id = u.emp_id WHERE d.emp_id = ? ORDER BY d.uploaded_at DESC", [session['emp_id']]).fetchall()
    conn.close()
    return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'name': r[3], 'category': r[4], 'file_path': r[5], 'file_size': r[6], 'uploaded_at': r[7].isoformat() if r[7] else None} for r in rows]), 200


def _can_access_document(actor, emp_id, *, write=False):
    if not actor:
        return False
    if actor[0] == emp_id:
        return not write or actor[1] not in ('Blocked',)
    return actor[1] in ('Admin', 'Super Admin') or actor[2] == 'HR'


@app.route('/api/v1/upload', methods=['POST'])
@app.route('/api/upload', methods=['POST'])
@login_required
def upload_document():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    f = request.files['file']
    if f.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    emp_id = request.form.get('emp_id', session['emp_id'])
    category = request.form.get('category', 'Other')
    actor = _lifecycle_actor()
    if not _can_access_document(actor, emp_id, write=False):
        return jsonify({'error': 'You may only upload documents for your own profile'}), 403
    target_conn = get_db()
    target_exists = target_conn.execute("SELECT 1 FROM users WHERE emp_id = ?", [emp_id]).fetchone()
    target_conn.close()
    if not target_exists:
        return jsonify({'error': 'Employee not found'}), 404
    safe_name = secure_filename(f.filename)
    if not safe_name:
        return jsonify({'error': 'Invalid filename'}), 400
    extension = safe_name.rsplit('.', 1)[-1].lower() if '.' in safe_name else ''
    if extension not in ('pdf', 'jpg', 'jpeg', 'png'):
        return jsonify({'error': 'Only PDF, JPG, JPEG and PNG documents are accepted'}), 400
    f.stream.seek(0)
    content = f.stream.read(app.config['MAX_CONTENT_LENGTH'] + 1)
    if not content or len(content) > app.config['MAX_CONTENT_LENGTH']:
        return jsonify({'error': 'Document is empty or too large'}), 413
    signatures = {
        'pdf': content.startswith(b'%PDF-'), 'jpg': content.startswith(b'\xff\xd8\xff'),
        'jpeg': content.startswith(b'\xff\xd8\xff'), 'png': content.startswith(b'\x89PNG\r\n\x1a\n'),
    }
    if not signatures.get(extension) or b'EICAR-STANDARD-ANTIVIRUS-TEST-FILE' in content:
        return jsonify({'error': 'Document failed content validation'}), 400
    filename = f"{int(datetime.now().timestamp())}_{safe_name}"
    fsize = len(content)
    conn = get_db()
    did = _next_generated_id(conn, 'documents', 'doc_id')
    # Object key for S3/MinIO (FR-DOC-02: persisted to object storage)
    object_key = f"documents/{emp_id}/{did}/{filename}"
    conn.execute("INSERT INTO documents (doc_id, emp_id, name, category, file_path, file_size, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 [did, emp_id, safe_name, category, object_key, fsize, datetime.now()])
    conn.close()

    # Upload to object storage
    try:
        content_type = {'pdf': 'application/pdf', 'jpg': 'image/jpeg',
                        'jpeg': 'image/jpeg', 'png': 'image/png'}.get(extension, 'application/octet-stream')
        object_storage.upload_document(emp_id, did, filename, content, content_type)
    except object_storage.ObjectStorageError as exc:
        logger.error("Document upload to object storage failed: %s", exc)
        return jsonify({'error': 'Document storage unavailable'}), 503

    audit_log(emp_id, 'DOCUMENT_UPLOAD', f'Uploaded document {did}', entity='documents', entity_id=did)
    return jsonify({'message': 'File uploaded', 'id': did, 'path': object_key}), 201


@app.route('/api/v1/documents/<int:did>/download')
@app.route('/api/documents/<int:did>/download')
@login_required
def download_document(did):
    conn = get_db()
    row = conn.execute("SELECT emp_id, file_path, name FROM documents WHERE doc_id = ?", [did]).fetchone()
    actor = _lifecycle_actor()
    conn.close()
    if not row or not _can_access_document(actor, row[0]):
        return jsonify({'error': 'Not found'}), 404
    original_name = row[2]
    # Generate presigned URL for object storage (FR-DOC-02)
    try:
        presigned_url = object_storage.get_document_presigned_url(row[0], did, original_name)
    except object_storage.ObjectStorageError as exc:
        logger.error("Document presigned URL generation failed: %s", exc)
        return jsonify({'error': 'Document unavailable'}), 503
    # FR-DOC-03: the download is audited. Document *reads* leaving no trail is
    # the one thing that makes a document store hard to reason about after an
    # incident, and the SRS asks for it explicitly.
    audit_log(
        session['emp_id'], 'DOCUMENT_DOWNLOAD',
        f'Downloaded document {did} ({original_name})',
        entity='documents', entity_id=did, after={'owner_emp_id': row[0]},
    )
    return redirect(presigned_url, code=302)


@app.route('/api/v1/documents/<int:did>', methods=['DELETE'])
@app.route('/api/documents/<int:did>', methods=['DELETE'])
@login_required
def delete_document(did):
    conn = get_db()
    row = conn.execute("SELECT emp_id, name, category, file_path FROM documents WHERE doc_id = ?", [did]).fetchone()
    actor = _lifecycle_actor()
    if not row or not _can_access_document(actor, row[0], write=True):
        conn.close()
        return jsonify({'error': 'Not found'}), 404
    emp_id, name, category, object_key = row
    conn.execute("DELETE FROM documents WHERE doc_id = ?", [did])
    conn.close()
    # Delete from object storage (FR-DOC-02)
    try:
        object_storage.delete_document_object(emp_id, did, name)
    except object_storage.ObjectStorageError as exc:
        logger.error("Document delete from object storage failed: %s", exc)
        # Log but don't fail the request — the DB row is gone
        pass
    # Irreversible: the row *and* the file are gone, and a document can be payroll
    # evidence or an identity document. The download of a document was audited
    # (FR-DOC-03) while the deletion was not, which is the wrong way round — reading
    # is reversible by definition and deleting is not.
    audit_log(
        session['emp_id'], 'DOCUMENT_DELETE',
        f'Deleted document {did} ({name}, {category}) belonging to {emp_id}',
        entity='documents', entity_id=did,
        before={'name': name, 'category': category, 'owner_emp_id': emp_id},
    )
    return jsonify({'message': 'Document deleted'}), 200


# ══════════════════════════════════════════════════════════════════════
#  PHASE 3 — EMAIL NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════

import smtplib  # noqa: E402
from email.mime.multipart import MIMEMultipart  # noqa: E402
from email.mime.text import MIMEText  # noqa: E402

SMTP_HOST = os.getenv('SMTP_HOST', '')
SMTP_PORT = int(os.getenv('SMTP_PORT', '587'))
SMTP_USER = os.getenv('SMTP_USER', '')
SMTP_PASS = os.getenv('SMTP_PASS', '')
EMAIL_FROM = os.getenv('EMAIL_FROM', 'noreply@hrms.com')

#: `None` means "decide from the port": 465 is implicit TLS (SMTP_SSL), everything
#: else is STARTTLS. Set explicitly for a provider that offers either on an
#: unconventional port.
_SSL_FLAG = os.getenv('SMTP_USE_SSL')
SMTP_USE_SSL = None if _SSL_FLAG is None else _SSL_FLAG.strip().lower() in ('1', 'true', 'yes')

#: A hung mail server must not hold the outbox dispatcher's thread open forever.
#: Without this the dispatcher stalls on delivery and every subsequent event backs
#: up behind it — a mail outage turning into an application outage.
SMTP_TIMEOUT_SECONDS = float(os.getenv('SMTP_TIMEOUT_SECONDS', '15'))


# ── Object Storage (S3/MinIO) — FR-PAY-07, FR-DOC-02 ─────────────────
S3_ENDPOINT_URL = os.getenv('S3_ENDPOINT_URL')
S3_ACCESS_KEY_ID = os.getenv('S3_ACCESS_KEY_ID')
S3_SECRET_ACCESS_KEY = os.getenv('S3_SECRET_ACCESS_KEY')
S3_BUCKET = os.getenv('S3_BUCKET', 'hrms')
S3_REGION = os.getenv('S3_REGION', 'us-east-1')
S3_PRESIGNED_EXPIRY = int(os.getenv('S3_PRESIGNED_EXPIRY', '3600'))


def _notification_email_wanted(conn, emp_id, category):
    """Does ``emp_id`` want email in ``category``? — FR-NOT-03's missing consumer.

    ``notification_preferences.email`` had no reader for its entire life: stored,
    reported by ``GET/PUT /api/notification-preferences``, and consulted by nothing.
    The outbox's ``notification.email`` handler is the consumer, and this is the one
    place the row is read, so the in-app and email channels cannot drift apart.

    Read at **dispatch** time, not enqueue time, on purpose: an employee who mutes a
    category after an event was queued should not receive that mail. The converse is
    accepted — an event queued while the category was on still sends if the switch is
    flipped before the dispatcher reaches it, because it was legitimately queued.
    """
    import notifications  # lazy: the module imports nothing from here at load time

    rows = conn.execute(
        "SELECT category, in_app, email FROM notification_preferences WHERE emp_id = ?",
        [emp_id],
    ).fetchall()
    effective = notifications.effective_for([
        {'category': r[0], 'in_app': r[1], 'email': r[2]} for r in rows
    ])
    return notifications.wants_email(effective, category or notifications.FALLBACK)


def enqueue_notification_email(conn, emp_id, to, subject, body, category=None,
                               force=False):
    """Queue an email for the dispatcher (FR-NOT-01). Never sends inline.

    Every route that used to call ``send_email`` on the request thread goes through
    here. SMTP is a third-party network call: doing it inline meant a slow provider
    held a web worker, which is an availability defect rather than a style preference.
    Returns the ``event_id`` so a caller can audit against it.
    """
    import outbox  # lazy: outbox imports from this module

    return outbox.enqueue(
        conn, 'notification.email',
        aggregate='notification', aggregate_id=emp_id,
        payload={
            'emp_id': emp_id, 'to': to, 'subject': subject, 'body': body,
            'category': category, 'force': bool(force),
        },
    )


def email_configured() -> bool:
    """Is there a real transport behind `send_email`?

    Exposed so `/api/health` can report it as a degraded condition instead of
    leaving a deployment to discover it when an employee says they never got
    their reset link.
    """
    return bool(SMTP_HOST)


def send_email(to, subject, body):
    """Deliver one email. **True only if a message actually left the process.**

    This used to `return True` when no SMTP host was configured, logging
    "would send". That is the single most consequential bug found in the
    go-live review, and it was found by auditing work done *with* it: FR-AUTH-08
    was rebuilt so the response carries nothing, which is only a real improvement
    if the link is actually delivered — and with the default configuration the
    queue drained, the handler returned success, the event was marked delivered,
    and **no human ever received a password reset link**. The user had already
    been told, correctly, that "if the account exists, a reset link has been
    sent".

    Reporting success for a send that did not happen is worse than no email
    feature at all: it converts a visible failure into an invisible one. So an
    unconfigured transport is now a **failure**, and because every delivery path
    goes through the outbox, that failure surfaces where it can be acted on —
    the event retries, then dead-letters into `GET /api/admin/outbox` instead of
    vanishing.
    """
    if not SMTP_HOST:
        logger.error(
            'Email NOT sent to %s: SMTP_HOST is not configured '
            '(subject: %s). Returning failure so this is retried and dead-lettered '
            'rather than silently reported as delivered.', to, subject,
        )
        return False
    try:
        msg = MIMEMultipart()
        msg['From'] = EMAIL_FROM
        msg['To'] = to
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'html'))
        # **Implicit TLS on port 465**, STARTTLS everywhere else. Calling
        # `starttls()` on a 465 connection fails, and port 465 is the default most
        # providers document — so the transport is chosen from the port rather than
        # hardcoded to STARTTLS. Setting `SMTP_USE_SSL` forces it either way for the
        # providers that offer 465 on a different port.
        implicit_tls = SMTP_USE_SSL if SMTP_USE_SSL is not None else (SMTP_PORT == 465)
        smtp_cls = smtplib.SMTP_SSL if implicit_tls else smtplib.SMTP
        with smtp_cls(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS) as server:
            if not implicit_tls:
                server.starttls()
            # Login only when credentials were supplied. An on-premise relay
            # commonly accepts mail from the host with no authentication at all, and
            # `login('', '')` against one raises — so a configuration that is
            # perfectly valid upstream was refused here.
            if SMTP_USER:
                server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        logger.info("Email sent to %s: %s", to, subject)
        return True
    except Exception as e:
        logger.warning("Email failed to %s: %s", to, e)
        return False


@app.route('/api/health')
@app.route('/api/v1/health')
def health():
    """Liveness plus the *configuration* a deployment can silently be missing.

    `/api/health` returning 200 only proves the process is up, which is not the
    question an operator has after deploying. The checks below are the ones whose
    absence produces a broken system that still looks healthy:

    * **email** — no SMTP host means password resets, welcome tokens and payroll
      notifications are queued and dead-lettered. Everything else in the product
      keeps working, so nothing surfaces the problem until an employee reports
      they never received a link. Reported as `degraded`, not `unhealthy`: the
      process is fine, and returning 503 here would take a load balancer's health
      check down over a missing optional integration.
    * **schema** — the target actually has the tables, so a boot against the wrong
      database is caught immediately.
    * **outbox** — the count of events that have exhausted their retries and
      dead-lettered, which is where a systematically failing integration becomes
      visible.
    """
    conn = None
    report = {'status': 'ok', 'schema': None, 'email_configured': None,
              'dead_lettered_events': None, 'scheduler_leader': None,
              'degraded': []}
    try:
        conn = get_db()
        report['schema'] = os.getenv('APP_DB_SCHEMA') or 'legacy'
        report['dead_lettered_events'] = conn.execute(
            "SELECT count(*) FROM outbox_events WHERE status = 'dead_letter'"
        ).fetchone()[0]
    except Exception as exc:
        report['status'] = 'unhealthy'
        report['degraded'].append(f'database unreachable: {exc.__class__.__name__}')
        return jsonify(report), 503
    finally:
        if conn is not None:
            conn.close()

    report['email_configured'] = email_configured()
    if not report['email_configured']:
        report['degraded'].append(
            'SMTP_HOST is not set: password resets and notifications are queued '
            'and will dead-letter rather than being delivered'
        )
    # FR-JOB-05: "exactly-once execution across all pods" is only checkable if a
    # deployment can ask *which* pod won the lease, so it is in the report rather
    # than only in a log line.
    try:
        import scheduler_leader

        report['scheduler_leader'] = scheduler_leader.holder()
    except Exception:
        report['scheduler_leader'] = None
    if report['dead_lettered_events']:
        report['degraded'].append(
            f"{report['dead_lettered_events']} outbox event(s) exhausted their retries"
        )
    if report['degraded']:
        report['status'] = 'degraded'
    return jsonify(report), 200


@app.route('/api/v1/send-notification-email', methods=['POST'])
@app.route('/api/send-notification-email', methods=['POST'])
@admin_required
def send_notification_email():
    data = request.get_json(silent=True) or {}
    to = data.get('to')
    subject = data.get('subject', 'HRMS Notification')
    body = data.get('body', '')
    if not to:
        return jsonify({'error': 'recipient required'}), 400
    # Queued rather than sent inline (FR-NOT-01): SMTP is a third-party network call
    # and this route is admin-triggered, so a slow provider would hold a worker for as
    # long as it chose. `force=True` — this is an explicit instruction to send mail to
    # a chosen recipient, so a notification preference belonging to that recipient must
    # not silently turn it into a no-op the admin believes succeeded.
    conn = get_db()
    try:
        event_id = enqueue_notification_email(
            conn, session['emp_id'], to, subject, body, force=True,
        )
        conn.commit()
        queued = True
    except Exception:
        logger.exception('Could not queue an admin notification email')
        event_id, queued = None, False
    finally:
        conn.close()
    # Audited before the return, and on both paths. An admin endpoint that sends mail
    # to **any address with any body** is a data-exfiltration route by construction —
    # the single most important thing to be able to answer afterwards is "who sent
    # what, to whom". Recording only the success path would hide exactly the attempts
    # that matter.
    audit_log(
        session['emp_id'], 'NOTIFICATION_EMAIL_SENT',
        f'Admin queued {subject!r} for {to} (event={event_id})',
        entity='notifications',
        # `delivered` is gone on purpose: this route no longer knows whether anything
        # was sent, and reporting a delivery it did not perform would be the same
        # defect as `send_email` returning True for a send that never happened. The
        # event id is what an operator correlates against /api/admin/outbox.
        after={'to': to, 'subject': subject, 'queued': queued, 'event_id': event_id},
    )
    if queued:
        return jsonify({
            'message': 'Email queued for delivery',
            'event_id': event_id,
            'note': 'Delivery happens on the outbox dispatcher, not in this request. '
                    'Watch GET /api/admin/outbox; a provider failure retries and then '
                    'dead-letters rather than being lost.',
        }), 202
    return jsonify({'error': 'Could not queue the email'}), 500


# ══════════════════════════════════════════════════════════════════════
#  PHASE 3 — ADVANCED ANALYTICS
# ══════════════════════════════════════════════════════════════════════

@app.route('/admin/analytics')
@hr_or_admin_required
def admin_analytics():
    return render_template('analytics.html')


@app.route('/api/v1/analytics/headcount')
@app.route('/api/analytics/headcount')
@admin_required
def analytics_headcount():
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'Employee'").fetchone()[0]
    dept = conn.execute("SELECT department, COUNT(*) FROM users WHERE role = 'Employee' AND department IS NOT NULL GROUP BY department ORDER BY COUNT(*) DESC").fetchall()
    conn.close()
    return jsonify({'total': total, 'by_department': [{'dept': r[0], 'count': r[1]} for r in dept]}), 200


@app.route('/api/v1/analytics/leave-trends')
@app.route('/api/analytics/leave-trends')
@admin_required
def analytics_leave_trends():
    months = request.args.get('months', 6, type=int)
    conn = get_db()
    rows = conn.execute(f"""
        SELECT strftime('%Y-%m', start_date) as month, leave_type, COUNT(*) as cnt
        FROM leave_requests WHERE status = 'Approved'
        AND start_date >= date('now', '-{months} months')
        GROUP BY month, leave_type ORDER BY month
    """).fetchall()
    conn.close()
    return jsonify([{'month': r[0], 'type': r[1], 'count': r[2]} for r in rows]), 200


@app.route('/api/v1/analytics/attrition-risk')
@app.route('/api/analytics/attrition-risk')
@admin_required
def analytics_attrition():
    conn = get_db()
    rows = conn.execute("""
        SELECT u.emp_id, u.name, u.department, u.designation,
            COALESCE(lr.leave_count, 0) as leave_count,
            COALESCE(reg.reg_count, 0) as reg_count,
            COALESCE(eb.early_break, 0) as early_break
        FROM users u
        LEFT JOIN (SELECT emp_id, COUNT(*) as leave_count FROM leave_requests WHERE status = 'Approved' AND start_date >= date('now', '-3 months') GROUP BY emp_id) lr ON u.emp_id = lr.emp_id
        LEFT JOIN (SELECT emp_id, COUNT(*) as reg_count FROM regularization_requests WHERE status = 'Pending' GROUP BY emp_id) reg ON u.emp_id = reg.emp_id
        LEFT JOIN (SELECT emp_id, COUNT(*) as early_break FROM breaks WHERE break_date >= date('now', '-1 months') AND duration_minutes < 5 GROUP BY emp_id) eb ON u.emp_id = eb.emp_id
        WHERE u.role = 'Employee' ORDER BY (COALESCE(lr.leave_count,0) * 0.5 + COALESCE(reg.reg_count,0) * 2) DESC LIMIT 20
    """).fetchall()
    conn.close()
    return jsonify([{'emp_id': r[0], 'name': r[1], 'department': r[2], 'designation': r[3], 'leave_count': r[4], 'reg_count': r[5], 'early_break': r[6], 'risk_score': round(r[4] * 0.5 + r[5] * 2, 1)} for r in rows]), 200


@app.route('/api/v1/analytics/expense-summary')
@app.route('/api/analytics/expense-summary')
@admin_required
def analytics_expense_summary():
    conn = get_db()
    total = conn.execute("SELECT COALESCE(SUM(amount),0) FROM expense_claims WHERE status IN ('Approved','Paid')").fetchone()[0]
    by_cat = conn.execute("SELECT e.name, COALESCE(SUM(c.amount),0) FROM expense_claims c JOIN expense_categories e ON c.cat_id = e.cat_id WHERE c.status IN ('Approved','Paid') GROUP BY e.name ORDER BY SUM(c.amount) DESC").fetchall()
    pending = conn.execute("SELECT COUNT(*) FROM expense_claims WHERE status = 'Pending'").fetchone()[0]
    conn.close()
    return jsonify({'total': float(total), 'by_category': [{'cat': r[0], 'amount': float(r[1])} for r in by_cat], 'pending_claims': pending}), 200


@app.route('/api/v1/analytics/performance-summary')
@app.route('/api/analytics/performance-summary')
@hr_or_admin_required
def analytics_performance():
    conn = get_db()
    avg_rating = conn.execute("SELECT COALESCE(AVG(overall_rating),0) FROM performance_reviews WHERE status = 'Submitted'").fetchone()[0]
    by_dept = conn.execute("""
        SELECT u.department, COALESCE(AVG(r.overall_rating),0)
        FROM performance_reviews r JOIN users u ON r.emp_id = u.emp_id
        WHERE r.status = 'Submitted' AND u.department IS NOT NULL
        GROUP BY u.department ORDER BY AVG(r.overall_rating) DESC
    """).fetchall()
    conn.close()
    return jsonify({'avg_rating': round(float(avg_rating), 2), 'by_department': [{'dept': r[0], 'avg': round(float(r[1]), 2)} for r in by_dept]}), 200


# ══════════════════════════════════════════════════════════════════════
#  PHASE 3 — FULL PAYROLL (Payslip PDF, Bank File, TDS)
# ══════════════════════════════════════════════════════════════════════

from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: E402
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle  # noqa: E402


def generate_payslip_pdf(run_id, emp_id):
    """Generate a payslip PDF and upload to object storage.
    Returns the object key on success, None if payslip not found.
    """
    conn = get_db()
    row = conn.execute(
        "SELECT p.item_id, r.month, r.year, p.emp_id, u.name, u.department, u.designation, p.gross_salary, p.deductions_total, p.net_salary, p.pf, p.esi, p.pt FROM payroll_items p JOIN payroll_runs r ON p.run_id = r.run_id JOIN users u ON p.emp_id = u.emp_id WHERE p.run_id = ? AND p.emp_id = ?",
        [run_id, emp_id]
    ).fetchone()
    if not row:
        conn.close()
        return None

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=30, bottomMargin=30)
    styles = getSampleStyleSheet()
    elements = []

    elements.append(Paragraph(f"PAYSLIP - {row[1]}/{row[2]}", styles['Title']))
    elements.append(Spacer(1, 12))

    data = [
        ['Employee ID', row[3]],
        ['Name', row[4]],
        ['Department', row[5] or '-'],
        ['Designation', row[6] or '-'],
        ['Gross Salary', f"₹{float(row[7]):,.2f}"],
        ['PF', f"₹{float(row[10]):,.2f}"],
        ['ESI', f"₹{float(row[11]):,.2f}"],
        ['Professional Tax', f"₹{float(row[12]):,.2f}"],
        ['Other Deductions', f"₹{float(row[8]) - float(row[10]) - float(row[11]) - float(row[12]):,.2f}"],
        ['Total Deductions', f"₹{float(row[8]):,.2f}"],
        ['NET SALARY', f"₹{float(row[9]):,.2f}"],
    ]
    t = Table(data, colWidths=[200, 300])
    t.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 11),
        ('BACKGROUND', (0, 0), (0, -1), colors.Color(0.95, 0.95, 0.95)),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.grey),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('SPAN', (0, -1), (1, -1)),
        ('BACKGROUND', (0, -1), (-1, -1), colors.Color(0.12, 0.16, 0.23)),
        ('TEXTCOLOR', (0, -1), (-1, -1), colors.white),
        ('FONTSIZE', (0, -1), (-1, -1), 14),
    ]))
    elements.append(t)

    doc.build(elements)
    buf.seek(0)
    pdf_bytes = buf.read()

    # Upload to object storage (FR-PAY-07: PDF stored in object storage)
    object_key = object_storage.upload_payslip(run_id, emp_id, pdf_bytes)

    conn.execute("UPDATE payroll_items SET payslip_generated = 1 WHERE run_id = ? AND emp_id = ?", [run_id, emp_id])
    conn.close()

    return object_key


@app.route('/api/v1/payroll-runs/<int:rid>/payslip-pdf/<emp_id>')
@app.route('/api/payroll-runs/<int:rid>/payslip-pdf/<emp_id>')
@login_required
def payslip_pdf(rid, emp_id):
    conn = get_db()
    if not _may_read_payslip(conn, emp_id):
        conn.close()
        return jsonify({'error': 'Forbidden'}), 403
    run = conn.execute("SELECT status FROM payroll_runs WHERE run_id = ?", [rid]).fetchone()
    conn.close()
    if not run:
        return jsonify({'error': 'Not found'}), 404
    if run[0] != 'Finalized':
        return jsonify({'error': 'Payslips are available only after payroll finalization'}), 409

    # Generate presigned URL for object storage (FR-PAY-07)
    try:
        presigned_url = object_storage.get_payslip_presigned_url(rid, emp_id)
    except object_storage.ObjectStorageError as exc:
        logger.error("Payslip presigned URL generation failed: %s", exc)
        return jsonify({'error': 'Payslip unavailable'}), 503

    # Redirect to presigned URL — works for both browser and API clients
    return redirect(presigned_url, code=302)


@app.route('/api/v1/payroll-runs/<int:rid>/bank-file')
@app.route('/api/payroll-runs/<int:rid>/bank-file')
@finance_or_admin_required
def bank_file_export(rid):
    conn = get_db()
    run = conn.execute("SELECT status FROM payroll_runs WHERE run_id = ?", [rid]).fetchone()
    if not run:
        conn.close()
        return jsonify({'error': 'Run not found'}), 404
    if run[0] != 'Finalized':
        conn.close()
        return jsonify({'error': 'Bank file is available only for Finalized payroll runs'}), 409
    rows = conn.execute(
        "SELECT p.emp_id, u.name, p.net_salary FROM payroll_items p JOIN users u ON p.emp_id = u.emp_id WHERE p.run_id = ? ORDER BY u.name",
        [rid]
    ).fetchall()
    conn.close()
    if not rows:
        return jsonify({'error': 'No items'}), 404
    import csv
    import io
    text_buf = io.StringIO()
    writer = csv.writer(text_buf)
    writer.writerow(['Employee ID', 'Name', 'Net Salary', 'Account Number', 'IFSC'])
    for r in rows:
        writer.writerow([r[0], r[1], f"{float(r[2]):.2f}", '', ''])
    buf = BytesIO(text_buf.getvalue().encode('utf-8'))
    return send_file(buf, mimetype='text/csv', as_attachment=True, download_name=f'payroll_{rid}.csv')


def calc_tds(annual_gross):
    if annual_gross <= 300000:
        return 0
    elif annual_gross <= 600000:
        return (annual_gross - 300000) * 0.05
    elif annual_gross <= 900000:
        return 15000 + (annual_gross - 600000) * 0.1
    elif annual_gross <= 1200000:
        return 45000 + (annual_gross - 900000) * 0.15
    elif annual_gross <= 1500000:
        return 90000 + (annual_gross - 1200000) * 0.2
    else:
        return 150000 + (annual_gross - 1500000) * 0.3


@app.route('/api/v1/payroll-runs/<int:rid>/tds-report')
@app.route('/api/payroll-runs/<int:rid>/tds-report')
@finance_or_admin_required
def tds_report(rid):
    conn = get_db()
    run = conn.execute("SELECT month, year, status FROM payroll_runs WHERE run_id = ?", [rid]).fetchone()
    if not run:
        conn.close()
        return jsonify({'error': 'Run not found'}), 404
    if run[2] != 'Finalized':
        conn.close()
        return jsonify({'error': 'TDS report is available only for Finalized payroll runs'}), 409
    rows = conn.execute(
        "SELECT p.emp_id, u.name, p.gross_salary FROM payroll_items p JOIN users u ON p.emp_id = u.emp_id WHERE p.run_id = ?",
        [rid]
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        monthly_gross = float(r[2])
        annual_gross = monthly_gross * 12
        tds = round(calc_tds(annual_gross) / 12, 2)
        result.append({'emp_id': r[0], 'name': r[1], 'monthly_gross': monthly_gross, 'annual_gross': annual_gross, 'tds': tds})
    return jsonify(result), 200


# ══════════════════════════════════════════════════════════════════════
#  LEAVE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════

@app.route('/leaves')
@login_required
def leaves_page():
    return render_template('leaves.html')


@app.route('/admin/leaves')
@hr_or_admin_required
def admin_leaves_page():
    return render_template('admin_leaves.html')


@app.route('/api/v1/leaves', methods=['GET', 'POST'])
@app.route('/api/leaves', methods=['GET', 'POST'])
@login_required
@idempotent
def leaves_api():
    """Create or list leave requests
    ---
    get:
      tags: [Leaves]
      parameters:
        - in: query
          name: status
          type: string
      responses:
        200:
          description: Leave list
    post:
      tags: [Leaves]
      parameters:
        - in: body
          name: body
          schema:
            type: object
            properties:
              leave_type: {type: string}
              start_date: {type: string, format: date}
              end_date: {type: string, format: date}
              reason: {type: string}
      responses:
        201:
          description: Leave created
    """
    emp_id = session['emp_id']
    conn = get_db()

    if request.method == 'GET':
        status_filter = request.args.get('status')
        month_filter = request.args.get('month', type=int)
        year_filter = request.args.get('year', type=int)
        # FR-LEA-01 — "list with pending_my_approval / admin filters / self filters,
        # plus delegated-manager visibility". The three scopes are mutually exclusive
        # on purpose, and collapsing the duplicated branch into one is what makes that
        # readable: the pending filter is *not* a narrowing of the company-wide list,
        # it is the caller's own approval queue. So it has to work for a manager who is
        # not in `can_view_all` at all (a Team Leader sees no leave but their reports'),
        # and it has to answer *nothing* rather than *everything* for somebody who
        # manages nobody and holds no delegation.
        pending_my_approval = request.args.get(
            'pending_my_approval', '').lower() in ('1', 'true', 'yes')
        conditions, params = [], []
        if pending_my_approval:
            clause, extra = _pending_my_approval_clause(conn, emp_id, 'l.')
            if clause is None:
                conn.close()
                return jsonify([]), 200
            conditions.append(clause)
            params.extend(extra)
        elif not policy.can_view_all(policy.current_actor(conn), 'leaves', conn=conn):
            conditions.append('l.emp_id = ?')
            params.append(emp_id)
        if status_filter:
            conditions.append('l.status = ?')
            params.append(status_filter)
        if year_filter:
            conditions.append('l.year = ?')
            params.append(year_filter)
        if month_filter:
            conditions.append("CAST(strftime('%m', l.start_date) AS INTEGER) = ?")
            params.append(month_filter)
        query = """SELECT l.leave_id, l.emp_id, u.name, l.leave_type, l.start_date, l.end_date,
                   l.reason, l.status, l.approved_by, l.created_at
                   FROM leave_requests l LEFT JOIN users u ON l.emp_id = u.emp_id"""
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY l.created_at DESC"
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return jsonify([{
            'leave_id': r[0], 'emp_id': r[1], 'emp_name': r[2] or r[1], 'leave_type': r[3],
            'start_date': r[4].isoformat(), 'end_date': r[5].isoformat(),
            'reason': r[6], 'status': r[7], 'approved_by': r[8],
            'created_at': r[9].isoformat() if r[9] else None
        } for r in rows]), 200

    data = request.get_json(silent=True) or {}
    lt = data.get('leave_type')
    sd = parse_date(data.get('start_date'))
    ed = parse_date(data.get('end_date'), sd)
    if not lt or not sd or not ed:
        conn.close()
        return jsonify({'error': 'leave_type, start_date, end_date required'}), 400
    if ed < sd:
        sd, ed = ed, sd

    # FR-LEA-06: the entitlement is derived from the employee's leave policy and
    # the requested days are *reserved* while the request is Pending, so two
    # overlapping-in-time requests can no longer spend the same balance. A leave
    # type with no entitlement keeps the old "unlimited" behaviour.
    #
    # FR-LEA-09: this was `(ed - sd).days + 1`, which counts **calendar** days — so a
    # Friday-to-Monday request cost four days of allowance, two of which were a
    # weekend. It now goes through the one shared working-day function that payroll
    # loss-of-pay and the reports use, so "how many days" has a single answer.
    # FR-LEA-02 lists `session` (Full | First-half | Second-half) in the create
    # payload and nothing implemented it — the column existed on the canonical schema
    # with no reader and no writer. A half-day request is charged half a day, which
    # is why `working_days(allow_half=True)` returns a Fraction: this is the one
    # caller that must be able to express a fraction, and rounding it here is what
    # would let a half-day leave through for free.
    session_kind = str(data.get('session') or 'Full').strip()
    if session_kind not in working_days.LEAVE_SESSIONS:
        conn.close()
        return jsonify({
            'error': 'session must be one of: '
                     + ', '.join(working_days.LEAVE_SESSIONS),
        }), 400
    half = session_kind in ('First-half', 'Second-half')
    requested_raw = working_days.working_days(conn, emp_id, sd, ed, allow_half=True)
    requested = working_days.whole_days(requested_raw)
    if half:
        # Half a session over a multi-day range still leaves whole days behind, so the
        # deduction is half of one day and never zero.
        requested = max(1, requested // 2)
    if requested <= 0:
        # Every day in the range is a weekly off or a holiday. Recording 0 reserved
        # would leave a Pending request that deducts nothing, which is not what
        # someone asking to book leave means.
        conn.close()
        return jsonify({
            'error': 'That range contains no working days',
            'start_date': sd.isoformat(),
            'end_date': ed.isoformat(),
            'hint': 'Every day in the range is a weekly off or a holiday for you.',
        }), 400
    leave_policy.ensure_balances(conn, emp_id, sd.year)
    remaining = leave_policy.remaining_days(conn, emp_id, lt, sd.year)
    if remaining is not None and requested > remaining:
        conn.close()
        return jsonify({'error': f'Insufficient balance. Remaining: {remaining} days'}), 400

    if conn.execute(
        "SELECT 1 FROM leave_requests WHERE emp_id = ? AND status IN ('Pending','Approved') AND start_date <= ? AND end_date >= ?",
        [emp_id, ed, sd]
    ).fetchone():
        conn.close()
        return jsonify({'error': 'Overlapping leave request already exists for these dates'}), 409

    leave_id = _next_generated_id(conn, 'leave_requests', 'leave_id')
    conn.execute(
        "INSERT INTO leave_requests (leave_id, emp_id, leave_type, start_date, end_date, year, reason, status, session, days) VALUES (?, ?, ?, ?, ?, ?, ?, 'Pending', ?, ?)",
        [leave_id, emp_id, lt, sd, ed, sd.year, data.get('reason', ''),
         session_kind, requested]
    )
    if remaining is not None:
        leave_policy.reserve(conn, emp_id, lt, requested, sd.year)
    conn.close()
    audit_log(emp_id, 'LEAVE_APPLY', f'{lt} leave {sd} to {ed}', entity='leave_requests', entity_id=leave_id)
    add_notification(session['emp_id'], 'LEAVE_APPLIED', f'Your {lt} leave ({sd} to {ed}) has been submitted.', '/leaves')
    return jsonify({
        'message': 'Leave application submitted',
        'leave_id': leave_id,
        # FR-LEA-09: state the working-day figure, so an employee asking for
        # Fri-to-Mon can see that it cost two days and not four.
        'days_requested': requested,
        'calendar_days': (ed - sd).days + 1,
        'session': session_kind,
    }), 201


@app.route('/api/v1/leave-grants', methods=['POST', 'GET'])
@app.route('/api/leave-grants', methods=['POST', 'GET'])
@hr_or_admin_required
def leave_grants_api():
    """FR-LEA-07 — HR/Admin grants days to one or more employees' balances.

    The SRS: *"Grants: HR/Admin can add days to one or more employees' balances for a
    type/month/year; fully audited as LEAVE_GRANT with before/after totals."* All five
    clauses are implemented here, and the gate is HR **or** Admin because the
    requirement names both and `@admin_required` would have excluded HR — the same
    gate-versus-requirement mismatch this codebase has now found in four places.

    **Each employee is committed and audited independently**, so one unknown id in a
    list of fifty does not fail the other forty-nine. A batch that is all-or-nothing
    would mean a single typo costs an administrator the whole afternoon.
    """
    if request.method == 'GET':
        emp_id = request.args.get('emp_id') or session['emp_id']
        conn = get_db()
        try:
            # A grant history is reason a ceiling moved. An administrator looking at a
            # number that does not match the policy needs this, and an employee
            # questioning a grant needs it too.
            return jsonify({
                'emp_id': emp_id,
                'grants': leave_grants.grants_for(conn, emp_id),
            }), 200
        finally:
            conn.close()

    data = request.get_json(silent=True) or {}
    try:
        spec = leave_grants.validate(data)
    except leave_grants.GrantError as exc:
        return jsonify({'error': exc.message}), exc.status

    actor = session['emp_id']
    results, failures = [], []
    # One connection per employee: the ledger write, the re-materialisation and the
    # audit row belong together, and an employee whose grant failed must not roll back
    # the forty-nine that succeeded.
    for emp_id in spec['emp_ids']:
        conn = get_db()
        try:
            outcome = leave_grants.apply_grant(conn, actor, {**spec, 'emp_id': emp_id})
            conn.commit()
        except leave_grants.GrantError as exc:
            conn.rollback()
            failures.append({'emp_id': exc.emp_id or emp_id, 'error': exc.message})
            continue
        except Exception:
            conn.rollback()
            logger.exception('leave grant failed for %s', emp_id)
            failures.append({'emp_id': emp_id, 'error': 'grant failed'})
            continue
        finally:
            conn.close()

        results.append(outcome)
        # Audit **per employee**, inside the success path, so the row and the ledger
        # write belong to the same outcome. A summary row written once at the end would
        # record one fact about forty-nine people, which is the shape FR-AUD-01's
        # before/after columns exist to avoid.
        audit_log(
            actor, 'LEAVE_GRANT',
            f"Granted {outcome['days']} day(s) {outcome['leave_type']} "
            f"({outcome['year']}) to {outcome['emp_id']}: {outcome['reason']}",
            entity='leave_grants', entity_id=str(outcome['grant_id']),
            before=outcome['before'], after=outcome['after'],
        )
        add_notification(
            outcome['emp_id'], 'LEAVE_GRANT',
            f"An administrator adjusted your {outcome['leave_type']} leave balance "
            f"for {outcome['year']} by {outcome['days']} day(s). Your remaining "
            f"balance is now {outcome['after']['remaining']} day(s). "
            f"Reason: {outcome['reason']}",
            '/leaves',
            category=notifications.category_for('LEAVE_GRANT'),
        )

    body = {
        'granted': len(results),
        'failed': len(failures),
        'results': results,
        'failures': failures,
        'total_days': sum(r['days'] for r in results),
    }
    if not results:
        # Nothing was granted. The specific reason is already in `failures`; this only
        # says the batch as a whole achieved nothing.
        return jsonify({**body, 'error': 'No grants were applied'}), 400
    # Partial success is a real outcome and is reported as one — 207 — rather than a
    # 200 that hides the failures or a 400 that hides the successes.
    return jsonify(body), (207 if failures else 201)


@app.route('/api/v1/delegations', methods=['GET', 'POST'])
@app.route('/api/delegations', methods=['GET', 'POST'])
@login_required
def delegations_api():
    """FR-LEA-08a — hand approval authority to somebody else for a date range.

    *"A manager can delegate approval authority to another employee for a date range
    (e.g. while on leave). Delegates appear in pending_my_approval views and their
    approvals are audited as 'approved by delegate for manager X'."*

    Creating one requires that the caller **actually manages somebody** — a delegation
    is a handover of authority they hold, so an employee with no reports has nothing to
    delegate and the row would be decorative. Admins may delegate for themselves; they
    can also delegate on behalf of anyone, which is the practical case for a manager who
    is on leave and cannot be asked to set this up first.

    `GET` returns both directions for the caller — what they have delegated away and
    what has been delegated *to* them — because "pending_my_approval" needs the second
    and an administrator needs both.
    """
    actor = session['emp_id']
    conn = get_db()
    try:
        actor_row = conn.execute(
            'SELECT role, manager_emp_id FROM users WHERE emp_id = ?', [actor],
        ).fetchone()
        is_admin = bool(actor_row) and actor_row[0] in policy.ADMIN_ROLES
        manages_anyone = _manages_any_employee(conn, actor)

        if request.method == 'GET':
            subject = request.args.get('emp_id') or actor
            return jsonify({
                'delegated_by_me': delegations.for_delegator(conn, subject),
                'delegated_to_me': delegations.for_delegate(conn, actor),
                # Who I can currently approve for — the "pending_my_approval" half
                # made explicit, and answered by the same function the list filters
                # use so this response and the queue cannot disagree. `null` means
                # "every employee but yourself" (the HR/Admin case), `[]` means
                # nobody, and the list itself never contains the caller.
                'act_for': delegations.approvable_employees(conn, actor),
            }), 200

        data = request.get_json(silent=True) or {}
        delegator = str(data.get('delegator_id') or actor).strip().upper()
        if delegator != actor and not is_admin:
            return jsonify({
                'error': 'You can only delegate your own approval authority',
            }), 403
        if delegator != actor and not _manages_any_employee(conn, delegator):
            return jsonify({
                'error': f'{delegator} manages nobody, so there is no authority to '
                         f'delegate',
            }), 409
        # The same rule for the ordinary case: delegating your *own* authority. The
        # docstring above promises this and nothing enforced it — `manages_anyone` was
        # computed, then dropped, so an employee with no reports could file a
        # decorative delegation that made it into `pending_my_approval` views looking
        # like coverage. Admins are exempt because they hold approval authority by
        # role rather than by having reports.
        if delegator == actor and not is_admin and not manages_anyone:
            return jsonify({
                'error': 'You manage nobody, so there is no approval authority to '
                         'delegate',
            }), 409

        starts_on = parse_date(data.get('starts_on'))
        ends_on = parse_date(data.get('ends_on'), starts_on)
        try:
            created = delegations.create(
                conn, delegator, data.get('delegate_id'), starts_on, ends_on,
                str(data.get('reason') or '').strip(),
            )
            conn.commit()
        except delegations.DelegationError as exc:
            conn.rollback()
            return jsonify({'error': exc.message}), exc.status
    finally:
        conn.close()

    audit_log(
        actor, 'APPROVAL_DELEGATION_CREATED',
        f"{actor} delegated approval authority for {created['delegator_id']} to "
        f"{created['delegate_id']} from {created['starts_on']} to {created['ends_on']}",
        entity='approval_delegations', entity_id=str(created['delegation_id']),
        after=created,
    )
    add_notification(
        created['delegate_id'], 'APPROVAL_DELEGATED',
        f"{actor} has delegated their approval authority to you from "
        f"{created['starts_on']} to {created['ends_on']}. Requests awaiting their approval "
        f"will appear in your pending list during that period.",
        category=notifications.category_for('APPROVAL_DELEGATED'),
    )
    return jsonify(created), 201


@app.route('/api/v1/delegations/<int:delegation_id>', methods=['DELETE'])
@app.route('/api/delegations/<int:delegation_id>', methods=['DELETE'])
@login_required
def delegations_revoke(delegation_id):
    """Withdraw a delegation early — the manager is back, or the delegate is unsuitable.

    Without this the only way to stop a delegation was to wait for it to expire, which
    for a delegation made in good faith and then regretted is the wrong answer.
    """
    conn = get_db()
    try:
        # `is_admin` is read from the **database**, never from the session's copy of
        # the role — the same rule every other authorization decision here follows
        # (FR-USR-15): a role change has to take effect on the next request, not the
        # next login.
        actor = policy.current_actor(conn)
        is_admin = str(actor.get('role') or '') in policy.ADMIN_ROLES
        delegations.revoke(
            conn, delegation_id, session['emp_id'], is_admin=is_admin)
        conn.commit()
    except delegations.DelegationError as exc:
        conn.rollback()
        return jsonify({'error': exc.message}), exc.status
    finally:
        conn.close()
    audit_log(
        session['emp_id'], 'APPROVAL_DELEGATION_REVOKED',
        f'Revoked delegation {delegation_id}',
        entity='approval_delegations', entity_id=str(delegation_id),
        before={'revoked': False}, after={'revoked': True},
    )
    return jsonify({'message': 'Delegation revoked', 'delegation_id': delegation_id}), 200


@app.route('/api/v1/leaves/export', methods=['GET'])
@app.route('/api/leaves/export', methods=['GET'])
@admin_required
def export_leaves():
    month = request.args.get('month', datetime.now().month, type=int)
    year = request.args.get('year', datetime.now().year, type=int)
    status_filter = request.args.get('status')
    conn = get_db()
    try:
        query = """SELECT l.emp_id, u.name, l.leave_type, l.start_date, l.end_date,
                   l.reason, l.status, l.approved_by, l.days
                   FROM leave_requests l LEFT JOIN users u ON l.emp_id = u.emp_id
                   WHERE l.year = ? AND CAST(strftime('%m', l.start_date) AS INTEGER) = ?"""
        params = [year, month]
        if status_filter:
            query += " AND l.status = ?"
            params.append(status_filter)
        query += " ORDER BY l.start_date"
        rows = conn.execute(query, params).fetchall()
        # FR-LEA-09: report the **stored** working-day figure, not a recomputation.
        # Reading `days` is what guarantees the sheet agrees with the ledger, and it
        # is the only way this could work at all — the connection is closed by the
        # `finally` below, so a per-row call to the shared function would run on a
        # closed cursor. A NULL is a pre-migration row and falls back to the function
        # *here*, while the connection is still open.
        computed = [
            (r, r[8] if r[8] is not None
             else working_days.working_days(conn, r[0], r[3], r[4]))
            for r in rows
        ]
    finally:
        conn.close()

    import io

    import pandas as pd
    # Both figures are shown: the working days the ledger used, and the calendar span
    # beside it. Reporting only one makes the other look like an error when an
    # employee reconciles the sheet against their own calendar.
    data = [{
        'Employee ID': r[0], 'Employee Name': r[1] or r[0], 'Leave Type': r[2],
        'From': r[3].isoformat(), 'To': r[4].isoformat(),
        'Working Days': days,
        'Calendar Days': (r[4] - r[3]).days + 1,
        'Reason': r[5] or '', 'Status': r[6], 'Approved By': r[7] or ''
    } for r, days in computed]

    buf = io.BytesIO()
    df = pd.DataFrame(data) if data else pd.DataFrame(columns=['Employee ID','Employee Name','Leave Type','From','To','Working Days','Calendar Days','Reason','Status','Approved By'])
    with pd.ExcelWriter(buf, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Leaves')
    buf.seek(0)
    month_name = datetime(2000, month, 1).strftime('%B')
    return send_file(buf, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     download_name=f'leaves_{month_name}_{year}.xlsx', as_attachment=True)


# FR-LEA-04 — *"Approve: actor is manager (or delegate) or HR/Admin, not the
# applicant; conditional update Pending → Approved; leave_balance.used_days += days,
# leave_balance.reserved −= days, in one transaction."* Three of those clauses were
# not enforced, and the matrix note claimed all three:
#
#   * the gate was `@admin_required`, so a Team Leader could not approve their own
#     report's leave — the third instance of the gate bug, after FR-EXP-03 (Finance
#     could not mark a claim Paid) and FR-PERF-01 (a reporting manager could not
#     rate their report's goal);
#   * there was no self-approval block of any kind, so an HR administrator could
#     sign off their own application;
#   * the write was `WHERE leave_id = ?` with only a prior SELECT in front of it, so
#     two approvers racing would **both** win and `leave_policy.consume` would run
#     twice against one reservation — the exact double spend CC-04 exists to
#     prevent, on the one ledger where it is silent and permanent.
#
# The first two are `_approval_denial`, shared with regularization and break approval
# so the four approval paths cannot re-answer the same question differently; the
# third is the `status = 'Pending'` predicate plus its rowcount.
@app.route('/api/v1/leaves/<int:leave_id>/approve', methods=['POST'])
@app.route('/api/leaves/<int:leave_id>/approve', methods=['POST'])
@reporting_line_required
def approve_leave(leave_id):
    """Approve a leave request
    ---
    post:
      tags: [Leaves]
      parameters:
        - in: path
          name: leave_id
          type: integer
      responses:
        200:
          description: Approved
    """
    conn = get_db()
    # `days` and `session` are read here for the first time: FR-LEA-09 moved the
    # figure to apply time and stored it, so approve moves **exactly** what was
    # reserved rather than recomputing. Recomputing is not equivalent — a holiday
    # added between applying and approving would release a different number of days,
    # and the balance would drift by the difference with every audit row still
    # honest. A NULL `days` is a pre-migration row: fall back to the shared function
    # and persist the answer, which is what lets old requests stay correct.
    try:
        row = conn.execute(
            "SELECT emp_id, leave_type, start_date, end_date, status, days, session "
            "FROM leave_requests WHERE leave_id = ?",
            [leave_id]
        ).fetchone()
        if not row:
            return jsonify({'error': 'Leave not found'}), 404
        if row[4] != 'Pending':
            return jsonify({'error': 'Leave is not pending'}), 400
        denial = _approval_denial(conn, session['emp_id'], row[0])
        if denial is not None:
            return denial
        note = delegations.approval_note(conn, session['emp_id'], row[0])

        days = row[5]
        if days is None:
            raw = working_days.working_days(conn, row[0], row[2], row[3], allow_half=True)
            days = working_days.whole_days(raw)
            if str(row[6] or 'Full') in ('First-half', 'Second-half'):
                days = max(1, days // 2)
            conn.execute(
                "UPDATE leave_requests SET days = ? WHERE leave_id = ?", [days, leave_id])
        # Conditional on `status = 'Pending'` (CC-04), so two approvers racing give
        # one winner and one 409. The comment above is the reason this is not
        # cosmetic: the loser must not reach `leave_policy.consume`.
        claimed = conn.execute(
            "UPDATE leave_requests SET status = 'Approved', approved_by = ?, updated_at = ? "
            "WHERE leave_id = ? AND status = 'Pending'",
            [session['emp_id'], datetime.now(), leave_id]
        )
        if not getattr(claimed, 'rowcount', 1):
            return jsonify({'error': 'Leave was reviewed by someone else'}), 409
        leave_policy.consume(conn, row[0], row[1], days, row[2].year)
        conn.commit()
    finally:
        conn.close()
    audit_log(
        session['emp_id'], 'LEAVE_APPROVE',
        f'Leave {leave_id} approved' + (f' ({note})' if note else ''),
        entity='leave_requests', entity_id=leave_id,
        before={'status': 'Pending'}, after={'status': 'Approved'},
    )
    add_notification(row[0], 'LEAVE_APPROVED', f'Your {row[1]} leave ({row[2]} to {row[3]}) has been approved.', '/leaves')
    return jsonify({'message': 'Leave approved'}), 200


# The same actor rule as approve (FR-LEA-04 gives both decisions to one actor), and
# the same reason the gate had to change: a queue where rejecting is admin-only while
# approving is manager-reachable is a queue that only ever gets approved. The
# conditional write matters here too — reject *releases* the reservation, so a loser
# in the race would give an employee their days back twice.
@app.route('/api/v1/leaves/<int:leave_id>/reject', methods=['POST'])
@app.route('/api/leaves/<int:leave_id>/reject', methods=['POST'])
@reporting_line_required
def reject_leave(leave_id):
    """Reject a leave request"""
    conn = get_db()
    # `days`/`session` appended for FR-LEA-09. Releasing the **stored** figure rather
    # than recomputing is what keeps reject symmetric with apply: a shared function
    # called twice can legitimately answer differently the second time — a holiday
    # added between applying and rejecting would otherwise give back a different
    # number of days than was taken, and the balance would drift with every audit row
    # still honest. A NULL `days` is a pre-migration row and falls back to the
    # function — with the *same* half-day handling approve uses, because a fallback
    # that releases more than apply reserved raises an employee's balance on every
    # rejected request.
    try:
        row = conn.execute(
            "SELECT emp_id, leave_type, start_date, end_date, status, days, session "
            "FROM leave_requests WHERE leave_id = ?", [leave_id]).fetchone()
        if not row:
            return jsonify({'error': 'Not found'}), 404
        if row[4] != 'Pending':
            return jsonify({'error': 'Leave is not pending'}), 400
        denial = _approval_denial(conn, session['emp_id'], row[0])
        if denial is not None:
            return denial
        note = delegations.approval_note(conn, session['emp_id'], row[0])
        claimed = conn.execute(
            "UPDATE leave_requests SET status = 'Rejected', approved_by = ?, updated_at = ? "
            "WHERE leave_id = ? AND status = 'Pending'",
            [session['emp_id'], datetime.now(), leave_id]
        )
        if not getattr(claimed, 'rowcount', 1):
            return jsonify({'error': 'Leave was reviewed by someone else'}), 409
        days = row[5]
        if days is None:
            days = working_days.whole_days(working_days.working_days(
                conn, row[0], row[2], row[3], allow_half=True))
            if str(row[6] or 'Full') in ('First-half', 'Second-half'):
                days = max(1, days // 2)
        leave_policy.release(conn, row[0], row[1], days, row[2].year)
    finally:
        conn.close()
    audit_log(
        session['emp_id'], 'LEAVE_REJECT',
        f'Leave {leave_id} rejected' + (f' ({note})' if note else ''),
        entity='leave_requests', entity_id=leave_id,
        before={'status': 'Pending'}, after={'status': 'Rejected'},
    )
    add_notification(row[0], 'LEAVE_REJECTED', f'Your {row[1]} leave ({row[2]} to {row[3]}) has been rejected.', '/leaves')
    return jsonify({'message': 'Leave rejected'}), 200


@app.route('/api/v1/leaves/<int:leave_id>/cancel', methods=['POST'])
@app.route('/api/leaves/<int:leave_id>/cancel', methods=['POST'])
@login_required
def cancel_leave(leave_id):
    """Cancel a leave request (FR-LEA-05).

    "Cancel: Pending only, or Approved with a future start date (with the same
    reserved/used reversal), by owner or admin." There was no cancel route at all,
    which had a concrete consequence: a Pending request reserves days against the
    employee's balance and nothing could ever give them back, so a leave request
    that changed its mind silently reduced their remaining leave for the year.

    The decision *and* the ledger reversal are `leave_policy.cancel`'s, so a caller
    cannot check one and apply the other — the hazard here is releasing days that
    approval has already moved into `used_days`, and that is decided and performed
    in one function.
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT leave_id, emp_id, leave_type, start_date, end_date, status, "
            "days, session "
            'FROM leave_requests WHERE leave_id = ?', [leave_id],
        ).fetchone()
        if not row:
            return jsonify({'error': 'Leave request not found'}), 404
        actor = policy.current_actor(conn)
        is_admin = actor.get('role') in policy.ADMIN_ROLES
        try:
            action = leave_policy.cancel(conn, actor.get('emp_id'), row, is_admin)
        except leave_policy.LeaveError as exc:
            return jsonify({'error': str(exc)}), exc.status
    finally:
        conn.close()
    # `release` returns a reservation; `unconsume` takes approved days back out of
    # usage. The action is in the response and the audit row because "how much of my
    # leave did this give back" is the question an employee actually has.
    audit_log(
        session['emp_id'], 'LEAVE_CANCEL',
        f'Leave {leave_id} cancelled ({action} of '
        f'{row[6] if row[6] is not None else leave_policy.days_between(row[3], row[4])} '
        f'working day(s))',
        entity='leave_requests', entity_id=leave_id,
        before={'status': row[5]}, after={'status': 'Cancelled', 'ledger': action},
    )
    if row[0] == session['emp_id']:
        add_notification(
            row[0], 'LEAVE_CANCELLED',
            f'Your {row[2]} leave ({row[3]} to {row[4]}) was cancelled.', '/leaves', 'Leaves',
        )
    return jsonify({
        'message': 'Leave cancelled', 'status': 'Cancelled', 'ledger': action,
    }), 200


@app.route('/api/v1/leave-balance')
@app.route('/api/leave-balance')
@login_required
def leave_balance_api():
    """Get leave balance for current user"""
    emp_id = session['emp_id']
    year = request.args.get('year', datetime.now().year, type=int)
    conn = get_db()
    try:
        # Materialise the policy-derived entitlement before answering, so a new
        # employee and a policy change are both visible here immediately.
        leave_policy.ensure_balances(conn, emp_id, year)
        balances = leave_policy.balances_for(conn, emp_id, year)
    finally:
        conn.close()
    return jsonify(balances), 200



@app.route('/api/users/<emp_id>/leave-policy', methods=['GET'])
@hr_or_admin_required
def get_leave_policy(emp_id):
    """The employee's effective leave policy and the balances it derives."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT emp_id, name, role, status FROM users WHERE UPPER(emp_id) = ?",
            [emp_id.strip().upper()],
        ).fetchone()
        if not row:
            return jsonify({'error': 'User not found'}), 404
        emp_id = row[0]
        assignment = leave_policy.effective_assignment(conn, emp_id)
        year = request.args.get('year', datetime.now().year, type=int)
        derived = {
            leave_type: leave_policy.entitlement_days(conn, emp_id, leave_type)
            for leave_type in leave_policy.DEFAULT_ENTITLEMENTS
        }
        balances = leave_policy.balances_for(conn, emp_id, year)
        history = [
            {
                'assignment_id': r[0], 'location': r[1], 'grade': r[2],
                'accrual_rate': float(r[3]) if r[3] is not None else None,
                'carry_forward_cap': r[4], 'encashment_rule': r[5],
                'weekly_off_pattern': r[6],
                'effective_from': r[7].isoformat() if r[7] else None,
                'effective_to': r[8].isoformat() if r[8] else None,
            }
            for r in conn.execute(
                "SELECT assignment_id, location, grade, accrual_rate, carry_forward_cap, "
                "encashment_rule, weekly_off_pattern, effective_from, effective_to "
                "FROM leave_policy_assignments WHERE emp_id = ? ORDER BY effective_from DESC, assignment_id DESC",
                [emp_id],
            ).fetchall()
        ]
    finally:
        conn.close()
    return jsonify({
        'emp_id': emp_id,
        'name': row[1],
        'role': row[2],
        'year': year,
        'effective': assignment,
        'history': history,
        'entitlements': {
            leave_type: {'days': days, 'source': source}
            for leave_type, (days, source) in derived.items()
        },
        'balances': balances,
        'defaults': dict(leave_policy.DEFAULT_ENTITLEMENTS),
    }), 200


@app.route('/api/users/<emp_id>/leave-policy', methods=['PUT'])
@hr_or_admin_required
def update_leave_policy(emp_id):
    """Assign (or re-assign) an effective-dated leave policy for an employee.

    The previous assignment is closed on the new ``effective_from`` so the
    effective-dated lookup stays unambiguous, and the derived entitlement is
    re-materialised immediately.
    """
    payload = {key: value for key, value in (request.get_json(silent=True) or {}).items()}
    if 'assignment' in payload and isinstance(payload['assignment'], dict):
        payload = dict(payload['assignment'])
    try:
        assignment = leave_policy.validate_assignment(payload)
    except leave_policy.LeavePolicyError as exc:
        return jsonify({'error': str(exc)}), exc.status
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT emp_id, name FROM users WHERE UPPER(emp_id) = ?", [emp_id.strip().upper()]
        ).fetchone()
        if not row:
            return jsonify({'error': 'User not found'}), 404
        emp_id = row[0]
        before = leave_policy.effective_assignment(conn, emp_id, assignment['effective_from'])
        previous = conn.execute(
            "SELECT assignment_id, effective_from FROM leave_policy_assignments "
            "WHERE emp_id = ? AND effective_to IS NULL AND effective_from < ? "
            "ORDER BY effective_from DESC LIMIT 1",
            [emp_id, assignment['effective_from']],
        ).fetchone()
        if previous:
            # Close the open-ended row the day before the new one starts.
            closed_on = assignment['effective_from'].toordinal() - 1
            conn.execute(
                "UPDATE leave_policy_assignments SET effective_to = ? WHERE assignment_id = ?",
                [datetime.fromordinal(closed_on).date(), previous[0]],
            )
        assignment_id = _next_generated_id(conn, 'leave_policy_assignments', 'assignment_id')
        conn.execute(
            "INSERT INTO leave_policy_assignments (assignment_id, emp_id, location, grade, "
            "accrual_rate, carry_forward_cap, encashment_rule, weekly_off_pattern, "
            "effective_from, effective_to) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [assignment_id, emp_id, assignment['location'], assignment['grade'],
             assignment['accrual_rate'], assignment['carry_forward_cap'],
             assignment['encashment_rule'], assignment['weekly_off_pattern'],
             assignment['effective_from'], assignment['effective_to']],
        )
        year = request.args.get('year', datetime.now().year, type=int)
        balances = leave_policy.ensure_balances(conn, emp_id, year)
        effective = leave_policy.effective_assignment(conn, emp_id)
    finally:
        conn.close()
    audit_log(
        session['emp_id'],
        'LEAVE_POLICY_ASSIGN',
        f"Leave policy assigned to {emp_id} from {assignment['effective_from']}",
        entity='leave_policy_assignments',
        entity_id=emp_id,
        before=before,
        after={**assignment, 'assignment_id': assignment_id},
    )
    return jsonify({
        'message': f'Leave policy assigned to {emp_id}',
        'emp_id': emp_id,
        'effective': effective,
        'balances': balances,
    }), 200

# ══════════════════════════════════════════════════════════════════════
#  AUDIT LOG
# ══════════════════════════════════════════════════════════════════════

@app.route('/admin/audit')
@hr_or_admin_required
def audit_page():
    return render_template('admin_audit.html')


@app.route('/api/v1/audit-log')
@app.route('/api/audit-log')
@admin_required
def get_audit_log():
    """View audit log"""
    limit = request.args.get('limit', 200, type=int)
    offset = request.args.get('offset', 0, type=int)
    conn = get_db()
    rows = conn.execute(
        "SELECT log_id, emp_id, actor, action, entity, entity_id, details, \"before\", \"after\", ip_address, request_id, created_at FROM audit_log ORDER BY created_at DESC LIMIT ? OFFSET ?",
        [limit, offset]
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    conn.close()
    return jsonify({
        'total': total,
        'data': [{
            'log_id': r[0], 'emp_id': r[1], 'actor': r[2], 'action': r[3],
            'entity': r[4], 'entity_id': r[5], 'details': r[6],
            'before': r[7], 'after': r[8], 'ip_address': r[9],
            'request_id': r[10],
            'created_at': r[11].isoformat() if r[11] else None
        } for r in rows]
    }), 200


# ══════════════════════════════════════════════════════════════════════
#  REPORT EXPORT (CSV / Excel)
# ══════════════════════════════════════════════════════════════════════

@app.route('/api/v1/reports/export')
@app.route('/api/reports/export')
@admin_required
def export_report():
    """Export report as CSV or Excel
    ---
    get:
      tags: [Reports]
      parameters:
        - in: query
          name: start_date
          type: string
        - in: query
          name: end_date
          type: string
        - in: query
          name: format
          type: string
          enum: [csv, xlsx]
      responses:
        200:
          description: File download
    """
    start_date = parse_date(request.args.get('start_date'), datetime.now().date())
    end_date = parse_date(request.args.get('end_date'), start_date)
    fmt = request.args.get('format', 'xlsx')

    conn = get_db()
    rows = conn.execute("""
        SELECT u.emp_id, u.name, u.department,
               COALESCE((SELECT SUM(total_hours) FROM user_sessions us WHERE us.emp_id = u.emp_id AND us.session_date BETWEEN ? AND ?), 0) AS total_hours,
               COALESCE((SELECT SUM(duration_minutes) FROM breaks b WHERE b.emp_id = u.emp_id AND b.break_date BETWEEN ? AND ? AND b.status = 'Completed'), 0) AS break_minutes,
               COALESCE((SELECT COUNT(*) FROM breaks b WHERE b.emp_id = u.emp_id AND b.break_date BETWEEN ? AND ? AND b.status = 'Completed'), 0) AS break_count,
               (SELECT MIN(login_time) FROM user_sessions us WHERE us.emp_id = u.emp_id AND us.session_date BETWEEN ? AND ?) AS first_login,
               (SELECT MAX(logout_time) FROM user_sessions us WHERE us.emp_id = u.emp_id AND us.session_date BETWEEN ? AND ?) AS last_logout
        FROM users u WHERE u.role = 'Employee' ORDER BY u.name
    """, [start_date, end_date, start_date, end_date, start_date, end_date,
          start_date, end_date, start_date, end_date]).fetchall()
    conn.close()

    def mins_to_hms(minutes):
        total = int(round(float(minutes) * 60))
        h = total // 3600
        m = (total % 3600) // 60
        s = total % 60
        return f'{h:02d}:{m:02d}:{s:02d}'

    def hours_to_hms(hours):
        total = int(round(float(hours) * 3600))
        h = total // 3600
        m = (total % 3600) // 60
        s = total % 60
        return f'{h:02d}:{m:02d}:{s:02d}'

    def fmt_time(val):
        if val is None:
            return '--:--:--'
        try:
            return val.strftime('%H:%M:%S')
        except Exception:
            return '--:--:--'

    data = []
    for r in rows:
        sh = float(r[3] or 0)
        bm = float(r[4] or 0)
        ph = max(0, sh - bm / 60)
        eff = round((ph / sh) * 100, 1) if sh > 0 else 0
        data.append({
            'Employee ID': r[0], 'Name': r[1], 'Department': r[2] or 'N/A',
            'First Login': fmt_time(r[6]),
            'Last Logout': fmt_time(r[7]),
            'Total Hours': hours_to_hms(sh),
            'Break Duration': mins_to_hms(bm),
            'Break Count': int(r[5]),
            'Productive Hours': hours_to_hms(ph),
            'Efficiency %': eff,
        })

    df = pd.DataFrame(data) if data else pd.DataFrame(columns=[
        'Employee ID', 'Name', 'Department', 'First Login', 'Last Logout',
        'Total Hours', 'Break Duration', 'Break Count', 'Productive Hours', 'Efficiency %'])
    df['Period'] = f'{start_date} to {end_date}'

    if fmt == 'xlsx':
        buf = BytesIO()
        with pd.ExcelWriter(buf, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Report')
        buf.seek(0)
        return send_file(
            buf, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f'hrms_report_{start_date}_{end_date}.xlsx'
        )

    csv_buf = BytesIO()
    df.to_csv(csv_buf, index=False)
    csv_buf.seek(0)
    return send_file(
        csv_buf, mimetype='text/csv',
        as_attachment=True,
        download_name=f'hrms_report_{start_date}_{end_date}.csv'
    )


@app.route('/api/v1/reports/pdf')
@app.route('/api/reports/pdf')
@admin_required
def export_report_pdf():
    """Export report as PDF
    ---
    get:
      tags: [Reports]
      parameters:
        - in: query
          name: start_date
          type: string
        - in: query
          name: end_date
          type: string
      responses:
        200:
          description: PDF file download
    """
    start_date = parse_date(request.args.get('start_date'), datetime.now().date())
    end_date = parse_date(request.args.get('end_date'), start_date)
    if end_date and end_date < start_date:
        start_date, end_date = end_date, start_date

    conn = get_db()
    rows = conn.execute("""
        SELECT u.emp_id, u.name, u.department,
               COALESCE((SELECT SUM(total_hours) FROM user_sessions us WHERE us.emp_id = u.emp_id AND us.session_date BETWEEN ? AND ?), 0) AS total_hours,
               COALESCE((SELECT SUM(duration_minutes) FROM breaks b WHERE b.emp_id = u.emp_id AND b.break_date BETWEEN ? AND ? AND b.status = 'Completed'), 0) AS break_minutes,
               COALESCE((SELECT COUNT(*) FROM user_sessions us WHERE us.emp_id = u.emp_id AND us.session_date BETWEEN ? AND ?), 0) AS session_count
        FROM users u WHERE u.role = 'Employee' ORDER BY u.name
    """, [start_date, end_date, start_date, end_date, start_date, end_date]).fetchall()
    conn.close()

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=30, bottomMargin=30)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('ReportTitle', parent=styles['Title'], fontSize=18, spaceAfter=6, textColor=colors.HexColor('#0F172A'))
    subtitle_style = ParagraphStyle('Subtitle', parent=styles['Normal'], fontSize=11, spaceAfter=20, textColor=colors.HexColor('#64748B'), alignment=1)
    elements = []

    elements.append(Paragraph('HRMS Employee Efficiency Report', title_style))
    elements.append(Paragraph(f'Period: {start_date} to {end_date}', subtitle_style))
    elements.append(Spacer(1, 12))

    header = ['Employee ID', 'Name', 'Department', 'Hours', 'Break Min', 'Sessions', 'Efficiency']
    table_data = [header]
    for r in rows:
        sh = float(r[3] or 0)
        bm = int(r[4] or 0)
        ph = max(0, sh - bm / 60)
        eff = f'{round((ph / sh) * 100, 1) if sh > 0 else 0}%'
        table_data.append([str(r[0]), str(r[1]), str(r[2] or 'N/A'), f'{sh:.2f}', str(int(bm)), str(int(r[5])), eff])

    table = Table(table_data, colWidths=[60, 90, 80, 50, 55, 55, 60])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0F172A')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 8),
        ('FONTSIZE', (0, 1), (-1, -1), 8),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F8FAFC')]),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))
    elements.append(table)

    doc.build(elements)
    buf.seek(0)
    return send_file(
        buf, mimetype='application/pdf',
        as_attachment=True,
        download_name=f'hrms_report_{start_date}_{end_date}.pdf'
    )


# ══════════════════════════════════════════════════════════════════════
#  USER BREAK ROUTES (existing, kept for backward compat)
# ══════════════════════════════════════════════════════════════════════

@app.route('/api/start-break', methods=['POST'])
@login_required
@idempotent
def start_break():
    data = request.get_json(silent=True) or {}
    break_type = data.get('break_type')
    emp_id = session['emp_id']
    if not break_type:
        return jsonify({'error': 'Break type required'}), 400
    conn = get_db()
    user = conn.execute("SELECT allow_breaks FROM users WHERE emp_id = ?", [emp_id]).fetchone()
    if user and not user[0]:
        conn.close()
        return jsonify({'error': 'Breaks not allowed'}), 403
    shift_start_dt = _get_shift_start_dt(emp_id, conn)
    bt = conn.execute("SELECT daily_limit_minutes FROM break_types WHERE break_type = ?", [break_type]).fetchone()
    if not bt:
        conn.close()
        return jsonify({'error': 'Invalid break type'}), 400
    limit = bt[0]
    if limit:
        today_total = conn.execute(
            "SELECT COALESCE(SUM(duration_minutes), 0) FROM breaks WHERE emp_id = ? AND break_type = ? AND start_time >= ? AND status = 'Completed'",
            [emp_id, break_type, shift_start_dt]
        ).fetchone()[0]
        if today_total >= limit:
            conn.close()
            return jsonify({'error': f'Daily limit of {limit} min reached for {break_type}'}), 400
    if break_type == 'Lunch':
        pending = conn.execute(
            "SELECT 1 FROM break_approvals WHERE emp_id = ? AND break_type = ? AND created_at >= ? AND status = 'Pending'",
            [emp_id, break_type, shift_start_dt]
        ).fetchone()
        if not pending:
            conn.close()
            return jsonify({'error': 'Lunch break requires manager approval'}), 403
    active = conn.execute(
        "SELECT break_id FROM breaks WHERE emp_id = ? AND status = 'Active'", [emp_id]
    ).fetchone()
    if active:
        conn.execute(
            "UPDATE breaks SET end_time = ?, status = 'Completed' WHERE break_id = ?",
            [datetime.now(), active[0]]
        )
        conn.commit()
    break_id = _next_generated_id(conn, 'breaks', 'break_id')
    now = datetime.now()
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    shift_date = _get_shift_date_for_dt(emp_id, now, conn)
    conn.execute(
        "INSERT INTO breaks (break_id, emp_id, break_type, start_time, break_date, status) "
        "VALUES (?, ?, ?, ?, ?, 'Active')",
        [break_id, emp_id, break_type, utc_now, shift_date],
    )
    conn.close()
    # A break start is the origin of an attendance record that feeds payroll, and the
    # auto-end above silently closes a previous one, so both halves are recorded.
    audit_log(
        emp_id, 'BREAK_START',
        f'Started {break_type} break {break_id}' + (
            f' (auto-ended previous break {active[0]})' if active else ''
        ),
        entity='breaks', entity_id=break_id,
        before={'auto_ended_break_id': active[0]} if active else None,
        after={'break_type': break_type, 'status': 'Active', 'break_date': str(shift_date)},
    )
    return jsonify({'message': 'Break started', 'break_id': break_id, 'break_type': break_type}), 201


@app.route('/api/end-break/<int:break_id>', methods=['POST'])
@login_required
def end_break(break_id):
    emp_id = session['emp_id']
    conn = get_db()
    info = conn.execute(
        "SELECT start_time, break_type FROM breaks WHERE break_id = ? AND emp_id = ?",
        [break_id, emp_id]
    ).fetchone()
    if not info:
        conn.close()
        return jsonify({'error': 'Break not found'}), 404
    end_time = datetime.now(timezone.utc).replace(tzinfo=None)
    duration = int((end_time - info[0]).total_seconds() / 60)
    conn.execute(
        "UPDATE breaks SET end_time = ?, duration_minutes = ?, status = 'Completed' "
        "WHERE break_id = ?",
        [end_time, duration, break_id]
    )
    conn.close()
    # FR-ATT-03 says "audited", and the matrix claimed it. It did not: a break is an
    # attendance record that feeds the payroll LOP calculation, so closing one is
    # exactly the kind of write that has to leave a trace of who closed it and when.
    audit_log(
        emp_id, 'BREAK_END',
        f'Ended {info[1]} break {break_id} after {duration} minutes',
        entity='breaks', entity_id=break_id,
        before={'status': 'Active', 'start_time': info[0].isoformat()},
        after={'status': 'Completed', 'duration_minutes': duration},
    )
    return jsonify({'message': 'Break ended', 'duration_minutes': duration}), 200


@app.route('/api/user-breaks')
@login_required

def get_user_breaks():
    emp_id = session['emp_id']
    conn = get_db()
    shift_start_dt = _get_shift_start_dt(emp_id, conn)
    breaks = conn.execute(
        "SELECT break_id, break_type, start_time, end_time, duration_minutes, status FROM breaks WHERE emp_id = ? AND (status = 'Active' OR start_time >= ?) ORDER BY start_time DESC",
        [emp_id, shift_start_dt]
    ).fetchall()
    conn.close()
    return jsonify([{
        'break_id': b[0], 'break_type': b[1],
        'start_time': b[2].isoformat() + 'Z' if b[2] else None,
        'end_time': b[3].isoformat() + 'Z' if b[3] else None,
        'duration_minutes': b[4] or 0, 'status': b[5]
    } for b in breaks]), 200


@app.route('/api/break-approvals', methods=['GET', 'POST'])
@login_required
@idempotent
def break_approvals_api():
    emp_id = session['emp_id']
    if request.method == 'GET':
        conn = get_db()
        if policy.can_view_all(policy.current_actor(conn), 'breaks', conn=conn):
            rows = conn.execute(
                "SELECT a.approval_id, a.emp_id, u.name, a.break_type, a.break_date, a.reason, a.status, a.approved_by, a.created_at FROM break_approvals a JOIN users u ON a.emp_id = u.emp_id ORDER BY a.created_at DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT a.approval_id, a.emp_id, u.name, a.break_type, a.break_date, a.reason, a.status, a.approved_by, a.created_at FROM break_approvals a JOIN users u ON a.emp_id = u.emp_id WHERE a.emp_id = ? ORDER BY a.created_at DESC",
                [emp_id]
            ).fetchall()
        conn.close()
        return jsonify([{
            'approval_id': r[0], 'emp_id': r[1], 'emp_name': r[2],
            'break_type': r[3], 'break_date': r[4].isoformat(),
            'reason': r[5], 'status': r[6], 'approved_by': r[7],
            'created_at': r[8].isoformat() if r[8] else None
        } for r in rows]), 200

    data = request.get_json(silent=True) or {}
    bt = data.get('break_type')
    if bt != 'Lunch':
        return jsonify({'error': 'Only Lunch breaks require approval'}), 400
    conn = get_db()
    try:
        # The pre-check keeps the friendly 409 for the sequential case; the
        # partial unique index (FR-ATT-05) is what makes "one Pending per
        # employee per shift date" true under concurrency, so a race surfaces as
        # a UniqueViolation on the insert and is translated to the same 409
        # rather than a 500.
        if conn.execute(
            "SELECT 1 FROM break_approvals WHERE emp_id = ? AND break_type = ? AND break_date = ? AND status = 'Pending'",
            [emp_id, bt, _get_shift_date_for_dt(emp_id, datetime.now(), conn)]
        ).fetchone():
            return jsonify({'error': 'Pending approval already exists for today'}), 409
        aid = _next_generated_id(conn, 'break_approvals', 'approval_id')
        shift_date = _get_shift_date_for_dt(emp_id, datetime.now(), conn)
        try:
            conn.execute(
                "INSERT INTO break_approvals (approval_id, emp_id, break_type, break_date, reason, status) VALUES (?, ?, ?, ?, ?, 'Pending')",
                [aid, emp_id, bt, shift_date, data.get('reason', '')]
            )
        except Exception as exc:
            if 'uq_pending_lunch_approval' in str(exc) or 'unique' in str(exc).lower():
                return jsonify({'error': 'Pending approval already exists for today'}), 409
            raise
    finally:
        conn.close()
    audit_log(
        emp_id, 'BREAK_APPROVAL_REQUEST',
        f'Requested approval for a {bt} break on {shift_date}',
        entity='break_approvals', entity_id=aid,
        after={'emp_id': emp_id, 'break_type': bt, 'break_date': str(shift_date),
               'status': 'Pending'},
    )
    return jsonify({'message': 'Lunch break approval requested', 'approval_id': aid}), 201


def _review_break_approval(aid, decision):
    """Approve or reject a Lunch break request (FR-ATT-06).

    The SRS is specific — "allowed if actor is the employee's **manager**
    (including an active delegate, FR-LEA-08a) or has role HR/Admin; conditional
    update (CC-04); **audited**; notifies employee" — and this was the third
    instance of the gate bug this codebase has now fixed twice before, in
    FR-EXP-03 (`Approved -> Paid` was unreachable for Finance) and FR-PERF-01 (goal
    rating was unreachable for the reporting manager). `@admin_required` meant a
    Team Leader who actually manages people could not approve their own report's
    break, so the requirement was unreachable for the role it names while the
    matrix recorded it as implemented.

    The other three clauses were missing too, and the matrix claimed all of them:
    the write was **unconditional** (so two approvers both won), nothing was
    audited, and the employee was never notified. All three now run, and the
    per-employee half is `_approval_denial` — so an active delegate of this
    employee's manager is admitted here exactly as they are on leave and
    regularization, which is what FR-ATT-06's "(including an active delegate,
    FR-LEA-08a)" asks for.
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT emp_id, break_type, break_date, status FROM break_approvals "
            "WHERE approval_id = ?", [aid],
        ).fetchone()
        if not row:
            return jsonify({'error': 'Approval request not found'}), 404
        if row[3] != 'Pending':
            # The same always-200 lie the regularization routes had: a request that
            # did not exist, or one already decided, used to answer 200 with a
            # success message. A caller could not tell a real decision from a no-op.
            return jsonify({
                'error': f'Request is already {row[3].lower()}',
                'status': row[3],
            }), 409
        denial = _approval_denial(conn, session['emp_id'], row[0])
        if denial is not None:
            return denial
        note = delegations.approval_note(conn, session['emp_id'], row[0])
        # Conditional on `status = 'Pending'` (CC-04). Two approvers racing now give
        # one winner and one 409 instead of two successes.
        claimed = conn.execute(
            "UPDATE break_approvals SET status = ?, approved_by = ? "
            "WHERE approval_id = ? AND status = 'Pending'",
            [decision, session['emp_id'], aid],
        )
        if not getattr(claimed, 'rowcount', 1):
            return jsonify({'error': 'Request was reviewed by someone else'}), 409
    finally:
        conn.close()
    audit_log(
        session['emp_id'], f'BREAK_APPROVAL_{decision.upper()}',
        f'{decision} {row[1]} break request {aid} for {row[0]} ({row[2]})'
        + (f' ({note})' if note else ''),
        entity='break_approvals', entity_id=aid,
        before={'status': 'Pending'}, after={'status': decision},
    )
    # The SRS asks for it and it is the point of the queue: an employee who has been
    # sitting on a Lunch request needs to be told the outcome without polling.
    add_notification(
        row[0], f'BREAK_APPROVAL_{decision.upper()}',
        f'Your {row[1]} break request for {row[2]} was {decision.lower()} by '
        f'{session["emp_id"]}.',
    )
    return jsonify({'message': f'Break {decision.lower()}', 'status': decision}), 200


@app.route('/api/break-approvals/<int:aid>/approve', methods=['POST'])
@reporting_line_required
def approve_break(aid):
    return _review_break_approval(aid, 'Approved')


@app.route('/api/break-approvals/<int:aid>/reject', methods=['POST'])
@reporting_line_required
def reject_break(aid):
    return _review_break_approval(aid, 'Rejected')


@app.route('/api/break-types')
@login_required
def get_break_types():
    conn = get_db()
    types = conn.execute("SELECT break_type, daily_limit_minutes, description FROM break_types").fetchall()
    conn.close()
    return jsonify([{
        'break_type': t[0], 'daily_limit_minutes': t[1], 'description': t[2]
    } for t in types]), 200


@app.route('/api/login-hours')
@login_required
def get_login_hours():
    emp_id = session['emp_id']
    conn = get_db()
    date_str = request.args.get('date', '')
    if date_str:
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            target_date = datetime.now().date()
    else:
        target_date = datetime.now().date()
    sessions = conn.execute(
        "SELECT login_time, logout_time, total_hours, session_date FROM user_sessions WHERE emp_id = ? AND session_date = ? ORDER BY login_time ASC",
        [emp_id, target_date]
    ).fetchall()
    conn.close()
    return jsonify([{
        'login_time': s[0].strftime('%H:%M:%S') if s[0] else 'N/A',
        'logout_time': s[1].strftime('%H:%M:%S') if s[1] else 'Active',
        'total_hours': float(s[2]) if s[2] else 0,
        'session_date': s[3].isoformat() if s[3] else None
    } for s in sessions]), 200


@app.route('/api/user/shift-summary')
@login_required
def get_shift_summary():
    emp_id = session['emp_id']
    conn = get_db()
    date_str = request.args.get('date', '')
    if date_str:
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            target_date = datetime.now().date()
    else:
        target_date = datetime.now().date()

    shift_start_dt = _get_shift_start_dt(emp_id, conn, target_date)
    shift_end_dt = _get_shift_end_dt(emp_id, shift_start_dt, conn)

    # A shiftless employee resolves to midnight-to-midnight — a **24 hour** scheduled
    # span — so the +25% cap would be 30 hours and a forgotten logout would show a
    # 30-hour day. That is the skew this requirement exists to prevent, arriving by a
    # different route. The shared rule's documented 8-hour default applies instead, and
    # `shift_configured` says so in the response rather than leaving a 30-hour figure to
    # be explained.
    #
    # The helpers themselves are left alone: the attendance finalisation path uses the
    # same values as its Present/Half-day thresholds and changing what "no shift"
    # means there is FR-JOB-01's decision, not this one.
    configured_start, _configured_end = get_shift(emp_id, conn)
    shift_configured = bool(configured_start and configured_start != '24x7')
    cap_start = shift_start_dt if shift_configured else None
    cap_end = shift_end_dt if shift_configured else None

    sessions = conn.execute(
        "SELECT login_time, logout_time, total_hours, session_date FROM user_sessions WHERE emp_id = ? AND session_date = ? ORDER BY login_time ASC",
        [emp_id, target_date]
    ).fetchall()

    breaks = conn.execute(
        "SELECT break_type, start_time, end_time, duration_minutes, status FROM breaks WHERE emp_id = ? AND break_date = ? ORDER BY start_time ASC",
        [emp_id, target_date]
    ).fetchall()

    conn.close()

    total_session_hours = sum(float(s[2]) for s in sessions if s[2])
    total_break_minutes = sum(int(b[3]) for b in breaks if b[3])
    session_count = len(sessions)

    first_login = sessions[0][0] if sessions else None
    last_logout = None
    for s in reversed(sessions):
        if s[1]:
            last_logout = s[1]
            break

    # FR-ATT-09, through the shared rule: elapsed window rather than a sum of
    # sessions, with an open shift capped at the scheduled length +25% and flagged.
    # This was `(now - first_login)` **uncapped**, while payroll finalisation capped
    # the same figure — so a forgotten logout showed 30 hours here and the capped
    # number on the payslip. Same class as FR-LEA-09's six rules for days.
    hours, estimated, capped = shift_hours.shift_hours(
        first_login, last_logout, cap_start, cap_end)
    scheduled_span = shift_hours.scheduled_hours(cap_start, cap_end)

    productive_hours = max(0, hours - total_break_minutes / 60)
    efficiency = round((productive_hours / hours) * 100, 1) if hours > 0 else 0

    return jsonify({
        'date': target_date.isoformat(),
        'shift_start': shift_start_dt.strftime('%H:%M'),
        'shift_end': shift_end_dt.strftime('%H:%M'),
        'first_login': first_login.strftime('%H:%M:%S') if first_login else None,
        'last_logout': last_logout.strftime('%H:%M:%S') if last_logout else None,
        'shift_hours': hours,
        # FR-ATT-09's three additions. `scheduled_hours` so a capped figure can be
        # read against what the shift was meant to be, `estimated` because an open
        # shift's end has not been observed yet, and `capped` because that is the
        # condition indicating a logout was probably forgotten — a consumer can tell
        # "still running" from "clamped down" without inferring one from the other.
        'scheduled_hours': round(scheduled_span, 2),
        'estimated': estimated,
        'capped': capped,
        # False means `scheduled_hours` above is the module's documented default rather
        # than this employee's configured shift, which is the difference between a real
        # figure and a fallback one.
        'shift_configured': shift_configured,
        'total_session_hours': total_session_hours,
        'total_break_minutes': total_break_minutes,
        'productive_hours': round(productive_hours, 2),
        'efficiency': efficiency,
        'session_count': session_count,
        'break_count': len(breaks)
    }), 200


@app.route('/api/user/calendar')
@login_required
def get_user_calendar():
    emp_id = session['emp_id']
    month = int(request.args.get('month', datetime.now().month))
    year = int(request.args.get('year', datetime.now().year))

    start_date = datetime(year, month, 1).date()
    if month == 12:
        end_date = datetime(year + 1, 1, 1).date() - timedelta(days=1)
    else:
        end_date = datetime(year, month + 1, 1).date() - timedelta(days=1)

    conn = get_db()

    sessions = conn.execute(
        "SELECT session_date, login_time, logout_time, total_hours FROM user_sessions WHERE emp_id = ? AND session_date BETWEEN ? AND ? ORDER BY session_date, login_time",
        [emp_id, start_date, end_date]
    ).fetchall()

    breaks = conn.execute(
        "SELECT break_date, break_type, start_time, end_time, duration_minutes, status FROM breaks WHERE emp_id = ? AND break_date BETWEEN ? AND ? ORDER BY break_date, start_time",
        [emp_id, start_date, end_date]
    ).fetchall()

    leaves = conn.execute(
        "SELECT start_date, end_date, leave_type, status FROM leave_requests WHERE emp_id = ? AND start_date <= ? AND end_date >= ? ORDER BY start_date",
        [emp_id, end_date, start_date]
    ).fetchall()

    holidays = conn.execute(
        "SELECT holiday_date, name FROM holidays WHERE holiday_date BETWEEN ? AND ? ORDER BY holiday_date",
        [start_date, end_date]
    ).fetchall()

    attendance_rows = conn.execute(
        "SELECT attendance_date, status, shift_hours, source FROM attendance_days "
        "WHERE emp_id = ? AND attendance_date BETWEEN ? AND ? ORDER BY attendance_date",
        [emp_id, start_date, end_date]
    ).fetchall()

    shift_start, shift_end = get_shift(emp_id, conn)

    conn.close()

    sess_map = {}
    for s in sessions:
        d = s[0].isoformat() if s[0] else None
        if not d:
            continue
        if d not in sess_map:
            sess_map[d] = []
        sess_map[d].append({
            'login': s[1].strftime('%H:%M') if s[1] else None,
            'logout': s[2].strftime('%H:%M') if s[2] else 'Active',
            'hours': float(s[3]) if s[3] else 0
        })

    brk_map = {}
    for b in breaks:
        d = b[0].isoformat() if b[0] else None
        if not d:
            continue
        if d not in brk_map:
            brk_map[d] = []
        brk_map[d].append({
            'type': b[1],
            'start': b[2].strftime('%H:%M') if b[2] else None,
            'end': b[3].strftime('%H:%M') if b[3] else None,
            'minutes': int(b[4]) if b[4] else 0,
            'status': b[5]
        })

    leave_map = {}
    for leave in leaves:
        ld_start = leave[0]
        ld_end = leave[1]
        leave_type = leave[2]
        leave_status = leave[3]
        current = ld_start
        while current <= ld_end:
            d = current.isoformat()
            leave_map[d] = {'type': leave_type, 'status': leave_status}
            current += timedelta(days=1)

    holiday_map = {}
    for h in holidays:
        d = h[0].isoformat() if h[0] else None
        if d:
            holiday_map[d] = h[1]

    attendance_map = {}
    for row in attendance_rows:
        d = row[0].isoformat() if row[0] else None
        if d:
            attendance_map[d] = {
                'status': row[1],
                'shift_hours': float(row[2]) if row[2] is not None else 0.0,
                'source': row[3] or 'job',
            }

    shift_start = shift_start or None
    shift_end = shift_end or None

    return jsonify({
        'sessions': sess_map,
        'breaks': brk_map,
        'leaves': leave_map,
        'holidays': holiday_map,
        'attendance_days': attendance_map,
        'shift_start': shift_start,
        'shift_end': shift_end,
        'month': month,
        'year': year
    }), 200


# ══════════════════════════════════════════════════════════════════════
#  ADMIN MONITORING ROUTES
# ══════════════════════════════════════════════════════════════════════

@app.route('/api/live-monitoring')
@admin_required
def live_monitoring():
    conn = get_db()
    now = datetime.now()
    all_employees = conn.execute("SELECT emp_id FROM users WHERE role = 'Employee'").fetchall()
    shift_starts = []
    for (eid,) in all_employees:
        shift_starts.append(_get_shift_start_dt(eid, conn))
    earliest = min(shift_starts) if shift_starts else now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = conn.execute("""
        SELECT u.emp_id, u.name, u.department, b.break_type, b.start_time, b.status
        FROM breaks b JOIN users u ON b.emp_id = u.emp_id
        WHERE b.status = 'Active' AND b.start_time >= ?
        ORDER BY b.start_time DESC
    """, [earliest]).fetchall()
    conn.close()
    return jsonify([{
        'emp_id': r[0], 'employee_name': r[1], 'department': r[2],
        'break_type': r[3], 'start_time': r[4].strftime('%H:%M:%S'), 'status': r[5]
    } for r in rows]), 200


@app.route('/api/break-summary')
@admin_required
def get_break_summary():
    conn = get_db()
    now = datetime.now()
    all_employees = conn.execute("SELECT emp_id FROM users WHERE role = 'Employee'").fetchall()
    shift_starts = []
    for (eid,) in all_employees:
        shift_starts.append(_get_shift_start_dt(eid, conn))
    earliest = min(shift_starts) if shift_starts else now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = conn.execute("""
        SELECT u.emp_id, u.name, u.department,
               COUNT(CASE WHEN b.status = 'Active' THEN 1 END),
               COUNT(CASE WHEN b.status = 'Completed' THEN 1 END),
               SUM(CASE WHEN b.status = 'Completed' THEN b.duration_minutes ELSE 0 END)
        FROM users u LEFT JOIN breaks b ON u.emp_id = b.emp_id AND b.start_time >= ?
        WHERE u.role = 'Employee' GROUP BY u.emp_id, u.name, u.department ORDER BY u.name
    """, [earliest]).fetchall()
    conn.close()
    return jsonify([{
        'emp_id': r[0], 'employee_name': r[1], 'department': r[2],
        'active_breaks': int(r[3] or 0), 'completed_breaks': int(r[4] or 0),
        'total_break_minutes': int(r[5] or 0)
    } for r in rows]), 200


@app.route('/api/disposed-breaks')
@admin_required
def get_disposed_breaks():
    one_hour_ago = datetime.now() - timedelta(hours=1)
    conn = get_db()
    all_employees = conn.execute("SELECT emp_id FROM users WHERE role = 'Employee'").fetchall()
    shift_starts = []
    for (eid,) in all_employees:
        shift_starts.append(_get_shift_start_dt(eid, conn))
    earliest_shift = min(shift_starts) if shift_starts else datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    current_shift_date = earliest_shift.date()
    rows = conn.execute("""
        SELECT u.emp_id, u.name, u.department, b.break_type, b.start_time, b.end_time, b.duration_minutes, b.status
        FROM breaks b JOIN users u ON b.emp_id = u.emp_id
        WHERE b.status = 'Completed' AND b.end_time >= ? AND b.break_date = ?
        ORDER BY b.end_time DESC
    """, [one_hour_ago, current_shift_date]).fetchall()
    conn.close()
    return jsonify([{
        'emp_id': r[0], 'employee_name': r[1], 'department': r[2],
        'break_type': r[3], 'start_time': r[4].strftime('%H:%M:%S'),
        'end_time': r[5].strftime('%H:%M:%S'), 'duration_minutes': r[6] or 0, 'status': r[7]
    } for r in rows]), 200


@app.route('/api/dashboard-stats')
@admin_required

def get_dashboard_stats():
    conn = get_db()
    now = datetime.now()
    total = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'Employee'").fetchone()[0]
    all_employees = conn.execute("SELECT emp_id FROM users WHERE role = 'Employee'").fetchall()
    shift_starts = []
    for (eid,) in all_employees:
        shift_starts.append(_get_shift_start_dt(eid, conn))
    shift_date = min(shift_starts).date() if shift_starts else now.date()
    logged_in_today = conn.execute(
        "SELECT COUNT(DISTINCT emp_id) FROM user_sessions WHERE session_date = ?",
        [shift_date]
    ).fetchone()[0]
    on_break = conn.execute(
        "SELECT COUNT(DISTINCT emp_id) FROM breaks WHERE status = 'Active' AND break_date = ?",
        [shift_date]
    ).fetchone()[0]
    blocked = conn.execute("SELECT COUNT(*) FROM users WHERE status = 'Blocked'").fetchone()[0]
    pending_leaves = conn.execute("SELECT COUNT(*) FROM leave_requests WHERE status = 'Pending'").fetchone()[0]
    conn.close()
    return jsonify({
        'total_employees': total, 'logged_in_today': logged_in_today,
        'on_break': on_break, 'blocked_users': blocked,
        'pending_leaves': pending_leaves
    }), 200


@app.route('/api/admin/breaks')
@admin_required

def admin_breaks():
    conn = get_db()
    all_employees = conn.execute("SELECT emp_id FROM users WHERE role = 'Employee'").fetchall()
    shift_starts = []
    for (eid,) in all_employees:
        shift_starts.append(_get_shift_start_dt(eid, conn))
    shift_date = min(shift_starts).date() if shift_starts else datetime.now().date()
    active = conn.execute("""
        SELECT b.break_id, u.name, b.break_type, b.start_time
        FROM breaks b JOIN users u ON b.emp_id = u.emp_id
        WHERE b.status = 'Active' AND b.break_date = ?
        ORDER BY b.start_time DESC
    """, [shift_date]).fetchall()
    disposed = conn.execute("""
        SELECT u.name, b.break_type, b.duration_minutes, b.end_time
        FROM breaks b JOIN users u ON b.emp_id = u.emp_id
        WHERE b.status = 'Completed' AND b.break_date = ?
        ORDER BY b.end_time DESC LIMIT 20
    """, [shift_date]).fetchall()
    summary = conn.execute("""
        SELECT break_type, COUNT(*), AVG(duration_minutes), MAX(duration_minutes),
               COUNT(DISTINCT emp_id)
        FROM breaks WHERE break_date = ? AND status = 'Completed'
        GROUP BY break_type
    """, [shift_date]).fetchall()
    conn.close()
    return jsonify({
        'active_breaks': [{
            'break_id': r[0], 'emp_name': r[1], 'break_type': r[2],
            'duration': int((datetime.now(timezone.utc).replace(tzinfo=None) - r[3]).total_seconds() / 60) if r[3] else 0
        } for r in active],
        'disposed_breaks': [{
            'emp_name': r[0], 'break_type': r[1],
            'duration': r[2] or 0,
            'end_time': r[3].strftime('%H:%M') if r[3] else ''
        } for r in disposed],
        'break_summary': [{
            'break_type': r[0], 'count': r[1],
            'avg_duration': float(r[2] or 0),
            'max_duration': float(r[3] or 0),
            'employees': r[4]
        } for r in summary]
    }), 200

@app.route('/api/admin/dispose-break/<int:break_id>', methods=['POST'])
@admin_required
def admin_dispose_break(break_id):
    """FR-ATT-16: "admin dispose ends any Active break with an **audited reason**".

    The reason was the missing half and it is not cosmetic. This is an administrator
    ending *someone else's* break, which shortens that employee's recorded attendance
    and therefore their pay; "an admin ended my break for no stated reason" is
    indistinguishable from a data-entry mistake once the row is written. A reason is
    now required, recorded in the audit row and returned to the caller.

    Nothing was audited here at all before, which is why the matrix could claim an
    audited reason for a route that had neither.
    """
    reason = str((request.get_json(silent=True) or {}).get('reason', '')).strip()
    if not reason:
        # Refused rather than defaulted. A blank reason is not a reason, and
        # "reason required" is more useful to the admin than a silent 200 that
        # discards their justification.
        return jsonify({
            'error': 'A reason is required to dispose of another employee\'s break',
            'policy': 'FR-ATT-16',
        }), 400
    conn = get_db()
    try:
        info = conn.execute(
            "SELECT start_time, emp_id, break_type FROM breaks "
            "WHERE break_id = ? AND status = 'Active'",
            [break_id],
        ).fetchone()
        if not info:
            return jsonify({'error': 'Break not found or already ended'}), 404
        end_time = datetime.now(timezone.utc).replace(tzinfo=None)
        duration = int((end_time - info[0]).total_seconds() / 60)
        conn.execute(
            "UPDATE breaks SET end_time = ?, duration_minutes = ?, status = 'Completed' "
            "WHERE break_id = ?",
            [end_time, duration, break_id],
        )
    finally:
        conn.close()
    audit_log(
        session['emp_id'], 'BREAK_DISPOSE',
        f'Admin disposed {info[2]} break {break_id} for {info[1]} after {duration} '
        f'minutes: {reason}',
        entity='breaks', entity_id=break_id,
        before={'status': 'Active', 'owner_emp_id': info[1]},
        after={'status': 'Completed', 'duration_minutes': duration, 'reason': reason},
    )
    # The employee is the one whose attendance changed, so they hear it from the app
    # and not only from a payroll query they did not know to run.
    add_notification(
        info[1], 'BREAK_DISPOSED',
        f'An administrator ended your {info[2]} break after {duration} minutes. '
        f'Reason given: {reason}',
    )
    return jsonify({
        'message': 'Break ended by admin', 'duration_minutes': duration, 'reason': reason,
    }), 200


@app.route('/api/admin/outbox', methods=['GET'])
@admin_required
def admin_outbox_list():
    """Outbox monitor: recent outbox events with status/attempts."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT event_id, event_type, aggregate, aggregate_id, status, attempts, created_at, delivered_at "
            "FROM outbox_events ORDER BY event_id DESC LIMIT 100"
        ).fetchall()
    except Exception:
        conn.close()
        return jsonify({'data': []}), 200
    conn.close()
    return jsonify({'data': [{
        'event_id': r[0], 'event_type': r[1], 'aggregate': r[2],
        'aggregate_id': r[3], 'status': r[4], 'attempts': r[5],
        'created_at': r[6].isoformat() if r[6] else None,
        'delivered_at': r[7].isoformat() if r[7] else None,
    } for r in rows]}), 200


@app.route('/api/admin/outbox/dispatch', methods=['POST'])
@admin_required
def admin_outbox_dispatch():
    """Manually trigger one outbox dispatch pass (CC-09).

    Audited even though it is an operational action rather than a business write,
    because a dispatch **sends email to employees**. "Who forced the queue out, when,
    and what did it deliver" is exactly what an audit trail is for — an operator
    triggering it at the wrong moment is a real incident, and the events it consumed
    are the evidence.
    """
    try:
        result = outbox.run_dispatch()
    except Exception as e:
        logger.warning('outbox dispatch failed: %s', e)
        return jsonify({'error': 'Outbox dispatch failed'}), 500
    audit_log(
        session['emp_id'], 'OUTBOX_DISPATCH',
        f'Manual outbox dispatch: {result}',
        entity='outbox_events', after=result if isinstance(result, dict) else None,
    )
    return jsonify(result), 200


# ══════════════════════════════════════════════════════════════════════
#  USER MANAGEMENT
# ══════════════════════════════════════════════════════════════════════

_USER_ROLES = frozenset({'Employee', 'Team Leader', 'HR', 'Finance', 'Admin', 'Super Admin'})
_USER_STATUSES = frozenset({'Active', 'Blocked', 'Inactive', 'Onboarding', 'Pre-hire'})
_USER_DEPARTMENTS = frozenset({
    'MIS', 'Operations', 'Support', 'HR', 'Finance',
    'Engineering', 'Marketing', 'Sales', 'Design',
})
_USER_SORT_COLUMNS = {
    'emp_id': 'emp_id',
    'name': 'name',
    'email': 'email',
    'role': 'role',
    'status': 'status',
    'department': 'department',
    'created_at': 'created_at',
}
_USER_CREATE_FIELDS = frozenset({
    'emp_id', 'name', 'email', 'password', 'role', 'department', 'designation',
    'shift_start', 'shift_end', 'weekly_off_pattern', 'allow_login', 'allow_breaks',
})
_USER_UPDATE_FIELDS = frozenset({
    'name', 'email', 'role', 'department', 'designation', 'status',
    'shift_start', 'shift_end', 'weekly_off_pattern', 'allow_login', 'allow_breaks',
})
_EMP_ID_RE = re.compile(r'^EMP\d{3,}$')
_EMAIL_RE = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')


class UserValidationError(ValueError):
    """A user-directory payload violates the public contract."""


def _normalized_emp_id(value):
    emp_id = str(value or '').strip().upper()
    if not _EMP_ID_RE.fullmatch(emp_id):
        raise UserValidationError('emp_id must match EMP followed by at least 3 digits')
    return emp_id


def _normalized_email(value):
    email = str(value or '').strip().lower()
    if not _EMAIL_RE.fullmatch(email):
        raise UserValidationError('email must be a valid email address')
    return email


def _validated_choice(value, allowed, field):
    normalized = str(value or '').strip()
    if normalized not in allowed:
        raise UserValidationError(f'{field} must be one of: {", ".join(sorted(allowed))}')
    return normalized


def _normalized_bool(value, field):
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ('true', 'false', '1', '0', 'yes', 'no'):
        return value.strip().lower() in ('true', '1', 'yes')
    raise UserValidationError(f'{field} must be true or false')


def _validate_user_payload(data, *, creating, existing=None):
    """Validate and normalize an FR-USR create/update payload."""
    allowed = _USER_CREATE_FIELDS if creating else _USER_UPDATE_FIELDS
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise UserValidationError(f'unknown fields: {", ".join(unknown)}')
    if not creating and not data:
        raise UserValidationError('at least one field is required')

    current = existing or {}
    merged = {
        'emp_id': current.get('emp_id'),
        'name': current.get('name'),
        'email': current.get('email'),
        'role': current.get('role', 'Employee'),
        'department': current.get('department', ''),
        'status': current.get('status', 'Active'),
    }
    merged.update({key: value for key, value in data.items() if key in merged})
    if creating:
        merged['emp_id'] = _normalized_emp_id(merged.get('emp_id'))
    if not str(merged.get('name') or '').strip():
        raise UserValidationError('name is required')
    if len(str(merged['name']).strip()) > 120:
        raise UserValidationError('name must be 120 characters or fewer')
    merged['name'] = str(merged['name']).strip()
    merged['email'] = _normalized_email(merged.get('email'))
    merged['role'] = _validated_choice(merged.get('role'), _USER_ROLES, 'role')
    merged['department'] = _validated_choice(merged.get('department'), _USER_DEPARTMENTS, 'department')
    if 'designation' in data:
        designation = str(data.get('designation') or '').strip()
        if len(designation) > 120:
            raise UserValidationError('designation must be 120 characters or fewer')
        merged['designation'] = designation
    elif existing is not None:
        merged['designation'] = existing.get('designation')
    if not creating:
        merged['status'] = _validated_choice(merged.get('status'), _USER_STATUSES, 'status')
    for flag in ('allow_login', 'allow_breaks'):
        if flag in data:
            merged[flag] = _normalized_bool(data[flag], flag)
        elif existing is not None:
            merged[flag] = bool(existing.get(flag))
    return merged


def _paged_user_args():
    try:
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 50))
    except (TypeError, ValueError):
        raise UserValidationError('page and per_page must be integers') from None
    if page < 1:
        raise UserValidationError('page must be at least 1')
    if per_page < 1 or per_page > 200:
        raise UserValidationError('per_page must be between 1 and 200')
    sort_by = request.args.get('sort_by', 'created_at').strip().lower()
    if sort_by not in _USER_SORT_COLUMNS:
        raise UserValidationError(f'sort_by must be one of: {", ".join(sorted(_USER_SORT_COLUMNS))}')
    sort_dir = request.args.get('sort_dir', 'desc').strip().lower()
    if sort_dir not in ('asc', 'desc'):
        raise UserValidationError('sort_dir must be asc or desc')
    return page, per_page, sort_by, sort_dir

@app.route('/admin/users')
@admin_required
def admin_users():
    # The PII action is only rendered for an actor that can actually use it.
    conn = get_db()
    try:
        actor = policy.current_actor(conn)
        can_reveal_pii = policy.can(actor, 'pii_reveal', conn=conn)
        can_anonymise = policy.can(actor, 'policy_admin', conn=conn)
    finally:
        conn.close()
    return render_template(
        'admin_users.html',
        my_emp_id=session['emp_id'],
        can_reveal_pii=can_reveal_pii,
        can_anonymise=can_anonymise,
    )


@app.route('/api/users', methods=['GET'])
@admin_required
def get_users():
    try:
        page, per_page, sort_by, sort_dir = _paged_user_args()
    except UserValidationError as exc:
        return jsonify({'error': str(exc)}), 400
    offset = (page - 1) * per_page
    order_clause = f"ORDER BY {_USER_SORT_COLUMNS[sort_by]} {sort_dir.upper()}, emp_id ASC"
    active_only = request.args.get('active', '').lower() in ('1', 'true', 'yes', 'on')
    search = request.args.get('search', '').strip()
    role_filter = request.args.get('role', '').strip()
    status_filter = request.args.get('status', '').strip()
    dept_filter = request.args.get('department', '').strip()
    conn = get_db()
    conditions = []
    params = []
    if active_only:
        conditions.append("status = 'Active'")
    if search:
        conditions.append("(LOWER(emp_id) LIKE ? OR LOWER(name) LIKE ? OR LOWER(email) LIKE ?)")
        like = f"%{search.lower()}%"
        params.extend([like, like, like])
    if role_filter:
        conditions.append("role = ?")
        params.append(role_filter)
    if status_filter:
        conditions.append("status = ?")
        params.append(status_filter)
    if dept_filter:
        conditions.append("department = ?")
        params.append(dept_filter)
    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
    total = conn.execute(f"SELECT COUNT(*) FROM users{where_clause}", params).fetchone()[0]
    if _shift_model():
        # v2.0 (public): users has no shift columns — resolve per row from
        # the effective-dated shift_assignments (FR-ATT-17).
        rows = conn.execute(
            f"SELECT emp_id, name, email, role, status, department, first_login, allow_login, allow_breaks FROM users{where_clause} {order_clause} LIMIT ? OFFSET ?",
            params + [per_page, offset]
        ).fetchall()
        data = []
        for r in rows:
            sstart, send = get_shift(r[0], conn)
            data.append({
                'emp_id': r[0], 'name': r[1], 'email': r[2], 'role': r[3],
                'status': r[4], 'department': r[5],
                'first_login': r[6].strftime('%I:%M %p') if r[6] else 'N/A',
                'allow_login': 1 if r[7] else 0,
                'allow_breaks': 1 if r[8] else 0,
                'shift_start': sstart or '',
                'shift_end': send or '',
                'weekly_off_pattern': get_weekly_off_pattern(r[0], conn)
            })
    else:
        rows = conn.execute(
            f"SELECT emp_id, name, email, role, status, department, first_login, allow_login, allow_breaks, shift_start, shift_end, weekly_off_pattern FROM users{where_clause} {order_clause} LIMIT ? OFFSET ?",
            params + [per_page, offset]
        ).fetchall()
        data = [{
            'emp_id': r[0], 'name': r[1], 'email': r[2], 'role': r[3],
            'status': r[4], 'department': r[5],
            'first_login': r[6].strftime('%I:%M %p') if r[6] else 'N/A',
            'allow_login': 1 if r[7] else 0,
            'allow_breaks': 1 if r[8] else 0,
            'shift_start': r[9] or '',
            'shift_end': r[10] or '',
            'weekly_off_pattern': r[11] or 'Sat,Sun'
        } for r in rows]

    # FR-AUTH-03: an administrator has to be able to *see* a lockout, or the only
    # way to clear one is to already know it exists — which means waiting for the
    # employee to complain that they cannot sign in. One query for the page.
    # `status` is deliberately left alone: a lockout is not an account state.
    lockouts = lockout.status_for_many(conn, [row['emp_id'] for row in data])
    for row in data:
        row.update(lockouts.get(
            row['emp_id'], {'locked': False, 'attempts': 0, 'locked_until': None},
        ))
    conn.close()
    return jsonify({
        'total': total, 'page': page, 'per_page': per_page,
        'sort_by': sort_by, 'sort_dir': sort_dir,
        'data': data
    }), 200


@app.route('/api/users', methods=['POST'])
@admin_required
@idempotent
def add_user():
    data = request.get_json(silent=True) or {}
    try:
        normalized = _validate_user_payload(data, creating=True)
    except UserValidationError as exc:
        return jsonify({'error': str(exc)}), 400
    data = {**data, **normalized}
    conn = get_db()
    if conn.execute("SELECT 1 FROM users WHERE UPPER(emp_id) = ?", [data['emp_id']]).fetchone():
        conn.close()
        return jsonify({'error': 'Employee ID already exists'}), 409
    if conn.execute("SELECT 1 FROM users WHERE LOWER(email) = ?", [data['email']]).fetchone():
        conn.close()
        return jsonify({'error': 'Email already exists'}), 409
    pwd = data.get('password')
    generated = pwd is None
    if generated:
        # A default is still a password, and a *shared* default is worse than
        # none: the old `data.get('password', 'pass123')` gave every employee
        # created without a password the same one, and `pass123` is on the breach
        # corpus. The seed writes its own demo hash directly and never comes here.
        pwd = _generate_initial_password()
    problem = _password_problem(pwd, 'password')
    if problem:  # unreachable for a generated one; a supplied password can fail
        return jsonify(problem[0]), problem[1]
    if _shift_model():
        conn.execute(
            "INSERT INTO users (emp_id, name, email, password, role, department, designation, status, first_login, created_at, allow_login, allow_breaks) VALUES (?, ?, ?, ?, ?, ?, ?, 'Active', ?, ?, ?, ?)",
            [data['emp_id'], data['name'], data['email'], hash_password(pwd),
             data.get('role', 'Employee'), data.get('department', ''),
             data.get('designation', ''),
             datetime.now(), datetime.now(),
             int(data.get('allow_login', 1)), int(data.get('allow_breaks', 1))]
        )
        set_shift(
            data['emp_id'], data.get('shift_start', ''), data.get('shift_end', ''),
            conn=conn, weekly_off=data.get('weekly_off_pattern')
        )
    else:
        conn.execute(
            "INSERT INTO users (emp_id, name, email, password, role, department, designation, status, first_login, created_at, allow_login, allow_breaks, shift_start, shift_end, weekly_off_pattern) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'Active', ?, ?, ?, ?, ?, ?, ?)",
            [data['emp_id'], data['name'], data['email'], hash_password(pwd),
             data.get('role', 'Employee'), data.get('department', ''),
             data.get('designation', ''),
             datetime.now(), datetime.now(),
             int(data.get('allow_login', 1)), int(data.get('allow_breaks', 1)),
             data.get('shift_start', ''), data.get('shift_end', ''),
             data.get('weekly_off_pattern', 'Sat,Sun')]
        )
    conn.close()
    audit_log(session['emp_id'], 'USER_CREATE', f'Created user {data["emp_id"]}',
              entity='users', entity_id=data['emp_id'], after={'role': data.get('role'), 'department': data.get('department')})

    admin_name = session.get('name', 'Admin')
    creds_body = f"""<div style="font-family:Arial,sans-serif;max-width:500px;margin:0 auto;padding:24px;background:#f8fafc;border-radius:12px;border:1px solid #e2e8f0;">
        <h2 style="color:#0f172a;margin:0 0 16px;">Welcome to HRMS</h2>
        <p style="color:#334155;">Hi <strong>{data['name']}</strong>,</p>
        <p style="color:#334155;">Your account has been created by <strong>{admin_name}</strong>. Here are your login credentials:</p>
        <div style="background:white;border:1px solid #e2e8f0;border-radius:8px;padding:16px;margin:16px 0;">
            <p style="margin:4px 0;"><strong>Employee ID:</strong> {data['emp_id']}</p>
            <p style="margin:4px 0;"><strong>Email:</strong> {data['email']}</p>
            <p style="margin:4px 0;"><strong>Password:</strong> {pwd}</p>
            <p style="margin:4px 0;"><strong>Role:</strong> {data.get('role', 'Employee')}</p>
            <p style="margin:4px 0;"><strong>Department:</strong> {data.get('department', 'N/A')}</p>
        </div>
        <p style="color:#64748b;font-size:12px;">Please change your password after first login. Do not share these credentials with anyone.</p>
        <p style="color:#64748b;font-size:12px;">- HRMS Team</p>
    </div>"""
    try:
        event_id = enqueue_notification_email(
            conn, data['emp_id'], data['email'],
            'Your HRMS Account Credentials', creds_body,
            category=notifications.category_for('CREDENTIALS_ISSUED'),
            force=True,
        )
        queued = True
    except Exception:
        # A user must not fail to be created because a mail queue row could not be
        # written. Logged loudly and reported in the response below, so the admin
        # knows the credentials did not reach the employee.
        logger.exception('Could not queue the welcome email for %s', data['emp_id'])
        event_id, queued = None, False

    # **`email_sent` is gone, and its removal is the point.** It was hardcoded `True`
    # — the same defect as `send_email` returning True for a send that never happened,
    # one level up: with no SMTP configured the employee is created, the response says
    # the welcome email went out, and nobody received it. Reporting `email_sent:
    # false` would not be an improvement either, because this route still does not
    # know: it now *queues* a message. What it can honestly report is that the
    # credentials were not delivered by this request, and where the recovery route is.
    body = {'message': 'User added', 'email_queued': bool(queued),
            'email_event_id': event_id}
    if not queued:
        body['notice'] = (
            'The welcome email could not be queued, so the credentials did not reach '
            f'{data["email"]}. Hand the password over directly, or use '
            'POST /api/admin/users/<emp_id>/password to issue a new one.'
        )
    else:
        body['notice'] = (
            'The credentials email is queued for delivery, not sent by this request. '
            'Hand the password over directly as well: a deployment with no mail server '
            'will queue the message and dead-letter it, and POST '
            '/api/admin/users/<emp_id>/password issues a new one on demand.'
        )
    if generated:
        # The admin supplied no password, so this is the only time the plaintext
        # exists anywhere but the email above. It is returned so a deployment with
        # no SMTP configured does not silently lock the new user out.
        body['generated_password'] = pwd
        body['password_source'] = 'generated'
    return jsonify(body), 201


@app.route('/api/users/<emp_id>', methods=['GET'])
@admin_required
def get_user_route(emp_id):
    u = get_user(emp_id)
    if not u:
        return jsonify({'error': 'Not found'}), 404
    sstart, send = get_shift(emp_id)
    return jsonify({
        'emp_id': u[0], 'name': u[1], 'email': u[2], 'role': u[3],
        'status': u[4], 'department': u[5],
        'allow_login': 1 if u[6] else 0,
        'allow_breaks': 1 if u[7] else 0,
        'shift_start': sstart or '',
        'shift_end': send or '',
        'weekly_off_pattern': get_weekly_off_pattern(emp_id)
    }), 200


@app.route('/api/users/<emp_id>', methods=['PUT'])
@admin_required
def update_user(emp_id):
    data = request.get_json(silent=True) or {}
    conn = get_db()
    row = conn.execute(
        "SELECT emp_id, name, email, role, department, status, designation, allow_login, allow_breaks "
        "FROM users WHERE UPPER(emp_id) = ?",
        [emp_id.strip().upper()],
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'User not found'}), 404
    existing = dict(zip(
        ('emp_id', 'name', 'email', 'role', 'department', 'status', 'designation',
         'allow_login', 'allow_breaks'),
        row,
    ))
    try:
        merged = _validate_user_payload(data, creating=False, existing=existing)
    except UserValidationError as exc:
        conn.close()
        return jsonify({'error': str(exc)}), 400
    if existing['status'] == 'Archived':
        conn.close()
        return jsonify({'error': 'Archived users must be restored before editing'}), 409
    if existing['status'] == 'Blocked' and merged['status'] == 'Active':
        conn.close()
        return jsonify({'error': 'Use the unblock action to restore a blocked user'}), 409
    if merged['email'] != str(existing['email']).lower() and conn.execute(
        "SELECT 1 FROM users WHERE LOWER(email) = ? AND emp_id <> ?",
        [merged['email'], existing['emp_id']],
    ).fetchone():
        conn.close()
        return jsonify({'error': 'Email already exists'}), 409

    columns = []
    values = []
    for field in ('name', 'email', 'role', 'department', 'designation', 'status',
                  'allow_login', 'allow_breaks'):
        if field in data or field in ('allow_login', 'allow_breaks'):
            columns.append(field)
            value = merged[field]
            # Flags stay int on the v1.0 (legacy/DuckDB) INTEGER columns; the
            # adapter rewrites them to booleans for the v2.0 BOOLEAN columns.
            values.append(int(value) if isinstance(value, bool) else value)
    if columns:
        values.append(existing['emp_id'])
        conn.execute(
            f"UPDATE users SET {', '.join(f'{column} = ?' for column in columns)} WHERE emp_id = ?",
            values,
        )
    current_start, current_end = get_shift(existing['emp_id'], conn)
    if 'shift_start' in data or 'shift_end' in data or 'weekly_off_pattern' in data:
        set_shift(
            existing['emp_id'],
            data.get('shift_start', current_start),
            data.get('shift_end', current_end),
            conn=conn,
            weekly_off=data.get('weekly_off_pattern'),
        )
    should_revoke = merged['status'] in ('Blocked', 'Inactive', 'Pre-hire') and existing['status'] not in ('Blocked', 'Inactive', 'Pre-hire')
    if should_revoke:
        conn.execute("UPDATE users SET allow_login = 0 WHERE emp_id = ?", [existing['emp_id']])
        _close_active_user_sessions(conn, existing['emp_id'])
    conn.close()
    if should_revoke:
        _revoke_redis_sessions(existing['emp_id'])
    audit_log(
        session['emp_id'],
        'USER_UPDATE',
        f'Updated user {existing["emp_id"]}',
        entity='users',
        entity_id=existing['emp_id'],
        before={key: existing[key] for key in ('name', 'email', 'role', 'department', 'status', 'allow_login', 'allow_breaks')},
        after={key: merged[key] for key in ('name', 'email', 'role', 'department', 'status', 'allow_login', 'allow_breaks')},
    )
    return jsonify({'message': 'User updated'}), 200


def _user_status_or_404(conn, emp_id):
    row = conn.execute("SELECT name, status FROM users WHERE emp_id = ?", [emp_id]).fetchone()
    if not row:
        return None
    return row


def _set_user_access_status(emp_id, status, allow_login, action, actor_emp_id):
    conn = get_db()
    try:
        user = _user_status_or_404(conn, emp_id)
        if not user:
            conn.close()
            return jsonify({'error': 'User not found'}), 404
        if action == 'restore' and user[1] != 'Archived':
            conn.close()
            return jsonify({'error': 'Only archived users can be restored'}), 409
        if action == 'archive' and user[1] == 'Archived':
            conn.close()
            return jsonify({'error': 'User is already archived'}), 409
        if user[1] == 'Archived' and action != 'restore':
            conn.close()
            return jsonify({'error': 'Archived users must be restored before changing access'}), 409
        if user[1] == status and action not in ('archive', 'restore'):
            conn.close()
            return jsonify({'error': f'User is already {status.lower()}'}), 409
        conn.execute(
            "UPDATE users SET status = ?, allow_login = ? WHERE emp_id = ?",
            [status, int(allow_login), emp_id],
        )
        revoked = _close_active_user_sessions(conn, emp_id) if not allow_login else 0
        conn.close()
        if not allow_login:
            _revoke_redis_sessions(emp_id)
        past = {'block': 'blocked', 'unblock': 'unblocked', 'archive': 'archived', 'restore': 'restored'}
        past_action = past[action]
        audit_log(
            actor_emp_id,
            action,
            f'{past_action.title()} user {emp_id}',
            entity='users',
            entity_id=emp_id,
            before={'status': user[1]},
            after={'status': status, 'allow_login': bool(allow_login), 'sessions_closed': revoked},
        )
        return jsonify({'message': f'User {emp_id} {past_action}', 'sessions_closed': revoked}), 200
    except Exception:
        conn.close()
        raise


#: FR-USR-07's `action` vocabulary, mapped to the single-employee operation each one
#: performs. Kept as data rather than an if/elif so a new action cannot be added
#: without stating the status and the `allow_login` value it implies — those two are
#: what decide whether sessions are closed, and getting them wrong is the difference
#: between blocking somebody and signing them out.
BULK_USER_ACTIONS = {
    'block': ('Blocked', 0, 'block'),
    'unblock': ('Active', 1, 'unblock'),
    'archive': ('Archived', 0, 'archive'),
    'restore': ('Active', 1, 'restore'),
}

#: A batch is bounded so one request cannot hold locks across the whole directory or
#: read as a single "why did that take so long".
BULK_USER_LIMIT = 500


@app.route('/api/v1/users/bulk', methods=['POST'])
@app.route('/api/users/bulk', methods=['POST'])
@admin_required
def bulk_user_action():
    """FR-USR-07 — bulk block / unblock / archive / restore.

    The SRS: *"Bulk POST /api/users/bulk {action, emp_ids[]}: self excluded; per-row
    result reported, partial failure does not fail the batch."* The single-employee
    routes took one employee at a time, so closing a leaver's team out, or
    offboarding a department when a project ended, was one request per person — and an
    administrator interrupted halfway had no way to tell which half.

    **Each row delegates to `_set_user_access_status`**, the same function the single
    routes call. That is the whole discipline: the batch cannot decide to be more
    permissive about a 409, cannot forget to close sessions, and cannot skip the audit
    row, because it has no implementation of its own to get wrong. Every refusal the
    single routes make — already archived, archived users must be restored first,
    already blocked — is therefore identical here, per row, with the same message.

    **Self is a per-row failure, not a silent skip.** The single routes refuse it with
    a 409 and so does this, reported in `failed` like any other refusal. Silently
    dropping the row would be worse: an administrator who selected thirty people
    including themselves would see "29 archived" with no indication that the
    thirtieth was skipped for a different reason than the others.

    **Partial failure does not fail the batch**, and the status code says which
    happened: `200` all succeeded, `207` mixed, `400` none. A 200 that hid a failure —
    or a 400 that hid twenty successes — is the reason the SRS asks for per-row
    results in the first place.
    """
    data = request.get_json(silent=True) or {}
    action = str(data.get('action') or '').strip().lower()
    if action not in BULK_USER_ACTIONS:
        return jsonify({
            'error': f'action must be one of: {", ".join(sorted(BULK_USER_ACTIONS))}',
        }), 400

    raw_ids = data.get('emp_ids')
    if isinstance(raw_ids, str):
        raw_ids = [part.strip() for part in raw_ids.replace(';', ',').split(',')]
    if not raw_ids:
        return jsonify({'error': 'emp_ids required: one or more employee IDs'}), 400
    if len(raw_ids) > BULK_USER_LIMIT:
        return jsonify({
            'error': f'At most {BULK_USER_LIMIT} employees per bulk request',
        }), 400

    # Normalised and blank-filtered **before** any work, so `['']` gets the same
    # "emp_ids required" answer as `[]` rather than falling through the loop and
    # arriving at a different message with zero rows processed.
    emp_ids = [str(value).strip().upper() for value in raw_ids if str(value).strip()]
    if not emp_ids:
        return jsonify({'error': 'emp_ids required: one or more employee IDs'}), 400

    status, allow_login, op = BULK_USER_ACTIONS[action]
    actor = session['emp_id']
    results = []
    for emp_id in emp_ids:
        if emp_id == actor:
            # Same refusal, same wording as `block_user`/`archive_user`, so a client
            # that handles the single-route message handles this one too.
            results.append({
                'emp_id': emp_id, 'ok': False,
                'error': f'Cannot {op} your own account', 'status': 409,
            })
            continue
        try:
            body, code = _set_user_access_status(emp_id, status, allow_login, op, actor)
            payload = body.get_json() if hasattr(body, 'get_json') else {}
            results.append({
                'emp_id': emp_id,
                'ok': code < 400,
                'status': code,
                'error': None if code < 400 else (payload or {}).get('error'),
                'sessions_closed': (payload or {}).get('sessions_closed'),
            })
        except Exception:
            # One row raising must not abandon the rest of the batch. Logged with the
            # employee, because a row that failed for an unexpected reason is the one
            # an administrator most needs to see.
            logger.exception('bulk %s failed for %s', action, emp_id)
            results.append({
                'emp_id': emp_id, 'ok': False, 'status': 500,
                'error': 'The action failed for this employee',
            })

    succeeded = [r for r in results if r['ok']]
    failed = [r for r in results if not r['ok']]
    body = {
        'action': action,
        'requested': len(results),
        'succeeded': len(succeeded),
        'failed': len(failed),
        'results': results,
    }
    if not succeeded:
        # Nothing changed. The per-row reasons are already in `results`.
        return jsonify({**body, 'error': f'No employees were {op}d'}), 400
    if failed:
        return jsonify(body), 207
    return jsonify(body), 200


@app.route('/api/users/<emp_id>/block', methods=['POST'])
@admin_required
def block_user(emp_id):
    if emp_id == session.get('emp_id'):
        return jsonify({'error': 'Cannot block your own account'}), 409
    return _set_user_access_status(emp_id, 'Blocked', 0, 'block', session['emp_id'])


@app.route('/api/users/<emp_id>/unblock', methods=['POST'])
@admin_required
def unblock_user(emp_id):
    if emp_id == session.get('emp_id'):
        return jsonify({'error': 'Cannot unblock your own account'}), 409
    return _set_user_access_status(emp_id, 'Active', 1, 'unblock', session['emp_id'])


@app.route('/api/users/<emp_id>/archive', methods=['POST'])
@admin_required
def archive_user(emp_id):
    if emp_id == session.get('emp_id'):
        return jsonify({'error': 'Cannot archive your own account'}), 409
    return _set_user_access_status(emp_id, 'Archived', 0, 'archive', session['emp_id'])


@app.route('/api/users/<emp_id>/restore', methods=['POST'])
@admin_required
def restore_user(emp_id):
    if emp_id == session.get('emp_id'):
        return jsonify({'error': 'Cannot restore your own account'}), 409
    return _set_user_access_status(emp_id, 'Active', 1, 'restore', session['emp_id'])


@app.route('/api/users/<emp_id>', methods=['DELETE'])
@admin_required
def delete_user(emp_id):
    """Backward-compatible DELETE: archive, never hard-delete statutory data."""
    if emp_id == session.get('emp_id'):
        return jsonify({'error': 'Cannot archive your own account'}), 409
    return _set_user_access_status(emp_id, 'Archived', 0, 'archive', session['emp_id'])


# ══════════════════════════════════════════════════════════════════════
#  USER PERMISSIONS (FR-USR-09 / FR-USR-15, matrix in policy.py)
# ══════════════════════════════════════════════════════════════════════

def _permissions_target(conn, emp_id):
    """Resolve a permission-editing target, or return an error response."""
    row = conn.execute(
        "SELECT emp_id, name, role, department, status FROM users WHERE UPPER(emp_id) = ?",
        [emp_id.strip().upper()],
    ).fetchone()
    if not row:
        return None, (jsonify({'error': 'User not found'}), 404)
    target = {
        'emp_id': row[0], 'name': row[1], 'role': row[2], 'department': row[3], 'status': row[4],
    }
    if target['status'] in ('Archived', 'Blocked'):
        return None, (jsonify({
            'error': f'{target["status"].lower()} users cannot have permissions changed; '
                     'restore or unblock them first',
        }), 409)
    return target, None


def _last_admin_would_be_locked_out(conn, target, overrides):
    """Refuse a change that leaves no administrator able to manage users."""
    guarded = ('users', 'import_users')
    defaults = policy.role_defaults(target['role'])
    if not any(not overrides.get(module, defaults[module]) for module in guarded):
        return False
    if str(target['role']) not in policy.ADMIN_ROLES:
        return False
    rows = conn.execute(
        "SELECT emp_id, role FROM users WHERE status = 'Active' AND role IN ('Admin', 'Super Admin')"
    ).fetchall()
    for row in rows:
        if row[0] == target['emp_id']:
            continue
        effective = policy.effective_permissions(conn, row[0], row[1])
        if all(effective.get(module, False) for module in guarded):
            return False
    return True


@app.route('/api/users/<emp_id>/permissions', methods=['GET'])
@admin_required
def get_user_permissions(emp_id):
    conn = get_db()
    try:
        target, error = _permissions_target(conn, emp_id)
        if error:
            return error
        overrides = policy.override_rows(conn, target['emp_id'])
        return jsonify({
            'emp_id': target['emp_id'],
            'name': target['name'],
            'role': target['role'],
            'modules': sorted(policy.PERMISSION_MODULES),
            'defaults': policy.role_defaults(target['role']),
            'overrides': overrides,
            'effective': policy.effective_permissions(conn, target['emp_id'], target['role']),
        }), 200
    finally:
        conn.close()


@app.route('/api/users/<emp_id>/permissions', methods=['PUT'])
@admin_required
def update_user_permissions(emp_id):
    """Full replace of a user's override set (FR-USR-09).

    A module present in ``modules`` is upserted; a module that is absent has its
    row deleted, so the user reverts to the role default.
    """
    payload = request.get_json(silent=True) or {}
    try:
        requested = policy.validate_module_map(payload.get('modules'))
    except policy.PolicyError as exc:
        return jsonify({'error': str(exc)}), exc.status
    conn = get_db()
    try:
        target, error = _permissions_target(conn, emp_id)
        if error:
            return error
        if target['emp_id'] == session.get('emp_id'):
            return jsonify({'error': 'You cannot change your own permissions'}), 409
        actor = policy.current_actor(conn)
        if not policy.can(actor, 'policy_admin', target, conn=conn):
            return jsonify({'error': 'Policy administration access required'}), 403
        if _last_admin_would_be_locked_out(conn, target, requested):
            return jsonify({
                'error': 'This change would leave no administrator able to manage users',
            }), 409

        before = policy.override_rows(conn, target['emp_id'])
        for module in sorted(set(before) - set(requested)):
            conn.execute(
                "DELETE FROM user_permissions WHERE emp_id = ? AND module = ?",
                [target['emp_id'], module],
            )
        now = datetime.now()
        for module, allow in sorted(requested.items()):
            if module in before:
                if before[module] == allow:
                    continue
                conn.execute(
                    "UPDATE user_permissions SET allow = ?, updated_at = ? WHERE emp_id = ? AND module = ?",
                    [int(allow), now, target['emp_id'], module],
                )
            else:
                conn.execute(
                    "INSERT INTO user_permissions (perm_id, emp_id, module, allow, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [_next_generated_id(conn, 'user_permissions', 'perm_id'), target['emp_id'],
                     module, int(allow), now, now],
                )
        effective = policy.effective_permissions(conn, target['emp_id'], target['role'])
    finally:
        conn.close()
    changes = policy.diff_overrides(before, requested)
    audit_log(
        session['emp_id'],
        'USER_PERMISSIONS_UPDATE',
        f'Updated permissions for {target["emp_id"]}: '
        f'added=[{",".join(changes["added"])}] removed=[{",".join(changes["removed"])}] '
        f'changed=[{",".join(changes["changed"])}]',
        entity='user_permissions',
        entity_id=target['emp_id'],
        before=before,
        after=requested,
    )
    return jsonify({
        'message': f'Permissions updated for {target["emp_id"]}',
        'emp_id': target['emp_id'],
        'overrides': requested,
        'effective': effective,
        'changes': changes,
    }), 200



@app.route('/api/users/<emp_id>/pii', methods=['GET'])
@hr_or_admin_required
def get_user_pii(emp_id):
    """Audited PII reveal for one employee (FR-USR-15, ``pii_reveal``).

    The directory never returns personal fields. This is the only route that
    does, it requires the ``pii_reveal`` capability for *another* employee, and
    every cross-employee reveal is written to the audit log. An employee's own
    record needs no capability and is not logged (that is what
    ``/api/profile`` serves).
    """
    conn = get_db()
    try:
        target, error = _permissions_target(conn, emp_id)
        if error:
            return error
        actor = policy.current_actor(conn)
        own_record = actor.get('emp_id') == target['emp_id']
        if not policy.pii_view(actor, target['emp_id'], conn=conn):
            return jsonify({'error': 'PII reveal not permitted'}), 403
        row = conn.execute(
            "SELECT name, date_of_birth, address, emergency_contact_name, emergency_contact_phone "
            "FROM users WHERE emp_id = ?",
            [target['emp_id']],
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({'error': 'User not found'}), 404
    payload = {
        'emp_id': target['emp_id'],
        'name': row[0],
        'date_of_birth': row[1].isoformat() if row[1] else None,
        'address': row[2],
        'emergency_contact_name': row[3],
        'emergency_contact_phone': row[4],
    }
    if own_record:
        return jsonify(payload), 200
    audit_log(
        session['emp_id'],
        'PII_REVEAL',
        f"Revealed personal data of {target['emp_id']} to {actor['emp_id']}",
        entity='users',
        entity_id=target['emp_id'],
        after={'fields': sorted(policy.USER_PII_FIELDS)},
    )
    return jsonify(payload), 200

# ══════════════════════════════════════════════════════════════════════
#  REPORTS
# ══════════════════════════════════════════════════════════════════════

@app.route('/admin/reports')
@hr_or_admin_required
def admin_reports():
    return render_template('admin_reports.html')


@app.route('/admin/holidays')
@admin_required
def admin_holidays():
    return render_template('holidays.html')


@app.route('/regularization')
@login_required
def regularization_page():
    return render_template('regularization.html')


@app.route('/admin/import-users')
@hr_or_admin_required
def import_users_page():
    return render_template('import_users.html')


@app.route('/api/reports')
@admin_required
def get_reports():
    start_date = parse_date(request.args.get('start_date'), datetime.now().date())
    end_date = parse_date(request.args.get('end_date'), start_date)
    if end_date and end_date < start_date:
        start_date, end_date = end_date, start_date

    department = request.args.get('department', '').strip()
    emp_id_filter = request.args.get('emp_id', '').strip()

    conn = get_db()

    user_where = "WHERE u.role = 'Employee'"
    user_params = []
    if department:
        user_where += " AND u.department = ?"
        user_params.append(department)
    if emp_id_filter:
        user_where += " AND u.emp_id = ?"
        user_params.append(emp_id_filter)

    summary = conn.execute(f"""
        SELECT u.emp_id, u.name, u.department,
               (SELECT MIN(login_time) FROM user_sessions us WHERE us.emp_id = u.emp_id AND us.session_date BETWEEN ? AND ?),
               (SELECT MAX(logout_time) FROM user_sessions us WHERE us.emp_id = u.emp_id AND us.session_date BETWEEN ? AND ?),
               COALESCE((SELECT SUM(total_hours) FROM user_sessions us WHERE us.emp_id = u.emp_id AND us.session_date BETWEEN ? AND ?), 0),
               COALESCE((SELECT SUM(duration_minutes) FROM breaks b WHERE b.emp_id = u.emp_id AND b.break_date BETWEEN ? AND ? AND b.status = 'Completed'), 0),
               COALESCE((SELECT COUNT(*) FROM breaks b WHERE b.emp_id = u.emp_id AND b.break_date BETWEEN ? AND ? AND b.status = 'Completed'), 0),
               COALESCE((SELECT COUNT(*) FROM user_sessions us WHERE us.emp_id = u.emp_id AND us.session_date BETWEEN ? AND ?), 0)
        FROM users u {user_where} ORDER BY u.name
    """, [start_date, end_date, start_date, end_date, start_date, end_date,
          start_date, end_date, start_date, end_date, start_date, end_date] + user_params).fetchall()

    break_where = "WHERE b.break_date BETWEEN ? AND ?"
    break_params = [start_date, end_date]
    if department:
        break_where += " AND u.department = ?"
        break_params.append(department)
    if emp_id_filter:
        break_where += " AND b.emp_id = ?"
        break_params.append(emp_id_filter)

    break_details = conn.execute(f"""
        SELECT b.break_id, b.emp_id, u.name, u.department, b.break_type, b.start_time, b.end_time, b.duration_minutes, b.break_date, b.status
        FROM breaks b JOIN users u ON b.emp_id = u.emp_id {break_where} ORDER BY b.break_date DESC, b.start_time DESC
    """, break_params).fetchall()

    sess_where = "WHERE us.session_date BETWEEN ? AND ?"
    sess_params = [start_date, end_date]
    if department:
        sess_where += " AND u.department = ?"
        sess_params.append(department)
    if emp_id_filter:
        sess_where += " AND us.emp_id = ?"
        sess_params.append(emp_id_filter)

    session_details = conn.execute(f"""
        SELECT us.session_id, us.emp_id, u.name, u.department, us.login_time, us.logout_time, us.total_hours, us.session_date
        FROM user_sessions us JOIN users u ON us.emp_id = u.emp_id {sess_where} ORDER BY us.session_date DESC, us.login_time DESC
    """, sess_params).fetchall()

    departments = [r[0] for r in conn.execute("SELECT DISTINCT department FROM users WHERE role = 'Employee' AND department IS NOT NULL ORDER BY department").fetchall()]
    employees = conn.execute(f"SELECT emp_id, name FROM users u {user_where} ORDER BY name", user_params).fetchall()
    conn.close()

    summary_list = []
    for r in summary:
        sh = float(r[5] or 0)
        bm = int(r[6] or 0)
        bh = bm / 60
        ph = max(0, sh - bh)
        eff = round((ph / sh) * 100, 1) if sh > 0 else 0
        summary_list.append({
            'emp_id': r[0], 'employee_name': r[1], 'department': r[2] or 'N/A',
            'first_login': r[3].strftime('%H:%M:%S') if r[3] else 'N/A',
            'last_logout': r[4].strftime('%H:%M:%S') if r[4] else 'N/A',
            'total_session_hours': sh, 'total_break_minutes': bm,
            'total_breaks': int(r[7] or 0), 'session_count': int(r[8] or 0),
            'efficiency_percent': eff, 'productive_hours': round(ph, 2)
        })

    break_list = [{
        'break_id': r[0], 'emp_id': r[1], 'employee_name': r[2], 'department': r[3] or 'N/A',
        'break_type': r[4], 'start_time': r[5].strftime('%H:%M:%S') if r[5] else 'N/A',
        'end_time': r[6].strftime('%H:%M:%S') if r[6] else 'Ongoing',
        'duration_minutes': int(r[7]) if r[7] else 0,
        'break_date': r[8].isoformat() if r[8] else 'N/A', 'status': r[9]
    } for r in break_details]

    session_list = [{
        'session_id': r[0], 'emp_id': r[1], 'employee_name': r[2], 'department': r[3] or 'N/A',
        'login_time': r[4].strftime('%H:%M:%S') if r[4] else 'N/A',
        'logout_time': r[5].strftime('%H:%M:%S') if r[5] else 'Active',
        'total_hours': float(r[6]) if r[6] else 0,
        'session_date': r[7].isoformat() if r[7] else 'N/A'
    } for r in session_details]

    return jsonify({
        'report_range': {'start_date': start_date.isoformat(), 'end_date': end_date.isoformat()},
        'departments': departments,
        'employees': [{'emp_id': e[0], 'name': e[1]} for e in employees],
        'summary': summary_list, 'break_details': break_list, 'session_details': session_list
    }), 200


@app.route('/api/reports/department-summary')
@admin_required
def get_department_summary():
    start_date = parse_date(request.args.get('start_date'), datetime.now().date())
    end_date = parse_date(request.args.get('end_date'), start_date)
    if end_date and end_date < start_date:
        start_date, end_date = end_date, start_date

    conn = get_db()
    rows = conn.execute("""
        SELECT u.department,
               COUNT(DISTINCT u.emp_id) AS employee_count,
               COALESCE(SUM(us.total_hours), 0) AS total_hours,
               COALESCE(SUM(b.duration_minutes), 0) AS total_break_minutes,
               COALESCE(SUM(CASE WHEN b.status = 'Completed' THEN 1 ELSE 0 END), 0) AS total_breaks
        FROM users u
        LEFT JOIN user_sessions us ON us.emp_id = u.emp_id AND us.session_date BETWEEN ? AND ?
        LEFT JOIN breaks b ON b.emp_id = u.emp_id AND b.break_date BETWEEN ? AND ?
        WHERE u.role = 'Employee' AND u.department IS NOT NULL
        GROUP BY u.department ORDER BY u.department
    """, [start_date, end_date, start_date, end_date]).fetchall()
    conn.close()

    result = []
    for r in rows:
        sh = float(r[2] or 0)
        bm = int(r[3] or 0)
        ph = max(0, sh - bm / 60)
        eff = round((ph / sh) * 100, 1) if sh > 0 else 0
        result.append({
            'department': r[0], 'employee_count': int(r[1]),
            'total_hours': round(sh, 2), 'total_break_minutes': bm,
            'total_breaks': int(r[4]), 'productive_hours': round(ph, 2),
            'efficiency_percent': eff
        })

    return jsonify({'departments': result}), 200


# ══════════════════════════════════════════════════════════════════════
#  PAGES
# ══════════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    if 'emp_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


# ══════════════════════════════════════════════════════════════════════
#  ERROR HANDLERS
# ══════════════════════════════════════════════════════════════════════

@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Not found'}), 404


@app.errorhandler(429)
def rate_limited(error):
    return jsonify({'error': 'Too many requests. Please try again later.'}), 429


@app.errorhandler(500)
def server_error(error):
    logger.exception("Internal server error")
    return jsonify({'error': 'Internal server error'}), 500


@app.after_request
def set_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    if os.getenv('FLASK_ENV') == 'production':
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response


# ══════════════════════════════════════════════════════════════════════
#  CSRF TOKEN
# ══════════════════════════════════════════════════════════════════════

@app.route('/api/csrf-token', methods=['GET'])
def get_csrf_token():
    """Return the session's CSRF token, creating it on demand.

    Anonymous by design: the login page and programmatic clients (test
    harness) need the token before they have any other session state.
    Global enforcement itself lives in security.init_csrf().
    """
    token = session.get('csrf_token') or secrets.token_urlsafe(32)
    session['csrf_token'] = token
    return jsonify({'csrf_token': token})


# ══════════════════════════════════════════════════════════════════════
#  SCHEDULED JOBS
# ══════════════════════════════════════════════════════════════════════

def cleanup_expired_tokens():
    """The hourly maintenance job — FR-AUTH-14 / FR-JOB-02.

    The SRS names both duties in one sentence: "a scheduled job purges expired reset
    tokens hourly **and** auto-closes breaks Active for more than 12 hours", and
    FR-JOB-02 lists them together as one High-priority hourly entry. Only the first
    shipped, so a break whose end was never pressed stayed ``Active`` indefinitely —
    which attendance and the payroll loss-of-pay calculation both read.

    The sweep lives in ``orphan_breaks.py``; the notification, audit and admin
    fallback live here because they need the application's helpers. Each closed
    break is **notified to the employee and audited with an actor of SYSTEM**, for
    the same reason `audit_log` degrades outside a request: a guess recorded
    silently is a guess nobody can correct.

    Wrapped in an application context because a scheduler thread has no request
    context — and `audit_log`'s own degradation covers the *request* metadata only.
    Without this it raised `Working outside of application context`, was swallowed
    by its own `except`, and the audit row silently did not exist, which is the
    third instance of this specific shape in this codebase.
    """
    try:
        with app.app_context():
            _run_hourly_maintenance()
    except Exception as exc:
        logger.warning('hourly maintenance failed: %s', exc)


def _run_hourly_maintenance():
    conn = get_db()
    try:
        conn.execute("DELETE FROM password_reset_tokens WHERE expires_at < ?", [datetime.now()])
        conn.execute("DELETE FROM idempotency_keys WHERE expires_at < ?", [datetime.now()])  # CC-07
        orphans = orphan_breaks.close_orphaned_breaks(conn)
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.warning("Cleanup failed: %s", e)
        conn.close()
        return
    closed = []
    try:
        conn = get_db()
        for row in conn.execute(
            "SELECT break_id, emp_id, break_type, duration_minutes FROM breaks "
            "WHERE ended_reason = 'orphan_timeout' AND end_time >= ?",
            [datetime.now() - timedelta(minutes=5)],
        ).fetchall():
            closed.append(row)
    except Exception as e:
        logger.warning("Could not read back auto-closed breaks for notification: %s", e)
    finally:
        conn.close()

    for break_id, emp_id, break_type, minutes in closed:
        audit_log(
            'SYSTEM', 'BREAK_AUTO_CLOSED',
            f'Closed break {break_id} for {emp_id} left Active with no break-end',
            entity='Breaks', entity_id=str(break_id),
            before={'status': 'Active', 'end_time': None},
            after={'status': 'Orphaned', 'duration_minutes': minutes,
                   'ended_reason': 'orphan_timeout'},
        )
        add_notification(
            emp_id, 'BREAK_AUTO_CLOSED',
            f'A {break_type} break on your record was left open and has been closed '
            f'after {orphan_breaks.ORPHAN_AFTER} hours, recorded as {minutes} minutes '
            '(the maximum for that break type). If that is wrong, ask an administrator '
            'to correct it.',
        )
    if orphans['closed']:
        logger.info(
            'Purged expired tokens; auto-closed %s orphaned break(s) totalling %s '
            'minutes (%s capped at the break-type limit)',
            orphans['closed'], orphans['minutes'], orphans['capped'],
        )
    else:
        logger.info("Cleaned up expired password reset tokens and idempotency keys")


def _register_scheduler_jobs(attendance_hour=2):
    """Register every background job. Separated from ``start()`` so a test can
    assert the wiring without a scheduler thread firing next to the assertions
    (on DuckDB a background job opening a connection during a request is the
    "Unique file handle conflict")."""
    scheduler.add_job(cleanup_expired_tokens, 'interval', hours=1, id='cleanup-tokens',
                      replace_existing=True, coalesce=True, max_instances=1)
    scheduler.add_job(outbox.run_dispatch, 'interval', seconds=60, id='outbox-dispatch',
                      replace_existing=True, coalesce=True, max_instances=1)
    # FR-USR-04: pick up one queued CSV import per tick. The claim is an atomic
    # status transition, so running several web workers is safe.
    scheduler.add_job(
        run_import_dispatch, 'interval', seconds=15,
        id='import-dispatch', replace_existing=True, coalesce=True, max_instances=1,
    )
    scheduler.add_job(
        run_attendance_finalization,
        'cron',
        hour=attendance_hour,
        minute=5,
        id='attendance-finalization',
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    # FR-LEA-08: credit the accrual for the months that have happened. The
    # grant is idempotent per (employee, type, year, month), so a missed run is
    # simply made up by the next one.
    scheduler.add_job(
        run_leave_accrual,
        'cron',
        day=1,
        hour=0,
        minute=30,
        timezone=IST,
        id='leave-accrual',
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        run_offboarding_access_revocation,
        'cron',
        hour=0,
        minute=0,
        timezone=IST,
        id='offboarding-access-revocation',
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )


def _install_lease_renewal(sched, leader_module):
    """Renew the lease while the scheduler runs; stop scheduling if it is lost.

    The renewal is itself a scheduler job, so it needs no extra thread. Losing the
    lease shuts the whole scheduler down rather than pausing individual jobs: a
    pod that cannot prove it still owns the term must run nothing, because the jobs
    it was running are precisely the ones that would now be duplicated by the new
    leader.
    """
    def _renew():
        if leader_module.renew():
            return True
        logger.error(
            'Scheduler lease lost (%s); shutting down cron jobs on this instance',
            leader_module.INSTANCE_ID,
        )
        try:
            sched.shutdown(wait=False)
        except Exception:
            pass
        return False

    sched.add_job(
        _renew, 'interval', seconds=leader_module.RENEW_INTERVAL_SECONDS,
        id='scheduler-lease-renewal', max_instances=1, coalesce=True,
        # Deliberately *not* misfire-graceful: a renewal that was skipped must not
        # be treated as if it happened.
        misfire_grace_time=5,
        replace_existing=True,
    )


# ── Start scheduler (only in master gunicorn process) ──────────────────
if not STARTED:
    _is_gunicorn_master = os.getenv('SERVER_SOFTWARE', '').startswith('gunicorn') or os.getenv('GUNICORN_MASTER') == 'true'
    _is_dev = os.getenv('FLASK_DEBUG') == '1' or os.getenv('FLASK_ENV') != 'production'
    if os.getenv('HRMS_DISABLE_SCHEDULER') == '1':
        # Deterministic test runs: background jobs would otherwise race the
        # assertions. The browser suite leaves the scheduler on for PostgreSQL.
        logger.info('Scheduler disabled (HRMS_DISABLE_SCHEDULER=1)')
    elif _is_dev or _is_gunicorn_master or not os.getenv('SERVER_SOFTWARE'):
        try:
            attendance_hour = min(max(int(os.getenv('ATTENDANCE_JOB_HOUR', '2')), 0), 23)
        except (TypeError, ValueError):
            attendance_hour = 2
        # FR-JOB-05: exactly-once across pods. The condition above narrows *when*
        # a scheduler is eligible (dev, or a gunicorn master, or a plain process);
        # it does not make it unique, because every pod has its own gunicorn master.
        # `scheduler_leader` is what makes it unique — see that module for the
        # failure modes, including the one that matters most: a configured but
        # unreachable Redis refuses to start the scheduler rather than running
        # every job unowned.
        import scheduler_leader

        if scheduler_leader.should_start_scheduler():
            _register_scheduler_jobs(attendance_hour)
            # FR-JOB-05: renew only where a lease was actually taken. On the
            # no-Redis fallback there is nothing to renew — `renew()` returns
            # False there — and installing the job anyway would shut the dev/CI
            # scheduler down on its first tick (~20 s), defeating the very
            # fallback `should_start_scheduler` just approved. Observed in a
            # browser-suite run: "Scheduler lease lost ...; shutting down cron
            # jobs on this instance" in an environment with no REDIS_URL.
            if scheduler_leader.renewal_required():
                _install_lease_renewal(scheduler, scheduler_leader)
            scheduler.start()
            STARTED = True
            logger.info("Scheduler started")


# ── Entry point ──────────────────────────────────────────────────────

if __name__ == '__main__':
    sentry_dsn = os.getenv('SENTRY_DSN')
    if sentry_dsn:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        sentry_sdk.init(dsn=sentry_dsn, integrations=[FlaskIntegration()])
        logger.info("Sentry initialized")

    app.run(debug=os.getenv('FLASK_DEBUG', '1') == '1',
            host='0.0.0.0',
            port=int(os.getenv('PORT', 5000)))
