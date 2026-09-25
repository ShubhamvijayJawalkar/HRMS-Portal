import hashlib
import json
import logging
import math
import os
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from io import BytesIO
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from flasgger import Swagger
from flask import Flask, g, jsonify, redirect, render_template, request, send_file, session, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.utils import secure_filename

from security import (
    check_password,
    hash_password,
    init_csrf,
    maybe_enable_redis_sessions,
    needs_rehash,
)

load_dotenv()

import outbox  # noqa: E402  # CC-09 transactional outbox (dispatcher job + enqueue helper)
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

# ── Phase 3a (SRS CC-06): Argon2id hashing, CSRF guard, Redis sessions ─
# Security lives in the request pipeline above the DB layer, so it applies
# to both the DuckDB and PostgreSQL backends unchanged.
init_csrf(app)
maybe_enable_redis_sessions(app)

DB_FILE = os.getenv('DB_FILE', 'hrms.duckdb')
IST = ZoneInfo('Asia/Kolkata')

# ── Rate Limiter ──────────────────────────────────────────────────────
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["200 per minute"],
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
    if os.getenv('APP_DB', 'duckdb').lower() in ('postgres', 'postgresql', 'pg'):
        import db_backend
        return db_backend.connect()
    conn = duckdb.connect(DB_FILE)
    try:
        conn.execute("PRAGMA enable_progress_bar")
    except Exception:
        pass
    return conn


def _is_public_target_schema():
    """True when the app is serving the immutable v2.0 ``public`` schema."""
    if os.getenv('APP_DB', 'duckdb').lower() not in ('postgres', 'postgresql', 'pg'):
        return False
    import db_backend
    return db_backend.app_schema() == 'public'


def _has_column(conn, table, column):
    if os.getenv('APP_DB', 'duckdb').lower() in ('postgres', 'postgresql', 'pg'):
        import db_backend
        return bool(conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = ? "
            "AND table_name = ? AND column_name = ?",
            [db_backend.app_schema(), table, column],
        ).fetchone())
    return bool(conn.execute(f'PRAGMA table_info("{table}")').fetchall()) and any(
        row[1] == column for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    )


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
    backend = os.getenv('APP_DB', 'duckdb').lower()
    is_pg = backend in ('postgres', 'postgresql', 'pg')
    if is_pg:
        import db_backend
        schema = db_backend.app_schema()
    else:
        schema = 'main'
    key = f"{backend}:{schema}"
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
    backend = os.getenv('APP_DB', 'duckdb').lower()
    is_pg = backend in ('postgres', 'postgresql', 'pg')
    schema = 'main'
    if is_pg:
        import db_backend
        schema = db_backend.app_schema()
    key = f"{backend}:{schema}"
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
    backend = os.getenv('APP_DB', 'duckdb').lower()
    is_pg = backend in ('postgres', 'postgresql', 'pg')
    schema = 'main'
    if is_pg:
        import db_backend
        schema = db_backend.app_schema()
    key = f"{backend}:{schema}"
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
    if not _is_public_target_schema() and not _has_column(conn, 'leave_balance', 'reserved'):
        conn.execute("ALTER TABLE leave_balance ADD COLUMN reserved INTEGER DEFAULT 0")

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

    if (not _is_public_target_schema()
            and os.getenv('APP_DB', 'duckdb').lower() in ('postgres', 'postgresql', 'pg')):
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

        # Seed some holidays
        year = datetime.now().year
        base = 9000000 + (datetime.now().microsecond % 100000)
        holidays_data = [
            [base + 1, 'New Year', f'{year}-01-01', year, 'National'],
            [base + 2, 'Republic Day', f'{year}-01-26', year, 'National'],
            [base + 3, 'Independence Day', f'{year}-08-15', year, 'National'],
            [base + 4, 'Diwali', f'{year}-11-01', year, 'Optional'],
            [base + 5, 'Christmas', f'{year}-12-25', year, 'Optional'],
        ]
        conn.executemany("INSERT INTO holidays VALUES (?, ?, ?, ?, ?)", holidays_data)

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
        conn.execute(
            "INSERT INTO password_reset_tokens (token_id, emp_id, token, expires_at, used, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            [base_id + 9, 'EMP002', 'reset-token-001', now + timedelta(hours=2), 0, now]
        )
        conn.execute(
            "INSERT INTO password_reset_tokens (token_id, emp_id, token, expires_at, used, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            [base_id + 10, 'EMP002', 'reset-token-002', now + timedelta(hours=4), 0, now]
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
            "INSERT INTO notifications (notification_id, emp_id, type, message, related_link, is_read, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [base_id + 15, 'EMP001', 'Leave', 'Your leave request is pending', '/leaves', 0, now]
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
        if actor is None:
            actor = session.get('name') or session.get('emp_id') or emp_id
        request_id = getattr(g, '_hrms_request_id', None)
        if request_id is None:
            request_id = request.headers.get('X-Request-ID') or f"req-{secrets.token_hex(8)}"
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
             _json(before), _json(after), request.remote_addr, request_id, datetime.now()]
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

    PostgreSQL ``public`` uses the CC-01 identity key. DuckDB and the legacy
    PostgreSQL schema use the v1.0 integer key and need a caller-supplied ID.
    """
    backend = os.getenv('APP_DB', 'duckdb').lower()
    is_pg = backend in ('postgres', 'postgresql', 'pg')
    schema = 'main'
    if is_pg:
        import db_backend
        schema = db_backend.app_schema()
    key = f"{backend}:{schema}"
    if key not in _ATTENDANCE_IDENTITY_CACHE:
        if not is_pg:
            _ATTENDANCE_IDENTITY_CACHE[key] = False
        else:
            import db_backend
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
    if any(row[1] is None for row in rows):
        as_of = as_of or datetime.now()
        scheduled = max(0.0, (shift_end_dt - shift_start_dt).total_seconds() / 3600)
        orphan_cap = shift_end_dt + timedelta(hours=scheduled * 0.25)
        last_event = min(as_of, orphan_cap)
    else:
        logout_times = [row[1] for row in rows if row[1]]
        last_event = max(logout_times) if logout_times else first_login
    if last_event < first_login:
        return 0.0

    raw_hours = max(0.0, (last_event - first_login).total_seconds() / 3600)
    scheduled = max(0.0, (shift_end_dt - shift_start_dt).total_seconds() / 3600)
    credit_cap = scheduled * 1.25
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

def _session_user_active():
    emp_id = session.get('emp_id')
    if not emp_id:
        return False
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT status, allow_login FROM users WHERE emp_id = ?", [emp_id]
        ).fetchone()
        return bool(row and row[0] in ('Active', 'Onboarding') and row[1])
    finally:
        conn.close()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'emp_id' not in session or not _session_user_active():
            session.clear()
            if request.is_json:
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'emp_id' not in session or not _session_user_active():
            session.clear()
            if request.is_json:
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('login'))
        emp_id = session['emp_id']
        conn = get_db()
        row = conn.execute("SELECT role FROM users WHERE emp_id = ?", [emp_id]).fetchone()
        conn.close()
        if not row or row[0] not in ('Admin', 'Super Admin'):
            if request.is_json:
                return jsonify({'error': 'Forbidden'}), 403
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated


def hr_or_admin_required(f):
    """Require Admin role OR HR department"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'emp_id' not in session or not _session_user_active():
            session.clear()
            if request.is_json:
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('login'))
        emp_id = session['emp_id']
        conn = get_db()
        row = conn.execute("SELECT role, department FROM users WHERE emp_id = ?", [emp_id]).fetchone()
        conn.close()
        if not row:
            return jsonify({'error': 'Forbidden'}), 403
        if row[0] in ('Admin', 'Super Admin', 'HR') or row[1] == 'HR':
            return f(*args, **kwargs)
        return jsonify({'error': 'Forbidden - HR access required'}), 403
    return decorated


