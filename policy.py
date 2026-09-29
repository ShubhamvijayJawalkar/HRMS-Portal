"""Central role/permission policy — FR-USR-09, FR-USR-15, CC-11.

The application used to answer "may this user do X?" in two disconnected
places: a set of decorators/inline checks in ``app.py`` that compare the
session role string against literals, and a hand-maintained rule set in
``templates/_navbar.html``. That duplicated the matrix, let the navigation and
the API disagree, and left the ``user_permissions`` table as DDL with no read
or write path.

This module owns the matrix and resolves it. The ``user_permissions`` rows are
*per-user overrides* on top of a static role default:

1. a module outside :data:`PERMISSION_MODULES` **fails closed** (deny);
2. an override row with ``allow = FALSE`` denies, even for Admin/Super Admin;
3. an override row with ``allow = TRUE`` grants, lifting a role-default deny;
4. otherwise the role default applies.

Two invariants make this safe to introduce incrementally:

* **The table is empty today, and an empty table reproduces
  ``ROLE_DEFAULTS[role]`` exactly.** Adding this module therefore cannot change
  any existing authorization outcome.
* **Scope is not a column.** ``self`` / direct-reports / department / all is the
  second argument of :func:`can` (CC-11), not a property of the module row.

The v1.0 compatibility ``user_permissions`` table stores ``allow`` as a
nullable INTEGER and has a plain ``perm_id INTEGER PRIMARY KEY`` (no
sequence), while the v2.0 target uses ``allow BOOLEAN NOT NULL`` with an
identity key — so reads go through ``bool(row)`` (a NULL denies) and writes use
the sequence-safe allocator in ``app._next_generated_id``.
"""

from __future__ import annotations

# ── Modules (one key per SRS permission module) ─────────────────────────────
PERMISSION_MODULES = frozenset({
    # Directory
    'users', 'import_users',
    # ATS (FR-ATS-01..04)
    'candidates', 'jobs', 'offers',
    # Payroll / compensation (FR-PAY-05/06)
    'payroll', 'salary_structures', 'payroll_rates', 'payroll_approve',
    # Time off and attendance (FR-LEA/FR-ATT)
    'leaves', 'regularization', 'breaks', 'holidays',
    # Expenses (FR-EXP)
    'expenses',
    # Lifecycle (FR-ONB/FR-OFF)
    'onboarding', 'offboarding',
    # Assurance
    'audit', 'reports', 'analytics',
    # Workplace operations
    'tickets', 'assets', 'performance', 'goals', 'documents',
    # Administration
    'shift_admin', 'policy_admin', 'pii_reveal',
})

# Roles that may administer the directory and the policy itself.
ADMIN_ROLES = frozenset({'Admin', 'Super Admin'})

