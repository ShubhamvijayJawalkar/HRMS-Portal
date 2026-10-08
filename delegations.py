"""FR-LEA-08a — approval delegation.

The SRS: *"Approval delegation. A manager can delegate approval authority to another
employee for a date range (e.g. while on leave). Delegates appear in
pending_my_approval views and their approvals are audited as 'approved by delegate
for manager X'."*

`approval_delegations` has been in the canonical schema since the baseline, with a
`no_overlapping_delegation` exclusion constraint on
`daterange(starts_on, ends_on) &&` per delegator — and **no route read or wrote it**.
The constraint documented an intent nobody had implemented, which is the
"schema without routes" shape the traceability pass exists to catch.

**This is the keystone for four other requirements.** FR-LEA-04 (approve), FR-ATT-06
(break approval), FR-ATT-07 (the manager's break summary) and FR-REG-01
(regularization) each name "delegate" as the thing they are missing. Fixing them
separately would have meant four slightly different answers to the same question.

**A delegation is active when *today* falls inside its range**, which is what the
SRS flow diagram means by "active delegate" (p25: "manager (or active delegate,
FR-LEA-08a)"). The alternative — testing the date the *request* was raised — would
let a delegate approve something raised before they were delegated to and something
raised after they stopped being a delegate, which is precisely the window the
delegation was created to close. Backdated approval is handled by an admin, who does
not need a delegation.

**Overlap is checked in the application as well as by the database.** The canonical
schema enforces it with a GiST exclusion constraint; the compatibility schema cannot
(partial/exclusion indexes are the same portability problem the optional-holiday
index hit), so the same predicate runs in ``create`` on every backend. Belt and braces
is right here: the constraint is what makes the rule true under concurrency, and the
check is what makes it true on a schema that cannot express it — and it is what turns a
raw database error into a 409 an administrator can act on.

**A delegate inherits the delegator's authority, not more.** ``can_approve_for`` answers
"may this actor act on this employee's request", and a delegate gets the answer their
manager would have got — not an admin's. A delegation is a handover for a fortnight,
not a promotion.

**Two entry points, one rule.** ``can_approve_for`` answers *for one request* and is
what the approve/reject routes call after their coarse gate; ``approvable_employees``
answers *for a whole list* and is what the ``pending_my_approval`` filters call. They
are deliberately the same question in two shapes, because a list that promised an
approver a request the approve route then refused is a dead end, and two
implementations of "who may decide" would drift the first time a third approval path
appeared. Both refuse ``actor == target`` — the applicant is never their own approver,
whatever their role.
"""

from __future__ import annotations

import logging
from datetime import date

import policy

logger = logging.getLogger(__name__)


