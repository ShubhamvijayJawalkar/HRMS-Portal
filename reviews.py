"""Performance review and 360° feedback integrity (FR-PERF-02).

The SRS has two sentences for this requirement, and neither was enforced:

    "submit requires the reviewer to be the assigned reviewer for that review
     (v1.0 allowed any authenticated user to submit any review — Appendix A-18)"
    "360° feedback: reviewer cannot be the subject"

Reading `reviews_api` off the traceability matrix found four live defects:

* **`submit_review` had no reviewer check at all** — ``@login_required`` and an id
  from the path, so any authenticated user could submit (and thereby sign off)
  anybody's performance review. That is Appendix A-18, recorded in the SRS as a
  known gap, still open.
* **A self-review could be created.** ``emp_id`` and ``reviewer_id`` were both
  taken from the request body with no relationship check, so HR could open a
  review whose subject and reviewer were the same person — and then that person
  could sign it off themselves.
* **A submitted review could be reopened and rewritten.** The write was
  unconditional, so a 5/5 and an honest comment could be replaced with a 1/1 and
  "retracted" after the fact, with no audit row of either version.
* **360° feedback had no self-feedback guard**, so anyone could rate themselves
  five stars.

The rules live here rather than in the routes so the browser, the API and any
future caller get the same answer. ``actor`` is ``(emp_id, role, department,
manager_emp_id)`` read from the database by ``app._lifecycle_actor``.
"""

from __future__ import annotations

# Draft -> Submitted, and Submitted is final. A review is a signed statement; if
# it has to be corrected, a new cycle is the honest way to say so.
REVIEW_STATUSES = frozenset({'Draft', 'Submitted'})
TERMINAL_REVIEW_STATUSES = frozenset({'Submitted'})

# `overall_rating` is REAL, so the scale is a float range rather than a set of
# integers. The goals scale (1-5 whole numbers) is deliberately not reused:
# reviews and goals are scored differently in every organisation I have seen.
RATING_MIN, RATING_MAX = 1.0, 5.0

MAX_COMMENTS = 4000
MAX_PERIOD = 40

HR_ROLES = frozenset({'HR', 'Admin', 'Super Admin'})
KNOWN_ROLES = frozenset({'Employee', 'Team Leader'}) | HR_ROLES | frozenset({'Finance'})

# The 360° categories, so a caller cannot invent one. An empty category is
# allowed and stored as NULL: the SRS does not require one, and the column is
# nullable.
FEEDBACK_CATEGORIES = frozenset({
    'Leadership', 'Communication', 'Collaboration', 'Technical', 'Reliability', 'Other',
})


class ReviewError(ValueError):
    """A review or feedback change is not permitted or not valid."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _user_exists(conn, emp_id: str) -> bool:
    if not emp_id:
        return False
    try:
        return bool(conn.execute(
            'SELECT 1 FROM users WHERE emp_id = ?', [emp_id]
        ).fetchone())
    except Exception:
        return False


def validate_assignment(conn, emp_id, reviewer_id, review_period) -> dict:
    """Validate a review assignment and return the cleaned values.

    The self-review check is the substantive one. HR assigning a review is a
    legitimate act, but HR assigning one to the subject is a review with no
    reviewer, and the whole integrity property of the feature is that somebody
    else signs it.
    """
    subject = str(emp_id or '').strip()
    reviewer = str(reviewer_id or '').strip()
    period = str(review_period or '').strip()
    if not subject or not reviewer or not period:
        raise ReviewError('emp_id, reviewer_id and review_period are required')
    if len(period) > MAX_PERIOD:
        raise ReviewError(f'review_period must be {MAX_PERIOD} characters or fewer')
    if not _user_exists(conn, subject):
        raise ReviewError(f'No such employee: {subject}', 404)
    if not _user_exists(conn, reviewer):
        raise ReviewError(f'No such reviewer: {reviewer}', 404)
    if subject == reviewer:
        raise ReviewError(
            'A review needs a different reviewer from its subject; a self-review '
            'has nobody to sign it', 409)
    return {'emp_id': subject, 'reviewer_id': reviewer, 'review_period': period}


def validate_rating(value, field='rating') -> float:
    """The rating itself, or a 400."""
    if value in (None, ''):
        raise ReviewError(f'{field} is required')
    try:
        rating = float(value)
    except (TypeError, ValueError):
        raise ReviewError(f'{field} must be a number between {RATING_MIN:g} and {RATING_MAX:g}') from None
    if not RATING_MIN <= rating <= RATING_MAX:
        raise ReviewError(f'{field} must be between {RATING_MIN:g} and {RATING_MAX:g}')
    return rating


def check_submit(actor, review) -> None:
    """May the actor submit this review? Raises ``ReviewError`` if not.

    **Only the assigned reviewer.** HR and Admin are *not* given a bypass, and
    that is a deliberate reading of the SRS rather than an omission: "submit
    requires the reviewer to be the assigned reviewer" exists precisely to stop a
    review being signed by somebody who did not write it, and an HR override
    would reintroduce exactly that. If HR needs to correct a review, the honest
    route is a new cycle, not an edit to a signed one.
    """
    if actor is None:
        raise ReviewError('Not authenticated', 401)
    if actor[1] not in KNOWN_ROLES:
        raise ReviewError('Unknown role', 403)
    reviewer_id = review[1]
    if actor[0] != reviewer_id:
        raise ReviewError(
            'Only the reviewer assigned to this review may submit it', 403)
    if actor[0] == review[0]:
        # Unreachable via validate_assignment, but the invariant is cheap to
        # assert at the point where it matters.
        raise ReviewError('You cannot submit a review of yourself', 403)


def validate_feedback(conn, actor, subject_emp_id, rating, category, comment) -> dict:
    """Validate a 360° feedback submission end to end.

    FR-PERF-02's second sentence: "360° feedback: reviewer cannot be the subject".
    The reviewer id always comes from the session, so the subject is the only
    choice, and this is where that choice is checked.
    """
    if actor is None:
        raise ReviewError('Not authenticated', 401)
    if not _user_exists(conn, subject_emp_id):
        raise ReviewError(f'No such employee: {subject_emp_id}', 404)
    if actor[0] == subject_emp_id:
        raise ReviewError('You cannot give 360° feedback about yourself', 403)
    cleaned_category = str(category or '').strip() or None
    if cleaned_category is not None and cleaned_category not in FEEDBACK_CATEGORIES:
        raise ReviewError(
            f'category must be one of {", ".join(sorted(FEEDBACK_CATEGORIES))}')
    cleaned_comment = str(comment or '').strip() or None
    if cleaned_comment and len(cleaned_comment) > MAX_COMMENTS:
        raise ReviewError(f'comment must be {MAX_COMMENTS} characters or fewer')
    return {
        'emp_id': subject_emp_id,
        'reviewer_id': actor[0],
        'category': cleaned_category,
        'rating': validate_rating(rating),
        'comment': cleaned_comment,
    }
