"""FR-ATT-09 — one rule for "how many hours was this shift".

The SRS is precise about the figure and about why it needs a limit:

> Shift summary: ``shift_hours = last_logout − first_login`` (**not** sum of
> sessions) when both exist, else ``now − first_login`` for an open shift, **capped
> at the scheduled shift length +25%** to avoid a forgotten-logout skewing the figure
> — **flagged ``estimated: true`` in that case**.

**This is the second time this codebase has found the same defect on a day-counting
figure** (the first was FR-LEA-09's six rules for "how many days"). Two places
computed "how long was this shift" and **only one of them had the rule**: the payroll
finalisation path (``_attendance_worked_hours``) capped an open shift, and
``/api/user/shift-summary`` did not. So an employee who forgot to log out saw **30
hours** on their own dashboard while payroll was credited the capped figure — a
support call every time, and an employee whose screen disagrees with their payslip has
no reason to trust either one.

**Why the elapsed window and not the sum of sessions.** An employee who logs out for
lunch and back in has two sessions; summing them gives roughly the right answer, but an
employee who forgets to log out of the *second* session gets the first session's
correct total plus an unbounded remainder. Elapsed first-login-to-last-logout is the
window the employee was actually on the clock for, and it is the only definition that
does not need the sessions to be individually correct.

**Why the cap is on the open-shift branch only.** The SRS attaches it there —
"to avoid a forgotten-logout skewing the figure" — and that is also the only case where
the number is unreliable. A *closed* shift is real recorded data; capping it would
under-credit genuine overtime, which is a payroll decision rather than a data-quality
one. ``_attendance_worked_hours`` additionally applies a payroll ceiling over this
figure, and that stays there: it is a different rule with a different purpose, and
folding it in here would silently change what a long-but-legitimate day is worth.

**``estimated`` is reported for any open shift**, not only for a capped one. The SRS
says "in that case", which reads most directly as the capped case; reporting the
superset is strictly more informative and serves the stated intent, since an open
shift's end is *by definition* not yet observed. ``capped`` is reported separately so
the two states remain distinguishable — a consumer can tell "still running" from
"clamped down" without inferring one from the other.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

#: The SRS's "scheduled shift length +25%".
OPEN_SHIFT_ALLOWANCE = 0.25

#: Used only when an employee has no shift configured at all, where "scheduled shift
#: length" has no value to read. A shift-length default has to exist or the cap could
#: not be computed; it is named and overridable rather than buried as a literal,
#: because a hard-coded eight-hour week is the v1.0 defect FR-LEA-09 was written to
#: remove and this app genuinely has no company-wide working week (FR-ATT-17).
DEFAULT_SCHEDULED_HOURS = 8.0


def scheduled_hours(shift_start, shift_end) -> float:
    """Length of the scheduled shift, in hours.

    ``shift_end`` earlier than ``shift_start`` means a shift crossing midnight — the
    normal case for a night shift — so the negative span is rolled forward a day rather
    than clamped to zero, which would make every overnight shift look instantaneous.
    """
    if shift_start is None or shift_end is None:
        return DEFAULT_SCHEDULED_HOURS
    span = (shift_end - shift_start).total_seconds() / 3600
    if span < 0:
        span += 24
    return max(span, 0.0)


def open_shift_cap(shift_start, shift_end) -> float:
    """The largest number of hours an **open** shift may be credited with."""
    return scheduled_hours(shift_start, shift_end) * (1 + OPEN_SHIFT_ALLOWANCE)


def shift_hours(first_login, last_logout, shift_start, shift_end, now=None):
    """``(hours, estimated, capped)`` for one shift.

    ``estimated`` is True whenever there is no logout to measure against, so the figure
    is an observation in progress rather than a measurement. ``capped`` is True only
    when the 25% allowance actually bound, which is the condition the SRS names and the
    one that indicates something is wrong worth investigating.
    """
    if first_login is None:
        return 0.0, False, False

    if last_logout is not None and last_logout >= first_login:
        # A closed shift is real data: elapsed window, no cap. Summing sessions here
        # would be the v1.0 error the SRS explicitly rules out.
        hours = (last_logout - first_login).total_seconds() / 3600
        return round(max(hours, 0.0), 2), False, False

    now = now or datetime.now()
    cap = open_shift_cap(shift_start, shift_end)
    raw = max((now - first_login).total_seconds() / 3600, 0.0)
    capped = raw > cap
    if capped:
        logger.info(
            'open shift capped at %.2fh (raw %.2fh) — the logout was probably forgotten',
            round(cap, 2), round(raw, 2),
        )
    return round(min(raw, cap), 2), True, capped


def clock_cap(shift_start, shift_end):
    """The latest **wall-clock** time an open shift may be credited to.

    Distinct from the hours cap above: this bounds *when* the shift is treated as
    having ended, which is what ``_attendance_worked_hours`` needs in order to clamp
    ``last_event`` before subtracting. It is the same policy expressed against the
    clock rather than against a duration, and both are derived from the same
    ``scheduled_hours`` so they cannot drift.
    """
    if shift_end is None:
        return None
    span = scheduled_hours(shift_start, shift_end) * OPEN_SHIFT_ALLOWANCE
    return shift_end + timedelta(hours=span)