# ---------------------------------------------------------------------------
# Role defaults (SRS Appendix B). ``True`` = the role may *use* the module;
# the resource scope ("self" vs "all") is resolved by ``can()``.
#
# A handful of cells widen today's decorator behaviour on purpose:
#   * Team Leader may see leaves/regularization/goals/performance/documents and
#     approve their own team's items (today only HR/Admin reach those pages);
#   * Team Leader may administer the modules they are responsible for.
# Those widenings become real when the decorators are wired to ``can()``.
# ---------------------------------------------------------------------------
_ROLE_MATRIX: dict[str, dict[str, bool]] = {
    'Super Admin': {module: True for module in PERMISSION_MODULES},
    'Admin': {module: True for module in PERMISSION_MODULES},
    'HR': {
        'users': False, 'import_users': True,
        'candidates': True, 'jobs': True, 'offers': True,
        'payroll': False, 'salary_structures': False, 'payroll_rates': False,
        'payroll_approve': False,
        'leaves': True, 'regularization': True, 'breaks': True, 'holidays': True,
        'expenses': True,
        'onboarding': True, 'offboarding': True,
        'audit': True, 'reports': True, 'analytics': True,
        'tickets': True, 'assets': True, 'performance': True, 'goals': True,
        'documents': True,
        'shift_admin': False, 'policy_admin': False, 'pii_reveal': True,
    },
    'Finance': {
        'users': False, 'import_users': False,
        'candidates': False, 'jobs': False, 'offers': False,
        'payroll': True, 'salary_structures': True, 'payroll_rates': True,
        'payroll_approve': True,
        'leaves': False, 'regularization': False, 'breaks': False, 'holidays': False,
        # FR-EXP-03 names Finance as the only role that may mark a claim Paid
        # ("Approved -> Paid by Finance only"). Without the module the paying
        # role could not reach the route at all, so the requirement was
        # unreachable as written. The list stays scoped to own + reports
        # (CC-11), so this grants reach, not company-wide visibility.
        'expenses': True,
        'onboarding': False, 'offboarding': False,
        'audit': False, 'reports': True, 'analytics': True,
        'tickets': False, 'assets': False, 'performance': False, 'goals': False,
        'documents': False,
        'shift_admin': False, 'policy_admin': False, 'pii_reveal': True,
    },
    'Team Leader': {
        'users': False, 'import_users': False,
        'candidates': False, 'jobs': False, 'offers': False,
        'payroll': False, 'salary_structures': False, 'payroll_rates': False,
        'payroll_approve': False,
        'leaves': True, 'regularization': True, 'breaks': True, 'holidays': False,
        'expenses': True,
        'onboarding': False, 'offboarding': False,
        'audit': False, 'reports': False, 'analytics': False,
        'tickets': True, 'assets': True, 'performance': True, 'goals': True,
        'documents': True,
        'shift_admin': False, 'policy_admin': False, 'pii_reveal': False,
    },
    'Employee': {
        'users': False, 'import_users': False,
        'candidates': False, 'jobs': False, 'offers': False,
        'payroll': False, 'salary_structures': False, 'payroll_rates': False,
        'payroll_approve': False,
        'leaves': True, 'regularization': True, 'breaks': True, 'holidays': False,
        'expenses': True,
        'onboarding': False, 'offboarding': False,
        'audit': False, 'reports': False, 'analytics': False,
        'tickets': True, 'assets': True, 'performance': True, 'goals': True,
        'documents': True,
        'shift_admin': False, 'policy_admin': False, 'pii_reveal': False,
    },
}

# Every declared role must describe every module: a missing key would silently
# deny, so the matrix is completed and validated once at import time.
ROLE_DEFAULTS: dict[str, dict[str, bool]] = {
    role: {module: bool(cells.get(module, False)) for module in PERMISSION_MODULES}
    for role, cells in _ROLE_MATRIX.items()
}

# An unknown role is not an error to guess around: it denies everything.
NO_PERMISSIONS: dict[str, bool] = {module: False for module in PERMISSION_MODULES}


# ── Department dimension (FR-USR-15) ──────────────────────────────────────
# `hr_or_admin_required` has always admitted an HR-*department* user of any
# role, not just the HR role. A flat role→bool matrix cannot express that, so
# the department grant is modelled here instead of being silently dropped (which
# would lock HR-department staff out of the pages they can reach today).
# `pii_reveal` is deliberately absent: department membership alone must not
# confer PII access, and no read path is gated by it yet.
DEPARTMENT_GRANTS: dict[str, frozenset] = {
    'HR': frozenset({
        'candidates', 'jobs', 'offers',
        'leaves', 'regularization', 'breaks', 'expenses',
        'onboarding', 'offboarding',
        'tickets', 'assets', 'documents', 'goals', 'performance',
        'audit', 'reports', 'analytics', 'import_users',
    }),
}