def finance_or_admin_required(f):
    """Require the v2.0 Finance role or an Admin operations role."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'emp_id' not in session or not _session_user_active():
            session.clear()
            if request.is_json:
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('login'))
        emp_id = session['emp_id']
        conn = get_db()
        row = conn.execute("SELECT role FROM users WHERE emp_id = ?", [emp_id]).fetchone()
        conn.close()
        if not row or str(row[0]).lower() not in ('finance', 'admin', 'super admin'):
            return jsonify({'error': 'Forbidden - Finance access required'}), 403
        return f(*args, **kwargs)
    return decorated


def department_required(*depts):
    """Require specific department(s) or Admin role"""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if 'emp_id' not in session or not _session_user_active():
                if request.is_json:
                    return jsonify({'error': 'Authentication required'}), 401
                return redirect(url_for('login'))
            emp_id = session['emp_id']
            conn = get_db()
            row = conn.execute("SELECT role, department FROM users WHERE emp_id = ?", [emp_id]).fetchone()
            conn.close()
            if not row:
                return jsonify({'error': 'Forbidden'}), 403
            if row[0] in ('Admin', 'Super Admin') or row[1] in depts:
                return f(*args, **kwargs)
            return jsonify({'error': 'Forbidden - insufficient department access'}), 403
        return decorated
    return decorator


# ══════════════════════════════════════════════════════════════════════
#  AUTH ROUTES
# ══════════════════════════════════════════════════════════════════════

@app.route('/api/credentials')
@login_required
def get_credentials():
    """Return known user credentials for demo purposes (admin only)"""
    if session.get('role') not in ('Admin', 'admin'):
        return jsonify({'error': 'Admin access required'}), 403
    conn = get_db()
    rows = conn.execute("SELECT emp_id, name, role, department FROM users ORDER BY emp_id").fetchall()
    conn.close()
    result = [{'emp_id': r[0], 'name': r[1], 'role': r[2], 'department': r[3] or '-'} for r in rows]
    return jsonify(result), 200


@app.route('/login', methods=['GET', 'POST'])
@limiter.limit(os.getenv('LOGIN_RATE_LIMIT', '20 per minute'))
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
    row = conn.execute(
        "SELECT emp_id, name, role, password, status, allow_login, department FROM users WHERE emp_id = ?",
        [emp_id]
    ).fetchone()
    conn.close()

    if not row:
        return jsonify({'error': 'Invalid Employee ID'}), 401

    stored_hash = row[3]
    if not check_password(password, stored_hash):
        return jsonify({'error': 'Invalid Password'}), 401

    # Phase 3a (CC-06): transparently upgrade legacy bcrypt hashes to Argon2id
    if needs_rehash(stored_hash):
        hconn = get_db()
        hconn.execute(
            "UPDATE users SET password = ? WHERE emp_id = ?",
            [hash_password(password), emp_id]
        )
        hconn.close()

    if not row[5]:
        return jsonify({'error': 'Login is not allowed for this user'}), 403
    if row[4] in ('Blocked', 'Inactive', 'Pre-hire'):
        return jsonify({'error': 'Account is blocked'}), 403

    session_id = gen_id()
    session['emp_id'] = row[0]
    session['name'] = row[1]
    session['role'] = row[2]
    session['department'] = row[6] or ''
    session['session_id'] = session_id

    now = datetime.now()
    conn = get_db()
    shift_date = _get_shift_date_for_dt(row[0], now, conn)
    conn.execute(
        "INSERT INTO user_sessions (session_id, emp_id, login_time, session_date) VALUES (?, ?, ?, ?)",
        [session_id, row[0], now, shift_date]
    )
    conn.close()

    audit_log(row[0], 'LOGIN', f'User {row[1]} logged in', entity='Auth', entity_id=row[0])
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


@app.route('/dashboard')
@login_required
def dashboard():
    if session.get('role') in ('Admin', 'Finance') or session.get('department') == 'HR':
        return render_template('admin_dashboard.html')
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

    if len(new_pwd) < 6:
        return jsonify({'error': 'New password must be at least 6 characters'}), 400

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


@app.route('/api/forgot-password', methods=['POST'])
@limiter.limit("5 per minute")
def forgot_password():
    """Request password reset (generates token)
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
              email: {type: string}
      responses:
        200:
          description: Token generated (shown in dev)
    """
    data = request.get_json(silent=True) or {}
    emp_id = data.get('emp_id', '').strip().upper()
    email = data.get('email', '')

    conn = get_db()
    row = conn.execute(
        "SELECT email FROM users WHERE emp_id = ? AND email = ?",
        [emp_id, email]
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'No matching user found'}), 404

    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO password_reset_tokens (token_id, emp_id, token, expires_at) VALUES (?, ?, ?, ?)",
        [gen_id(), emp_id, _token_digest(token), datetime.now() + timedelta(hours=1)]
    )
    conn.close()

    logger.info("Password reset token for %s: %s", emp_id, token)
    return jsonify({
        'message': 'If the account exists, a reset link has been generated.',
        'token': token,
    }), 200


@app.route('/api/reset-password', methods=['POST'])
@limiter.limit("5 per minute")
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

    if len(new_pwd) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400

    conn = get_db()
    row = conn.execute(
        "SELECT token_id, emp_id FROM password_reset_tokens "
        "WHERE token IN (?, ?) AND used = 0 AND expires_at > ?",
        [token, _token_digest(token), datetime.now()]
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Invalid or expired token'}), 400

    conn.execute("UPDATE password_reset_tokens SET used = 1 WHERE token_id = ?", [row[0]])
    conn.execute("UPDATE users SET password = ? WHERE emp_id = ?", [hash_password(new_pwd), row[1]])
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
    did = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO dependents VALUES (?, ?, ?, ?, ?)",
                 [did, emp_id, data['name'], data['relationship'], parse_date(data.get('date_of_birth'))])
    conn.close()
    return jsonify({'message': 'Dependent added', 'id': did}), 201


@app.route('/api/v1/dependents/<int:did>', methods=['DELETE'])
@app.route('/api/dependents/<int:did>', methods=['DELETE'])
@login_required
def delete_dependent(did):
    conn = get_db()
    conn.execute("DELETE FROM dependents WHERE dependent_id = ? AND emp_id = ?", [did, session['emp_id']])
    conn.close()
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
    did = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO employee_documents VALUES (?, ?, ?, ?, ?)",
                 [did, emp_id, data['doc_type'], data.get('file_name', ''), datetime.now()])
    conn.close()
    return jsonify({'message': 'Document recorded', 'id': did}), 201


# ══════════════════════════════════════════════════════════════════════
#  HOLIDAY CALENDAR
# ══════════════════════════════════════════════════════════════════════

@app.route('/api/v1/holidays', methods=['GET'])
@app.route('/api/holidays', methods=['GET'])
@login_required
def get_holidays():
    year = request.args.get('year', datetime.now().year, type=int)
    conn = get_db()
    try:
        rows = conn.execute("SELECT holiday_id, name, holiday_date, type FROM holidays WHERE year = ? ORDER BY holiday_date", [year]).fetchall()
        return jsonify([{'id': r[0], 'name': r[1], 'date': r[2].isoformat(), 'type': r[3]} for r in rows]), 200
    finally:
        conn.close()


@app.route('/api/v1/holidays', methods=['POST'])
@app.route('/api/holidays', methods=['POST'])
@admin_required
def add_holiday():
    data = request.get_json(silent=True) or {}
    if not data.get('name') or not data.get('date'):
        return jsonify({'error': 'name and date required'}), 400
    htype = data.get('type', 'National')
    if htype not in ('National', 'Optional'):
        return jsonify({'error': 'type must be National or Optional'}), 400
    d = parse_date(data['date'])
    if d is None:
        return jsonify({'error': 'Invalid date format'}), 400
    hid = gen_id()
    conn = get_db()
    try:
        dup = conn.execute("SELECT 1 FROM holidays WHERE holiday_date = ? AND name = ?", [d, data['name']]).fetchone()
        if dup:
            return jsonify({'error': 'Holiday with this name and date already exists'}), 409
        conn.execute("INSERT INTO holidays VALUES (?, ?, ?, ?, ?)",
                     [hid, data['name'], d, d.year, htype])
        return jsonify({'message': 'Holiday added', 'id': hid}), 201
    finally:
        conn.close()


@app.route('/api/v1/holidays/<int:hid>', methods=['DELETE'])
@app.route('/api/holidays/<int:hid>', methods=['DELETE'])
@admin_required
def delete_holiday(hid):
    conn = get_db()
    try:
        result = conn.execute("DELETE FROM holidays WHERE holiday_id = ?", [hid])
        if result.rowcount == 0:
            return jsonify({'error': 'Holiday not found'}), 404
        return jsonify({'message': 'Deleted'}), 200
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════
#  ORG CHART
# ══════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════
#  NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════

def _notification_category(ntype):
    """FR-NOT-03 preference category derived from the notification type."""
    t = (ntype or '').upper()
    if 'LEAVE' in t:
        return 'Leave'
    if 'PAYROLL' in t:
        return 'Payroll'
    if 'ONBOARDING' in t:
        return 'Onboarding'
    if 'BREAK' in t:
        return 'Break'
    if 'OFFER' in t:
        return 'Offer'
    return 'General'


def add_notification(emp_id, ntype, message, link=None, category=None):
    conn = None
    try:
        conn = get_db()
        if category is None:
            category = _notification_category(ntype)
        conn.execute(
            "INSERT INTO notifications (notification_id, emp_id, type, category, message, related_link, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [gen_id(), emp_id, ntype, category, message, link, datetime.now()]
        )
    except Exception as e:
        logger.warning("Notification failed: %s", e)
    finally:
        if conn:
            conn.close()


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
    conn = get_db()
    conn.execute("UPDATE notifications SET is_read = 1 WHERE emp_id = ?", [session['emp_id']])
    conn.close()
    return jsonify({'message': 'Marked read'}), 200


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
        conn = get_db()
        if session.get('role') == 'Admin':
            rows = conn.execute(
                "SELECT request_id, emp_id, request_date, reason, status, approved_by, created_at FROM regularization_requests ORDER BY created_at DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT request_id, emp_id, request_date, reason, status, approved_by, created_at FROM regularization_requests WHERE emp_id = ? ORDER BY created_at DESC",
                [emp_id]
            ).fetchall()
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
    rid = gen_id()
    conn.execute(
        "INSERT INTO regularization_requests (request_id, emp_id, request_date, reason, status) VALUES (?, ?, ?, ?, 'Pending')",
        [rid, emp_id, d, data['reason']]
    )
    conn.close()
    return jsonify({'message': 'Request submitted', 'id': rid}), 201


@app.route('/api/v1/regularization/<int:rid>/approve', methods=['POST'])
@app.route('/api/regularization/<int:rid>/approve', methods=['POST'])
@admin_required
def approve_regularization(rid):
    conn = get_db()
    row = conn.execute(
        "SELECT emp_id, request_date, status FROM regularization_requests WHERE request_id = ?",
        [rid],
    ).fetchone()
    if row and row[2] == 'Pending':
        conn.execute(
            "UPDATE regularization_requests SET status = 'Approved', approved_by = ?, updated_at = ? "
            "WHERE request_id = ? AND status = 'Pending'",
            [session['emp_id'], datetime.now(), rid]
        )
        conn.close()
        # FR-JOB-01/FR-REG-03: a later approved correction recomputes only
        # the affected employee/date, rather than waiting for the next night.
        try:
            finalize_attendance_for_date(row[1], employee_ids=[row[0]])
        except Exception as exc:
            logger.warning('attendance recompute after regularization failed: %s', exc)
        return jsonify({'message': 'Approved'}), 200
    conn.close()
    return jsonify({'message': 'Approved'}), 200


@app.route('/api/v1/regularization/<int:rid>/reject', methods=['POST'])
@app.route('/api/regularization/<int:rid>/reject', methods=['POST'])
@admin_required
def reject_regularization(rid):
    conn = get_db()
    conn.execute(
        "UPDATE regularization_requests SET status = 'Rejected', approved_by = ?, updated_at = ? WHERE request_id = ? AND status = 'Pending'",
        [session['emp_id'], datetime.now(), rid]
    )
    conn.close()
    return jsonify({'message': 'Rejected'}), 200


# ══════════════════════════════════════════════════════════════════════
#  CSV IMPORT
# ══════════════════════════════════════════════════════════════════════

@app.route('/api/v1/users/import', methods=['POST'])
@app.route('/api/users/import', methods=['POST'])
@admin_required
def import_users_csv():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    f = request.files['file']
    if not f.filename.endswith('.csv'):
        return jsonify({'error': 'CSV file required'}), 400
    conn = None
    try:
        df = pd.read_csv(f)
        required = ['emp_id', 'name', 'email']
        missing = [c for c in required if c not in df.columns]
        if missing:
            return jsonify({'error': f'Missing columns: {missing}'}), 400
        conn = get_db()
        pwd = hash_password('pass123')
        count = 0
        for _, row in df.iterrows():
            eid = str(row['emp_id']).strip().upper()
            if conn.execute("SELECT 1 FROM users WHERE emp_id = ?", [eid]).fetchone():
                continue
            conn.execute(
                "INSERT INTO users (emp_id, name, email, password, role, department, status, first_login, created_at, allow_login, allow_breaks) VALUES (?, ?, ?, ?, ?, ?, 'Active', ?, ?, 1, 1)",
                [eid, str(row.get('name', '')), str(row.get('email', '')), pwd,
                 str(row.get('role', 'Employee')), str(row.get('department', '')),
                 datetime.now(), datetime.now()]
            )
            count += 1
        return jsonify({'message': f'{count} users imported'}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    finally:
        if conn:
            conn.close()


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
    aid = gen_id()
    conn = get_db()
    employee = conn.execute("SELECT 1 FROM users WHERE emp_id = ? AND status = 'Active'", [data['emp_id']]).fetchone()
    if not employee:
        conn.close()
        return jsonify({'error': 'Employee not found or inactive'}), 400
    conn.execute("INSERT INTO assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 [aid, data['emp_id'], data['asset_type'], data.get('asset_tag'), data.get('brand'), data.get('model'), data.get('serial_number'),
                  parse_date(data.get('issued_date'), datetime.now().date()), None, 'Issued', data.get('notes')])
    conn.close()
    return jsonify({'message': 'Asset issued', 'id': aid}), 201


@app.route('/api/v1/assets/<int:aid>/return', methods=['POST'])
@app.route('/api/assets/<int:aid>/return', methods=['POST'])
@admin_required
def return_asset(aid):
    conn = get_db()
    conn.execute("UPDATE assets SET return_date = ?, status = 'Returned' WHERE asset_id = ?", [datetime.now().date(), aid])
    conn.close()
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
    """Allocate a collision-free ID and keep public identity sequences ahead.

    Legacy schemas need explicit IDs; v2.0 public tables use identity keys but
    the compatibility app still supplies one. Advancing the backing sequence
    before the explicit insert preserves CC-01 after runtime lifecycle writes.
    """
    while True:
        value = gen_id()
        if not conn.execute(f"SELECT 1 FROM {table} WHERE {column} = ?", [value]).fetchone():
            if _is_public_target_schema():
                try:
                    import db_backend
                    ident = conn.execute(
                        "SELECT is_identity FROM information_schema.columns "
                        "WHERE table_schema = ? AND table_name = ? AND column_name = ?",
                        [db_backend.app_schema(), table, column],
                    ).fetchone()
                    if ident and str(ident[0]).upper() == 'YES':
                        sequence = conn.execute(
                            "SELECT pg_get_serial_sequence(?, ?)",
                            [f"{db_backend.app_schema()}.{table}", column],
                        ).fetchone()
                        if sequence and sequence[0]:
                            current = conn.execute(f"SELECT last_value FROM {sequence[0]}").fetchone()
                            next_value = max(value, int(current[0])) if current else value
                            conn.execute("SELECT setval(?, ?, true)", [sequence[0], next_value])
                except Exception:
                    logger.debug('could not advance identity sequence for %s.%s', table, column)
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


def _token_digest(token):
    return hashlib.sha256(str(token).encode()).hexdigest()


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
                    conn, admin[0], f'Access revoked for {emp_id} on last working day', '/offboarding', 'Offboarding'
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
        conn.close()
        return jsonify([{'id': r[0], 'job_id': r[1], 'job_title': r[2] or 'N/A', 'name': r[3], 'email': r[4], 'phone': r[5], 'status': r[6], 'applied_at': r[7].isoformat() if r[7] else None} for r in rows]), 200
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
        conn.close()
        return jsonify([{
            'id': r[0], 'candidate_id': r[1], 'candidate_name': r[2], 'email': r[3],
            'salary': float(r[4]) if r[4] else 0,
            'basic_pct': float(r[5]) if r[5] is not None else None,
            'hra_pct': float(r[6]) if r[6] is not None else None,
            'allowances_pct': float(r[7]) if r[7] is not None else None,
            'offer_date': r[8].isoformat() if r[8] else None, 'status': r[9],
            'accepted_at': r[10].isoformat() if r[10] else None, 'notes': r[11],
        } for r in rows]), 200

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
    try:
        lop_days = int(conn.execute(
            "SELECT COUNT(*) FROM attendance_days WHERE emp_id = ? AND status IN ('Absent', 'Half-day')",
            [emp_id],
        ).fetchone()[0] or 0)
    except Exception:
        lop_days = 0
    lop_adjustment = round(daily_salary * lop_days, 2)
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
    sid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO salary_structures (struct_id, emp_id, basic, hra, allowances, deductions, effective_from, effective_to) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 [sid, data['emp_id'], float(data['basic']), float(data.get('hra', 0)), float(data.get('allowances', 0)), float(data.get('deductions', 0)),
                  parse_date(data.get('effective_from'), datetime.now().date()),
                  parse_date(data.get('effective_to'))])
    conn.close()
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
    backend = os.getenv('APP_DB', 'duckdb').lower()
    is_pg = backend in ('postgres', 'postgresql', 'pg')
    schema = 'main'
    if is_pg:
        import db_backend
        schema = db_backend.app_schema()
    key = f"{backend}:{schema}"
    if key not in _PAYROLL_APPROVAL_IDENTITY_CACHE:
        if not is_pg:
            _PAYROLL_APPROVAL_IDENTITY_CACHE[key] = False
        else:
            import db_backend
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
        rid = gen_id()
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
                [gen_id(), rid, emp_id, gross, total_ded, net, pf, esi, pt],
            )
        conn.close()
    except Exception:
        conn.close()
        raise
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


@app.route('/api/v1/payslip/<int:run_id>/<emp_id>')
@app.route('/api/payslip/<int:run_id>/<emp_id>')
@login_required
def get_payslip(run_id, emp_id):
    if session.get('role') not in ('Admin', 'Finance') and session['emp_id'] != emp_id:
        return jsonify({'error': 'Forbidden'}), 403
    conn = get_db()
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
        if session.get('role') == 'Admin':
            rows = conn.execute("SELECT g.goal_id, g.emp_id, u.name, g.title, g.description, g.target_date, g.weight, g.rating, g.status, g.created_at FROM goals g JOIN users u ON g.emp_id = u.emp_id ORDER BY g.created_at DESC").fetchall()
        else:
            rows = conn.execute("SELECT g.goal_id, g.emp_id, u.name, g.title, g.description, g.target_date, g.weight, g.rating, g.status, g.created_at FROM goals g JOIN users u ON g.emp_id = u.emp_id WHERE g.emp_id = ? ORDER BY g.created_at DESC", [session['emp_id']]).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'title': r[3], 'description': r[4], 'target_date': r[5].isoformat() if r[5] else None, 'weight': r[6], 'rating': r[7], 'status': r[8], 'created_at': r[9].isoformat() if r[9] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('title'):
        return jsonify({'error': 'title required'}), 400
    gid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO goals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 [gid, data.get('emp_id', session['emp_id']), data['title'], data.get('description'),
                  parse_date(data.get('target_date')), data.get('weight', 1), None, 'Active', datetime.now()])
    conn.close()
    return jsonify({'message': 'Goal created', 'id': gid}), 201


@app.route('/api/v1/goals/<int:gid>/rate', methods=['PUT'])
@app.route('/api/goals/<int:gid>/rate', methods=['PUT'])
@admin_required
def rate_goal(gid):
    data = request.get_json(silent=True) or {}
    rating = data.get('rating')
    if not rating or rating < 1 or rating > 5:
        return jsonify({'error': 'rating must be 1-5'}), 400
    conn = get_db()
    conn.execute("UPDATE goals SET rating = ?, status = 'Completed' WHERE goal_id = ?", [rating, gid])
    conn.close()
    return jsonify({'message': 'Goal rated'}), 200


@app.route('/api/v1/goals/<int:gid>', methods=['PUT'])
@app.route('/api/goals/<int:gid>', methods=['PUT'])
@login_required
def update_goal(gid):
    data = request.get_json(silent=True) or {}
    conn = get_db()
    for field in ('title', 'description', 'target_date', 'weight', 'status'):
        if field in data:
            conn.execute(f"UPDATE goals SET {field} = ? WHERE goal_id = ?", [data[field], gid])
    conn.close()
    return jsonify({'message': 'Goal updated'}), 200


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
    if not data.get('emp_id') or not data.get('reviewer_id') or not data.get('review_period'):
        return jsonify({'error': 'emp_id, reviewer_id, review_period required'}), 400
    rid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO performance_reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 [rid, data['emp_id'], data['reviewer_id'], data['review_period'], None, None, 'Draft', datetime.now(), None])
    conn.close()
    return jsonify({'message': 'Review created', 'id': rid}), 201


@app.route('/api/v1/performance-reviews/<int:rid>/submit', methods=['PUT'])
@app.route('/api/performance-reviews/<int:rid>/submit', methods=['PUT'])
@login_required
def submit_review(rid):
    data = request.get_json(silent=True) or {}
    conn = get_db()
    conn.execute("UPDATE performance_reviews SET overall_rating = ?, comments = ?, status = 'Submitted', submitted_at = ? WHERE review_id = ?",
                 [data.get('rating'), data.get('comments'), datetime.now(), rid])
    conn.close()
    return jsonify({'message': 'Review submitted'}), 200


# ── 360 Feedback ──────────────────────────────────────────────────

@app.route('/api/v1/feedback-360', methods=['GET', 'POST'])
@app.route('/api/feedback-360', methods=['GET', 'POST'])
@login_required
def feedback_api():
    if request.method == 'GET':
        conn = get_db()
        if session.get('role') == 'Admin':
            rows = conn.execute("SELECT f.feedback_id, f.emp_id, u.name, f.reviewer_id, rev.name, f.category, f.rating, f.comment, f.submitted_at FROM feedback_360 f JOIN users u ON f.emp_id = u.emp_id JOIN users rev ON f.reviewer_id = rev.emp_id ORDER BY f.submitted_at DESC").fetchall()
        else:
            rows = conn.execute("SELECT f.feedback_id, f.emp_id, u.name, f.reviewer_id, rev.name, f.category, f.rating, f.comment, f.submitted_at FROM feedback_360 f JOIN users u ON f.emp_id = u.emp_id JOIN users rev ON f.reviewer_id = rev.emp_id WHERE f.emp_id = ? ORDER BY f.submitted_at DESC", [session['emp_id']]).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'reviewer_id': r[3], 'reviewer': r[4], 'category': r[5], 'rating': r[6], 'comment': r[7], 'submitted_at': r[8].isoformat() if r[8] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('emp_id') or not data.get('rating'):
        return jsonify({'error': 'emp_id and rating required'}), 400
    fid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO feedback_360 VALUES (?, ?, ?, ?, ?, ?, ?)",
                 [fid, data['emp_id'], session['emp_id'], data.get('category'), data['rating'], data.get('comment'), datetime.now()])
    conn.close()
    return jsonify({'message': 'Feedback submitted'}), 201


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
        if session.get('role') == 'Admin':
            rows = conn.execute("SELECT c.claim_id, c.emp_id, u.name, c.cat_id, e.name, c.amount, c.description, c.status, c.created_at FROM expense_claims c JOIN users u ON c.emp_id = u.emp_id JOIN expense_categories e ON c.cat_id = e.cat_id ORDER BY c.created_at DESC").fetchall()
        else:
            rows = conn.execute("SELECT c.claim_id, c.emp_id, u.name, c.cat_id, e.name, c.amount, c.description, c.status, c.created_at FROM expense_claims c JOIN users u ON c.emp_id = u.emp_id JOIN expense_categories e ON c.cat_id = e.cat_id WHERE c.emp_id = ? ORDER BY c.created_at DESC", [session['emp_id']]).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'cat_id': r[3], 'category': r[4], 'amount': float(r[5]), 'description': r[6], 'status': r[7], 'created_at': r[8].isoformat() if r[8] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('cat_id') or not data.get('amount'):
        return jsonify({'error': 'cat_id and amount required'}), 400
    cid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO expense_claims VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 [cid, data.get('emp_id', session['emp_id']), data['cat_id'], float(data['amount']), data.get('description'), data.get('receipt_path'), 'Pending', None, datetime.now()])
    conn.close()
    return jsonify({'message': 'Expense claimed', 'id': cid}), 201


@app.route('/api/v1/expenses/<int:eid>/status', methods=['PUT'])
@app.route('/api/expenses/<int:eid>/status', methods=['PUT'])
@admin_required
def update_expense_status(eid):
    data = request.get_json(silent=True) or {}
    status = data.get('status')
    if status not in ('Pending', 'Approved', 'Rejected', 'Paid'):
        return jsonify({'error': 'Invalid status'}), 400
    conn = get_db()
    conn.execute("UPDATE expense_claims SET status = ?, approved_by = ? WHERE claim_id = ?", [status, session['emp_id'], eid])
    conn.close()
    return jsonify({'message': f'Expense {status.lower()}'}), 200


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
        if session.get('role') == 'Admin':
            rows = conn.execute("SELECT t.ticket_id, t.emp_id, u.name, t.subject, t.category, t.priority, t.status, t.assigned_to, t.created_at, t.updated_at FROM tickets t JOIN users u ON t.emp_id = u.emp_id ORDER BY t.created_at DESC").fetchall()
        else:
            rows = conn.execute("SELECT t.ticket_id, t.emp_id, u.name, t.subject, t.category, t.priority, t.status, t.assigned_to, t.created_at, t.updated_at FROM tickets t JOIN users u ON t.emp_id = u.emp_id WHERE t.emp_id = ? ORDER BY t.created_at DESC", [session['emp_id']]).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'subject': r[3], 'category': r[4], 'priority': r[5], 'status': r[6], 'assigned_to': r[7], 'created_at': r[8].isoformat() if r[8] else None, 'updated_at': r[9].isoformat() if r[9] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('subject'):
        return jsonify({'error': 'subject required'}), 400
    tid = gen_id()
    conn = get_db()
    conn.execute(
        "INSERT INTO tickets (ticket_id, emp_id, subject, description, category, priority, status, assigned_to, created_at, updated_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [tid, session['emp_id'], data['subject'], data.get('description'), data.get('category'), data.get('priority', 'Medium'),
         'Open', None, datetime.now(), None, None])
    conn.close()
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
    if session.get('role') != 'Admin' and session['emp_id'] != row[1]:
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
    data = request.get_json(silent=True) or {}
    if not data.get('comment'):
        return jsonify({'error': 'comment required'}), 400
    conn = get_db()
    chk = conn.execute("SELECT 1 FROM tickets WHERE ticket_id = ?", [tid]).fetchone()
    if not chk:
        conn.close()
        return jsonify({'error': 'Ticket not found'}), 404
    cid = gen_id()
    conn.execute("INSERT INTO ticket_comments (comment_id, ticket_id, emp_id, comment, created_at) VALUES (?, ?, ?, ?, ?)", [cid, tid, session['emp_id'], data['comment'], datetime.now()])
    conn.execute("UPDATE tickets SET updated_at = ? WHERE ticket_id = ?", [datetime.now(), tid])
    conn.close()
    return jsonify({'message': 'Comment added', 'id': cid}), 201


@app.route('/api/v1/tickets/<int:tid>/status', methods=['PUT'])
@app.route('/api/tickets/<int:tid>/status', methods=['PUT'])
@login_required
def update_ticket_status(tid):
    data = request.get_json(silent=True) or {}
    status = data.get('status')
    if status not in ('Open', 'In Progress', 'Resolved', 'Closed'):
        return jsonify({'error': 'Invalid status'}), 400
    conn = get_db()
    now = datetime.now()
    resolved_at = now if status == 'Resolved' else None
    conn.execute("UPDATE tickets SET status = ?, updated_at = ?, resolved_at = ? WHERE ticket_id = ?", [status, now, resolved_at, tid])
    conn.close()
    return jsonify({'message': f'Status set to {status}'}), 200


@app.route('/api/v1/tickets/<int:tid>/assign', methods=['PUT'])
@app.route('/api/tickets/<int:tid>/assign', methods=['PUT'])
@admin_required
def assign_ticket(tid):
    data = request.get_json(silent=True) or {}
    conn = get_db()
    conn.execute("UPDATE tickets SET assigned_to = ?, updated_at = ? WHERE ticket_id = ?", [data.get('assigned_to'), datetime.now(), tid])
    conn.close()
    return jsonify({'message': 'Ticket assigned'}), 200


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
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    with open(filepath, 'wb') as handle:
        handle.write(content)
    fsize = len(content)
    conn = get_db()
    did = _next_generated_id(conn, 'documents', 'doc_id')
    conn.execute("INSERT INTO documents (doc_id, emp_id, name, category, file_path, file_size, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 [did, emp_id, safe_name, category, filename, fsize, datetime.now()])
    conn.close()
    audit_log(emp_id, 'DOCUMENT_UPLOAD', f'Uploaded document {did}', entity='documents', entity_id=did)
    return jsonify({'message': 'File uploaded', 'id': did, 'path': filename}), 201


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
    filepath = os.path.join(UPLOAD_FOLDER, os.path.basename(row[1]))
    if not os.path.exists(filepath):
        return jsonify({'error': 'File not found on disk'}), 404
    return send_file(filepath, as_attachment=True, download_name=row[1])


@app.route('/api/v1/documents/<int:did>', methods=['DELETE'])
@app.route('/api/documents/<int:did>', methods=['DELETE'])
@login_required
def delete_document(did):
    conn = get_db()
    row = conn.execute("SELECT emp_id, file_path FROM documents WHERE doc_id = ?", [did]).fetchone()
    actor = _lifecycle_actor()
    if not row or not _can_access_document(actor, row[0], write=True):
        conn.close()
        return jsonify({'error': 'Not found'}), 404
    conn.execute("DELETE FROM documents WHERE doc_id = ?", [did])
    conn.close()
    filepath = os.path.join(UPLOAD_FOLDER, os.path.basename(row[1]))
    if os.path.exists(filepath):
        os.remove(filepath)
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


def send_email(to, subject, body):
    if not SMTP_HOST:
        logger.info("Email disabled (SMTP_HOST not set) — would send to %s: %s", to, subject)
        return True
    try:
        msg = MIMEMultipart()
        msg['From'] = EMAIL_FROM
        msg['To'] = to
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'html'))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        logger.info("Email sent to %s: %s", to, subject)
        return True
    except Exception as e:
        logger.warning("Email failed to %s: %s", to, e)
        return False


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
    ok = send_email(to, subject, body)
    if ok:
        return jsonify({'message': 'Email sent'}), 200
    return jsonify({'warning': 'Email sending failed (SMTP may not be configured)'}), 200


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
    conn = get_db()
    row = conn.execute(
        "SELECT p.item_id, r.month, r.year, p.emp_id, u.name, u.department, u.designation, p.gross_salary, p.deductions_total, p.net_salary, p.pf, p.esi, p.pt FROM payroll_items p JOIN payroll_runs r ON p.run_id = r.run_id JOIN users u ON p.emp_id = u.emp_id WHERE p.run_id = ? AND p.emp_id = ?",
        [run_id, emp_id]
    ).fetchone()
    conn.close()
    if not row:
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

    conn = get_db()
    conn.execute("UPDATE payroll_items SET payslip_generated = 1 WHERE run_id = ? AND emp_id = ?", [run_id, emp_id])
    conn.close()

    return buf


@app.route('/api/v1/payroll-runs/<int:rid>/payslip-pdf/<emp_id>')
@app.route('/api/payroll-runs/<int:rid>/payslip-pdf/<emp_id>')
@login_required
def payslip_pdf(rid, emp_id):
    if session.get('role') not in ('Admin', 'Finance') and session['emp_id'] != emp_id:
        return jsonify({'error': 'Forbidden'}), 403
    conn = get_db()
    run = conn.execute("SELECT status FROM payroll_runs WHERE run_id = ?", [rid]).fetchone()
    conn.close()
    if not run:
        return jsonify({'error': 'Not found'}), 404
    if run[0] != 'Finalized':
        return jsonify({'error': 'Payslips are available only after payroll finalization'}), 409
    pdf = generate_payslip_pdf(rid, emp_id)
    if not pdf:
        return jsonify({'error': 'Not found'}), 404
    return send_file(pdf, mimetype='application/pdf', as_attachment=True, download_name=f'payslip_{emp_id}_{rid}.pdf')


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
        if session.get('role') == 'Admin':
            query = """SELECT l.leave_id, l.emp_id, u.name, l.leave_type, l.start_date, l.end_date,
                       l.reason, l.status, l.approved_by, l.created_at
                       FROM leave_requests l LEFT JOIN users u ON l.emp_id = u.emp_id"""
            params = []
            conditions = []
            if status_filter:
                conditions.append("l.status = ?")
                params.append(status_filter)
            if year_filter:
                conditions.append("l.year = ?")
                params.append(year_filter)
            if month_filter:
                conditions.append("CAST(strftime('%m', l.start_date) AS INTEGER) = ?")
                params.append(month_filter)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY l.created_at DESC"
        else:
            query = """SELECT l.leave_id, l.emp_id, u.name, l.leave_type, l.start_date, l.end_date,
                       l.reason, l.status, l.approved_by, l.created_at
                       FROM leave_requests l LEFT JOIN users u ON l.emp_id = u.emp_id
                       WHERE l.emp_id = ?"""
            params = [emp_id]
            if status_filter:
                query += " AND l.status = ?"
                params.append(status_filter)
            if year_filter:
                query += " AND l.year = ?"
                params.append(year_filter)
            if month_filter:
                query += " AND CAST(strftime('%m', l.start_date) AS INTEGER) = ?"
                params.append(month_filter)
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

    balance = conn.execute(
        "SELECT balance_id, total_days, used_days FROM leave_balance WHERE emp_id = ? AND leave_type = ? AND year = ?",
        [emp_id, lt, datetime.now().year]
    ).fetchone()
    if balance:
        requested = (ed - sd).days + 1
        remaining = balance[1] - balance[2]
        if requested > remaining:
            conn.close()
            return jsonify({'error': f'Insufficient balance. Remaining: {remaining} days'}), 400

    if conn.execute(
        "SELECT 1 FROM leave_requests WHERE emp_id = ? AND status IN ('Pending','Approved') AND start_date <= ? AND end_date >= ?",
        [emp_id, ed, sd]
    ).fetchone():
        conn.close()
        return jsonify({'error': 'Overlapping leave request already exists for these dates'}), 409

    leave_id = gen_id()
    conn.execute(
        "INSERT INTO leave_requests (leave_id, emp_id, leave_type, start_date, end_date, year, reason, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'Pending')",
        [leave_id, emp_id, lt, sd, ed, sd.year, data.get('reason', '')]
    )
    conn.close()
    audit_log(emp_id, 'LEAVE_APPLY', f'{lt} leave {sd} to {ed}', entity='leave_requests', entity_id=leave_id)
    add_notification(session['emp_id'], 'LEAVE_APPLIED', f'Your {lt} leave ({sd} to {ed}) has been submitted.', '/leaves')
    return jsonify({'message': 'Leave application submitted', 'leave_id': leave_id}), 201


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
                   l.reason, l.status, l.approved_by, l.created_at
                   FROM leave_requests l LEFT JOIN users u ON l.emp_id = u.emp_id
                   WHERE l.year = ? AND CAST(strftime('%m', l.start_date) AS INTEGER) = ?"""
        params = [year, month]
        if status_filter:
            query += " AND l.status = ?"
            params.append(status_filter)
        query += " ORDER BY l.start_date"
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    import io

    import pandas as pd
    data = [{
        'Employee ID': r[0], 'Employee Name': r[1] or r[0], 'Leave Type': r[2],
        'From': r[3].isoformat(), 'To': r[4].isoformat(), 'Days': (r[4] - r[3]).days + 1,
        'Reason': r[5] or '', 'Status': r[6], 'Approved By': r[7] or ''
    } for r in rows]

    buf = io.BytesIO()
    df = pd.DataFrame(data) if data else pd.DataFrame(columns=['Employee ID','Employee Name','Leave Type','From','To','Days','Reason','Status','Approved By'])
    with pd.ExcelWriter(buf, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Leaves')
    buf.seek(0)
    month_name = datetime(2000, month, 1).strftime('%B')
    return send_file(buf, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     download_name=f'leaves_{month_name}_{year}.xlsx', as_attachment=True)


@app.route('/api/v1/leaves/<int:leave_id>/approve', methods=['POST'])
@app.route('/api/leaves/<int:leave_id>/approve', methods=['POST'])
@admin_required
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
    row = conn.execute(
        "SELECT emp_id, leave_type, start_date, end_date, status FROM leave_requests WHERE leave_id = ?",
        [leave_id]
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Leave not found'}), 404
    if row[4] != 'Pending':
        conn.close()
        return jsonify({'error': 'Leave is not pending'}), 400

    days = (row[3] - row[2]).days + 1
    conn.execute(
        "UPDATE leave_requests SET status = 'Approved', approved_by = ?, updated_at = ? WHERE leave_id = ?",
        [session['emp_id'], datetime.now(), leave_id]
    )
    conn.execute(
        "UPDATE leave_balance SET used_days = used_days + ? WHERE emp_id = ? AND leave_type = ? AND year = ?",
        [days, row[0], row[1], row[2].year]
    )
    conn.close()
    audit_log(session['emp_id'], 'LEAVE_APPROVE', f'Leave {leave_id} approved', entity='leave_requests', entity_id=leave_id)
    add_notification(row[0], 'LEAVE_APPROVED', f'Your {row[1]} leave ({row[2]} to {row[3]}) has been approved.', '/leaves')
    return jsonify({'message': 'Leave approved'}), 200


@app.route('/api/v1/leaves/<int:leave_id>/reject', methods=['POST'])
@app.route('/api/leaves/<int:leave_id>/reject', methods=['POST'])
@admin_required
def reject_leave(leave_id):
    """Reject a leave request"""
    conn = get_db()
    row = conn.execute("SELECT emp_id, leave_type, start_date, end_date, status FROM leave_requests WHERE leave_id = ?", [leave_id]).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Not found'}), 404
    if row[4] != 'Pending':
        conn.close()
        return jsonify({'error': 'Leave is not pending'}), 400
    conn.execute(
        "UPDATE leave_requests SET status = 'Rejected', approved_by = ?, updated_at = ? WHERE leave_id = ?",
        [session['emp_id'], datetime.now(), leave_id]
    )
    conn.close()
    audit_log(session['emp_id'], 'LEAVE_REJECT', f'Leave {leave_id} rejected', entity='leave_requests', entity_id=leave_id)
    add_notification(row[0], 'LEAVE_REJECTED', f'Your {row[1]} leave ({row[2]} to {row[3]}) has been rejected.', '/leaves')
    return jsonify({'message': 'Leave rejected'}), 200


@app.route('/api/v1/leave-balance')
@app.route('/api/leave-balance')
@login_required
def leave_balance_api():
    """Get leave balance for current user"""
    emp_id = session['emp_id']
    year = datetime.now().year
    conn = get_db()
    rows = conn.execute(
        "SELECT leave_type, total_days, used_days FROM leave_balance WHERE emp_id = ? AND year = ?",
        [emp_id, year]
    ).fetchall()
    conn.close()
    return jsonify([{
        'leave_type': r[0], 'total_days': r[1],
        'used_days': r[2], 'remaining': r[1] - r[2]
    } for r in rows]), 200


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
    break_id = gen_id()
    now = datetime.now()
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    shift_date = _get_shift_date_for_dt(emp_id, now, conn)
    conn.execute(
        "INSERT INTO breaks (break_id, emp_id, break_type, start_time, break_date, status) VALUES (?, ?, ?, ?, ?, 'Active')",
        [break_id, emp_id, break_type, utc_now, shift_date]
    )
    conn.close()
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
        "UPDATE breaks SET end_time = ?, duration_minutes = ?, status = 'Completed' WHERE break_id = ?",
        [end_time, duration, break_id]
    )
    conn.close()
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
        if session.get('role') == 'Admin':
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
    if conn.execute(
        "SELECT 1 FROM break_approvals WHERE emp_id = ? AND break_type = ? AND break_date = ? AND status = 'Pending'",
        [emp_id, bt, _get_shift_date_for_dt(emp_id, datetime.now(), conn)]
    ).fetchone():
        conn.close()
        return jsonify({'error': 'Pending approval already exists for today'}), 409
    aid = gen_id()
    shift_date = _get_shift_date_for_dt(emp_id, datetime.now(), conn)
    conn.execute(
        "INSERT INTO break_approvals (approval_id, emp_id, break_type, break_date, reason, status) VALUES (?, ?, ?, ?, ?, 'Pending')",
        [aid, emp_id, bt, shift_date, data.get('reason', '')]
    )
    conn.close()
    return jsonify({'message': 'Lunch break approval requested', 'approval_id': aid}), 201


@app.route('/api/break-approvals/<int:aid>/approve', methods=['POST'])
@admin_required
def approve_break(aid):
    conn = get_db()
    row = conn.execute(
        "SELECT emp_id, break_type, break_date FROM break_approvals WHERE approval_id = ? AND status = 'Pending'",
        [aid]
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Approval request not found or already processed'}), 404
    conn.execute(
        "UPDATE break_approvals SET status = 'Approved', approved_by = ? WHERE approval_id = ?",
        [session['emp_id'], aid]
    )
    conn.close()
    return jsonify({'message': 'Break approved'}), 200


@app.route('/api/break-approvals/<int:aid>/reject', methods=['POST'])
@admin_required
def reject_break(aid):
    conn = get_db()
    conn.execute(
        "UPDATE break_approvals SET status = 'Rejected', approved_by = ? WHERE approval_id = ? AND status = 'Pending'",
        [session['emp_id'], aid]
    )
    conn.close()
    return jsonify({'message': 'Break rejected'}), 200


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

    shift_hours = 0
    if first_login and last_logout:
        shift_hours = round((last_logout - first_login).total_seconds() / 3600, 2)
    elif first_login and not last_logout:
        shift_hours = round((datetime.now() - first_login).total_seconds() / 3600, 2)

    productive_hours = max(0, shift_hours - total_break_minutes / 60)
    efficiency = round((productive_hours / shift_hours) * 100, 1) if shift_hours > 0 else 0

    return jsonify({
        'date': target_date.isoformat(),
        'shift_start': shift_start_dt.strftime('%H:%M'),
        'shift_end': shift_end_dt.strftime('%H:%M'),
        'first_login': first_login.strftime('%H:%M:%S') if first_login else None,
        'last_logout': last_logout.strftime('%H:%M:%S') if last_logout else None,
        'shift_hours': shift_hours,
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
    conn = get_db()
    info = conn.execute(
        "SELECT start_time FROM breaks WHERE break_id = ? AND status = 'Active'",
        [break_id]
    ).fetchone()
    if not info:
        conn.close()
        return jsonify({'error': 'Break not found or already ended'}), 404
    end_time = datetime.now(timezone.utc).replace(tzinfo=None)
    duration = int((end_time - info[0]).total_seconds() / 60)
    conn.execute(
        "UPDATE breaks SET end_time = ?, duration_minutes = ?, status = 'Completed' WHERE break_id = ?",
        [end_time, duration, break_id]
    )
    conn.close()
    return jsonify({'message': 'Break ended by admin', 'duration_minutes': duration}), 200


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
    """Manually trigger one outbox dispatch pass (CC-09)."""
    try:
        result = outbox.run_dispatch()
    except Exception as e:
        logger.warning('outbox dispatch failed: %s', e)
        return jsonify({'error': 'Outbox dispatch failed'}), 500
    return jsonify(result), 200


# ══════════════════════════════════════════════════════════════════════
#  USER MANAGEMENT
# ══════════════════════════════════════════════════════════════════════

@app.route('/admin/users')
@admin_required
def admin_users():
    return render_template('admin_users.html')


@app.route('/api/users', methods=['GET'])
@admin_required
def get_users():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    offset = (page - 1) * per_page
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
            f"SELECT emp_id, name, email, role, status, department, first_login, allow_login, allow_breaks FROM users{where_clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
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
            f"SELECT emp_id, name, email, role, status, department, first_login, allow_login, allow_breaks, shift_start, shift_end, weekly_off_pattern FROM users{where_clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
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
    conn.close()
    return jsonify({
        'total': total, 'page': page, 'per_page': per_page,
        'data': data
    }), 200


@app.route('/api/users', methods=['POST'])
@admin_required
@idempotent
def add_user():
    data = request.get_json(silent=True) or {}
    if not data.get('emp_id') or not data.get('name') or not data.get('email'):
        return jsonify({'error': 'Missing required fields'}), 400
    if '@' not in data.get('email', ''):
        return jsonify({'error': 'Invalid email'}), 400
    conn = get_db()
    if conn.execute("SELECT 1 FROM users WHERE emp_id = ?", [data['emp_id']]).fetchone():
        conn.close()
        return jsonify({'error': 'Employee ID already exists'}), 409
    pwd = data.get('password', 'pass123')
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
    send_email(data['email'], 'Your HRMS Account Credentials', creds_body)

    return jsonify({'message': 'User added', 'email_sent': True}), 201


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
    conn.execute(
        "UPDATE users SET name = ?, email = ?, role = ?, department = ?, status = ?, allow_login = ?, allow_breaks = ? WHERE emp_id = ?",
        [data.get('name'), data.get('email'), data.get('role'), data.get('department', ''),
         data.get('status', 'Active'), int(data.get('allow_login', 1)),
         int(data.get('allow_breaks', 1)), emp_id]
    )
    current_start, current_end = get_shift(emp_id, conn)
    if 'shift_start' in data or 'shift_end' in data or 'weekly_off_pattern' in data:
        set_shift(
            emp_id,
            data.get('shift_start', current_start),
            data.get('shift_end', current_end),
            conn=conn,
            weekly_off=data.get('weekly_off_pattern'),
        )
    conn.close()
    audit_log(session['emp_id'], 'USER_UPDATE', f'Updated user {emp_id}', entity='users', entity_id=emp_id)
    return jsonify({'message': 'User updated'}), 200


@app.route('/api/users/<emp_id>/block', methods=['POST'])
@admin_required
def block_user(emp_id):
    conn = get_db()
    conn.execute("UPDATE users SET status = 'Blocked' WHERE emp_id = ?", [emp_id])
    conn.close()
    audit_log(session['emp_id'], 'USER_BLOCK', f'Blocked user {emp_id}', entity='users', entity_id=emp_id)
    return jsonify({'message': 'User blocked'}), 200


@app.route('/api/users/<emp_id>/unblock', methods=['POST'])
@admin_required
def unblock_user(emp_id):
    conn = get_db()
    conn.execute("UPDATE users SET status = 'Active' WHERE emp_id = ?", [emp_id])
    conn.close()
    audit_log(session['emp_id'], 'USER_UNBLOCK', f'Unblocked user {emp_id}', entity='users', entity_id=emp_id)
    return jsonify({'message': 'User unblocked'}), 200


@app.route('/api/users/<emp_id>', methods=['DELETE'])
@admin_required
def delete_user(emp_id):
    if emp_id == session.get('emp_id'):
        return jsonify({'error': 'Cannot delete your own account'}), 400
    conn = get_db()
    user = conn.execute("SELECT name FROM users WHERE emp_id = ?", [emp_id]).fetchone()
    if not user:
        conn.close()
        return jsonify({'error': 'User not found'}), 404
    tables = [
        ('user_sessions', 'emp_id'), ('breaks', 'emp_id'), ('leave_requests', 'emp_id'),
        ('leave_balance', 'emp_id'), ('break_approvals', 'emp_id'), ('audit_log', 'emp_id'),
        ('notifications', 'emp_id'), ('password_reset_tokens', 'emp_id'),
        ('regularization_requests', 'emp_id'), ('attendance_days', 'emp_id'),
        ('holiday_optins', 'emp_id'), ('shift_assignments', 'emp_id'),
        ('onboarding_tasks', 'emp_id'),
        ('offboarding_tasks', 'emp_id'), ('exit_interviews', 'emp_id'),
        ('salary_structures', 'emp_id'), ('payroll_items', 'emp_id'),
        ('payroll_approvals', 'actor_emp_id'), ('payroll_runs', 'submitted_by'),
        ('payroll_runs', 'approved_by'),
        ('goals', 'emp_id'), ('performance_reviews', 'emp_id'),
        ('feedback_360', 'emp_id'), ('expense_claims', 'emp_id'),
        ('tickets', 'emp_id'), ('ticket_comments', 'emp_id'),
        ('assets', 'emp_id'), ('documents', 'emp_id'), ('dependents', 'emp_id'),
        ('interviews', 'interviewer'), ('interviews', 'emp_id'),
        ('offer_letters', 'emp_id'), ('offer_letters', 'candidate_id'),
    ]
    for table, col in tables:
        try:
            conn.execute(f"DELETE FROM {table} WHERE {col} = ?", [emp_id])
        except Exception:
            pass
    conn.execute("DELETE FROM users WHERE emp_id = ?", [emp_id])
    conn.close()
    audit_log(session['emp_id'], 'USER_DELETE', f'Deleted user {emp_id} ({user[0]})', entity='users', entity_id=emp_id)
    return jsonify({'message': f'User {emp_id} deleted permanently'}), 200


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
    try:
        conn = get_db()
        conn.execute("DELETE FROM password_reset_tokens WHERE expires_at < ?", [datetime.now()])
        conn.execute("DELETE FROM idempotency_keys WHERE expires_at < ?", [datetime.now()])  # CC-07
        conn.close()
        logger.info("Cleaned up expired password reset tokens and idempotency keys")
    except Exception as e:
        logger.warning("Cleanup failed: %s", e)


# ── Start scheduler (only in master gunicorn process) ──────────────────
if not STARTED:
    _is_gunicorn_master = os.getenv('SERVER_SOFTWARE', '').startswith('gunicorn') or os.getenv('GUNICORN_MASTER') == 'true'
    _is_dev = os.getenv('FLASK_DEBUG') == '1' or os.getenv('FLASK_ENV') != 'production'
    if _is_dev or _is_gunicorn_master or not os.getenv('SERVER_SOFTWARE'):
        try:
            attendance_hour = min(max(int(os.getenv('ATTENDANCE_JOB_HOUR', '2')), 0), 23)
        except (TypeError, ValueError):
            attendance_hour = 2
        scheduler.add_job(cleanup_expired_tokens, 'interval', hours=1)
        scheduler.add_job(outbox.run_dispatch, 'interval', seconds=60)
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
