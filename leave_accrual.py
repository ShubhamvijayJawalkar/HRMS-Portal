"""Accrual: turn a policy's ``accrual_rate`` into days earned month by month.

`monthly_leave_grants(year, month, days)` exists in the v2.0 target and nothing
wrote to it, so an employee's accrual rate was collapsed into a single flat
annual ceiling the moment the policy was assigned: someone on 1.5 days/month
showed the full 18 days in January. That is not accrual accounting.

With this module the entitlement for a rate-driven leave type is what the
employee has actually **earned so far** — ``floor(rate × months elapsed)`` over
the months the assignment was in force — so the balance grows through the year:

    days earned by the end of month m = floor(rate * m)

Two deliberate design choices, both recorded here rather than left to be
rediscovered:

* **The entitlement is derived, the grant is the record.** Days earned come from
  the policy and today's date, not from the grant rows, so a missed scheduler
  run can never leave an employee with no leave at all — the balance is right
  whether or not the monthly job has fired. The grant rows are the auditable
  ledger of what was posted, when, and by which run; they are also the natural
  input to a year-end carry-forward later. A test asserts the two agree after a
  run, which is what keeps the ledger honest.
* **The fraction is carried, never truncated.** A rate of 1.5/month credits 1 day
  in January and 2 in February, reaching exactly 18 by December, so no day is
  silently lost or invented and the twelve credits always sum to
  ``floor(rate * 12)``.

The carry-forward cap applies to the year's earned total, in both the derived
value and the ledger. Employees **without** an effective policy are untouched:
the published default matrix still applies, so nothing changes until a policy is
assigned.
"""

from __future__ import annotations

import math
from datetime import date, datetime

from leave_policy import RATE_DRIVEN_TYPES, effective_assignment, ensure_balances

# `monthly_leave_grants.granted_by` is a foreign key to `users(emp_id)` in the
# v2.0 target, so a system-driven accrual leaves it NULL rather than inventing an
# actor id. Who *triggered* an on-demand run is recorded in `audit_log`.
GRANTED_BY_SYSTEM = None

_TABLE_PRESENT: dict[str, bool] = {}


def _table_exists(conn, table: str = 'monthly_leave_grants') -> bool:
    """Does the connected schema have the accrual ledger? Cached per process.

    A compatibility database created before this slice does not have the table
    until `init_db` runs, and the module is imported before that; the read paths
    must not raise in that window.
    """
    import os

    key = f"{os.getenv('APP_DB', 'duckdb').lower()}:{table}"
    if key not in _TABLE_PRESENT:
        try:
            conn.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone()
            _TABLE_PRESENT[key] = True
        except Exception:
            _TABLE_PRESENT[key] = False
    return _TABLE_PRESENT[key]


def _as_date(value):
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], '%Y-%m-%d').date()


def days_earned_through(rate: float, month: int) -> int:
    """Days earned by the end of ``month`` (0 when ``month`` is 0 or negative)."""
    if month <= 0:
        return 0
    return int(math.floor(float(rate) * month))


def month_days(rate: float, month: int) -> int:
    """Days credited *in* ``month``, with the fraction carried from earlier months.

    The twelve credits sum to exactly ``floor(rate * 12)``.
    """
    if month < 1 or month > 12:
        return 0
    return days_earned_through(rate, month) - days_earned_through(rate, month - 1)


def accrual_window(assignment: dict, year: int, today: date):
    """``(first_month, last_month)`` of ``year`` the assignment earns in.

    ``None`` when the assignment does not reach that year, or when the year has
    not started yet. A month is only included once it has actually happened, so
    the derived entitlement never counts the future.
    """
    start = _as_date(assignment.get('effective_from'))
    end = _as_date(assignment.get('effective_to'))
    if start and start > date(year, 12, 31):
        return None
    if end and end < date(year, 1, 1):
        return None
    if year > today.year:
        return None
    clock = 12 if year < today.year else today.month
    first = start.month if start and start.year == year else 1
    last = end.month if end and end.year == year else 12
    last = min(last, clock)
    return None if last < first else (first, last)


def accrual_entitlement(conn, emp_id, leave_type, as_of=None, year=None):
    """Days ``emp_id`` has earned of ``leave_type`` in ``year``, cap applied.

    ``None`` means "this employee is not accrual-driven for that year" — no
    policy row, no rate, or the assignment does not reach the year — and the
    caller then falls back to the published default. That is what keeps every
    employee without a policy, and every year a policy did not cover, exactly as
    they are today.
    """
    if leave_type not in RATE_DRIVEN_TYPES:
        return None
    assignment = effective_assignment(conn, emp_id, as_of)
    if not assignment or not assignment.get('accrual_rate'):
        return None
    today = as_of or date.today()
    window = accrual_window(assignment, year or today.year, today)
    if window is None:
        return None
    first, last = window
    rate = float(assignment['accrual_rate'])
    days = days_earned_through(rate, last) - days_earned_through(rate, first - 1)
    cap = assignment.get('carry_forward_cap')
    if cap is not None:
        days = min(days, int(cap))
    return max(days, 0)


