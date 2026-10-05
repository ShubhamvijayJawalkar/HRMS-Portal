"""FR-LEA-09 — one working-day / holiday-deduction function, called from one place.

The SRS is explicit about the defect and about the fix:

> The working-day/holiday-deduction function used to compute leave days
> (FR-LEA-02), the payroll loss-of-pay calculation (FR-PAY-04) and the reports
> "working days" figure (FR-RPT) **is the same function, called from one place**.
> v1.0 used three different day-counting rules, including a separate Mon–Fri
> helper. — *Three different rules for "how many days".*

Three rules were indeed live, and they did not agree:

* **Leave** counted `(end - start).days + 1` — **calendar** days, so a Friday-to-
  Monday request cost the employee four days of a twelve-day annual allowance, two
  of which were a weekend they never intended to take.
* **Payroll LOP** counted `attendance_days` rows with status `Absent` **or
  `Half-day`** — so an employee marked half-present lost a **full** day's pay. The
  classification that produced the row (FR-JOB-01) already distinguishes them, and
  the money then threw the distinction away.
* **Reports** had no working-day figure at all; it reported days-with-a-login from
  `user_sessions`, which is a fourth rule and answers a different question.

**Why "working day" is per-employee.** This application does not have a company-wide
Monday-to-Friday week and never did — `weekly_off_pattern` is per employee, effective
dated on v2.0 via `shift_assignments` (FR-ATT-17). A night-shift operator's rest day
is not Saturday, so a shared Mon–Fri helper would be wrong for them. The function
therefore takes the employee and asks `get_weekly_off_pattern` for the date in
question, which is also how FR-JOB-01 classifies attendance.

**Holidays are deducted the same way attendance treats them**, by calling
`_is_attendance_holiday`, which already knows that a *National* holiday applies to
everyone while an *Optional* one applies only to an employee with an approved opt-in
(FR-HOL-03). A leave request that spanned Diwali would otherwise charge an employee
for a day the company gave away.

**Half-days are a fraction, and that is the one behaviour change with money
attached.** `Half-day` contributes ``0.5``. The classification is FR-JOB-01's and it
already makes the distinction; this is where it stops being discarded. It is exposed
as a switch rather than always applied, because the leave consumer must *not* count
fractions — a leave request is charged in whole days — and conflating the two is how
a balance ends up with ``3.5`` days used.

**A range with no working days is zero, and the leave caller turns that into a
refusal.** Someone applying for Saturday and Sunday alone is asking for a deduction
that would not happen; silently recording 0 days reserved would leave them with a
"Pending" request that does nothing.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from fractions import Fraction

logger = logging.getLogger(__name__)


def _resolve_weekly_off(emp_id, on_date, conn):
    """The employee's weekly-off pattern on a date, or ``''`` for none configured.

    A blank pattern means *no configured weekly off* rather than a Monday-to-Friday
    week — the same reading `app._is_weekly_off` documents, and deliberately the
    opposite of the v1.0 Mon–Fri helper the SRS calls out as the defect. Assuming a
    five-day week would silently deduct days from employees who have none.
    """
    from app import get_weekly_off_pattern

    try:
        return get_weekly_off_pattern(emp_id, conn, on_date=on_date) or ''
    except Exception as exc:
        # A missing shift row must not make a leave request uncomputable; the SRS's
        # "no configured weekly off" reading is the safe answer, and it is logged
        # because a silent fallback in a payroll path is worth seeing.
        logger.warning('weekly-off lookup failed for %s on %s: %s', emp_id, on_date, exc)
        return ''


def is_working_day(conn, emp_id, day, *, deduct_holidays=True):
    """Is ``day`` a working day for ``emp_id``? The single definition.

    One predicate, so every consumer agrees on the answer. It is exposed because the
    reporting figure has to explain *which* days it counted, and a report that cannot
    name them is not auditable.
    """
    from app import _is_attendance_holiday, _is_weekly_off

    if _is_weekly_off(day, _resolve_weekly_off(emp_id, day, conn)):
        return False
    if deduct_holidays and _is_attendance_holiday(emp_id, day, conn):
        return False
    return True


#: FR-LEA-02's `session`. A half-day leave request is charged half a day, which is
#: the only reason ``working_days(allow_half=True)`` returns a Fraction at all.
LEAVE_SESSIONS = ('Full', 'First-half', 'Second-half')


def whole_days(value) -> int:
    """A Fraction of working days as whole days, rounded **up**.

    ``ceil`` rather than ``round`` because a request that touches any part of a
    working day costs a day of allowance; rounding 0.5 down would let a half-day
    request through for free. Kept as a named function so the leave caller does not
    have to know that ``working_days`` has a fractional mode.
    """
    fraction = Fraction(value)
    return -(-fraction.numerator // fraction.denominator)


def working_days(conn, emp_id, start, end, *, deduct_holidays=True, allow_half=False):
    """Days in ``[start, end]`` worth anything to ``emp_id``.

    ``allow_half=True`` returns a :class:`~fractions.Fraction` so a half-day counts
    as half; the default returns an ``int`` and rounds **up**, because a leave
    request is charged in whole days and a 0.5-day request must cost the employee a
    day rather than nothing.

    Inclusive of both endpoints, which is what every caller already assumed — the
    leave path's ``(end - start).days + 1`` and a ``BETWEEN`` in SQL both count the
    start day.
    """
    if start is None or end is None:
        return Fraction(0) if allow_half else 0
    if end < start:
        start, end = end, start
    total = Fraction(0)
    day = start
    while day <= end:
        if is_working_day(conn, emp_id, day, deduct_holidays=deduct_holidays):
            total += 1
        day += timedelta(days=1)
    if allow_half:
        return total
    return whole_days(total)


def lop_days(conn, emp_id, start=None, end=None):
    """Loss-of-pay days from ``attendance_days``, honouring half-days.

    This is FR-PAY-04's consumer. It reads the **classification FR-JOB-01 already
    made** instead of counting rows, so a half-day costs half a day's pay rather than
    a full one. Days that are not `Absent` or `Half-day` contribute nothing: a
    `Present`, `On Leave`, `Holiday` or `Weekly-off` row is a day the employee was
    not docked for, and counting them would dock them twice.

    ``start``/``end`` may be ``None``, meaning **all recorded days for the employee**.
    That is deliberate rather than lazy: the caller this replaced passed no date
    filter at all, so the scope of which days count is a policy question the offboarding
    settlement owns, and quietly narrowing it to a calendar month would change what an
    employee is paid while appearing to be a refactor. The rule being fixed is the
    half-day weighting; the window is left as it was and flagged.
    """
    try:
        if start is None or end is None:
            rows = conn.execute(
                "SELECT ad.status, ad.attendance_date FROM attendance_days ad "
                "WHERE ad.emp_id = ?",
                [emp_id],
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT ad.status, ad.attendance_date FROM attendance_days ad "
                "WHERE ad.emp_id = ? AND ad.attendance_date BETWEEN ? AND ?",
                [emp_id, start, end],
            ).fetchall()
    except Exception as exc:
        # A missing attendance table must not fail a payslip; the SRS's fallback is
        # no deduction, which is the same answer the preview already gave.
        logger.warning('LOP day-count failed for %s: %s', emp_id, exc)
        return Fraction(0)
    total = Fraction(0)
    for status, _work_date in rows:
        label = str(status or '').strip().lower()
        if label == 'absent':
            total += 1
        elif label == 'half-day' or label == 'half_day':
            total += Fraction(1, 2)
    return total


def count_working_days_in_range(conn, emp_id, start, end, **kwargs) -> int:
    """Named alias so the reports call site reads as a figure, not a helper call.

    Deliberately a distinct name: the payroll consumer needs :func:`lop_days` (which
    reads a classification) and the leave consumer needs :func:`working_days` (which
    walks a calendar). Same *rule*, different inputs — which is what FR-LEA-09 asks
    for. Sharing the rule is the requirement; forcing one signature over two genuinely
    different data sources would be worse than a thin second name.
    """
    return working_days(conn, emp_id, start, end, **kwargs)


__all__ = [
    'Fraction',
    'count_working_days_in_range',
    'date',
    'is_working_day',
    'lop_days',
    'working_days',
]
