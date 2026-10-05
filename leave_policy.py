"""Policy-derived leave balances (FR-LEA-06 / FR-LEA-08).

`leave_balance` used to be a hand-maintained ledger: the boot seed wrote a
fixed entitlement per employee and per year (Casual 12, Sick 10, Annual 20) and
nothing derived it from anything. Two consequences:

* an employee created after the seed had **no** balance row, and the apply path
  only checked the balance ``if balance:`` — so a missing row silently meant
  *unlimited* leave;
* the `reserved` column (FR-LEA-06: "reserved by Pending leaves") was never
  written, so two Pending requests could each spend the same remaining days.

This module makes the entitlement a function of the employee's effective
``leave_policy_assignments`` row and gives the balance an explicit three-part
state:

    remaining = total_days - used_days - reserved_days

* ``total_days`` is **derived** from the policy and materialised into
  ``leave_balance`` on read, so every existing read path keeps working. For the
  accrual-driven type it is what the employee has **earned so far this year**
  (``leave_accrual.py``), capped by the carry-forward cap, so the balance grows
  month by month instead of showing the whole year in January;
* ``used_days`` is the approval ledger (written once, on approval);
* ``reserved_days`` is the pending ledger, written on apply and released on
  approve/reject, which is what makes double-spending impossible.

The default matrix below is exactly what the boot seed used, so an employee with
no policy assignment resolves to the same numbers they have today.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

# Entitlement used when the employee has no effective policy assignment. These
# are the values the v1.0 boot seed wrote, kept verbatim so deriving changes
# nothing until a policy is actually assigned.
logger = logging.getLogger(__name__)

DEFAULT_ENTITLEMENTS = {
    'Casual': 12,
    'Sick': 10,
    'Annual': 20,
}

# The accrual rate applies to the employee's annual (earned) leave; the other
# types keep the published default unless a policy says otherwise.
RATE_DRIVEN_TYPES = frozenset({'Annual'})


class LeavePolicyError(ValueError):
    """A leave-policy payload is not valid."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _as_date(value):
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y'):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise LeavePolicyError(f'unrecognised date: {value!r} (use YYYY-MM-DD)')


def _as_number(value, field, *, minimum=0.0, maximum=999.0):
    if value is None or value == '':
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise LeavePolicyError(f'{field} must be a number') from None
    if number < minimum or number > maximum:
        raise LeavePolicyError(f'{field} must be between {minimum:g} and {maximum:g}')
    return number


def effective_assignment(conn, emp_id, as_of=None) -> dict | None:
    """The employee's leave-policy row in force on ``as_of`` (default today).

    Effective dating is the v2.0 rule: ``effective_from <= as_of`` and either
    ``effective_to`` is NULL or ``effective_to >= as_of``; the most recent
    ``effective_from`` wins. A table that does not exist yet (an old
    compatibility database) reads as "no policy" rather than raising.
    """
    when = as_of or date.today()
    try:
        row = conn.execute(
            "SELECT assignment_id, location, grade, accrual_rate, carry_forward_cap, "
            "encashment_rule, weekly_off_pattern, effective_from, effective_to "
            "FROM leave_policy_assignments WHERE emp_id = ? AND effective_from <= ? "
            "AND (effective_to IS NULL OR effective_to >= ?) "
            "ORDER BY effective_from DESC, assignment_id DESC LIMIT 1",
            [emp_id, when, when],
        ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    return {
        'assignment_id': row[0],
        'location': row[1],
        'grade': row[2],
        'accrual_rate': float(row[3]) if row[3] is not None else None,
        'carry_forward_cap': int(row[4]) if row[4] is not None else None,
        'encashment_rule': row[5],
        'weekly_off_pattern': row[6],
        'effective_from': row[7],
        'effective_to': row[8],
    }


def granted_days(conn, emp_id, leave_type, year=None) -> int:
    """Manual grants recorded for ``emp_id``/``leave_type`` in ``year`` (FR-LEA-07).

    A separate ledger rather than a write to ``leave_balance.total_days``, because
    that column is **derived** and ``ensure_balances`` overwrites it on every read.
    A grant written straight into it would therefore be silently erased the next time
    anybody looked at the balance — an administrator's manual adjustment surviving
    only until the next page load. So the grant is a row, and the entitlement adds
    it up.
    """
    year = year or date.today().year
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(days), 0) FROM leave_grants "
            "WHERE emp_id = ? AND leave_type = ? AND grant_year = ?",
            [emp_id, leave_type, year],
        ).fetchone()
    except Exception as exc:
        # A pre-migration database without the table must not break every balance
        # read; zero grants is the right answer, and it is logged because a silent
        # fallback here would make an adjustment vanish without trace.
        logger.warning('leave_grants lookup failed for %s: %s', emp_id, exc)
        return 0
    return int(row[0] or 0)