# ── Navigation (FR-USR-15) ────────────────────────────────────────────────
# One spec for the navbar, so the navigation and the API can no longer drift.
# ``module`` is the permission a *page route* requires; ``always`` marks
# self-service pages that are gated by login alone (no module). ``children``
# renders the Modules dropdown, which disappears when it has no visible child.
NAV_ENTRIES: tuple[dict, ...] = (
    {'href': '/dashboard', 'label': 'Dashboard', 'icon': 'speedometer2', 'always': True},
    {'href': '/admin/users', 'label': 'Users', 'icon': 'people-fill', 'module': 'users'},
    {'href': '/admin/holidays', 'label': 'Holidays', 'icon': 'calendar-event', 'module': 'holidays'},
    {
        'href': '#', 'label': 'Modules', 'icon': 'grid-3x3-gap-fill', 'group': 'modules',
        'children': (
            {'href': '/admin/assets', 'label': 'Assets', 'icon': 'laptop', 'module': 'assets'},
            {'href': '/admin/jobs', 'label': 'Jobs', 'icon': 'briefcase', 'module': 'jobs'},
            {'href': '/admin/candidates', 'label': 'Candidates', 'icon': 'person-lines-fill', 'module': 'candidates'},
            {'href': '/admin/payroll', 'label': 'Payroll', 'icon': 'cash-stack', 'module': 'payroll'},
            {'href': '/admin/salary-structures', 'label': 'Salary', 'icon': 'wallet', 'module': 'salary_structures'},
            {'href': '/admin/documents', 'label': 'Documents', 'icon': 'file-earmark-text', 'module': 'documents'},
            {'divider': True},
            {'href': '/admin/goals', 'label': 'Goals', 'icon': 'bullseye', 'module': 'goals'},
            {'href': '/admin/reviews', 'label': 'Reviews', 'icon': 'star', 'module': 'performance'},
            {'href': '/admin/expenses', 'label': 'Expenses', 'icon': 'receipt', 'module': 'expenses'},
            {'href': '/admin/tickets', 'label': 'Tickets', 'icon': 'ticket', 'module': 'tickets'},
            {'href': '/admin/leaves', 'label': 'Leave Requests', 'icon': 'calendar-x', 'module': 'leaves'},
            {'href': '/admin/analytics', 'label': 'Analytics', 'icon': 'graph-up', 'module': 'analytics'},
            {'href': '/admin/audit', 'label': 'Audit', 'icon': 'journal-text', 'module': 'audit'},
            {'href': '/admin/reports', 'label': 'Reports', 'icon': 'file-spreadsheet', 'module': 'reports'},
            {'href': '/admin/import-users', 'label': 'Import', 'icon': 'upload', 'module': 'import_users'},
        ),
    },
    {'href': '/leaves', 'label': 'Leaves', 'icon': 'calendar-check', 'always': True},
    {'href': '/regularization', 'label': 'Regularization', 'icon': 'pencil-square', 'always': True},
    {'href': '/onboarding', 'label': 'Onboarding', 'icon': 'rocket-takeoff', 'always': True},
    {'href': '/offboarding', 'label': 'Offboarding', 'icon': 'box-arrow-right', 'always': True},
    {'href': '/profile', 'label': 'Profile', 'icon': 'person-fill-gear', 'always': True},
)


class PolicyError(ValueError):
    """A permission payload violates the policy contract."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def role_defaults(role) -> dict[str, bool]:
    """Static capability set for ``role`` (unknown role -> deny everything)."""
    return dict(ROLE_DEFAULTS.get(str(role or '').strip(), NO_PERMISSIONS))


def override_rows(conn, emp_id) -> dict[str, bool]:
    """Literal ``user_permissions`` rows for one user, normalised to bools.

    Unknown module names are dropped (they fail closed) and a NULL ``allow``
    is read as a deny, which is the only safe reading of the nullable
    compatibility column.
    """
    rows = conn.execute(
        "SELECT module, allow FROM user_permissions WHERE emp_id = ?", [emp_id]
    ).fetchall()
    overrides: dict[str, bool] = {}
    for module, allow in rows:
        if module not in PERMISSION_MODULES:
            continue
        overrides[module] = bool(allow)
    return overrides


def effective_permissions(conn, emp_id, role) -> dict[str, bool]:
    """Resolve one user's effective capability map (defaults + overrides)."""
    effective = role_defaults(role)
    effective.update(override_rows(conn, emp_id))
    return effective


def actor_from_row(row) -> dict:
    """Build an actor dict from a ``(emp_id, role, department)`` row."""
    if row is None:
        return {}
    return {
        'emp_id': row[0],
        'role': row[1],
        'department': row[2] if len(row) > 2 else None,
    }


