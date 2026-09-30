"""Ticket state machine and visibility (FR-TKT-03, FR-TKT-04).

The SRS asks for two things the routes did not do:

    FR-TKT-03 "Detail view enforces the same visibility rule server-side, not just
                in the list query (defence in depth)."
    FR-TKT-04 "Status transitions Open → In Progress → Resolved → Closed, and
                Reopened (new) if a closed ticket receives a comment from the
                reporter within 7 days. Assignment audited."

The list query and the detail view implemented the visibility rule, so the
"defence in depth" the requirement describes only existed on the *read* paths.
Verifying it on the two write paths found:

* **`add_ticket_comment` had no visibility check at all** — only an existence
  check. An employee with no relationship to a ticket, who is correctly refused
  the detail view with a 403, could still append a comment to it. In the same
  probe the list hid the ticket, the detail view refused it, and the comment was
  accepted.
* **`update_ticket_status` had neither a visibility check nor a state machine** —
  `@login_required` and four accepted strings. Any authenticated user could move
  any ticket to any state, so any employee could close anyone else's ticket and
  mark it Resolved without anything being fixed.
* **No `Reopened` status exists**, and the transition chain was not enforced at
  all: `Open -> Closed` in one step, and `Closed -> Open` by anyone at any time.

Two decisions worth stating:

* **The chain is strict.** The SRS writes `Open → In Progress → Resolved →
  Closed` as a chain, so it is implemented as one. A one-line ticket can still
  reach `Resolved` in three calls, and skipping a step is refused rather than
  allowed and reported later — an SLA dashboard can only trust the states if the
  states mean what they say.
* **`Resolved -> In Progress` is allowed, and so is `Reopened -> In Progress`.**
  Those are the ways back when a "fix" turns out not to be one. A ticket has to
  be able to come back from a claim of resolution, or the only honest thing an
  employee can do with a badly-resolved ticket is file a second one.

The visibility rule is *widened* here in one respect, deliberately: the assignee
can now see and comment on a ticket assigned to them. The implemented rule was
"owner or a role that can view all", which meant a ticket assigned to an IT
officer was invisible to the very person asked to fix it. The SRS lists
"owner, assignee, matching department, or admin"; department scoping is still not
implemented, which the traceability matrix records.
"""

from __future__ import annotations

from datetime import datetime, timedelta

# The chain, strictly. Anything not listed is refused with a 409 naming what is.
TRANSITIONS: dict[str, frozenset[str]] = {
    'Open': frozenset({'In Progress'}),
    'In Progress': frozenset({'Resolved'}),
    'Resolved': frozenset({'Closed', 'In Progress'}),
    'Closed': frozenset(),
    'Reopened': frozenset({'In Progress'}),
}

ALL_STATUSES = frozenset(TRANSITIONS)
TERMINAL_STATUSES = frozenset({'Closed'})

PRIORITIES = frozenset({'Low', 'Medium', 'High', 'Critical'})
QUEUES = frozenset({'HR', 'IT'})

# FR-TKT-04: a Closed ticket reopens when *the reporter* comments on it within
# seven days of it closing. "Within seven days of closing" is read as seven days
# before now, and an older closure is not reopened — a stale ticket should go back
# through a fresh request rather than re-entering the queue weeks later.
REOPEN_WINDOW = timedelta(days=7)

MAX_SUBJECT = 200
MAX_COMMENT = 4000


class TicketError(ValueError):
    """A ticket change is not permitted or not valid."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def can_view(actor, ticket) -> bool:
    """The FR-TKT-02/03 visibility rule, in one place.

    ``actor`` is the ``policy.current_actor`` dict; ``ticket`` is
    ``(reporter_emp_id, assigned_to)``. A role that ``policy.can_view_all`` admits
    sees everything, so the callers pass the already-resolved decision for that.
    """
    if actor is None:
        return False
    if actor.get('emp_id') == ticket[0]:
        return True
    # An assignee who cannot see the ticket cannot work on it.
    return bool(ticket[1]) and actor.get('emp_id') == ticket[1]


def check_visibility(actor, ticket, *, can_view_all: bool) -> None:
    """Raise a 403 unless the actor may see (and therefore act on) the ticket."""
    if can_view_all or can_view(actor, ticket):
        return
    raise TicketError('You do not have access to this ticket', 403)


def check_transition(current: str, target: str) -> str:
    """Is ``current -> target`` a legal edge? Returns ``target`` or raises a 409."""
    if target not in ALL_STATUSES:
        raise TicketError(
            f'Unknown status {target!r}; expected one of {", ".join(sorted(ALL_STATUSES))}')
    allowed = TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        if current in TERMINAL_STATUSES:
            raise TicketError(
                f'This ticket is {current}; only a reporter comment reopens it', 409)
        raise TicketError(
            f'Cannot move a ticket from {current} to {target} '
            f'(allowed: {", ".join(sorted(allowed))})', 409)
    return target


def resolved_at_for(target: str, now: datetime):
    """The `resolved_at` a transition should write, or None to clear it."""
    if target == 'Resolved':
        return now
    return None


def should_reopen(ticket, commenter_emp_id: str, now: datetime) -> bool:
    """Does this comment reopen a Closed ticket? (FR-TKT-04.)

    ``ticket`` is ``(status, reporter_emp_id, closed_at)``. Both conditions matter:
    the comment must come from the *reporter* (not a bystander or the IT officer
    triaging) and land within the window. A ticket closed before the window opens
    stays closed.
    """
    status, reporter_emp_id, closed_at = ticket
    if status != 'Closed':
        return False
    if commenter_emp_id != reporter_emp_id:
        return False
    if closed_at is None:
        # Closed without a timestamp: treat it as just closed rather than
        # assuming it is ancient, so a comment still reopens it.
        return True
    if isinstance(closed_at, str):
        closed_at = datetime.fromisoformat(closed_at)
    return now - closed_at <= REOPEN_WINDOW


def validate_priority(value) -> str:
    """The priority, or a 400. Configurable per FR-TKT-01's SLA table."""
    priority = str(value or 'Medium').strip() or 'Medium'
    if priority not in PRIORITIES:
        raise TicketError(f'priority must be one of {", ".join(sorted(PRIORITIES))}')
    return priority


def validate_comment(text) -> str:
    comment = str(text or '').strip()
    if not comment:
        raise TicketError('comment is required')
    if len(comment) > MAX_COMMENT:
        raise TicketError(f'comment must be {MAX_COMMENT} characters or fewer')
    return comment