def granted_days(conn, emp_id, leave_type, year=None) -> int:
    """Days actually posted to the ledger for ``year`` (0 when nothing has run)."""
    year = year or date.today().year
    if not _table_exists(conn):
        return 0
    total = conn.execute(
        'SELECT COALESCE(SUM(days), 0) FROM monthly_leave_grants '
        'WHERE emp_id = ? AND leave_type = ? AND year = ?',
        [emp_id, leave_type, year],
    ).fetchone()[0]
    return int(total or 0)


def has_grant(conn, emp_id, leave_type, year, month) -> bool:
    if not _table_exists(conn):
        return False
    return bool(conn.execute(
        'SELECT 1 FROM monthly_leave_grants WHERE emp_id = ? AND leave_type = ? '
        'AND year = ? AND month = ?',
        [emp_id, leave_type, year, month],
    ).fetchone())


def grant_month(conn, emp_id, leave_type, year, month, granted_by=None) -> int:
    """Post one month to the ledger. Idempotent; returns the days posted (0 if already there)."""
    if not _table_exists(conn):
        return 0
    if has_grant(conn, emp_id, leave_type, year, month):
        return 0
    assignment = effective_assignment(conn, emp_id, date(year, month, 1))
    if not assignment or not assignment.get('accrual_rate'):
        return 0
    days = month_days(float(assignment['accrual_rate']), month)
    cap = assignment.get('carry_forward_cap')
    if cap is not None:
        # Never post past the cap, so the ledger total matches the derived value.
        days = min(days, max(int(cap) - granted_days(conn, emp_id, leave_type, year), 0))
    if days <= 0:
        return 0
    from app import _is_public_target_schema, _next_generated_id, gen_id  # lazy

    if _is_public_target_schema():
        grant_id = _next_generated_id(conn, 'monthly_leave_grants', 'grant_id')
    else:
        grant_id = gen_id()
        while conn.execute(
            'SELECT 1 FROM monthly_leave_grants WHERE grant_id = ?', [grant_id]
        ).fetchone():
            grant_id = gen_id()
    conn.execute(
        'INSERT INTO monthly_leave_grants '
        '(grant_id, emp_id, leave_type, days, month, year, granted_by) '
        'VALUES (?, ?, ?, ?, ?, ?, ?)',
        [grant_id, emp_id, leave_type, days, month, year, granted_by],
    )
    return days


def run_accrual(conn, as_of=None, *, employee_ids=None) -> dict:
    """Post every elapsed month of the current year for every policy holder.

    Each (employee, type, year, month) is posted at most once, so the monthly job
    and an on-demand run are the same safe operation and a retry after a crash
    converges instead of double-crediting. Returns the counters the route and the
    audit row report.
    """
    today = _as_date(as_of) or date.today()
    year = today.year
    result = {'employees': 0, 'grants': 0, 'days': 0, 'already_posted': 0}
    if not _table_exists(conn):
        return result
    params = [today.isoformat(), today.isoformat()]
    where = ''
    if employee_ids:
        where = ' AND emp_id IN ({})'.format(','.join('?' for _ in employee_ids))
        params.extend(employee_ids)
    rows = conn.execute(
        'SELECT DISTINCT emp_id FROM leave_policy_assignments '
        'WHERE effective_from <= ? AND (effective_to IS NULL OR effective_to >= ?)' + where,
        params,
    ).fetchall()
    for (emp_id,) in rows:
        assignment = effective_assignment(conn, emp_id, today)
        if not assignment or not assignment.get('accrual_rate'):
            continue
        result['employees'] += 1
        window = accrual_window(assignment, year, today)
        if window:
            first, last = window
            for leave_type in sorted(RATE_DRIVEN_TYPES):
                for month in range(first, last + 1):
                    if has_grant(conn, emp_id, leave_type, year, month):
                        result['already_posted'] += 1
                        continue
                    posted = grant_month(conn, emp_id, leave_type, year, month)
                    if posted:
                        result['grants'] += 1
                        result['days'] += posted
        # The balance follows the derived entitlement immediately, so a read
        # after the run shows what the employee has earned.
        ensure_balances(conn, emp_id, year)
    return result