class DelegationError(ValueError):
    """A delegation that cannot be created. ``status`` is the HTTP status to answer."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _table_exists(conn) -> bool:
    try:
        conn.execute('SELECT 1 FROM approval_delegations LIMIT 1')
        return True
    except Exception:
        return False


def create(conn, delegator_id: str, delegate_id: str, starts_on: date, ends_on: date,
           reason: str = '') -> dict:
    """Delegate approval authority for a date range. Returns the stored delegation.

    Validated here rather than in the route so the rule cannot be bypassed by a second
    caller — the same reasoning as `leave_grants.validate`.
    """
    delegator_id = str(delegator_id or '').strip().upper()
    delegate_id = str(delegate_id or '').strip().upper()

    if not delegator_id or not delegate_id:
        raise DelegationError('delegator_id and delegate_id are required')
    if delegator_id == delegate_id:
        raise DelegationError(
            'You cannot delegate approval authority to yourself', 409,
        )
    if starts_on is None or ends_on is None:
        raise DelegationError('starts_on and ends_on are required')
    if ends_on < starts_on:
        raise DelegationError('ends_on must not be before starts_on')

    if not _table_exists(conn):
        raise DelegationError('Delegation storage is not available on this database', 503)

    for emp_id, label in ((delegator_id, 'delegator'), (delegate_id, 'delegate')):
        row = conn.execute(
            'SELECT name, status FROM users WHERE emp_id = ?', [emp_id],
        ).fetchone()
        if not row:
            raise DelegationError(f'{label} {emp_id} not found', 404)
        if row[1] != 'Active':
            # Delegating to someone who cannot sign in produces a delegation nobody can
            # use, and it would sit in the table looking like coverage.
            raise DelegationError(
                f'{label.capitalize()} {emp_id} is {row[1]}, so a delegation to them '
                f'could never be used', 409,
            )

    overlapping = conn.execute(
        'SELECT delegation_id, delegate_id, starts_on, ends_on '
        'FROM approval_delegations WHERE delegator_id = ? '
        'AND starts_on <= ? AND ends_on >= ? LIMIT 1',
        [delegator_id, ends_on, starts_on],
    ).fetchone()
    if overlapping:
        raise DelegationError(
            f'You already delegate to {overlapping[1]} for '
            f'{overlapping[2]} to {overlapping[3]}. Revoke that first, or widen it.',
            409,
        )

    next_id = _next_delegation_id(conn)
    conn.execute(
        'INSERT INTO approval_delegations (delegation_id, delegator_id, delegate_id, '
        'starts_on, ends_on) VALUES (?, ?, ?, ?, ?)',
        [next_id, delegator_id, delegate_id, starts_on, ends_on],
    )
    return {
        'delegation_id': next_id,
        'delegator_id': delegator_id,
        'delegate_id': delegate_id,
        'starts_on': starts_on.isoformat(),
        'ends_on': ends_on.isoformat(),
        'reason': reason,
        'active': _is_active(starts_on, ends_on),
    }


def _next_delegation_id(conn) -> int:
    # The compat column is a bare INTEGER PRIMARY KEY with no default, so a test and a
    # v1.0-shaped deployment both need an explicit id — the same constraint that forces
    # `_next_generated_id` in the write paths.
    from app import _next_generated_id

    return _next_generated_id(conn, 'approval_delegations', 'delegation_id')


def _is_active(starts_on, ends_on, on_date=None) -> bool:
    on_date = on_date or date.today()
    return starts_on <= on_date <= ends_on


def for_delegator(conn, delegator_id: str) -> list[dict]:
    """Every delegation this manager has handed out, newest first."""
    return _rows(conn, 'WHERE delegator_id = ?', [delegator_id])


def for_delegate(conn, delegate_id: str) -> list[dict]:
    """Every delegation handed *to* this employee."""
    return _rows(conn, 'WHERE delegate_id = ?', [delegate_id])


def _rows(conn, where: str, params: list) -> list[dict]:
    if not _table_exists(conn):
        return []
    out = []
    for r in conn.execute(
        'SELECT delegation_id, delegator_id, delegate_id, starts_on, ends_on, created_at '
        f'FROM approval_delegations {where} ORDER BY starts_on DESC, delegation_id DESC',
        params,
    ).fetchall():
        out.append({
            'delegation_id': int(r[0]),
            'delegator_id': r[1],
            'delegate_id': r[2],
            'starts_on': r[3].isoformat() if r[3] else None,
            'ends_on': r[4].isoformat() if r[4] else None,
            'created_at': r[5].isoformat() if r[5] else None,
            'active': bool(r[3] and r[4] and _is_active(r[3], r[4])),
        })
    return out


def delegators_for(conn, delegate_id: str, on_date=None) -> list[str]:
    """The managers ``delegate_id`` may currently approve for."""
    on_date = on_date or date.today()
    if not _table_exists(conn):
        return []
    return [
        r[0] for r in conn.execute(
            'SELECT delegator_id FROM approval_delegations '
            'WHERE delegate_id = ? AND starts_on <= ? AND ends_on >= ?',
            [delegate_id, on_date, on_date],
        ).fetchall()
    ]


def manager_of(conn, target_emp_id: str) -> str | None:
    """``target_emp_id``'s manager, or ``None`` when the employee is unknown or unmanaged."""
    row = conn.execute(
        'SELECT manager_emp_id FROM users WHERE emp_id = ?', [target_emp_id],
    ).fetchone()
    return row[0] if row and row[0] else None


def role_authorises(conn, actor: str) -> bool:
    """Does ``actor`` hold approval authority by role rather than by reporting line?

    HR/Admin, or a member of the HR *department* whatever their role — the same test
    `reporting_line_required` applies at the gate. It lives here as well because the
    gate only decides whether the caller may reach the route at all; the decision is
    made against one employee, and a rule asked in two places must have one answer.
    Read from the database, never from the session's stale role copy.
    """
    if not actor:
        return False
    row = conn.execute(
        'SELECT role, department FROM users WHERE emp_id = ?', [actor],
    ).fetchone()
    if not row:
        return False
    return row[0] in policy.ADMIN_ROLES or row[0] == 'HR' or row[1] == 'HR'


def acts_for(conn, actor: str, target_emp_id: str, on_date=None) -> bool:
    """May ``actor`` approve on behalf of ``target_emp_id``'s manager, today?

    The delegate's own manager matters: a delegation transfers one manager's
    authority, so the check is that ``actor`` is an active delegate **of the person
    whose report is being actioned**, not an active delegate of anybody. Without that
    scoping a delegate could approve for a different department entirely.
    """
    on_date = on_date or date.today()
    manager = manager_of(conn, target_emp_id)
    if not manager:
        return False
    if actor == manager:
        return True
    return manager in delegators_for(conn, actor, on_date)


