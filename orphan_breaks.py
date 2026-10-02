"""FR-AUTH-14 / FR-JOB-02 — auto-close breaks left Active by a forgotten break-end.

The SRS asks for both, in one sentence and at **high** priority:

* FR-AUTH-14: "a scheduled job purges expired reset tokens hourly and auto-closes
  breaks Active for more than 12 hours"
* FR-JOB-02: "Hourly: purge expired reset tokens; close orphaned breaks"

Only the first half of FR-AUTH-14 shipped. `cleanup_expired_tokens` purged tokens and
idempotency keys and nothing else, so a break whose end was never pressed stayed
`Active` **indefinitely**. The consequences are not cosmetic:

* ``endBreak`` auto-ends any active break, so the employee is not blocked from
  starting another — but the stale row is what attendance and the payroll loss-of-pay
  calculation read, and it counts as *still on break* for ever.
* FR-ATT-09's shift summary adds open break time to the worked figure, so a forgotten
  end **inflates the hours an employee appears to have worked** — in the employer's
  favour on paper and against them in reality, and impossible to detect without
  spotting the row.
* Nothing else in the system ever revisits it.

**Why 12 hours is a threshold and not a duration.** The gap between `start_time` and
the sweep says how long the *row* has been open, not how long the break was. An
employee who forgot at 11:00 and whose row is swept at 23:00 has not taken a 12-hour
break, and recording one would invent an absence and a loss-of-pay deduction out of a
forgotten button press. So the sweep decides **status** (this row can no longer be
believed to be running) and records **duration** from the break type's own daily limit
— the most that break could have been worth. The alternative of closing at
`start_time + 12 h` would charge twelve hours of pay for a forgotten UI click.

That is a reasoned guess, not a fact, so it is **audited, notified, and reversible by
an admin** rather than being silently absorbed into a payslip. The canonical schema
already anticipated this: ``breaks.ended_reason`` carries
``orphan_timeout|admin_dispose|auto_end_new_break``, so ``orphan_timeout`` is the
value written here and the reason is queryable afterwards.

**The write is conditional.** ``UPDATE ... WHERE status = 'Active'`` means two pods
sweeping at once give one winner and one no-op, rather than two notifications and two
audit rows for the same break. FR-JOB-05's leader election makes that unlikely, but
the jobs are idempotent by design elsewhere (accrual grants, outbox claims) and this
belongs to that family too.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

#: FR-AUTH-14's "Active for more than 12 hours". Read from the environment so a
#: deployment with a longer or shorter shift pattern can tune it, because the number
#: is a policy decision rather than a constant.
ORPHAN_AFTER = timedelta(hours=int(__import__('os').getenv('ORPHAN_BREAK_HOURS', '12')))

#: Used when the break type has no limit row, or the row disappeared. The largest
#: limit the seed defines is Lunch at 60; 60 is the conservative choice here because
#: guessing low under-records time (the employee loses at most a break's worth) while
#: guessing high can manufacture a loss-of-pay deduction.
FALLBACK_MAX_MINUTES = 60


def orphaned_breaks(conn, now: datetime | None = None) -> list[dict]:
    """Active breaks older than :data:`ORPHAN_AFTER`, newest-orphaned last.

    Read-only, so a caller can report what *would* be closed before closing it — which
    is what an operator reviewing a payslip adjustment needs, and what makes this
    function safe to call from a read path.
    """
    now = now or datetime.now()
    cutoff = now - ORPHAN_AFTER
    rows = conn.execute(
        "SELECT b.break_id, b.emp_id, b.break_type, b.start_time, "
        "       COALESCE(t.daily_limit_minutes, ?) AS max_minutes "
        "FROM breaks b "
        "LEFT JOIN break_types t ON t.break_type = b.break_type "
        "WHERE b.status = 'Active' AND b.end_time IS NULL AND b.start_time < ? "
        "ORDER BY b.start_time",
        [FALLBACK_MAX_MINUTES, cutoff],
    ).fetchall()
    found = []
    for break_id, emp_id, break_type, start_time, max_minutes in rows:
        age = now - start_time if start_time else None
        found.append({
            'break_id': break_id,
            'emp_id': emp_id,
            'break_type': break_type,
            'start_time': start_time,
            'open_for': age,
            # Never more than the type allows, and never more than actually elapsed
            # — a row created 13 hours ago whose type caps at 15 minutes records 15.
            'minutes': max(0, min(int(max_minutes or FALLBACK_MAX_MINUTES),
                                  int(age.total_seconds() // 60) if age else 0)),
        })
    return found


def close_orphaned_breaks(conn, now: datetime | None = None) -> dict:
    """Close every orphaned break. Returns what happened, for the audit row and the log.

    ``{'scanned', 'closed', 'capped', 'minutes'}`` — ``capped`` counts the breaks whose
    recorded duration came from the type's limit rather than from elapsed time, because
    those are the ones where a human may disagree and want to correct.
    """
    now = now or datetime.now()
    scanned = 0
    closed = 0
    capped = 0
    minutes = 0
    for candidate in orphaned_breaks(conn, now):
        scanned += 1
        # Conditional on the state this decision was made on (CC-04): a second sweep
        # racing this one updates nothing instead of double-closing.
        result = conn.execute(
            "UPDATE breaks SET end_time = ?, duration_minutes = ?, "
            "status = 'Orphaned', ended_reason = 'orphan_timeout' "
            "WHERE break_id = ? AND status = 'Active'",
            [now, candidate['minutes'], candidate['break_id']],
        )
        if result.rowcount != 1:
            continue
        closed += 1
        minutes += candidate['minutes']
        if candidate['open_for'] is not None and (
            candidate['open_for'].total_seconds() / 60 > candidate['minutes']
        ):
            capped += 1
        logger.warning(
            'Auto-closed orphaned break %s for %s (%s, open since %s, recorded %d min) '
            '— the break-end was never pressed. Correct it via an admin break disposal '
            'if that is wrong.',
            candidate['break_id'], candidate['emp_id'], candidate['break_type'],
            candidate['start_time'], candidate['minutes'],
        )
    return {'scanned': scanned, 'closed': closed, 'capped': capped, 'minutes': minutes}
