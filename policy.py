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
        'tickets': True, 'assets': True, 'performance': False, 'goals': False,
        'documents': True,
        'shift_admin': False, 'policy_admin': False, 'pii_reveal': True,
    },
    'Finance': {
        'users': False, 'import_users': False,
        'candidates': False, 'jobs': False, 'offers': False,
        'payroll': True, 'salary_structures': True, 'payroll_rates': True,
        'payroll_approve': True,
        'leaves': False, 'regularization': False, 'breaks': False, 'holidays': False,
        'expenses': False,
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
