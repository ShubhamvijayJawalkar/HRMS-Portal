"""CC-09 transactional outbox.

Business writes that have downstream side effects (payroll finalisation,
offer-letter issuance/acceptance) enqueue a ``pending`` ``outbox_events``
row on the *same* connection, inside an explicit transaction, so the
business change and its event commit atomically (see ``outbox.transaction``).
A dispatcher later reads due ``pending`` events, applies the registered
handler, and marks them ``delivered`` — or, on failure, advances ``attempts``
with exponential backoff and finally ``dead_letter`` after ``MAX_ATTEMPTS``.

Backend notes
-------------
* ``outbox_events`` is created by ``app.init_db`` on every backend
  (``CREATE TABLE IF NOT EXISTS``) — on the v2.0 ``public`` schema it is the
  identity-keyed infrastructure table already defined in
  ``db/postgres_schema.sql``.
* Enqueues are best-effort: when the table is unavailable the insert is
  skipped so the pattern degrades gracefully on schemas that pre-date it.
* Handlers are designed to be side-effect-idempotent; the delivered-flag
  update is best-effort atomic (at-least-once semantics after a crash).
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 30


def _is_postgres() -> bool:
    return os.getenv('APP_DB', 'duckdb').lower() in ('postgres', 'postgresql', 'pg')


def _db_file() -> str:
    return os.getenv('DB_FILE', 'hrms.duckdb')


@contextmanager
def transaction():
    """Open a DB transaction on whichever backend is configured.

    Yields a connection on which the map-me business change and its outbox
    event are atomic (CC-09): the transaction is committed on clean exit and
    rolled back on exception. DuckDB tracks explicit ``BEGIN``/``COMMIT``;
    PostgreSQL uses a dedicated non-autocommit connection.
    """
    if _is_postgres():
        import db_backend
        with db_backend.transaction() as conn:
            yield conn
        return
    import duckdb
    conn = duckdb.connect(_db_file())
    conn.execute("BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _table_exists(conn) -> bool:
    """True when the target schema carries an outbox_events table."""
    try:
        conn.execute("SELECT 1 FROM outbox_events LIMIT 1").fetchone()
        return True
    except Exception:
        return False


def enqueue(conn, event_type: str, aggregate=None, aggregate_id=None, payload=None):
    """Insert a ``pending`` outbox event on ``conn`` (caller's transaction).

    Returns the event_id, or ``None`` when the outbox table is unavailable
    (the pattern is a strict no-op on schemas without it).
    """
    if not _table_exists(conn):
        return None
    from app import gen_id  # lazy: never runs at import time
    event_id = gen_id()
    now = datetime.now()
    payload_json = json.dumps(payload) if payload is not None else None
    conn.execute(
        "INSERT INTO outbox_events (event_id, event_type, aggregate, aggregate_id, payload, status, next_attempt_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
        [event_id, event_type, aggregate, aggregate_id, payload_json, now, now],
    )
    return event_id


def _payload(row) -> dict | None:
    """Decode the payload cell: psycopg decodes JSONB to dict; the TEXT
    storage used elsewhere comes back as a JSON string."""
    raw = row[4]
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _backoff_seconds(attempts: int) -> int:
    return BACKOFF_BASE_SECONDS * (2 ** (attempts - 1))


def _handle_payroll_finalized(conn, row) -> bool:
    """Post-payroll: notify every employee on the run with their net pay."""
    from app import gen_id  # lazy
    payload = _payload(row) or {}
    run_id = int(payload.get('run_id', row[3] or 0))
    try:
        employees = conn.execute(
            "SELECT p.emp_id, u.name, p.net_salary FROM payroll_items p "
            "JOIN users u ON p.emp_id = u.emp_id WHERE p.run_id = ?",
            [run_id],
        ).fetchall()
        now = datetime.now()
        for i, (emp_id, name, net) in enumerate(employees):
            conn.execute(
                "INSERT INTO notifications (notification_id, emp_id, type, category, message, related_link, created_at) "
                "VALUES (?, ?, 'Payroll', 'Payroll', ?, '/my-payslips', ?)",
                [gen_id(), emp_id, f'Salary for run {run_id} credited: Rs.{float(net):,.2f}', now],
            )
        return True
    except Exception as exc:
        logger.warning('outbox payroll.finalized handler failed: %s', exc)
        return False


def _handle_offer_created(conn, row) -> bool:
    """ATS: email the candidate their offer letter."""
    from app import send_email  # lazy
    payload = _payload(row) or {}
    email = payload.get('email')
    if not email:
        logger.warning('outbox offer.created: no candidate email in payload')
        return False
    subject = 'Your HRMS Offer Letter'
    body = (
        f"Hi {payload.get('name', 'there')}, congratulations! Your offer letter "
        f"(salary Rs.{payload.get('salary', 0):,}) is ready. Please review and respond."
    )
    try:
        send_email(email, subject, body)
        return True
    except Exception as exc:
        logger.warning('outbox offer.created email failed: %s', exc)
        return False


def _handle_offer_accepted(conn, row) -> bool:
    """Onboarding: notify the admin team to start the hire workflow."""
    from app import gen_id  # lazy
    payload = _payload(row) or {}
    cid = payload.get('candidate_id')
    try:
        conn.execute(
            "INSERT INTO notifications (notification_id, emp_id, type, category, message, related_link, created_at) "
            "VALUES (?, 'EMP001', 'Onboarding', 'Onboarding', ?, '/onboarding', ?)",
            [gen_id(), f'Candidate {cid} accepted — start onboarding', datetime.now()],
        )
        return True
    except Exception as exc:
        logger.warning('outbox offer.accepted handler failed: %s', exc)
        return False


HANDLERS = {
    'payroll.finalized': _handle_payroll_finalized,
    'offer.created': _handle_offer_created,
    'offer.accepted': _handle_offer_accepted,
}


def _due_rows(conn, limit: int, now) -> list:
    try:
        return conn.execute(
            "SELECT event_id, event_type, aggregate, aggregate_id, payload, attempts "
            "FROM outbox_events WHERE status = 'pending' AND next_attempt_at <= ? "
            "ORDER BY event_id LIMIT ?",
            [now, limit],
        ).fetchall()
    except Exception as exc:
        logger.warning('outbox due-query failed: %s', exc)
        return []


def dispatch_once(conn, limit: int = 20, now=None) -> dict:
    """Process up to ``limit`` due pending events on ``conn``.

    Returns ``{'dispatched', 'delivered', 'failed', 'dead_lettered'}``.
    """
    stats = {'dispatched': 0, 'delivered': 0, 'failed': 0, 'dead_lettered': 0}
    if not _table_exists(conn):
        return stats
    now = now or datetime.now()
    for row in _due_rows(conn, limit, now):
        event_id, event_type, attempts = row[0], row[1], row[5]
        stats['dispatched'] += 1
        ok = False
        handler = HANDLERS.get(event_type)
        if handler is None:
            logger.warning('outbox: no handler for %s (event %s)', event_type, event_id)
        else:
            try:
                ok = bool(handler(conn, row))
            except Exception as exc:
                logger.warning('outbox handler %s raised: %s', event_type, exc)
        if ok:
            conn.execute(
                "UPDATE outbox_events SET status = 'delivered', delivered_at = ? WHERE event_id = ?",
                [datetime.now(), event_id],
            )
            stats['delivered'] += 1
            continue
        attempts += 1
        if attempts >= MAX_ATTEMPTS:
            conn.execute(
                "UPDATE outbox_events SET status = 'dead_letter', attempts = ? WHERE event_id = ?",
                [attempts, event_id],
            )
            stats['dead_lettered'] += 1
        else:
            conn.execute(
                "UPDATE outbox_events SET status = 'pending', attempts = ?, next_attempt_at = ? WHERE event_id = ?",
                [attempts, datetime.now() + timedelta(seconds=_backoff_seconds(attempts)), event_id],
            )
            stats['failed'] += 1
    return stats


def run_dispatch(limit: int = 50) -> dict:
    """Standalone dispatch entry (scheduler job / admin endpoint / CLI).

    Opens its own connection on the configured backend and closes it.
    """
    if _is_postgres():
        import db_backend
        conn = db_backend.connect()
    else:
        import duckdb
        conn = duckdb.connect(_db_file())
    try:
        return dispatch_once(conn, limit=limit)
    finally:
        conn.close()
