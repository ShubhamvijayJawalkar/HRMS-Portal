"""FR-LEA-07 — manual leave grants.

The SRS, at **High** priority: *"Grants: HR/Admin can add days to one or more
employees' balances for a type/month/year; fully audited as LEAVE_GRANT with
before/after totals."*

Nothing implemented any of it. Only a policy assignment or the accrual job could change
a balance, so an administrator who needed to give someone three days — a long-service
award, a settlement agreed in negotiation, correcting a policy applied to the wrong
person — had no route at all and would have gone to a database console.

**The one design decision, and it is load-bearing.** A grant is **not** written to
``leave_balance.total_days``. That column is *derived*: ``ensure_balances`` recomputes
it from the effective policy on every read and overwrites it. A grant written there
would be silently erased the next time anybody opened the balance, and no audit row
could explain where it went. So a grant is a **row** in ``leave_grants`` and
``leave_policy.entitlement_days`` adds the year's grants to the policy's figure. That
is why the whole ledger, rather than a number, is the thing that persists.

**Before/after totals are computed from the balance, not from the grant.** The SRS
asks for them and the obvious implementation — ``after = before + days`` — is wrong the
moment the entitlement is not simply additive: a grant can push an employee past the
carry-forward cap, in which case they are entitled to fewer days than they had plus
what they were given. Recording ``after`` as the arithmetic sum would then state a
ceiling the employee does not have, in the record an administrator reads to decide
whether to grant again. So the "after" is what ``ensure_balances`` actually
materialises, and the two can legitimately differ — which is itself the interesting
fact.

**A grant may be negative**, because the same route is how a mis-keyed one is
corrected, and a "grant" endpoint that cannot subtract forces an administrator to ask
for a code change to undo a typo.

**One request, many employees** — the SRS says "one or more", and a batch is what makes
an administrator's afternoon bearable when the answer is "the same for everyone on the
night shift". Each employee is committed and audited **independently**, so one unknown
employee id does not fail the other forty-nine: a batch that is all-or-nothing would
make a single typo mean retyping the whole list.
"""

from __future__ import annotations

import logging
from datetime import date

logger = logging.getLogger(__name__)


