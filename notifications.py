"""Notification preferences (FR-NOT-03).

The SRS asks for "Preferences per category (Onboarding, Leaves, Expenses,
Tickets, Payroll, Tickets-SLA), {in_app, email} each, default true." The
``category`` column on ``notifications`` was written for exactly this, and
``app._notification_category`` was written to feed it — and **the two never met.**

Reading the taxonomy against the events the app actually sends is what surfaced
this. Before this module, every one of the nine notification types the app raises
landed *outside* the SRS's list:

============================  ==========================
notification type              category it was stored as
============================  ==========================
``LEAVE_*`` (four types)      ``Leave``   — the SRS says ``Leaves``
``TICKET_ASSIGNED``           ``General``
``TICKET_UPDATED``            ``General``
``GOAL_RATED``                ``General``
``REVIEW_SUBMITTED``          ``General``
``HOLIDAY_OPTIN_REQUESTED``   ``General``
============================  ==========================

So a preference keyed on ``Leaves`` would never match a leave notification, and
``Tickets``, ``Expenses`` and ``Tickets-SLA`` had no producer at all. Any
preference screen built on that derivation would have been a set of switches that
did nothing — which is a worse outcome than no screen, because it teaches employees
that the control is meaningless. The taxonomy is therefore fixed *here* and the
derivation is the only place a notification type is turned into a category.

**A recorded deviation.** The SRS names six categories and the app emits events
outside them, so the set is the six plus two documented extras:

* ``Performance`` — goal ratings and submitted reviews (FR-PERF). Nothing in the
  SRS covers them, but forcing a goal rating into ``General`` would lump it with
  genuinely uncategorisable events.
* ``Holiday`` — optional-holiday opt-in decisions (FR-HOL-03).

Forcing those into one of the six would be worse than naming them, and the extra
two are reported alongside the six everywhere the taxonomy is exposed, so the
deviation is visible rather than buried.

**``Tickets-SLA`` is kept even though nothing produces it yet.** FR-TKT-01
(configurable SLA targets) is unimplemented, so toggling it changes nothing today.
It is in the SRS, and silently dropping a named category would be a second
taxonomy bug of exactly the kind this module exists to remove. Instead
:func:`describe` reports whether a category currently has a producer, so the API
can say so rather than leaving an admin to wonder.

**The default is true, and the absence of a row means true.** Storing a row per
employee per category would mean 7 rows created for every employee on first login
and a table that has to be backfilled when a category is added. A missing row is
the default, which is also what "default true" asks for, and it makes adding a
category a no-op for existing employees.
"""

from __future__ import annotations

# The SRS's six, in its order, then the two documented extras.
SRS_CATEGORIES = ('Onboarding', 'Leaves', 'Expenses', 'Tickets', 'Payroll', 'Tickets-SLA')
EXTRA_CATEGORIES = ('Performance', 'Holiday')
CATEGORIES = SRS_CATEGORIES + EXTRA_CATEGORIES

# The catch-all for an event with no better home. It is deliberately *not*
# preferenceable: a user cannot sensibly switch off "everything we failed to
# categorise", and offering the switch would imply the categorisation is
# complete. It is reported in :func:`describe` so the gaps are visible.
FALLBACK = 'General'

CHANNELS = ('in_app', 'email')

# The canonical table in the v1.0 order, the SRS order, then the extras. Ordered
# so the preference payload is stable for a UI and for a test.
_PREFERRED_ORDER = ('Leaves', 'Expenses', 'Tickets', 'Tickets-SLA', 'Payroll',
                    'Onboarding', 'Performance', 'Holiday')

