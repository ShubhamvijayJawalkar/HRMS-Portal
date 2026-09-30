"""Optional-holiday opt-ins (FR-HOL-03).

The table exists. Nothing ever wrote to it. The consequence is not theoretical,
it is a wrong attendance record:

* the boot seed creates two **Optional** holidays (Diwali, Christmas);
* `_is_attendance_holiday` counts an Optional holiday as a holiday **only** for an
  employee with an `Approved` opt-in, which is the correct rule;
* with no route, no employee can ever have one, so on those dates the nightly
  FR-JOB-01 finalisation classified the seeded employee as **`Weekly-off`** where
  a company holiday was the right answer, and would have said `Absent` on any
  other day. A high-priority implemented requirement (FR-JOB-01) was producing a
  wrong answer because a Medium one had no route.

The SRS: "Opt-in/opt-out for Optional holidays, one active opt-in per employee
per holiday (unique constraint); approval queue for HR."

The state machine is deliberately tiny — ``Pending -> Approved | Rejected`` for the
approval, and ``-> Cancelled`` for the employee withdrawing — and the *interesting*
rules are the eligibility ones:

* **Only Optional holidays can be opted into.** A National holiday is a holiday
  for everyone; an opt-in for one is a mistake, and silently ignoring it would
  leave the employee thinking they had done something.
* **A Rejected opt-in does not block a new request.** Only ``Pending`` and
  ``Approved`` count as *active*, so an employee whose request was declined can
  ask again after the circumstances change, and the partial unique index is
  written on that same definition. This is why the constraint is
  ``WHERE status IN ('Pending', 'Approved')`` and not ``WHERE status <> ...``.
* **A holiday in the past cannot be opted into**, because the attendance it would
  affect has already been finalised and reruns would rewrite a published record.
* **Cancelling an Approved opt-in is allowed before the holiday**, and the
  attendance for that date is recomputed, because the employee's answer changed.

``actor`` is the ``policy.current_actor`` dict; this module takes plain values so
it is testable without a request context.
"""

from __future__ import annotations

from datetime import date, datetime

# The status set the canonical schema documents. `optins` is the one place that
# list is written, so a typo cannot leave a row the attendance query cannot read.
STATUSES = ('Pending', 'Approved', 'Rejected', 'Cancelled')

# "One active opt-in per employee per holiday". A Rejected row is not active, so a
# declined employee may ask again.
ACTIVE_STATUSES = frozenset({'Pending', 'Approved'})

MAX_COMMENT = 500


class HolidayOptInError(ValueError):
    """An opt-in request is not permitted or not valid."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _as_date(value):
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], '%Y-%m-%d').date()
    except ValueError:
        return None


def check_request(holiday, existing_optin, as_of=None) -> None:
    """May this employee request an opt-in for this holiday? Raises if not.

    ``holiday`` is ``(holiday_id, name, holiday_date, holiday_type)`` and
    ``existing_optin`` is the caller's current row for it —
    ``(optin_id, status)`` or ``None``.
    """
    holiday_id, name, holiday_date, holiday_type = holiday
    if str(holiday_type or '').lower() != 'optional':
        raise HolidayOptInError(
            f'{name} is a National holiday, so it applies to everyone and cannot be '
            'opted into', 409)
    today = as_of or date.today()
    when = _as_date(holiday_date)
    if when is None:
        raise HolidayOptInError('That holiday has no usable date', 409)
    if when <= today:
        raise HolidayOptInError(
            f'{name} is on {when.isoformat()}, which has already passed; its '
            'attendance has been finalised', 409)
    if existing_optin is not None and existing_optin[1] in ACTIVE_STATUSES:
        raise HolidayOptInError(
            f'You already have a {existing_optin[1].lower()} opt-in for {name}', 409)


def check_cancel(optin, as_of=None) -> None:
    """May this employee withdraw their opt-in? ``optin`` is ``(optin_id, status, holiday_date)``."""
    _optin_id, status, holiday_date = optin
    if status not in ACTIVE_STATUSES:
        raise HolidayOptInError(f'This opt-in is already {status.lower()}', 409)
    when = _as_date(holiday_date)
    if when is not None and when <= (as_of or date.today()):
        raise HolidayOptInError(
            'This holiday has already passed, so the opt-in can no longer be '
            'withdrawn', 409)


def check_review(optin, target: str) -> str:
    """May this request move to ``target``? Returns it, or raises a 409."""
    _optin_id, status = optin[0], optin[1]
    if target not in ('Approved', 'Rejected'):
        raise HolidayOptInError(
            f'A review must approve or reject; expected one of Approved, Rejected '
            f'(got {target!r})')
    if status != 'Pending':
        raise HolidayOptInError(
            f'This request is already {status} and cannot be reviewed again', 409)
    return target


def active_optin(existing_optin):
    """The existing row if it is active, else None — the "one active" test."""
    if existing_optin is not None and existing_optin[1] in ACTIVE_STATUSES:
        return existing_optin
    return None