def delegate_is_standing_in(conn, emp_id: str, on_date=None) -> bool:
    """Is ``emp_id`` an active delegate of *some* manager today?

    The coarse approval gate's third admission clause. FR-LEA-08a lets a manager hand
    their authority to "another employee for a date range" — not "another manager" —
    so a delegate who happens to manage nobody would otherwise be refused by the
    gate's `manages someone` clause before the per-employee check was ever asked.
    That is the gate bug this codebase has now fixed in four places, and refusing it
    here would reintroduce it by way of the delegation feature itself.

    *Which* manager, and therefore which employee, is `can_approve_for`'s question:
    this only establishes that the caller is standing in for somebody, exactly as the
    gate's `_manages_any_employee` only establishes that they manage somebody.
    """
    return bool(delegators_for(conn, emp_id, on_date))


def can_approve_for(conn, actor: str, target_emp_id: str, on_date=None) -> bool:
    """May ``actor`` decide ``target_emp_id``'s request, today?

    Three clauses in one answer, because they are the same rule asked three ways and
    each approval path used to answer it differently — two of them by not answering it:

      * **not the applicant** (FR-LEA-04's "not the applicant"), refused first so an
        employee cannot approve their own request by outranking the check below;
      * **HR/Admin** by role or department (FR-ATT-06's "or has role HR/Admin");
      * **the employee's manager, or an active delegate of that manager**
        (FR-LEA-04's "manager (or delegate)" and FR-ATT-06's "including an active
        delegate, FR-LEA-08a").
    """
    if not actor or not target_emp_id or actor == target_emp_id:
        return False
    if role_authorises(conn, actor):
        return True
    return acts_for(conn, actor, target_emp_id, on_date)


def approvable_employees(conn, actor: str, on_date=None) -> list[str] | None:
    """Exactly whose requests ``actor`` may decide — the ``pending_my_approval`` filter.

    ``None`` means *every* employee but the caller's own (the HR/Admin case), so the
    role half of the rule stays here rather than being re-derived by each list
    endpoint. Otherwise the concrete list: the caller's reports, plus the reports of
    every manager who has delegated to them — a delegate appears in
    ``pending_my_approval`` precisely because they can see the *delegator's* queue, not
    because they can see the delegator themselves.

    Always excluding ``actor``: a delegate who happens to be a report of their own
    delegator would otherwise be handed their own request to approve, and
    ``can_approve_for`` would then refuse what this list just offered.
    """
    if not actor:
        return []
    if role_authorises(conn, actor):
        return None
    managers = [actor, *delegators_for(conn, actor, on_date)]
    if not managers:
        return []
    placeholders = ','.join('?' for _ in managers)
    rows = conn.execute(
        f'SELECT emp_id FROM users WHERE manager_emp_id IN ({placeholders})',
        managers,
    ).fetchall()
    return sorted({r[0] for r in rows} - {actor})


def approval_note(conn, actor: str, target_emp_id: str, on_date=None) -> str | None:
    """The audit wording the SRS asks for, or ``None`` when it does not apply.

    *"their approvals are audited as 'approved by delegate for manager X'"* — so the
    trail distinguishes a delegate's decision from the manager's own rather than
    recording both as the same actor.

    Scoped two ways, and both matter:

      * to **this target's manager**, exactly like ``acts_for``. An employee delegating
        one department must not have their name attached to an approval in another,
        and the first version of this function picked an arbitrary active delegation of
        the actor's, which would have done precisely that.
      * to the case where the delegation is **what authorised the decision**. HR/Admin
        never needed one, so tagging their approval "by delegate for manager X"
        describes a handover they did not use — and they are exactly the people most
        likely to also be a delegate somewhere, since a delegation can name anybody.
    """
    on_date = on_date or date.today()
    if not _table_exists(conn) or not actor or actor == target_emp_id:
        return None
    if role_authorises(conn, actor):
        return None
    manager = manager_of(conn, target_emp_id)
    if not manager or manager not in delegators_for(conn, actor, on_date):
        return None
    return f'approved by delegate for manager {manager}'


def revoke(conn, delegation_id: int, actor: str, *, is_admin: bool = False) -> None:
    """Withdraw a delegation. Only the delegator or an admin may do so.

    The admin half was in this docstring from the day the module was written and the
    code only ever tested ``row[0] != actor``, so an administrator had no way to clear a
    delegation left behind by somebody who had since left — which is exactly the case an
    admin override exists for, and the reason the route passes ``is_admin`` rather than
    the caller being asked to claim it.
    """
    if not _table_exists(conn):
        raise DelegationError('Delegation storage is not available on this database', 503)
    row = conn.execute(
        'SELECT delegator_id FROM approval_delegations WHERE delegation_id = ?',
        [delegation_id],
    ).fetchone()
    if not row:
        raise DelegationError('Delegation not found', 404)
    if row[0] != actor and not is_admin:
        raise DelegationError(
            'Only the delegator or an administrator can withdraw a delegation', 403,
        )
    conn.execute(
        'DELETE FROM approval_delegations WHERE delegation_id = ?', [delegation_id],
    )