# Explicit type -> category mapping, longest-prefix-wins. Written out rather than
# inferred from substrings because substring inference is what produced 'Leave'
# instead of 'Leaves' and 'General' for every ticket: it cannot express "this
# category is called something the type name does not contain".
#
# Every rule lives here — there is no second table consulted afterwards. A bare
# `LEAVE_*` type that is not in `_EXACT` lands on the `LEAVE` prefix, so adding a
# new leave notification needs no second thought about where it goes.
_PREFIX_RULES = (
    ('LEAVE', 'Leaves'),
    ('TICKET', 'Tickets'),
    ('GOAL', 'Performance'),
    ('REVIEW', 'Performance'),
    ('FEEDBACK', 'Performance'),
    ('HOLIDAY_OPTIN', 'Holiday'),
    ('EXPENSE', 'Expenses'),
    ('BREAK', 'Leave'),
    ('ONBOARDING', 'Onboarding'),
    ('OFFER', 'Onboarding'),
    ('CANDIDATE', 'Onboarding'),
    ('RESIGN', 'Onboarding'),
    # Offboarding has no SRS category of its own; the SRS's `Onboarding` covers the
    # whole join-to-exit lifecycle, and inventing an `Offboarding` preference the
    # requirement does not name would be the same taxonomy drift this module
    # exists to remove.
    ('OFFBOARD', 'Onboarding'),
    ('PAYROLL', 'Payroll'),
    ('PUNCH', 'Leave'),
)

# Exact type -> category, checked first. These are the names that would otherwise
# fall through to FALLBACK, and they are the ones the app really sends.
_EXACT = {
    'LEAVE_APPLIED': 'Leaves',
    'LEAVE_APPROVED': 'Leaves',
    'LEAVE_REJECTED': 'Leaves',
    'LEAVE_CANCELLED': 'Leaves',
    'TICKET_ASSIGNED': 'Tickets',
    'TICKET_UPDATED': 'Tickets',
    'TICKET_COMMENT': 'Tickets',
    'GOAL_RATED': 'Performance',
    'REVIEW_SUBMITTED': 'Performance',
    'HOLIDAY_OPTIN_REQUESTED': 'Holiday',
    'HOLIDAY_OPTIN_APPROVED': 'Holiday',
    'HOLIDAY_OPTIN_REJECTED': 'Holiday',
    'BREAK_APPROVED': 'Leave',
    'PAYROLL': 'Payroll',
    'PAYSLIP': 'Payroll',
}


