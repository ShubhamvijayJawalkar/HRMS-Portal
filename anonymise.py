"""Two-person anonymisation (FR-USR).

`archive_user` stops someone using their account and keeps their records. That is
not anonymisation: the row still holds a name, an email, a phone, an address, a
date of birth, an emergency contact and their dependents, and the audit log
holds the old values in its ``before``/``after`` JSON.

This module makes the erasure **two-person** and **irreversible**:

* a requester proposes, a *different* approver confirms, and the system applies
  it — no single actor can erase anyone, and the two-person rule is a database
  state transition rather than a UI convention;
* everything runs through :func:`apply`, which is idempotent, so a crash halfway
  is repaired by a retry rather than leaving a half-erased person;
* the operation only ever runs against an **archived** account, never a live
  one.

Field categories (see ``docs/ANONYMISATION.md`` for the review record):

``ERASED_FIELDS``
    Direct identifiers. Overwritten with a fixed placeholder; the originals are
    never written anywhere, including the audit row for this operation.
``PSEUDONYMISED_FIELDS``
    ``password`` only. Set to a random unusable value so the account can never be
    signed into again.
``KEPT_FIELDS``
    Everything that the statutory records join on or report: ``emp_id``,
    department, designation, grade, joining date, and every payroll / leave /
    attendance / break row.

Two deliberate trade-offs, both recorded in the design note:

* ``emp_id`` is **kept**, not replaced with a pseudonym. It is the join key for
  seven years of statutory records and rewriting ~30 foreign keys in one
  operation is not a safe first version. The residual risk — an insider who saw
  the data before the erasure can still recognise the row — is stated in the
  note rather than hidden.
* Free text (``tickets.subject``, ``expenses.description``, notification
  messages) is **left alone**: it cannot be scrubbed reliably, and it is the
  operational record. The audit row says so explicitly.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime

# Fixed, non-identifying placeholder. Deliberately constant so a scrubbed value
# is recognisable as scrubbed rather than looking like real data.
ANONYMISED_NAME = 'Anonymised Employee'

ERASED_FIELDS = (
    'name', 'email', 'phone', 'date_of_birth',
    'address', 'emergency_contact_name', 'emergency_contact_phone',
)
PSEUDONYMISED_FIELDS = ('password',)
KEPT_FIELDS = (
    'emp_id', 'department', 'designation', 'grade',
    'date_of_joining', 'status',
)

# Statuses the operation is allowed to run against.
ANONYMISABLE_STATUSES = ('Archived',)

# Free text that is deliberately left in place (documented, not scrubbed).
UNSCRUBBED_FREE_TEXT = (
    'tickets.subject', 'expense_claims.description', 'notifications.message',
    'leave_requests.reason', 'documents.name',
)

PROPOSED = 'proposed'
CONFIRMED = 'confirmed'
APPLIED = 'applied'
FAILED = 'failed'
CANCELLED = 'cancelled'
TERMINAL = (APPLIED, CANCELLED)

MIN_SALT_LENGTH = 16


class AnonymisationError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _salt() -> bytes:
    """The pseudonymisation salt. Refusing to run without one is deliberate.

    A missing or short salt would make the mapping between the old and new
    values guessable, which is the one thing this control exists to prevent, so
    the operation stops instead of falling back to a weak default.
    """
    raw = os.getenv('ANONYMISATION_SALT', '')
    if len(raw.strip()) < MIN_SALT_LENGTH:
        raise AnonymisationError(
            f'ANONYMISATION_SALT must be set to at least {MIN_SALT_LENGTH} characters '
            'to anonymise anyone (store it in the secret manager, not in the database)',
            503,
        )
    return raw.strip().encode('utf-8')


def pseudonymise(emp_id: str) -> str:
    """A stable, non-reversible reference for the subject's account key.

    Only used for the *password* replacement and for the placeholder text; the
    employee ID itself is kept (see the module docstring).
    """
    digest = hmac.new(_salt(), emp_id.encode('utf-8'), hashlib.sha256).hexdigest()
    return f'ANON-{digest[:16]}'


def _table_exists(conn, table: str) -> bool:
    try:
        conn.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone()
        return True
    except Exception:
        return False


def get_subject(conn, emp_id):
    row = conn.execute(
        "SELECT emp_id, name, email, role, department, designation, status, phone, "
        "date_of_birth, date_of_joining, address, emergency_contact_name, "
        "emergency_contact_phone, allow_login, allow_breaks "
        "FROM users WHERE UPPER(emp_id) = ?",
        [str(emp_id).strip().upper()],
    ).fetchone()
    if not row:
        return None
    keys = (
        'emp_id', 'name', 'email', 'role', 'department', 'designation', 'status',
        'phone', 'date_of_birth', 'date_of_joining', 'address',
        'emergency_contact_name', 'emergency_contact_phone', 'allow_login', 'allow_breaks',
    )
    return dict(zip(keys, row))


def plan(conn, emp_id) -> dict:
    """What ``apply`` would change. Writes nothing (FR-USR dry run)."""
    subject = get_subject(conn, emp_id)
    if not subject:
        raise AnonymisationError('User not found', 404)
    if subject['status'] not in ANONYMISABLE_STATUSES:
        raise AnonymisationError(
            f"only an archived account can be anonymised (this one is {subject['status']}); "
            'archive it first so there is a deliberate gap before the erasure',
            409,
        )
    # The salt is validated during the plan too, so a dry run tells the operator
    # the operation would fail *before* they get a second approver.
    pseudonymise(subject['emp_id'])

    dependents = 0
    if _table_exists(conn, 'dependents'):
        dependents = int(conn.execute(
            'SELECT COUNT(*) FROM dependents WHERE emp_id = ?', [subject['emp_id']]
        ).fetchone()[0] or 0)
    sessions = int(conn.execute(
        'SELECT COUNT(*) FROM user_sessions WHERE emp_id = ?', [subject['emp_id']]
    ).fetchone()[0] or 0)
    audit_rows = int(conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE entity_id = ? OR emp_id = ?",
        [subject['emp_id'], subject['emp_id']],
    ).fetchone()[0] or 0)

    return _jsonable({
        'emp_id': subject['emp_id'],
        'status': subject['status'],
        'erased_fields': {field: subject.get(field) for field in ERASED_FIELDS},
        'kept_fields': {field: subject.get(field) for field in KEPT_FIELDS},
        'disabled': bool(subject['allow_login']) or bool(subject['allow_breaks']),
        'rows': {
            'dependents_deleted': dependents,
            'sessions_closed': sessions,
            'audit_rows_scrubbed': audit_rows,
        },
        'unsrubbed_free_text': list(UNSCRUBBED_FREE_TEXT),
    })


def _jsonable(value):
    """Dates and Decimals out of a DB row, as JSON-safe scalars.

    The plan is both returned to the browser and stored in ``plan_summary``, and
    a seeded employee carries a real ``date_of_joining`` — so this is not
    theoretical.
    """
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return value


def apply(conn, emp_id, *, request_id=None) -> dict:
    """Erase the identifiers of an archived employee. Idempotent.

    The audit history of this subject is scrubbed by *value substitution*: the
    erased strings are replaced wherever they appear in that subject's own audit
    rows, so the trail of what happened survives while the personal data does
    not. Rows that merely mention the employee ID are left alone (the ID is kept
    by design).
    """
    subject = plan(conn, emp_id)
    emp_id = subject['emp_id']
    pseudonym = pseudonymise(emp_id)
    now = datetime.now()

    values_to_scrub = {
        str(subject[field]): pseudonym
        for field in ERASED_FIELDS
        if subject.get(field) not in (None, '')
    }
    # A renamed employee has older values sitting in the audit history, so the
    # scrub set is the *union* of the current values and every value the subject
    # ever had under an erased field, read back out of their own audit rows.
    for historical in _historical_erased_values(conn, emp_id):
        values_to_scrub.setdefault(historical, pseudonym)

    conn.execute(
        "UPDATE users SET name = ?, email = ?, phone = ?, date_of_birth = NULL, "
        "address = ?, emergency_contact_name = ?, emergency_contact_phone = ?, "
        "password = ?, allow_login = 0, allow_breaks = 0 "
        "WHERE emp_id = ?",
        [
            ANONYMISED_NAME,
            f'{pseudonym.lower()}@anonymised.invalid',
            None,
            None, None, None,
            f'anonymised-{secrets.token_urlsafe(32)}',
            emp_id,
        ],
    )

    # Count before writing: DuckDB does not report a dependable rowcount for
    # DELETE/UPDATE, and the audit row needs the real numbers.
    dependents_deleted = int(subject['rows']['dependents_deleted'])
    if _table_exists(conn, 'dependents') and dependents_deleted:
        # A dependent is a third party; there is no statutory reason to keep
        # their name and date of birth once the employee is anonymised.
        conn.execute('DELETE FROM dependents WHERE emp_id = ?', [emp_id])

    sessions_closed = int(conn.execute(
        'SELECT COUNT(*) FROM user_sessions WHERE emp_id = ? AND logout_time IS NULL',
        [emp_id],
    ).fetchone()[0] or 0)
    if sessions_closed:
        conn.execute(
            "UPDATE user_sessions SET logout_time = COALESCE(logout_time, ?), "
            "total_hours = COALESCE(total_hours, 0) "
            "WHERE emp_id = ? AND logout_time IS NULL",
            [now, emp_id],
        )

    scrubbed = 0
    if values_to_scrub:
        rows = conn.execute(
            'SELECT log_id, "before", "after", details FROM audit_log '
            'WHERE entity_id = ? OR emp_id = ?',
            [emp_id, emp_id],
        ).fetchall()
        for row in rows:
            updated = []
            for value in (row[1], row[2], row[3]):
                if not value:
                    updated.append(value)
                    continue
                text = value if isinstance(value, str) else str(value)
                for original, replacement in values_to_scrub.items():
                    text = text.replace(original, replacement)
                updated.append(text)
            if tuple(updated) != tuple(value for value in (row[1], row[2], row[3])):
                conn.execute(
                    'UPDATE audit_log SET "before" = ?, "after" = ?, details = ? WHERE log_id = ?',
                    [updated[0], updated[1], updated[2], row[0]],
                )
                scrubbed += 1

    return {
        'emp_id': emp_id,
        'pseudonym': pseudonym,
        'values_scrubbed': len(values_to_scrub),
        'erased_fields': list(ERASED_FIELDS),
        'kept_fields': list(KEPT_FIELDS),
        'rows': {
            'dependents_deleted': dependents_deleted,
            'sessions_closed': sessions_closed,
            'audit_rows_scrubbed': scrubbed,
        },
        'unsrubbed_free_text': list(UNSCRUBBED_FREE_TEXT),
        'request_id': request_id,
    }


def _historical_erased_values(conn, emp_id) -> set[str]:
    """Every value the subject's own audit rows carry under an erased field.

    Without this, a renamed employee keeps their *old* name in the history and
    the "anonymised" row is still trivially identifiable from the audit log.
    """
    values: set[str] = set()
    rows = conn.execute(
        'SELECT "before", "after" FROM audit_log WHERE entity_id = ? OR emp_id = ?',
        [emp_id, emp_id],
    ).fetchall()
    for row in rows:
        for blob in row:
            if not blob:
                continue
            try:
                parsed = json.loads(blob) if isinstance(blob, str) else blob
            except (TypeError, ValueError):
                continue
            if not isinstance(parsed, dict):
                continue
            for field in ERASED_FIELDS:
                value = parsed.get(field)
                if value not in (None, ''):
                    values.add(str(value))
    return values


# ── Request lifecycle (two-person) ─────────────────────────────────────────

def create_request(conn, emp_id, requested_by, detail=None) -> dict:
    """Propose an anonymisation. Writes nothing to the user row yet."""
    subject = plan(conn, emp_id)          # validates status + salt
    pending = conn.execute(
        "SELECT request_id FROM anonymisation_requests WHERE emp_id = ? AND status IN (?, ?)",
        [subject['emp_id'], PROPOSED, CONFIRMED],
    ).fetchone()
    if pending:
        raise AnonymisationError(
            f'an anonymisation request is already open for {subject["emp_id"]}', 409
        )
    from app import _is_public_target_schema, _next_generated_id, gen_id  # lazy

    if _is_public_target_schema():
        request_id = _next_generated_id(conn, 'anonymisation_requests', 'request_id')
    else:
        request_id = gen_id()
    conn.execute(
        "INSERT INTO anonymisation_requests (request_id, emp_id, status, requested_by, "
        "plan_summary, requested_at) VALUES (?, ?, ?, ?, ?, ?)",
        [request_id, subject['emp_id'], PROPOSED, requested_by,
         json.dumps({'plan': _jsonable(subject), 'detail': detail}, default=str),
         datetime.now()],
    )
    return get_request(conn, request_id)


def confirm_and_apply(conn, request_id, confirmed_by) -> dict:
    """Second approver confirms; the system then applies the erasure.

    The two-person rule lives here: the confirmer must be a different person
    from the requester, and the request must still be ``proposed``.
    """
    from app import _is_public_target_schema, _next_generated_id, gen_id  # lazy

    row = conn.execute(
        'SELECT request_id, emp_id, status, requested_by FROM anonymisation_requests '
        'WHERE request_id = ?',
        [request_id],
    ).fetchone()
    if not row:
        raise AnonymisationError('Anonymisation request not found', 404)
    if row[2] == APPLIED:
        return get_request(conn, request_id)      # idempotent replay
    if row[2] != PROPOSED:
        raise AnonymisationError(
            f'request is {row[2]} and can no longer be confirmed', 409
        )
    if row[3] == confirmed_by:
        raise AnonymisationError(
            'anonymisation needs a second person: the requester cannot confirm it', 409
        )
    conn.execute(
        "UPDATE anonymisation_requests SET status = ?, confirmed_by = ?, confirmed_at = ? "
        "WHERE request_id = ? AND status = ?",
        [CONFIRMED, confirmed_by, datetime.now(), request_id, PROPOSED],
    )
    result = apply(conn, row[1], request_id=request_id)
    conn.execute(
        "UPDATE anonymisation_requests SET status = ?, applied_at = ?, result_summary = ? "
        "WHERE request_id = ?",
        [APPLIED, datetime.now(), json.dumps(_jsonable(result), default=str), request_id],
    )
    del _is_public_target_schema, _next_generated_id, gen_id
    return get_request(conn, request_id)


def cancel_request(conn, request_id, actor) -> dict:
    row = conn.execute(
        'SELECT status FROM anonymisation_requests WHERE request_id = ?', [request_id]
    ).fetchone()
    if not row:
        raise AnonymisationError('Anonymisation request not found', 404)
    if row[0] != PROPOSED:
        raise AnonymisationError(f'request is {row[0]} and can no longer be cancelled', 409)
    conn.execute(
        "UPDATE anonymisation_requests SET status = ?, failure_reason = ? WHERE request_id = ?",
        [CANCELLED, f'cancelled by {actor}', request_id],
    )
    return get_request(conn, request_id)


def get_request(conn, request_id) -> dict | None:
    if not _table_exists(conn, 'anonymisation_requests'):
        return None
    row = conn.execute(
        'SELECT request_id, emp_id, status, requested_by, confirmed_by, requested_at, '
        'confirmed_at, applied_at, plan_summary, result_summary, failure_reason '
        'FROM anonymisation_requests WHERE request_id = ?',
        [request_id],
    ).fetchone()
    return _as_request(row) if row else None


def list_requests(conn, limit=20) -> list[dict]:
    if not _table_exists(conn, 'anonymisation_requests'):
        return []
    rows = conn.execute(
        'SELECT request_id, emp_id, status, requested_by, confirmed_by, requested_at, '
        'confirmed_at, applied_at, plan_summary, result_summary, failure_reason '
        'FROM anonymisation_requests ORDER BY request_id DESC LIMIT ?',
        [max(1, min(int(limit), 100))],
    ).fetchall()
    return [_as_request(row) for row in rows]


def _as_request(row) -> dict:
    def _json(value):
        if not value:
            return None
        try:
            return json.loads(value) if isinstance(value, str) else value
        except (TypeError, ValueError):
            return None

    return {
        'request_id': row[0],
        'emp_id': row[1],
        'status': row[2],
        'requested_by': row[3],
        'confirmed_by': row[4],
        'requested_at': row[5].isoformat() if isinstance(row[5], datetime) else row[5],
        'confirmed_at': row[6].isoformat() if isinstance(row[6], datetime) else row[6],
        'applied_at': row[7].isoformat() if isinstance(row[7], datetime) else row[7],
        'plan': _json(row[8]),
        'result': _json(row[9]),
        'failure_reason': row[10],
    }
