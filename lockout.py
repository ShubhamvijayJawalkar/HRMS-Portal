"""FR-AUTH-03 — consecutive-failure account lockout.

The SRS is precise and there is little to decide: *"10 consecutive failures
within 15 minutes locks the account for 15 minutes and notifies the user by
email."* Those four numbers are the module, and they are asserted by a test so
they cannot drift into something a deployment did not agree to.

**Why this is in the database and not in Redis.** The SRS's flow diagram puts the
counter in Redis ("failed-attempt counter += 1 (Redis, 15 min window)"), and
Redis would do the job. It is not used here, and the deviation is deliberate:

* The app treats Redis as **optional** — sessions fall back to signed cookies and
  dev/CI needs no Redis at all. A lockout that silently stops existing when Redis
  is unreachable is not a weaker lockout, it is *no* lockout, and it fails open in
  exactly the situation an attacker can arrange.
* The same reasoning already decided the password-policy check: a check that
  silently fails when the network is down does not exist. This is that rule
  applied to the second account-level defence.
* The rows are already on ``users``, the increment is a single-row atomic
  ``UPDATE``, and it is transactional with everything else about the login.

So the window is enforced in SQL-land columns instead of a Redis TTL. The
semantics are identical: a streak that goes quiet for longer than the window is no
longer consecutive, so it starts again from zero.

**The cost every lockout design has, stated rather than hidden.** Ten wrong
passwords locks an account for a quarter of an hour, and an attacker who knows an
employee ID can therefore deny that employee access on demand. That is inherent
to the requirement, not a defect in this implementation. Three things bound it:
the lock expires by itself, the lockout is emailed to the account owner (so it is
never a silent denial), and an administrator can clear it immediately. The
"shared NAT office" variant — one attacker on the same egress IP poisons many
accounts — is the reason the window and the streak both exist rather than a
simpler lifetime counter.

**The counter never distinguishes a correct password from a wrong one to the
caller.** A locked account is refused *after* the password is verified, and the
route answers every failure the same way, because FR-AUTH-02 requires it and a
lockout is just one more state that would otherwise leak.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

#: The SRS's numbers. Ten failures, inside a fifteen-minute window, produce a
#: fifteen-minute lock.
MAX_FAILED_ATTEMPTS = 10
FAILURE_WINDOW = timedelta(minutes=15)
LOCK_DURATION = timedelta(minutes=15)

#: Account states for which counting a failure is pointless — the password is
#: never going to be accepted, so the counter would just grow behind a refusal
#: that already happened. They are still refused, and refused identically.
_REFUSAL_STATES = ('Blocked', 'Inactive', 'Pre-hire', 'Archived')


def refusal_state(status) -> bool:
    """Is this account state one that cannot sign in at all?"""
    return status in _REFUSAL_STATES


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now()


def lock_remaining(conn, emp_id: str, now: datetime | None = None) -> timedelta | None:
    """How much longer this account is locked, or None if it is not locked.

    Returns a *duration* rather than a boolean so the caller can say how long is
    left; "try again in a moment" when the answer is fourteen minutes is the kind
    of sentence that makes people try eleven more passwords.
    """
    row = conn.execute(
        'SELECT locked_until FROM users WHERE emp_id = ?', [emp_id]
    ).fetchone()
    if not row or not row[0]:
        return None
    left = row[0] - _now(now)
    return left if left > timedelta(0) else None


def is_locked(conn, emp_id: str, now: datetime | None = None) -> bool:
    return lock_remaining(conn, emp_id, now) is not None


def register_failure(conn, emp_id: str, now: datetime | None = None):
    """Count one wrong password. Returns ``(attempts, just_locked)``.

    ``just_locked`` is True only on the failure that *caused* the lock, so the
    caller sends exactly one notification rather than one per subsequent attempt
    against an already-locked account.
    """
    now = _now(now)
    row = conn.execute(
        'SELECT failed_attempts, last_failed_login, locked_until '
        'FROM users WHERE emp_id = ?', [emp_id],
    ).fetchone()
    if not row:
        return 0, False

    attempts, last_failed, locked_until = row[0] or 0, row[1], row[2]

    # Already locked: nothing to count. The streak is frozen, and the caller
    # refuses without touching the counter.
    if locked_until and locked_until > now:
        return attempts, False

    # The streak must be *consecutive within the window*. A last failure older
    # than the window breaks it, so this one starts a new count at 1 — not at 2.
    if last_failed is None or (now - last_failed) > FAILURE_WINDOW:
        attempts = 0
    attempts += 1

    if attempts >= MAX_FAILED_ATTEMPTS:
        # Reset the streak as we lock. Otherwise unlocking 14 minutes early would
        # leave nine failures banked and the next wrong password would re-lock the
        # account instantly, which reads as "the lock is not working".
        conn.execute(
            'UPDATE users SET failed_attempts = 0, last_failed_login = ?, '
            'locked_until = ? WHERE emp_id = ?',
            [now, now + LOCK_DURATION, emp_id],
        )
        logger.warning(
            'Account %s locked after %d consecutive failures within %d minutes',
            emp_id, attempts, int(FAILURE_WINDOW.total_seconds() // 60),
        )
        return attempts, True

    conn.execute(
        'UPDATE users SET failed_attempts = ?, last_failed_login = ? WHERE emp_id = ?',
        [attempts, now, emp_id],
    )
    return attempts, False


def register_success(conn, emp_id: str, now: datetime | None = None) -> None:
    """A successful login breaks the streak.

    This is what "consecutive" means, and it is why an employee who fat-fingers
    their password twice, signs in correctly and fat-fingers it twice more is not
    locked out — whereas one who cannot sign in at all is.
    """
    conn.execute(
        'UPDATE users SET failed_attempts = 0, last_failed_login = NULL, '
        'locked_until = NULL WHERE emp_id = ?',
        [emp_id],
    )


def unlock(conn, emp_id: str) -> bool:
    """Administrator recovery: clear the lock now.

    Not a status change. ``Blocked``/``Archived`` are deliberate account states an
    administrator sets through the employee lifecycle; a lockout is a *temporary*
    consequence of failed sign-ins, and folding it into ``status`` would make a
    fifteen-minute nuisance indistinguishable from a sanctioned decision — in the
    database, in the admin UI, and in the audit trail. False = the account was not
    locked, so the caller can answer honestly.
    """
    row = conn.execute(
        'SELECT locked_until FROM users WHERE emp_id = ?', [emp_id]
    ).fetchone()
    was_locked = bool(row and row[0] and row[0] > _now())
    if was_locked:
        conn.execute(
            'UPDATE users SET failed_attempts = 0, last_failed_login = NULL, '
            'locked_until = NULL WHERE emp_id = ?',
            [emp_id],
        )
    return was_locked


def status_for(conn, emp_id: str, now: datetime | None = None) -> dict:
    """What the admin directory needs to say about a lockout."""
    row = conn.execute(
        'SELECT failed_attempts, locked_until FROM users WHERE emp_id = ?', [emp_id]
    ).fetchone()
    if not row:
        return {'locked': False, 'attempts': 0, 'locked_until': None}
    remaining = lock_remaining(conn, emp_id, now)
    return {
        'locked': remaining is not None,
        'attempts': row[0] or 0,
        'locked_until': row[1].isoformat() if row[1] else None,
    }


def status_for_many(conn, emp_ids, now: datetime | None = None) -> dict:
    """The same, for a whole page of the directory — in **one** query.

    An admin list of 20 employees locked out one at a time is 20 round trips on a
    page that has to render quickly, and it is the *third* copy of the "is this
    locked" arithmetic if done inline at the call site. One query, one rule, both
    living here.
    """
    emp_ids = [e for e in emp_ids if e]
    if not emp_ids:
        return {}
    placeholders = ','.join('?' for _ in emp_ids)
    rows = conn.execute(
        f'SELECT emp_id, failed_attempts, locked_until FROM users '
        f'WHERE emp_id IN ({placeholders})',
        emp_ids,
    ).fetchall()
    now = _now(now)
    return {
        emp_id: {
            'locked': bool(locked_until and locked_until > now),
            'attempts': attempts or 0,
            'locked_until': locked_until.isoformat() if locked_until else None,
        }
        for emp_id, attempts, locked_until in rows
    }