def current_actor(conn=None) -> dict:
    """Resolve the session's actor from the database (never from the session copy).

    The session caches the role at login time, so a role change would not be
    reflected there; every authorization decision reads the live row.
    """
    from flask import session

    from app import get_db  # lazy: no circular import at module load

    emp_id = session.get('emp_id')
    if not emp_id:
        return {}
    own_conn = conn is None
    conn = conn or get_db()
    try:
        row = conn.execute(
            "SELECT emp_id, role, department FROM users WHERE emp_id = ?", [emp_id]
        ).fetchone()
    finally:
        if own_conn:
            conn.close()
    return actor_from_row(row)


def department_grant(actor, module) -> bool:
    """Does the actor's department grant ``module`` regardless of role?

    Kept separate from :func:`role_defaults` so the department dimension stays
    visible in review instead of being folded into the role table.
    """
    if not isinstance(actor, dict) or module not in PERMISSION_MODULES:
        return False
    return module in DEPARTMENT_GRANTS.get(str(actor.get('department') or '').strip(), frozenset())


def can(actor, module, resource=None, *, conn=None) -> bool:
    """CC-11 capability check: may ``actor`` use ``module`` on ``resource``?

    ``actor`` is ``{'emp_id', 'role', 'department'}`` — see
    :func:`current_actor`. ``resource`` is the row/target the action applies to;
    it is only consulted for the self-scoped ``pii_reveal`` rule here, and the
    remaining scopes (direct reports, department, all) are resolved by the
    decorator wiring that calls this function.
    """
    if not isinstance(actor, dict) or not actor.get('emp_id'):
        return False
    if module not in PERMISSION_MODULES:
        return False
    if department_grant(actor, module):
        return True
    own_conn = conn is None
    if own_conn:
        from app import get_db  # lazy: no circular import at module load

        conn = get_db()
    try:
        allowed = effective_permissions(conn, actor['emp_id'], actor.get('role')).get(module, False)
    finally:
        if own_conn:
            conn.close()
    if allowed:
        return True
    # Own-record access never depends on the PII flag (FR-USR-15: an employee
    # can always see their own profile fields).
    if module == 'pii_reveal' and isinstance(resource, dict):
        return resource.get('emp_id') == actor.get('emp_id')
    return False


def nav_entries(actor, *, conn=None) -> list[dict]:
    """Navbar items visible to ``actor`` (FR-USR-15: nav == API).

    Self-service pages (``always``) are visible to any signed-in user; module
    pages are visible when the actor can use that module. A Modules dropdown
    with no visible child is dropped entirely.
    """
    visible: list[dict] = []
    for entry in NAV_ENTRIES:
        if entry.get('always'):
            visible.append(entry)
            continue
        if entry.get('group') == 'modules':
            children = [
                child for child in entry.get('children', ())
                if child.get('divider') or can(actor, child['module'], conn=conn)
            ]
            if not children:
                continue
            visible.append({**entry, 'children': children})
            continue
        if can(actor, entry['module'], conn=conn):
            visible.append(entry)
    return visible

# ── CC-11 scope ───────────────────────────────────────────────────────────
# A capability is not a scope. ``can(actor, module)`` answers "may this role
# touch the module at all"; ``can_view_all`` answers "does it see the whole
# company or only its own records". The self-service list endpoints have always
# split on ``role == 'Admin'``, so the company-wide scope is granted to the
# operations roles only. HR and Finance are deliberately *not* in this set:
# widening what they can list is a product decision, not a hardening step, so
# it is left exactly as it is today.
ALL_SCOPE_ROLES = frozenset({'Admin', 'Super Admin'})

# Modules that mean "administers something" — used to pick the admin dashboard
# variant, which today is ``role in (Admin, Finance) or department == 'HR'``.
ADMIN_SURFACE_MODULES = (
    'users', 'import_users',
    'candidates', 'jobs', 'offers',
    'payroll', 'salary_structures',
    'holidays', 'audit', 'reports', 'analytics',
    'shift_admin', 'policy_admin',
)


