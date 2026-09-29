"""Expense claim state machine (FR-EXP-03).

The v1.0 route accepted any of the four statuses from any state, from anyone who
reached it:

    PUT /api/expenses/<id>/status  {"status": "Paid"}

which made three things possible that should not be: an admin could approve
their own claim, a claim could jump `Pending -> Paid` with no approval at all,
and a second write could move an already-`Paid` claim back to `Pending`. The
v1.0 "Paid bypasses the manager check" deviation is recorded in Appendix A-11 of
the SRS; this module is the fix.

The SRS transition table (FR-EXP-03):

    Pending  -> Approved | Rejected   by the claim owner's manager, or HR/Admin
    Approved -> Paid                   by Finance or Admin/Super Admin only
    Rejected, Paid                     terminal

Four rules are enforced here rather than in the route, so the browser, the API
and any future caller get the same answer:

* **Self-approval is blocked** (`actor != claim owner`), which the SRS states
  explicitly and which nothing enforced before.
* **Only the listed edges exist.** There is no catch-all branch: a target not in
  ``TRANSITIONS[current]`` is refused with a 409, and a terminal status says so.
* **Authority is per-target.** Approving and *paying* are different acts, so
  ``Paid`` is Finance/Admin only while ``Approved``/``Rejected`` are manager or
  HR. Appendix A-11 is precisely the note that v1.0 let any logged-in user pay.
* **Rejection requires a reason**, because "rejected" with nothing to act on is
  not useful to the claimant.

`actor` is ``(emp_id, role, department, manager_emp_id)`` read from the database
by ``app._lifecycle_actor`` — never the session copy, so a role change takes
effect immediately rather than at the next login.
"""

from __future__ import annotations

# The only legal transitions. Anything absent here is refused, which is what
# makes "strict" true rather than aspirational.
TRANSITIONS: dict[str, frozenset[str]] = {
    'Pending': frozenset({'Approved', 'Rejected'}),
    'Approved': frozenset({'Paid'}),
    'Rejected': frozenset(),
    'Paid': frozenset(),
}

ALL_STATUSES = frozenset(TRANSITIONS) | {t for targets in TRANSITIONS.values() for t in targets}

TERMINAL_STATUSES = frozenset(s for s, targets in TRANSITIONS.items() if not targets)

# The states a claim can be moved *out of*. The list endpoint uses this to work
# out which claims a reviewer needs to see, so the answer is derived from the
# same table the write enforces rather than duplicated as a literal.
ACTIVE_STATUSES = frozenset(TRANSITIONS) - TERMINAL_STATUSES

HR_ROLES = frozenset({'HR', 'Admin', 'Super Admin'})
ADMIN_ROLES = frozenset({'Admin', 'Super Admin'})
FINANCE_ROLES = frozenset({'Finance'})
KNOWN_ROLES = frozenset({'Employee', 'Team Leader'}) | HR_ROLES | FINANCE_ROLES

# Paying money is not the same act as approving it, so the "may" rule is
# attached to the target rather than to the route.
_MAY_APPROVE = frozenset({'Approved', 'Rejected'})
_MAY_PAY = frozenset({'Paid'})

REJECTION_REASON_REQUIRED = 'A rejection reason is required so the claimant can act on it'


class ExpenseTransitionError(ValueError):
    """An expense claim status change is not permitted."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def is_paying_role(role: str) -> bool:
    """Finance, or an administrator as the fallback when there is no Finance user."""
    return role in FINANCE_ROLES or role in ADMIN_ROLES


def is_hr_role(role: str) -> bool:
    return role in HR_ROLES


def is_manager_of(actor, owner_emp_id: str) -> bool:
    """Is the actor the reporting manager of this claim's owner?"""
    if actor is None:
        return False
    actor_emp_id, _role, _dept, manager_emp_id = actor
    if actor_emp_id == owner_emp_id:
        # Also guards a data problem: a user whose own manager_emp_id points at
        # them would otherwise be treated as their own reporting line.
        return False
    return bool(manager_emp_id) and manager_emp_id == owner_emp_id