class PreferenceError(ValueError):
    """A preference payload is not valid."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def category_for(ntype: str) -> str:
    """The preference category for a notification type.

    Exact match first, then the longest matching prefix, then the catch-all. The
    exact table is checked before the prefixes so a future type that *starts* with
    a known word cannot be captured by a broader rule.
    """
    key = str(ntype or '').strip().upper()
    if not key:
        return FALLBACK
    if key in _EXACT:
        return _EXACT[key]
    best = None
    for prefix, category in _PREFIX_RULES:
        if key.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, category)
    return best[1] if best else FALLBACK


def check_payload(payload: dict, current: dict) -> dict:
    """Validate a preference update. Returns the full effective mapping.

    ``current`` is the effective mapping before the change, so a *partial* body
    answers the same as a full one — an absent category keeps what it had rather
    than silently reverting to the default. That matters for the same reason it
    does on `PUT /api/users`: a client sending one switch must not reset the other
    six.
    """
    if not isinstance(payload, dict) or not payload:
        raise PreferenceError('A JSON object with at least one category is required')
    unknown = set(payload) - set(CATEGORIES)
    if unknown:
        raise PreferenceError(
            f'Unknown category/categories: {", ".join(sorted(unknown))}. '
            f'Expected one of {", ".join(CATEGORIES)}')
    # A *deep* copy. `dict(current)` shares the per-category dicts, so writing
    # `effective['Leaves']['in_app'] = False` would also mutate `current['Leaves']`,
    # and a caller comparing the two to work out what changed would find every
    # category equal to itself — which made the whole PUT a silent no-op that
    # returned 200 and wrote nothing.
    effective = {category: dict(channels) for category, channels in current.items()}
    for category, channels in payload.items():
        if not isinstance(channels, dict):
            raise PreferenceError(
                f'{category} must be an object of {{"in_app": bool, "email": bool}}')
        extra = set(channels) - set(CHANNELS)
        if extra:
            raise PreferenceError(
                f'{category}: unknown channel(s) {", ".join(sorted(extra))}. '
                f'Expected {", ".join(CHANNELS)}')
        for channel, value in channels.items():
            if not isinstance(value, bool):
                raise PreferenceError(
                    f'{category}.{channel} must be true or false, not {value!r}')
            effective.setdefault(category, {})[channel] = value
    return effective


def _defaults() -> dict:
    return {category: {channel: True for channel in CHANNELS} for category in CATEGORIES}


def effective_for(rows) -> dict:
    """Apply stored rows over the defaults.

    ``rows`` is ``(category, in_app, email)`` for one employee. A stored row for a
    category that is no longer in the taxonomy is **ignored rather than dropped**,
    so re-adding a category later does not resurrect a stale setting by accident —
    but it is not deleted either, because a rename in one release and a restore in
    the next should not lose the employee's choice.
    """
    effective = _defaults()
    for category, in_app, email in rows:
        if category not in effective:
            continue
        effective[category]['in_app'] = bool(in_app)
        effective[category]['email'] = bool(email)
    return effective


def ordered(effective: dict) -> dict:
    """The mapping in a stable order: the SRS's six, then the extras, then any
    unknown category that somehow arrived (never, but an ordered dict that silently
    drops one would hide a data problem)."""
    result = {}
    for category in _PREFERRED_ORDER:
        if category in effective:
            result[category] = effective[category]
    for category in CATEGORIES:
        if category in effective and category not in result:
            result[category] = effective[category]
    for category, value in effective.items():
        if category not in result:
            result[category] = value
    return result


def wants_in_app(effective: dict, category: str) -> bool:
    """Should an in-app notification in this category be delivered?

    A category with no stored preference is on, which is the SRS's "default true".
    The catch-all is always on for the reason given in the module docstring.
    """
    if category == FALLBACK:
        return True
    return bool(effective.get(category, {}).get('in_app', True))


# Every notification type the app raises **today**, read off the actual call sites
# in `app.py` and `outbox.py`. A test greps those two files and asserts this list
# covers every type found and that none of them lands in FALLBACK — the defect this
# module was written to remove was a type with no mapping, so a new type with no
# mapping must fail the build rather than land as a silent 'General'.
#
# Deliberately not padded with plausible future types. Padding it would make
# `describe()` claim Expenses and Tickets-SLA have a producer, and telling an admin
# a switch does something when it does not is worse than marking it as having
# nothing behind it yet.
KNOWN_TYPES = (
    # app.py
    'LEAVE_APPLIED', 'LEAVE_APPROVED', 'LEAVE_REJECTED', 'LEAVE_CANCELLED',
    'TICKET_ASSIGNED', 'TICKET_UPDATED',
    'GOAL_RATED', 'REVIEW_SUBMITTED',
    'HOLIDAY_OPTIN_REQUESTED',
    # outbox.py
    'PAYROLL', 'ONBOARDING',
)


def describe() -> list:
    """The taxonomy with whether each category currently has a producer.

    Surfaced by the API so an admin toggling ``Tickets-SLA`` — which FR-TKT-01 will
    populate and nothing does today — is told that, rather than left to conclude the
    control is broken.

    ``KNOWN_TYPES`` is a declared list, and keeping it in step is asserted by a test
    that reads the real ``add_notification`` call sites out of ``app.py`` and checks
    every one of them lands in a preferenceable category. That test is the real
    guarantee; this list exists so the API can say which switches currently do
    something without re-parsing the source on a request.
    """
    produced = {category_for(ntype) for ntype in KNOWN_TYPES}
    rows = []
    for category in CATEGORIES:
        rows.append({
            'category': category,
            'in_srs': category in SRS_CATEGORIES,
            'has_producer': category in produced,
        })
    return rows
