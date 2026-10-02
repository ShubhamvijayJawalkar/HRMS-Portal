"""CC-09 transactional outbox.

Business writes that have downstream side effects (payroll finalisation,
offer issuance/acceptance, onboarding credentials) enqueue a ``pending``
``outbox_events`` row on the *same* connection, inside an explicit transaction,
so the
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
from contextlib import contextmanager
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 30


@contextmanager
def transaction():
    """Open a DB transaction.

    Yields a connection on which the map-me business change and its outbox
    event are atomic (CC-09): the transaction is committed on clean exit and
    rolled back on exception. PostgreSQL uses a dedicated non-autocommit
    connection; the DuckDB backend this used to also serve tracked explicit
    ``BEGIN``/``COMMIT`` and was removed at the Phase-6 decommission.
    """
    import db_backend
    with db_backend.transaction() as conn:
        yield conn


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
    from app import _is_public_target_schema, _next_generated_id, gen_id  # lazy
    event_id = (
        _next_generated_id(conn, 'outbox_events', 'event_id')
        if _is_public_target_schema() else gen_id()
    )
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


def _preference_category(ntype: str) -> str:
    """The FR-NOT-03 preference category for a type this handler writes directly.

    The outbox inserts into `notifications` rather than calling
    `app.add_notification`, so it used to hardcode the category. That is the second
    half of the taxonomy defect: the two writers of the same column disagreed, and a
    hardcoded string drifts the moment a category is renamed. Both handlers route
    through the one derivation now.
    """
    import notifications  # lazy: avoids a circular import at module load

    return notifications.category_for(ntype)


def _handle_payroll_finalized(conn, row) -> bool:
    """Post-payroll: notify every employee on the run with their net pay."""
    from app import _is_public_target_schema, _next_generated_id, gen_id  # lazy
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
                "VALUES (?, ?, 'Payroll', ?, ?, '/my-payslips', ?)",
                [(_next_generated_id(conn, 'notifications', 'notification_id') if _is_public_target_schema() else gen_id()),
                 emp_id, _preference_category('Payroll'),
                 f'Salary for run {run_id} credited: Rs.{float(net):,.2f}', now],
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
        return bool(send_email(email, subject, body))
    except Exception as exc:
        logger.warning('outbox offer.created email failed: %s', exc)
        return False


def _handle_offer_accepted(conn, row) -> bool:
    """Onboarding: notify HR/Admin that the guarded hire workflow started."""
    from app import _is_public_target_schema, _next_generated_id, gen_id  # lazy
    payload = _payload(row) or {}
    cid = payload.get('candidate_id')
    try:
        recipients = conn.execute(
            "SELECT emp_id FROM users WHERE role IN ('Admin', 'Super Admin', 'HR') OR department = 'HR' ORDER BY emp_id"
        ).fetchall()
        if not recipients:
            recipients = [('EMP001',)]
        for (recipient,) in recipients:
            conn.execute(
                "INSERT INTO notifications (notification_id, emp_id, type, category, message, related_link, created_at) "
                "VALUES (?, ?, 'Onboarding', ?, ?, '/onboarding', ?)",
                [(_next_generated_id(conn, 'notifications', 'notification_id') if _is_public_target_schema() else gen_id()),
                 recipient, _preference_category('Onboarding'),
                 f'Candidate {cid} accepted — onboarding workflow started', datetime.now()],
            )
        return True
    except Exception as exc:
        logger.warning('outbox offer.accepted handler failed: %s', exc)
        return False


def _handle_credentials_issued(conn, row) -> bool:
    """Email the one-time credential link after provisioning completes."""
    from app import _decrypt_lifecycle_secret, send_email  # lazy
    payload = _payload(row) or {}
    emp_id = payload.get('emp_id')
    encrypted_token = payload.get('reset_token_encrypted')
    if not emp_id or not encrypted_token:
        return False
    try:
        reset_token = _decrypt_lifecycle_secret(encrypted_token)
        user = conn.execute("SELECT email, name FROM users WHERE emp_id = ?", [emp_id]).fetchone()
        if not user:
            return False
        return bool(send_email(
            user[0], 'Your HRMS login is ready',
            f"Hi {user[1]}, your HRMS account is active. Use this one-time password reset token: "
            f"{reset_token} (valid for 24 hours).",
        ))
    except Exception as exc:
        logger.warning('outbox credentials.issued handler failed: %s', exc)
        return False


def _handle_candidate_hired(conn, row) -> bool:
    """Reconcile the hire event idempotently after an at-least-once replay."""
    from app import ONBOARDING_REQUIRED_DOCS, _next_generated_id  # lazy
    payload = _payload(row) or {}
    workflow_id = payload.get('workflow_id')
    if not workflow_id:
        return True
    try:
        exists = conn.execute(
            "SELECT 1 FROM onboarding_workflow WHERE workflow_id = ?", [workflow_id]
        ).fetchone()
        if not exists:
            return False
        for doc_type in ONBOARDING_REQUIRED_DOCS:
            present = conn.execute(
                "SELECT 1 FROM onboarding_checklist WHERE workflow_id = ? AND doc_type = ?",
                [workflow_id, doc_type],
            ).fetchone()
            if not present:
                conn.execute(
                    "INSERT INTO onboarding_checklist (item_id, workflow_id, doc_type, status) "
                    "VALUES (?, ?, ?, 'Pending')",
                    [_next_generated_id(conn, 'onboarding_checklist', 'item_id'), workflow_id, doc_type],
                )
        return True
    except Exception as exc:
        logger.warning('outbox candidate.hired reconciliation failed: %s', exc)
        return False


def _handle_password_reset(conn, row) -> bool:
    """Email the password-reset link (FR-AUTH-08/09).

    The reason this handler exists is that ``POST /api/forgot-password`` must not
    return the token. That endpoint used to answer 404 "No matching user found" for
    an unknown account and 200 *with the token* for a real one — the single most
    direct enumeration oracle in the application, and recorded in the traceability
    matrix as IMPLEMENTED. Moving delivery into the outbox is what makes the honest
    response possible: the SRS puts the link "queued via outbox", and once it is
    queued the request has nothing left to disclose.

    The link itself was built at enqueue time (``reset_url``, in the request
    context) because the dispatcher has no host to build one from. The token
    travels **encrypted** in the payload (``reset_token_encrypted``), matching
    ``credentials.issued`` above, and that matters: the token is hashed in
    ``password_reset_tokens``, so without the encrypted copy there would be no way to
    mail a link the reset endpoint can verify. A database read still cannot mint a
    reset — the digest column cannot be reversed, and the payload cannot be read
    without the app's Fernet key.
    """
    from app import send_email  # lazy
    payload = _payload(row) or {}
    emp_id = payload.get('emp_id')
    reset_url = payload.get('reset_url')
    if not emp_id or not reset_url:
        return False
    try:
        user = conn.execute(
            "SELECT email, name FROM users WHERE emp_id = ?", [emp_id]
        ).fetchone()
        if not user or not user[0]:
            return False
        minutes = int(payload.get('expires_in_minutes') or 60)
        return bool(send_email(
            user[0],
            'Reset your HRMS password',
            f"Hi {user[1]},<br><br>Use this link to choose a new HRMS password: "
            f"<a href='{reset_url}'>{reset_url}</a><br><br>"
            f"It is valid for {minutes} minutes and can be used once. "
            "If you did not request this, you can ignore this message — nothing "
            "changes until the link is used.",
        ))
    except Exception as exc:
        logger.warning('outbox password.reset handler failed: %s', exc)
        return False


def _handle_notification_email(conn, row) -> bool:
    """Deliver a queued notification email — FR-NOT-01 and FR-NOT-03.

    Two PARTIAL rows meet here, and it is worth being explicit about why one handler
    closes both:

    * **FR-NOT-01** was PARTIAL because delivery was a direct ``send_email`` on the
      request thread. That is an availability defect, not a style preference: SMTP is
      a network call to a third party, and the old code had no timeout at all, so a
      slow or hanging provider held a worker for as long as it liked. Three call sites
      did this — the lockout notice, the admin "send an email" endpoint, and the
      welcome mail on user creation.
    * **FR-NOT-03** was PARTIAL because the per-category ``email`` preference was
      stored and reported but **nothing consumed it**: the column was a switch with no
      circuit behind it. This handler is the consumer, and it reads the preference
      before sending.

    The preference is read **here**, at delivery time, rather than at enqueue time. A
    queued event must not carry a decision made when it was written: an employee who
    mutes a category after the event was queued should not receive that mail, and
    reading at dispatch makes that automatic. The flip side is recorded honestly —
    an event queued while the category was enabled still sends if the preference is
    turned off before dispatch, because it was legitimately queued.

    ``force`` exists for the one case where the message is not a notification: the
    admin compose endpoint, which is an explicit instruction to send mail and must not
    be silently suppressed by a preference. It is audited separately either way.
    """
    from app import send_email  # lazy
    payload = _payload(row) or {}
    to = payload.get('to')
    subject = payload.get('subject')
    body = payload.get('body')
    if not to or subject is None:
        logger.warning('outbox notification.email: incomplete payload')
        return False

    if not payload.get('force'):
        emp_id = payload.get('emp_id')
        if emp_id:
            try:
                from app import _notification_email_wanted  # lazy

                if not _notification_email_wanted(conn, emp_id, payload.get('category')):
                    # Suppressed by preference. **Handled, not failed** — returning
                    # True retires the event as delivered, because "we decided not to
                    # send" is the outcome we wanted. Retrying would mail it anyway on
                    # a later dispatch, which would be the opposite of the preference.
                    logger.info(
                        'outbox notification.email: suppressed by preference for %s (%s)',
                        emp_id, payload.get('category'),
                    )
                    return True
            except Exception as exc:
                # Failing *closed* here would drop a legitimate notification because a
                # preference lookup broke. Failing open would send something the
                # employee asked not to receive. The honest choice is to let the send
                # proceed and make the failure visible in the log, because an unwanted
                # email is recoverable and a silently dropped account-security notice
                # is not.
                logger.warning(
                    'outbox notification.email: preference lookup failed, sending anyway '
                    '(%s)', exc,
                )

    try:
        return bool(send_email(to, subject, body or ''))
    except Exception as exc:
        logger.warning('outbox notification.email failed: %s', exc)
        return False


HANDLERS = {
    'payroll.finalized': _handle_payroll_finalized,
    'offer.created': _handle_offer_created,
    'offer.accepted': _handle_offer_accepted,
    'candidate.hired': _handle_candidate_hired,
    'credentials.issued': _handle_credentials_issued,
    'password.reset': _handle_password_reset,
    'notification.email': _handle_notification_email,
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
    import db_backend
    conn = db_backend.connect()
    try:
        return dispatch_once(conn, limit=limit)
    finally:
        conn.close()