class GrantError(ValueError):
    """A grant that cannot be applied. ``status`` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 400, emp_id: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.emp_id = emp_id


def validate(payload: dict) -> dict:
    """Check the request before touching a balance.

    Returns the normalised payload. Raises :class:`GrantError`. Kept separate from the
    write so a caller cannot validate in one place and write in another, and so the
    rules are testable without a database.
    """
    from leave_policy import DEFAULT_ENTITLEMENTS

    emp_ids = payload.get('emp_ids')
    if isinstance(emp_ids, str):
        emp_ids = [part.strip() for part in emp_ids.replace(';', ',').split(',')]
    if not emp_ids:
        raise GrantError('emp_ids required: one or more employee IDs')
    if len(emp_ids) > 500:
        # A paste accident, not an intent. Bounded so a single request cannot hold a
        # lock across the whole directory.
        raise GrantError('At most 500 employees per grant request')

    leave_type = str(payload.get('leave_type') or '').strip()
    if leave_type not in DEFAULT_ENTITLEMENTS:
        raise GrantError(
            f'leave_type must be one of: {", ".join(sorted(DEFAULT_ENTITLEMENTS))}'
        )

    raw_days = payload.get('days')
    try:
        days = int(raw_days)
    except (TypeError, ValueError):
        raise GrantError('days must be a whole number') from None
    if days == 0:
        raise GrantError('days must not be zero; use a negative value to reverse a grant')
    if abs(days) > 365:
        raise GrantError('days must be within +/- 365')

    year = payload.get('year')
    try:
        year = int(year) if year is not None else date.today().year
    except (TypeError, ValueError):
        raise GrantError('year must be a number') from None
    if not 2000 <= year <= date.today().year + 1:
        # Backdated further than that is a data-entry error; a future year more than one
        # ahead is a typo, because no balance row exists for it yet.
        raise GrantError('year must be this year, next year, or no earlier than 2000')

    month = payload.get('month')
    if month is not None:
        try:
            month = int(month)
        except (TypeError, ValueError):
            raise GrantError('month must be a number between 1 and 12') from None
        if not 1 <= month <= 12:
            raise GrantError('month must be between 1 and 12')

    reason = str(payload.get('reason') or '').strip()
    if not reason:
        # FR-AUD-01 wants the *fact* recorded. "HR/Admin added 3 days" is not a fact an
        # auditor can use, and a grant is the one adjustment most likely to be disputed.
        raise GrantError('reason is required: a leave grant is an audited adjustment')

    return {
        'emp_ids': [str(e).strip().upper() for e in emp_ids if str(e).strip()],
        'leave_type': leave_type,
        'days': days,
        'year': year,
        'month': month,
        'reason': reason,
    }


def apply_grant(conn, actor: str, spec: dict) -> dict:
    """Apply one grant to one employee. Returns the before/after record.

    Single-employee on purpose: the batch route loops this so a failure is isolated and
    the caller can report which employees were affected.
    """
    import leave_policy

    emp_id = spec['emp_id']
    row = conn.execute(
        'SELECT name, status FROM users WHERE emp_id = ?', [emp_id],
    ).fetchone()
    if not row:
        raise GrantError(f'Employee {emp_id} not found', 404, emp_id)
    # An archived or blocked employee has no balance to adjust. Granting days to
    # somebody who cannot sign in produces a number nobody will ever spend, and reads
    # in the audit trail as though it mattered.
    if row[1] != 'Active':
        raise GrantError(
            f'{emp_id} is {row[1]}; a grant would not give them balance to spend',
            409, emp_id,
        )

    leave_type, days, year, month = (
        spec['leave_type'], spec['days'], spec['year'], spec['month'],
    )

    # `before` is the materialised balance, so it is the figure the employee actually
    # had — not the policy's entitlement, which may differ from it.
    leave_policy.ensure_balances(conn, emp_id, year, leave_types=[leave_type])
    before = conn.execute(
        'SELECT total_days, used_days, reserved FROM leave_balance '
        'WHERE emp_id = ? AND leave_type = ? AND year = ?',
        [emp_id, leave_type, year],
    ).fetchone()

    grant_id = _next_grant_id(conn)
    conn.execute(
        'INSERT INTO leave_grants (grant_id, emp_id, leave_type, days, grant_month, '
        'grant_year, granted_by, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        [grant_id, emp_id, leave_type, days, month, year, actor, spec['reason']],
    )

    leave_policy.ensure_balances(conn, emp_id, year, leave_types=[leave_type])
    after = conn.execute(
        'SELECT total_days, used_days, reserved FROM leave_balance '
        'WHERE emp_id = ? AND leave_type = ? AND year = ?',
        [emp_id, leave_type, year],
    ).fetchone()

    _, source = leave_policy.entitlement_days(conn, emp_id, leave_type, year=year)
    return {
        'emp_id': emp_id,
        'name': row[0],
        'leave_type': leave_type,
        'days': days,
        'grant_id': grant_id,
        'year': year,
        'month': month,
        # Carried on the outcome so the audit row, the notification and the API
        # response all quote the administrator's own words rather than a route
        # re-deriving it from the spec — three reads of the same value is three
        # chances to disagree.
        'reason': spec['reason'],
        'source': source,
        # Named explicitly rather than as a tuple so the audit row and the API response
        # read the same way. `remaining` is what an employee would see.
        'before': {
            'total_days': int(before[0] or 0) if before else 0,
            'used_days': int(before[1] or 0) if before else 0,
            'reserved': int(before[2] or 0) if before else 0,
            'remaining': _remaining(before),
        },
        'after': {
            'total_days': int(after[0] or 0) if after else 0,
            'used_days': int(after[1] or 0) if after else 0,
            'reserved': int(after[2] or 0) if after else 0,
            'remaining': _remaining(after),
        },
    }


def _remaining(row) -> int:
    if not row:
        return 0
    return int(row[0] or 0) - int(row[1] or 0) - int(row[2] or 0)


def _next_grant_id(conn) -> int:
    from app import _next_generated_id

    return _next_generated_id(conn, 'leave_grants', 'grant_id')


def grants_for(conn, emp_id, year=None):
    """A grant history, newest first — the reason a ceiling moved."""
    year = year or date.today().year
    return [
        {
            'grant_id': r[0],
            'emp_id': r[1],
            'leave_type': r[2],
            'days': int(r[3] or 0),
            'month': r[4],
            'year': r[5],
            'granted_by': r[6],
            'reason': r[7],
            'created_at': r[8].isoformat() if r[8] else None,
        }
        for r in conn.execute(
            'SELECT grant_id, emp_id, leave_type, days, grant_month, grant_year, '
            'granted_by, reason, created_at FROM leave_grants '
            'WHERE emp_id = ? AND grant_year = ? ORDER BY grant_id DESC',
            [emp_id, year],
        ).fetchall()
    ]
