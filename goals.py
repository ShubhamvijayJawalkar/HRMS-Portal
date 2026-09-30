"""Goal ownership and rating rules (FR-PERF-01).

Reading `goals_api` off the traceability matrix turned up three defects, and the
first is the reason this module exists:

* **`POST /api/goals` has never worked.** The insert was a bare
  ``INSERT INTO goals VALUES (?, ?, ...)`` with ten placeholders against a
  nine-column table, so every create returned 500. The seed used an explicit
  column list, which is why the seed worked and nobody noticed. No test created a
  goal, and the public-flip probe had no goals write flow, so nothing caught it.
* **`PUT /api/goals/<id>` had no ownership check at all** — ``@login_required``
  and an id from the path, so any authenticated user could rewrite any goal in
  the company, including setting ``status`` and thereby skipping the rating flow
  entirely. Goal ids are sequential integers, so they are guessable. The *list*
  was already correctly scoped; only the write was open.
* **`PUT /api/goals/<id>/rate` had no manager check and no self-rating block.**
  The SRS says "rating 1-5 by manager (not self)". The route was
  ``@admin_required``, so an employee could not reach it at all, but an admin
  could rate their own goal.

The rules live here rather than in the routes so the browser, the API and any
future caller get the same answer.

``actor`` is ``(emp_id, role, department, manager_emp_id)`` read from the
database by ``app._lifecycle_actor``, never the session copy.
"""

from __future__ import annotations

from datetime import date, datetime

# `status` and `rating` are deliberately absent: they are the rating flow's to
# set, and letting an edit patch set `status` was the way to skip rating. A goal
# reaches Completed by being rated, and by nothing else.
EDITABLE_FIELDS = ('title', 'description', 'target_date', 'weight')

# The only statuses a goal may be in. Mirrors what the seed writes and what the
# rating flow produces; `ratings` is what a rater may move it to.
STATUSES = frozenset({'Active', 'Completed'})
RATINGS = frozenset({1, 2, 3, 4, 5})

WEIGHT_MIN, WEIGHT_MAX = 1, 10

HR_ROLES = frozenset({'HR', 'Admin', 'Super Admin'})
KNOWN_ROLES = frozenset({'Employee', 'Team Leader'}) | HR_ROLES | frozenset({'Finance'})

MAX_TITLE = 200
MAX_DESCRIPTION = 2000


class GoalError(ValueError):
    """A goal change is not permitted or not valid."""

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
        raise GoalError('target_date must be YYYY-MM-DD') from None


def validate_create(payload) -> dict:
    """Validate a create payload and return the cleaned values (CC-12).

    ``emp_id`` is *not* taken from here: it always comes from the session (CC-10),
    so the v1.0 impersonation route is closed rather than merely discouraged.
    """
    unknown = sorted(set(payload) - {'title', 'description', 'target_date', 'weight', 'emp_id'})
    if unknown:
        raise GoalError(f'unknown fields: {", ".join(unknown)}')
    title = str(payload.get('title') or '').strip()
    if not title:
        raise GoalError('title is required')
    if len(title) > MAX_TITLE:
        raise GoalError(f'title must be {MAX_TITLE} characters or fewer')
    description = payload.get('description')
    if description is not None:
        description = str(description).strip() or None
        if description and len(description) > MAX_DESCRIPTION:
            raise GoalError(f'description must be {MAX_DESCRIPTION} characters or fewer')
    weight = payload.get('weight', 1)
    if weight in (None, ''):
        weight = 1
    try:
        weight = int(weight)
    except (TypeError, ValueError):
        raise GoalError('weight must be a whole number') from None
    if not WEIGHT_MIN <= weight <= WEIGHT_MAX:
        raise GoalError(f'weight must be between {WEIGHT_MIN} and {WEIGHT_MAX}')
    return {
        'title': title,
        'description': description,
        'target_date': _as_date(payload.get('target_date')),
        'weight': weight,
    }


def validate_patch(payload) -> dict:
    """Validate an update payload; returns only the fields that may be written."""
    unknown = sorted(set(payload) - set(EDITABLE_FIELDS) - {'status', 'rating'})
    if unknown:
        raise GoalError(f'unknown fields: {", ".join(unknown)}')
    if 'status' in payload or 'rating' in payload:
        # A 400 rather than a silent ignore: silently dropping a field the caller
        # asked for is how a client ends up believing a goal was completed.
        raise GoalError(
            'status and rating are set by the rating flow (PUT /api/goals/<id>/rate), '
            'not by an edit')
    if not payload:
        raise GoalError('nothing to update')
    cleaned = validate_create({**payload, 'title': payload.get('title', 'unchanged')})
    return {field: cleaned[field] for field in EDITABLE_FIELDS if field in payload}


def is_hr(role: str) -> bool:
    return role in HR_ROLES


def manages(actor, owner_emp_id: str) -> bool:
    """Is the actor the reporting manager of this goal's owner?"""
    if actor is None or actor[0] == owner_emp_id:
        # The `actor[0] == owner` guard also protects against a user whose own
        # manager_emp_id points at them.
        return False
    return bool(actor[3]) and actor[3] == owner_emp_id


def check_edit(actor, goal) -> None:
    """May the actor edit this goal? Raises ``GoalError`` if not.

    The owner may edit their own goal; so may their manager and HR/Admin. The
    SRS is silent on managers editing, but a manager who cannot correct a
    mis-stated goal is not much use, and the *rating* — the part that carries
    judgement — is separately restricted.
    """
    if actor is None:
        raise GoalError('Not authenticated', 401)
    if actor[1] not in KNOWN_ROLES:
        raise GoalError('Unknown role', 403)
    owner_emp_id = goal[0]
    if actor[0] == owner_emp_id or is_hr(actor[1]) or manages(actor, owner_emp_id):
        return
    raise GoalError('You may only edit your own goals', 403)


def check_rating(actor, goal) -> None:
    """May the actor rate this goal? Raises ``GoalError`` if not.

    FR-PERF-01: "rating 1-5 by manager (not self)". So the owner's manager or
    HR/Admin, and explicitly not the owner — the SRS calls that out because the
    v1.0 route allowed it.
    """
    if actor is None:
        raise GoalError('Not authenticated', 401)
    if actor[1] not in KNOWN_ROLES:
        raise GoalError('Unknown role', 403)
    owner_emp_id = goal[0]
    if actor[0] == owner_emp_id:
        raise GoalError('You cannot rate your own goal; the rating is your manager\'s', 403)
    if is_hr(actor[1]) or manages(actor, owner_emp_id):
        return
    raise GoalError('Only the goal owner\'s manager or HR/Admin may rate a goal', 403)


def check_rating_value(value) -> int:
    """The rating itself, or a 400."""
    if value in (None, ''):
        raise GoalError('rating is required')
    try:
        rating = int(value)
    except (TypeError, ValueError):
        raise GoalError('rating must be a whole number from 1 to 5') from None
    if rating not in RATINGS:
        raise GoalError('rating must be between 1 and 5')
    return rating