def can_view_all(actor, module, *, conn=None) -> bool:
    """May ``actor`` list every record in ``module`` (not just their own)?"""
    if not isinstance(actor, dict):
        return False
    if str(actor.get('role') or '') not in ALL_SCOPE_ROLES:
        return False
    return can(actor, module, conn=conn)


def sees_admin_surface(actor, *, conn=None) -> bool:
    """Does this actor administer anything (chooses the dashboard variant)?"""
    if not isinstance(actor, dict) or not actor.get('emp_id'):
        return False
    return any(can(actor, module, conn=conn) for module in ADMIN_SURFACE_MODULES)


def pii_view(actor, target_emp_id, *, conn=None) -> bool:
    """May ``actor`` see the personal fields (DOB, address, emergency contact) of ``target_emp_id``?

    Own record: always allowed, whatever the role matrix says. Someone else's
    record: requires the ``pii_reveal`` capability, which an override can deny
    per user. Callers that expose another employee's PII must also write a
    ``PII_REVEAL`` audit row — see ``_pii_audit_fields``.
    """
    if not isinstance(actor, dict) or not actor.get('emp_id'):
        return False
    if actor.get('emp_id') == target_emp_id:
        return True
    return can(actor, 'pii_reveal', resource={'emp_id': target_emp_id}, conn=conn)


# Personal fields the v2.0 schema marks as PII (db/postgres_schema.sql users).
# The personal columns per entity, as the data dictionary classifies them
# (SRS Appendix B). `name` is deliberately *not* in any of these: a person has to
# be identifiable for the workflow to function (an interviewer has to know who
# they are meeting, HR has to know whose record they are editing), and a name on
# its own is a far weaker identifier than a contact bundle. The contact and
# document fields are what this control actually withholds.
PII_FIELDS = {
    'users': (
        'date_of_birth', 'address', 'emergency_contact_name', 'emergency_contact_phone',
        'phone',
    ),
    'dependents': ('name', 'relationship', 'date_of_birth'),
    'candidates': ('email', 'phone', 'resume_text'),
}

# Kept for the users reveal route, which reports the fields it exposed.
USER_PII_FIELDS = PII_FIELDS['users']


def redact_pii(payload: dict, allowed: bool, entity: str = 'users') -> dict:
    """Blank the entity's PII keys unless the actor may see them.

    The keys are kept with a ``None`` value rather than removed, so a client can
    tell "withheld" from "not present" and the response shape stays stable.
    """
    if allowed:
        return dict(payload)
    fields = PII_FIELDS.get(entity, ())
    return {key: (value if key not in fields else None) for key, value in payload.items()}


def pii_fields_for(entity: str) -> tuple:
    """The PII column names for an entity (empty for an unknown one)."""
    return PII_FIELDS.get(entity, ())


def validate_module_map(modules) -> dict[str, bool]:
    """Validate a ``{module: allow}`` override payload (CC-12: no unknown keys)."""
    if not isinstance(modules, dict):
        raise PolicyError('modules must be an object of {module: allow}')
    unknown = sorted(set(modules) - PERMISSION_MODULES)
    if unknown:
        raise PolicyError(f'unknown permission modules: {", ".join(unknown)}')
    validated: dict[str, bool] = {}
    for module, allow in modules.items():
        validated[module] = _coerce_allow(allow, module)
    return validated


def _coerce_allow(value, module) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ('true', 'false', '1', '0', 'yes', 'no'):
        return value.strip().lower() in ('true', '1', 'yes')
    raise PolicyError(f'{module} must be true or false')


def diff_overrides(before: dict[str, bool], after: dict[str, bool]) -> dict[str, list[str]]:
    """Summarise an override-set change for the audit trail."""
    before = before or {}
    after = after or {}
    return {
        'added': sorted(m for m in after if m not in before),
        'removed': sorted(m for m in before if m not in after),
        'changed': sorted(f'{m}:{int(before[m])}->{int(after[m])}' for m in before.keys() & after.keys() if before[m] != after[m]),
    }