def entitlement_days(conn, emp_id, leave_type, as_of=None, year=None) -> tuple[int, str]:
    """Days entitled for ``leave_type`` in ``year``, **including** manual grants.

    The policy calculation lives in :func:`_policy_entitlement_days`; this wrapper
    adds FR-LEA-07's manual grants. Splitting it this way is deliberate — the policy
    function has four early returns, and adding the grant to each one is how a future
    branch would silently forget it. A manual grant that is not in the entitlement
    does not exist.

    ``source`` gains a ``+grants`` suffix when any grant applies, so
    ``GET /api/leave-balance`` can explain a number that came from an administrator
    rather than from the policy. An unexplained ceiling is the failure this project
    keeps having to undo.
    """
    days, source = _policy_entitlement_days(conn, emp_id, leave_type, as_of, year)
    granted = granted_days(conn, emp_id, leave_type, year)
    if granted:
        days += granted
        source = f'{source}+grants'
    return days, source


def _policy_entitlement_days(conn, emp_id, leave_type, as_of=None, year=None) -> tuple[int, str]:
    """What the employee's leave policy alone entitles them to. See :func:`entitlement_days`."""
    fallback = DEFAULT_ENTITLEMENTS.get(leave_type)
    if fallback is None:
        return 0, 'unlimited'
    assignment = effective_assignment(conn, emp_id, as_of)
    if not assignment:
        return fallback, 'default'
    if leave_type not in RATE_DRIVEN_TYPES or not assignment.get('accrual_rate'):
        return fallback, 'default'

    # An accrual-driven entitlement is what has actually been earned, not a flat
    # annual ceiling: the balance grows month by month. Employees with no policy
    # row never reach here, so their numbers are unchanged.
    from leave_accrual import accrual_entitlement  # lazy: leave_accrual imports this module

    accrued = accrual_entitlement(conn, emp_id, leave_type, as_of, year)
    if accrued is None:
        return fallback, 'default'
    return max(int(accrued), 0), 'accrual'


def ensure_balances(conn, emp_id, year=None, *, leave_types=None) -> list[dict]:
    """Materialise the derived entitlement for ``year`` (idempotent).

    Existing rows keep their ``used_days``/``reserved``; only ``total_days`` is
    refreshed from the policy, so approving a policy change updates the ceiling
    without rewriting history.
    """
    year = year or date.today().year
    types = list(leave_types or DEFAULT_ENTITLEMENTS)
    existing = {
        row[0]: row
        for row in conn.execute(
            "SELECT leave_type, total_days, used_days, reserved FROM leave_balance "
            "WHERE emp_id = ? AND year = ?",
            [emp_id, year],
        ).fetchall()
    }
    for leave_type in types:
        derived, source = entitlement_days(conn, emp_id, leave_type, year=year)
        row = existing.get(leave_type)
        if row is None:
            balance_id = _next_id(conn, 'leave_balance', 'balance_id')
            conn.execute(
                "INSERT INTO leave_balance (balance_id, emp_id, leave_type, total_days, "
                "used_days, reserved, year) VALUES (?, ?, ?, ?, 0, 0, ?)",
                [balance_id, emp_id, leave_type, derived, year],
            )
        elif row[1] != derived:
            conn.execute(
                "UPDATE leave_balance SET total_days = ? WHERE emp_id = ? AND leave_type = ? AND year = ?",
                [derived, emp_id, leave_type, year],
            )
    return balances_for(conn, emp_id, year)


def balances_for(conn, emp_id, year=None) -> list[dict]:
    """The employee's balance rows, each with the source of its ceiling."""
    year = year or date.today().year
    rows = conn.execute(
        "SELECT leave_type, total_days, used_days, reserved FROM leave_balance "
        "WHERE emp_id = ? AND year = ? ORDER BY leave_type",
        [emp_id, year],
    ).fetchall()
    result = []
    for leave_type, total, used, reserved in rows:
        _derived, source = entitlement_days(conn, emp_id, leave_type, year=year)
        result.append({
            'leave_type': leave_type,
            'total_days': int(total or 0),
            'used_days': int(used or 0),
            'reserved_days': int(reserved or 0),
            'remaining': int(total or 0) - int(used or 0) - int(reserved or 0),
            'source': source,
        })
    return result