def actionable_statuses(actor) -> list[str]:
    """The claim states this actor could move out of, for *someone else's* claim.

    The list endpoint uses this to decide what a reviewer needs to see, so the
    answer comes from the same rules the write enforces rather than being
    duplicated as a literal. Ownership is deliberately not a factor here: the
    question is "which states could I act on", not "which of my own claims".
    """
    if actor is None:
        return []
    _actor_emp_id, role, _dept, _manager = actor
    if role not in KNOWN_ROLES:
        return []
    out = []
    for status in sorted(ACTIVE_STATUSES):
        for target in TRANSITIONS[status]:
            if target in _MAY_PAY:
                if is_paying_role(role) or is_hr_role(role):
                    out.append(status)
                    break
            elif is_hr_role(role) or is_manager_of(actor, 'SOMEONE-ELSE'):
                out.append(status)
                break
    return sorted(set(out))


def permitted_targets(actor, claim) -> list[str]:
    """The statuses this actor may move this claim to right now.

    The API returns this so the client renders only valid actions, instead of
    offering a button that is guaranteed to be refused.
    """
    if actor is None:
        return []
    actor_emp_id, role, _dept, _manager = actor
    owner_emp_id, current_status = claim[0], claim[1]
    if actor_emp_id == owner_emp_id or role not in KNOWN_ROLES:
        return []
    out = []
    for target in sorted(TRANSITIONS.get(current_status, frozenset())):
        if target in _MAY_PAY:
            if is_paying_role(role) or is_hr_role(role):
                out.append(target)
        elif is_hr_role(role) or is_manager_of(actor, owner_emp_id):
            out.append(target)
    return out


def check_transition(actor, claim, target: str, reason: str | None = None) -> str:
    """Validate the whole move and return ``target``, or raise.

    ``claim`` is ``(emp_id, status, amount)`` as read from the row. The order is
    deliberate — identity, then the legality of the edge, then authority for this
    specific target, then the reason requirement — so the message a user sees is
    about the thing they actually got wrong.

    Raises ``ExpenseTransitionError``: 401 unauthenticated, 403 authorisation,
    409 a state that is stale or illegal, 400 a malformed request.
    """
    if actor is None:
        raise ExpenseTransitionError('Not authenticated', 401)
    actor_emp_id, role, _dept, _manager = actor
    owner_emp_id, current_status = claim[0], claim[1]

    if role not in KNOWN_ROLES:
        raise ExpenseTransitionError('Unknown role', 403)
    if actor_emp_id == owner_emp_id:
        raise ExpenseTransitionError('You cannot act on your own expense claim', 403)
    if target not in ALL_STATUSES:
        raise ExpenseTransitionError(
            f'Unknown status {target!r}; expected one of {", ".join(sorted(ALL_STATUSES))}')
    if target not in TRANSITIONS.get(current_status, frozenset()):
        if current_status in TERMINAL_STATUSES:
            raise ExpenseTransitionError(
                f'This claim is {current_status}, which is a final state', 409)
        allowed = ', '.join(sorted(TRANSITIONS.get(current_status, frozenset())))
        raise ExpenseTransitionError(
            f'Cannot move an expense claim from {current_status} to {target} '
            f'(allowed: {allowed})', 409)
    if target == 'Rejected' and not (reason or '').strip():
        raise ExpenseTransitionError(REJECTION_REASON_REQUIRED)

    if target in _MAY_PAY:
        if not (is_paying_role(role) or is_hr_role(role)):
            raise ExpenseTransitionError(
                'Only Finance or an Admin may mark a claim Paid', 403)
    elif not (is_hr_role(role) or is_manager_of(actor, owner_emp_id)):
        raise ExpenseTransitionError(
            "Only the claim owner's manager or HR/Admin may review this claim", 403)
    return target
