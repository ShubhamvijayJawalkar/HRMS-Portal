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

* ``total_days`` is **derived** from the policy (annual accrual rate, capped by
  the carry-forward cap) and materialised into ``leave_balance`` on read, so
  every existing read path keeps working;
* ``used_days`` is the approval ledger (written once, on approval);
* ``reserved_days`` is the pending ledger, written on apply and released on
  approve/reject, which is what makes double-spending impossible.

The default matrix below is exactly what the boot seed used, so an employee with
no policy assignment resolves to the same numbers they have today.
"""

from __future__ import annotations

import math
from datetime import date, datetime

# Entitlement used when the employee has no effective policy assignment. These
# are the values the v1.0 boot seed wrote, kept verbatim so deriving changes
# nothing until a policy is actually assigned.
DEFAULT_ENTITLEMENTS = {
    'Casual': 12,
    'Sick': 10,
    'Annual': 20,
}

# `accrual_rate` is read as *days earned per month*, which is the only reading
# consistent with an annual entitlement and with `monthly_leave_grants`
# (year, month, days) in the v2.0 schema. 12 months of accrual = the annual
# entitlement, and `carry_forward_cap` (when set) is the ceiling on it.
MONTHS_PER_YEAR = 12

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


def entitlement_days(conn, emp_id, leave_type, as_of=None) -> tuple[int, str]:
    """Days entitled for ``leave_type``: ``(days, source)``.

    ``source`` is ``'policy'`` when an effective assignment decided the number and
    ``'default'`` when the published matrix did. An unknown leave type has no
    entitlement, which the apply path reads as "unlimited" exactly as before.
    """
    fallback = DEFAULT_ENTITLEMENTS.get(leave_type)
    if fallback is None:
        return 0, 'unlimited'
    assignment = effective_assignment(conn, emp_id, as_of)
    if not assignment:
        return fallback, 'default'
    rate = assignment.get('accrual_rate')
    if leave_type not in RATE_DRIVEN_TYPES or not rate:
        return fallback, 'default'
    derived = int(math.floor(rate * MONTHS_PER_YEAR))
    cap = assignment.get('carry_forward_cap')
    if cap is not None:
        derived = min(derived, cap)
    return max(derived, 0), 'policy'


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
        derived, source = entitlement_days(conn, emp_id, leave_type)
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
        _derived, source = entitlement_days(conn, emp_id, leave_type)
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
    derived, _source = entitlement_days(conn, emp_id, leave_type)
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