def remaining_days(conn, emp_id, leave_type, year=None) -> int | None:
    """Days still available, or ``None`` for a type with no entitlement."""
    year = year or date.today().year
    if leave_type not in DEFAULT_ENTITLEMENTS:
        return None
    derived, _source = entitlement_days(conn, emp_id, leave_type, year=year)
    row = conn.execute(
        "SELECT total_days, used_days, reserved FROM leave_balance "
        "WHERE emp_id = ? AND leave_type = ? AND year = ?",
        [emp_id, leave_type, year],
    ).fetchone()
    if row is None:
        return derived
    return int(row[0] or 0) - int(row[1] or 0) - int(row[2] or 0)


def reserve(conn, emp_id, leave_type, days, year=None) -> None:
    """Hold ``days`` against the balance while a request is Pending."""
    year = year or date.today().year
    ensure_balances(conn, emp_id, year, leave_types=[leave_type] if leave_type not in DEFAULT_ENTITLEMENTS else None)
    conn.execute(
        "UPDATE leave_balance SET reserved = reserved + ? WHERE emp_id = ? AND leave_type = ? AND year = ?",
        [days, emp_id, leave_type, year],
    )


def release(conn, emp_id, leave_type, days, year=None) -> None:
    """Return a reservation to the pool (rejection)."""
    year = year or date.today().year
    conn.execute(
        "UPDATE leave_balance SET reserved = CASE WHEN reserved >= ? THEN reserved - ? ELSE 0 END "
        "WHERE emp_id = ? AND leave_type = ? AND year = ?",
        [days, days, emp_id, leave_type, year],
    )


def consume(conn, emp_id, leave_type, days, year=None) -> None:
    """Move a reservation into used (approval): the reservation becomes usage."""
    year = year or date.today().year
    conn.execute(
        "UPDATE leave_balance SET reserved = CASE WHEN reserved >= ? THEN reserved - ? ELSE 0 END, "
        "used_days = used_days + ? WHERE emp_id = ? AND leave_type = ? AND year = ?",
        [days, days, days, emp_id, leave_type, year],
    )


SETTLED_STATUSES = frozenset({'Rejected', 'Cancelled'})

# ── cancellation (FR-LEA-05) ─────────────────────────────────────────────
#
# "Cancel: Pending only, or Approved with a future start date (with the same
# reserved/used reversal), by owner or admin."
#
# The ledger effect differs by the state being reversed, and that is the whole
# reason this is a decision function rather than a flag:
#
#   Pending  -> the days are sitting in ``reserved``, so cancel releases them.
#   Approved -> approval already moved them out of ``reserved`` into
#               ``used_days``, so cancel has to take them back out of
#               ``used_days`` instead. Releasing here would leave the balance
#               permanently understated, which is the bug this shape prevents.
#
# The day count is deliberately the *same* expression the apply, approve and
# reject paths use, so a cancellation reverses exactly what the original
# reservation took out. That expression counts calendar days and ignores weekends
# and holidays, which is the FR-LEA-09 approximation the traceability matrix
# records as open; a more accurate count here would desynchronise the reversal
# from the reservation it is undoing.
CANCELLABLE_STATUSES = frozenset({'Pending', 'Approved'})


class LeaveError(ValueError):
    """A leave request change is not permitted."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def days_between(start, end, conn=None, emp_id=None) -> int:
    """The day count every leave path uses, so a reversal is symmetrical.

    **This was the fourth implementation of "how many days"** and the one FR-LEA-09
    names by implication: it returned ``(end - start).days + 1``, calendar days,
    while FR-LEA-02 charged working days. So a Friday-to-Monday request reserved two
    days and a rejection gave back four — the balance drifted *upward* on every
    rejected leave, silently, and cancel had the same asymmetry.

    It now **delegates** to the one shared function. With a ``conn`` it is the real
    working-day rule; without one it stays a calendar count, which is only reached
    from a log line and from pre-`days` rows, and the docstring says so rather than
    leaving the two indistinguishable.
    """
    if conn is None or emp_id is None:
        return (end - start).days + 1
    import working_days

    return working_days.working_days(conn, emp_id, start, end)


def check_cancel(actor_emp_id, request_row, is_admin, as_of=None) -> str:
    """May this request be cancelled, and what does the ledger need?

    ``request_row`` is ``(leave_id, emp_id, leave_type, start_date, end_date,
    status)``. Returns ``'release'`` for a Pending request or ``'unconsume'`` for
    an Approved one.

    Raises ``LeaveError``: 403 for somebody else's request, 409 for a state or a
    start date that cannot be cancelled.
    """
    # 6 columns for a pre-FR-LEA-09 row, 8 for a current one (`days`, `session`
    # appended). Unpacked by position rather than by a fixed arity so a request written
    # before the migration and one written after it both work — which is the whole
    # reason the columns are nullable.
    _leave_id, emp_id, _leave_type, start_date, _end_date, status = request_row[:6]
    if actor_emp_id != emp_id and not is_admin:
        raise LeaveError('You can only cancel your own leave requests', 403)
    if status not in CANCELLABLE_STATUSES:
        if status == 'Rejected':
            raise LeaveError('This request was already rejected', 409)
        raise LeaveError(f'A {status.lower()} leave request cannot be cancelled', 409)
    if status == 'Approved':
        if start_date <= (as_of or date.today()):
            raise LeaveError(
                'An approved leave can only be cancelled before it starts', 409)
        return 'unconsume'
    return 'release'


def cancel(conn, actor_emp_id, request_row, is_admin, as_of=None) -> str:
    """Validate a cancellation and reverse the ledger. Returns the action taken.

    The decision and the reversal live together so a caller cannot check one and
    apply the other: the whole hazard here is releasing when the days are already
    used, and that is decided and performed in one function.
    """
    action = check_cancel(actor_emp_id, request_row, is_admin, as_of)
    _leave_id, emp_id, leave_type, start_date, end_date, status = request_row[:6]
    # The **stored** figure when the route has one, because a shared function called
    # twice can legitimately answer differently the second time: a holiday added
    # between applying and cancelling would otherwise return a different number of
    # days than was taken.
    #
    # Index 6, not 7: the route's SELECT is
    # `(leave_id, emp_id, leave_type, start_date, end_date, status, days, session)`,
    # so `days` is the 7th element and `session` the 8th. Reading 7 passed the string
    # `'Full'` into an INTEGER parameter — an error that surfaced as
    # "invalid input syntax for type integer: Full" inside the balance UPDATE, two
    # frames away from the route column list that caused it. Named here so the next
    # reader does not have to recount.
    stored = request_row[6] if len(request_row) > 6 else None
    days = stored if stored is not None else days_between(start_date, end_date,
                                                           conn=conn, emp_id=emp_id)
    year = start_date.year
    if action == 'release':
        release(conn, emp_id, leave_type, days, year)
    else:
        # Take the days back out of `used_days`, and never below zero: a
        # corrected ledger matters more than an exactly-symmetric one.
        conn.execute(
            "UPDATE leave_balance SET used_days = CASE WHEN used_days >= ? "
            'THEN used_days - ? ELSE 0 END '
            'WHERE emp_id = ? AND leave_type = ? AND year = ?',
            [days, days, emp_id, leave_type, year],
        )
    result = conn.execute(
        "UPDATE leave_requests SET status = 'Cancelled', updated_at = ? "
        "WHERE leave_id = ? AND status = ?",
        [datetime.now(), request_row[0], status],
    )
    if result.rowcount == 0:
        raise LeaveError(
            'The request changed while you were cancelling it; reload and retry', 409)
    return action


def validate_assignment(payload) -> dict:
    """Validate a leave-policy assignment payload (CC-12: no unknown keys)."""
    allowed = {
        'location', 'grade', 'accrual_rate', 'carry_forward_cap',
        'encashment_rule', 'weekly_off_pattern', 'effective_from', 'effective_to',
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise LeavePolicyError(f'unknown fields: {", ".join(unknown)}')
    effective_from = _as_date(payload.get('effective_from'))
    if effective_from is None:
        raise LeavePolicyError('effective_from is required (YYYY-MM-DD)')
    effective_to = _as_date(payload.get('effective_to'))
    if effective_to is not None and effective_to < effective_from:
        raise LeavePolicyError('effective_to must not be before effective_from')
    accr = _as_number(payload.get('accrual_rate'), 'accrual_rate', maximum=31.0)
    cap = payload.get('carry_forward_cap')
    cap = int(cap) if cap not in (None, '') else None
    if cap is not None and cap < 0:
        raise LeavePolicyError('carry_forward_cap must not be negative')
    for field in ('location', 'grade', 'encashment_rule', 'weekly_off_pattern'):
        value = payload.get(field)
        if value in (None, ''):
            payload[field] = None
            continue
        value = str(value).strip()
        if len(value) > 120:
            raise LeavePolicyError(f'{field} must be 120 characters or fewer')
        payload[field] = value or None
    payload['accrual_rate'] = accr
    payload['carry_forward_cap'] = cap
    payload['effective_from'] = effective_from
    payload['effective_to'] = effective_to
    return payload


def _next_id(conn, table, column):
    from app import _is_public_target_schema, _next_generated_id, gen_id  # lazy

    if _is_public_target_schema():
        return _next_generated_id(conn, table, column)
    while True:
        value = gen_id()
        if not conn.execute(f'SELECT 1 FROM {table} WHERE {column} = ?', [value]).fetchone():
            return value
