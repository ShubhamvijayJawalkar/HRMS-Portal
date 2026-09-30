"""SRS requirement traceability (SRS v2.0 §6-§14).

**Why this is code and not a markdown table.** A hand-maintained traceability
matrix rots silently: a route gets renamed, the doc keeps claiming it covers
FR-USR-03, and nothing notices. So the matrix lives here as data, the routes it
claims are checked against the live `url_map` by
`test_traceability_routes_exist`, and `scripts/generate_traceability.py` renders
`docs/TRACEABILITY.md` from this file. A rename breaks the build instead of
quietly invalidating a document.

**Honesty rule.** A requirement is only ``IMPLEMENTED`` if the code enforces it,
not if a handler merely exists. Where the SRS asks for something the product
deliberately does not do, the row says so and says why — an over-claiming matrix
is worse than a short one, because it is used to decide what is safe to ship.

Status values:

* ``IMPLEMENTED`` — the behaviour is enforced server-side and covered by a test.
* ``PARTIAL``    — a usable subset ships; the row names what is missing.
* ``NOT_STARTED``— nothing enforces it. Tables/columns may exist from the
  canonical schema; that is not implementation.
* ``RETIRED``    — folded into another requirement by Appendix A.
"""

# Requirement ID -> (priority, delta, status, routes, note)
# ``routes`` are Flask rules, verified against app.url_map by the test suite.
# A route the matrix does not name may still support the requirement; the list is
# the *primary* surface, not an exhaustive one.
TRACEABILITY: dict[str, tuple[str, str, str, tuple[str, ...], str]] = {
    # ── FR-USR: user management ─────────────────────────────────────────
    'FR-USR-01': ('M', 'R', 'IMPLEMENTED', ('/api/users',),
                  'page/per_page parsed as ints, per_page capped at 200, sort on an '
                  'allow-list; invalid input is 400, never a silent default.'),
    'FR-USR-02': ('M', 'C', 'PARTIAL', ('/api/users',),
                  'emp_id/email/role/department validated, case-insensitive email '
                  'uniqueness, default status Active + allow_login. Missing: the '
                  'welcome email with a 24 h single-use reset token, and balances '
                  'seeded from the grade/location policy rather than the default '
                  'matrix (leave_policy derives on read instead).'),
    'FR-USR-03': ('M', 'R', 'IMPLEMENTED', ('/api/users/<emp_id>',),
                  'Field allow-list, true partial update, role changes audited with a '
                  'before/after diff, email re-verifies uniqueness.'),
    'FR-USR-04': ('H', 'C', 'IMPLEMENTED', ('/api/users/<emp_id>/block', '/api/users/<emp_id>/unblock'),
                  'Blocking closes the DB sessions and revokes the Redis sessions; '
                  'asserted in tests/test_redis_sessions.py.'),
    'FR-USR-05': ('H', 'R', 'IMPLEMENTED', ('/api/users/<emp_id>/archive', '/api/users/<emp_id>/restore'),
                  'Status-based archive/restore, self-action 409, payroll and audit '
                  'records retained. The legacy DELETE route is archive-compatible.'),
    'FR-USR-06': ('H', 'C', 'IMPLEMENTED', ('/api/users/<emp_id>/anonymise',),
                  'Purge replaced by anonymisation: a dry-run plan, a required salt, '
                  'and an audit history scrubbed by value substitution. Trade-offs '
                  '(emp_id kept, free text left) are recorded in docs/ANONYMISATION.md §8.'),
    'FR-USR-06a': ('H', 'N', 'IMPLEMENTED', ('/api/anonymisation/<int:request_id>/confirm',),
                   'Two-person state machine proposed -> confirmed -> applied; the '
                   'confirmer must be a different user, and only an archived account qualifies.'),
    'FR-USR-07': ('H', 'R', 'NOT_STARTED', (),
                  'No POST /api/users/bulk. The block/archive routes take a single '
                  'employee; there is no batch endpoint with per-row results.'),
    'FR-USR-08': ('L', 'R', 'PARTIAL', ('/api/users/<emp_id>',),
                  'Missing the meta endpoint, the async CSV export and the last-50 '
                  'sessions history. The list endpoint carries the pagination meta.'),
    'FR-USR-09': ('H', 'C', 'IMPLEMENTED', ('/api/users/<emp_id>/permissions',),
                  'Full replace of the override set, audited with a real before/after '
                  'diff, anti-lockout guard, module mapped into policy.PERMISSION_MODULES.'),
    'FR-USR-10': ('H', 'C', 'PARTIAL', ('/api/users/import', '/api/users/import/<int:job_id>'),
                  'CSV via pandas as a background job (202 + job id), per-row '
                  'validation, {imported, skipped, errors, job_id}. The progress '
                  'endpoint is /api/users/import/<job_id>, not the /api/imports/... '
                  'path the SRS names, and errors are capped at 20 rather than 50.'),
    'FR-USR-11': ('M', 'C', 'IMPLEMENTED', ('/api/dependents', '/api/dependents/<int:did>'),
                  'emp_id always from the session, never the payload; delete is '
                  'scoped by emp_id as well.'),
    'FR-USR-12': ('L', 'C', 'PARTIAL', ('/api/upload',),
                  'Employee uploads go through the same route as admin uploads, so '
                  'one validation pipeline exists, and it does sniff the content. It '
                  'inherits the FR-DOC-02 gaps: no per-category size cap and no real '
                  'scanner.'),
    'FR-USR-13': ('M', 'C', 'PARTIAL', ('/api/profile',),
                  'Profile read/write is self-scoped and routed through the PII '
                  'helper. The field allow-list is not a declared strict subset: an '
                  'employee cannot change their own role, but the boundary is '
                  'implied by the handler rather than asserted by a test.'),
    'FR-USR-14': ('M', 'R', 'PARTIAL', ('/api/change-password',),
                  'The current password is required. The session token is not '
                  're-issued on change, so an existing cookie keeps working.'),
    'FR-USR-15': ('M', 'C', 'IMPLEMENTED', (),
                  'policy.navigation_for() is the same predicate the route gates use, '
                  'injected into every template; five tests assert the navbar and the '
                  'gate of the linked route never disagree.'),

    # ── FR-AUTH: authentication ─────────────────────────────────────────
    'FR-AUTH-01': ('M', 'C', 'PARTIAL', ('/login',),
                   'Employee code is trimmed and matched case-insensitively, with a '
                   'rate limit (LOGIN_RATE_LIMIT, default 20/min). The limit is per '
                   'remote address rather than per account *and* per IP.'),
    'FR-AUTH-02': ('M', 'N', 'IMPLEMENTED', ('/login',),
                   'One response for every failure mode, so an attacker cannot tell '
                   'an unknown account from a wrong password.'),
    'FR-AUTH-03': ('M', 'N', 'NOT_STARTED', (),
                   'No consecutive-failure counter and no timed account lock. Only '
                   'the IP rate limit stands between an attacker and a password spray.'),
    'FR-AUTH-04': ('M', 'C', 'PARTIAL', ('/logout',),
                   'Server-side Redis sessions (opaque cookie, 8 h TTL), HttpOnly, '
                   'SameSite=Lax, Secure in production; logout deletes the server copy. '
                   'There is no 24 h *absolute* timeout distinct from the 8 h idle one.'),
    'FR-AUTH-05': ('M', 'R', 'IMPLEMENTED', ('/logout',),
                   'Logout closes the active user_sessions row with total_hours = '
                   'logout - login and audits the action.'),
    'FR-AUTH-06': ('M', 'R', 'IMPLEMENTED', ('/',), 'Redirects by session state.'),
    'FR-AUTH-07': ('M', 'N', 'IMPLEMENTED', ('/dashboard',),
                   'Admin vs self dashboard chosen by policy.sees_admin_surface(); '
                   'unauthenticated gets 302 for a page and 401 for JSON.'),
    'FR-AUTH-08': ('M', 'N', 'IMPLEMENTED', ('/api/forgot-password',),
                   'Always 202 with the same message, so the endpoint cannot be used '
                   'to enumerate accounts.'),
    'FR-AUTH-09': ('M', 'N', 'PARTIAL', ('/api/reset-password',),
                   'Single-use token, invalidated after a successful reset, purged '
                   'hourly. Missing: the token is stored unhashed, and the expiry is '
                   '1 h where the SRS asks for 24 h.'),
    'FR-AUTH-10': ('M', 'N', 'IMPLEMENTED',
                   ('/api/users', '/api/change-password', '/api/reset-password'),
                   'passwords.py: a 10-character minimum (Appendix A-01 calls 6 a defect) '
                   'and a breach-corpus check, enforced at every point a password is set. '
                   'The corpus is a bundled offline list, extended optionally by the HIBP '
                   'k-anonymity range API; leet variants and known-password-plus-suffix are '
                   'caught too. Deliberately no complexity rules and no expiry, per NIST '
                   'SP 800-63B, and a test parses the module to keep them out. The old shared '
                   "default of 'pass123' is gone: a user created without a password gets a "
                   'generated compliant one, returned once. The rejection message is generic, '
                   'so it is not an oracle for confirming a guess.'),
    'FR-AUTH-11': ('M', 'N', 'NOT_STARTED', (),
                   'No MFA. mfa_credentials exists in the canonical schema with an '
                   'encrypted secret, but nothing reads or writes it: no enrolment, '
                   'no challenge, no gate. This is the largest single gap found by the '
                   'traceability pass.'),
    'FR-AUTH-12': ('M', 'C', 'IMPLEMENTED', ('/api/csrf-token',),
                   'Double-submit on every mutating /api request; a fetch wrapper '
                   'attaches the header and native forms carry the hidden field. '
                   'Asserted end to end with server-side sessions too.'),
    'FR-AUTH-13': ('H', 'N', 'PARTIAL', ('/api/credentials',),
                   'Restricted by the permission policy and no longer returns '
                   'passwords or hashes. Missing the 5-minute re-authentication and '
                   'the audit row for a credential read.'),
    'FR-AUTH-14': ('H', 'N', 'PARTIAL', (),
                   'An hourly job purges expired reset and idempotency tokens. It does '
                   'not auto-close breaks Active for more than 12 hours, so a '
                   'forgotten break-end leaves a row Active indefinitely.'),

    # ── FR-ATT: attendance, breaks, shifts ──────────────────────────────
    'FR-ATT-01': ('M', 'C', 'PARTIAL', ('/api/break-types',),
                  'The three seeded types with their limits ship and Lunch requires '
                  'approval. The read endpoint exposes no per-location '
                  'configuration and there is no CRUD route, so the types are '
                  'configurable only by editing rows.'),
    'FR-ATT-02': ('M', 'C', 'PARTIAL', ('/api/start-break',),
                  'allow_breaks and the daily quota are enforced, the Lunch approval '
                  'is required, an existing Active break is auto-ended, and the write '
                  'is idempotent. The "one transaction" and "partial unique index" '
                  'parts are not met on the compatibility schema: the auto-end plus '
                  'insert is two statements, and only v2.0 carries the index.'),
    'FR-ATT-03': ('M', 'R', 'IMPLEMENTED', ('/api/end-break/<int:break_id>',),
                  'Ownership checked against the session employee, duration computed '
                  'from the timestamps, action audited.'),
    'FR-ATT-04': ('M', 'R', 'IMPLEMENTED', ('/api/user-breaks',),
                  'Own breaks only, UNIONed with any Active break from another date '
                  'so a forgotten break-end is still visible.'),
    'FR-ATT-05': ('H', 'C', 'PARTIAL', ('/api/break-approvals',),
                  'Lunch only, one Pending per employee enforced in the handler. The '
                  'partial unique index that would enforce it under concurrency exists '
                  'only in the v2.0 schema.'),
    'FR-ATT-06': ('H', 'C', 'PARTIAL', ('/api/break-approvals/<int:aid>/approve', '/api/break-approvals/<int:aid>/reject'),
                  'Manager/HR/Admin may approve, the update is conditional, the action '
                  'is audited and the employee is notified. Delegated approvers '
                  '(FR-LEA-08a) are not consulted, because delegation is unimplemented.'),
    'FR-ATT-07': ('L', 'R', 'PARTIAL', ('/api/break-types', '/api/user-breaks',),
                  'Minutes used and the approval flag are exposed. The per-type summary '
                  'is assembled by the client from two calls rather than served as one '
                  'projection.'),
    'FR-ATT-08': ('M', 'R', 'IMPLEMENTED', ('/api/login-hours',),
                  'First login to last logout, scoped to a shift date.'),
    'FR-ATT-09': ('H', 'C', 'PARTIAL', ('/api/user/shift-summary',),
                  'last_logout - first_login, productive_hours and efficiency all ship. '
                  'The 25% cap on an open shift and the estimated flag are missing, so '
                  'a forgotten logout inflates the figure.'),
    'FR-ATT-10': ('—', '—', 'RETIRED', (), 'Folded into FR-ATT-09 by Appendix A.'),
    'FR-ATT-11': ('H', 'C', 'IMPLEMENTED', ('/api/user/calendar',),
                  'Sessions, breaks, day-expanded leaves, holidays and the finalised '
                  'FR-JOB-01 status per day.'),
    'FR-ATT-12': ('L', 'R', 'IMPLEMENTED', ('/api/live-monitoring',), 'Active-break monitoring.'),
    'FR-ATT-13': ('L', 'R', 'IMPLEMENTED', ('/api/break-summary',), 'Per-employee counts and minutes.'),
    'FR-ATT-14': ('L', 'R', 'IMPLEMENTED', ('/api/disposed-breaks',), 'Last hour.'),
    'FR-ATT-15': ('M', 'C', 'PARTIAL', ('/api/dashboard-stats',),
                  'The five keys are stable. They are recomputed per request, with no '
                  'Redis 15 s cache and no worker refresh, so the cost grows with the '
                  'employee count.'),
    'FR-ATT-16': ('H', 'R', 'IMPLEMENTED', ('/api/admin/breaks', '/api/admin/dispose-break/<int:break_id>'),
                  'One console call for active/disposed/summary; dispose ends any '
                  'Active break with an audited reason.'),
    'FR-ATT-17': ('M', 'C', 'IMPLEMENTED', (),
                  'shift_assignments, effective-dated; get_shift/set_shift resolve the '
                  'model per backend+schema and init_db no longer re-adds the removed '
                  'columns to v2.0 users.'),

    # ── FR-REG: regularization ──────────────────────────────────────────
    'FR-REG-01': ('M', 'R', 'PARTIAL', ('/api/regularization',),
                  'Filters and the company-wide/self split ship, the split decided by '
                  'policy.can_view_all. Delegated reports are not included.'),
    'FR-REG-02': ('M', 'C', 'PARTIAL', ('/api/regularization',),
                  'Corrected times are captured and future dates are refused. The '
                  '"a specific corrected time is required" rule is not enforced: a '
                  'reason-only request is accepted.'),
    'FR-REG-03': ('H', 'C', 'IMPLEMENTED', ('/api/regularization/<int:rid>/approve',),
                  'Approval writes the corrected time and triggers the FR-JOB-01 '
                  'recompute for that day.'),
    'FR-REG-04': ('M', 'N', 'NOT_STARTED', (), 'No regularization Excel export.'),

    # ── FR-LEA: leave ───────────────────────────────────────────────────
    'FR-LEA-01': ('M', 'R', 'PARTIAL', ('/api/leaves',),
                  'Filters and the scope split ship. Missing: delegated-manager visibility.'),
    'FR-LEA-02': ('M', 'C', 'PARTIAL', ('/api/leaves',),
                  'Dates are swapped if reversed and the session is recorded. Working-day '
                  'deduction ignores holidays — FR-LEA-09 asks for one shared function '
                  'and there is none.'),
    'FR-LEA-03': ('M', 'N', 'PARTIAL', ('/api/leaves/export',),
                  'Excel export ships, synchronously. The async variant for large ranges '
                  'is not implemented.'),
    'FR-LEA-04': ('H', 'C', 'IMPLEMENTED', ('/api/leaves/<int:leave_id>/approve',),
                  'Not the applicant, conditional update, used_days incremented and the '
                  'reservation consumed.'),
    'FR-LEA-05': ('M', 'C', 'PARTIAL', ('/api/leaves/<int:leave_id>/reject',),
                  'Reject is conditional and releases the reservation. Cancel is not '
                  'implemented at all — there is no cancel route.'),
    'FR-LEA-06': ('H', 'C', 'IMPLEMENTED', ('/api/leave-balance',),
                  'total/used/reserved/remaining derived per type per year, with the '
                  'source reported so a number can be traced to a policy or a default.'),
    'FR-LEA-07': ('H', 'C', 'NOT_STARTED', (),
                  'No manual grant route and no LEAVE_GRANT audit action. An admin '
                  'cannot add days to an employee; only the policy and the accrual job can.'),
    'FR-LEA-08': ('H', 'C', 'IMPLEMENTED', ('/api/users/<emp_id>/leave-policy', '/api/accrual/run',),
                  'Effective-dated per employee, entitlement derived and accrued month '
                  'by month from the rate, capped by the carry-forward cap, the ledger '
                  'posted by cron, on demand, or from the admin UI.'),
    'FR-LEA-08a': ('M', 'N', 'NOT_STARTED', (),
                   'approval_delegations exists in the canonical schema with a '
                   'no-overlap exclusion constraint, but no route reads or writes it. '
                   'A manager going on leave has no way to delegate.'),
    'FR-LEA-09': ('M', 'C', 'NOT_STARTED', (),
                  'There is no single working-day/holiday-deduction function. Leave day '
                  'counting, payroll LOP and the reports each approximate it '
                  'differently, which is the inconsistency the requirement exists to remove.'),

    # ── FR-NOT: notifications ───────────────────────────────────────────
    'FR-NOT-01': ('M', 'C', 'PARTIAL', ('/api/notifications',),
                  'Last-50 list with an unread count. Delivery is a direct SMTP call on '
                  'the request thread rather than an outbox enqueue, so a slow provider '
                  'can block the request that triggered it.'),
    'FR-NOT-02': ('M', 'R', 'IMPLEMENTED', ('/api/notifications/read',),
                  'Sets is_read and keeps the row, so a read notification does not vanish.'),
    'FR-NOT-03': ('S', 'R', 'NOT_STARTED', (),
                  'No per-category {in_app, email} preferences. category exists on the '
                  'notification row, but the preference model and its defaults do not.'),

    # ── FR-AST / FR-ATS: assets and recruitment ─────────────────────────
    'FR-AST-01': ('M', 'R', 'IMPLEMENTED', ('/api/assets', '/api/my-assets', '/api/assets/<int:aid>/return'),
                  'Issue/return with return_date set on return, own-assets view, and '
                  'outstanding counts feeding the offboarding clearance gate.'),
    'FR-ATS-01': ('M', 'C', 'IMPLEMENTED', ('/api/candidates/<int:cid>/status', '/api/pipeline'),
                  'Applied -> Screened -> Interviewed -> Offered -> Hired, with Rejected '
                  'and Withdrawn reachable before Hired; direct Hired is 409; the '
                  'pipeline aggregates counts per stage.'),
    'FR-ATS-02': ('M', 'C', 'IMPLEMENTED', ('/api/jobs', '/api/candidates', '/api/jobs/<int:jid>/close', '/api/candidates/<int:cid>'),
                  'Jobs and candidates CRUD for HR/Admin; status changes go through the '
                  'same guarded stage machine.'),
    'FR-ATS-03': ('H', 'C', 'IMPLEMENTED', ('/api/offers/<int:oid>/accept',),
                  'The only path to Hired is offer acceptance, which atomically creates '
                  'the pre-hire user, salary structure, onboarding workflow and checklist.'),
    'FR-ATS-04': ('H', 'C', 'IMPLEMENTED', ('/api/offers',),
                  'Offers require a 100% salary split, basic_pct/hra_pct/allowances_pct '
                  'are stored, and a unique constraint keeps two active checklists per '
                  'employee from existing.'),

    # ── FR-ONB / FR-OFF: lifecycle ──────────────────────────────────────
    'FR-ONB-01': ('H', 'C', 'IMPLEMENTED', ('/api/onboarding-workflows',),
                  'All five steps with their guards, task owners and per-step timestamps.'),
    'FR-ONB-02': ('H', 'C', 'IMPLEMENTED', ('/api/onboarding-workflows', '/api/onboarding-workflows/<int:workflow_id>'),
                  'HR/Admin see everyone; the candidate view is token-scoped through '
                  '/api/preboarding/<token>.'),
    'FR-ONB-03': ('H', 'C', 'IMPLEMENTED', ('/api/offers/<int:oid>/accept',),
                  'The checklist is created by the offer-acceptance transaction, not by '
                  'a separate call a caller could forget.'),
    'FR-ONB-04': ('H', 'C', 'PARTIAL', ('/api/preboarding/<token>/documents/<doc_type>', '/api/onboarding-checklist/<int:item_id>/review'),
                  'Upload, review, and mandatory rejection notes all ship. The shared '
                  'pipeline only checks the file extension (FR-DOC-02 is partial).'),
    'FR-ONB-05': ('H', 'C', 'IMPLEMENTED', ('/api/onboarding-workflows/<int:workflow_id>/steps/<int:step>/complete',),
                  'Advancement is guard-based; HR only confirms the physical/logistics steps.'),
    'FR-ONB-06': ('L', 'C', 'IMPLEMENTED', ('/api/onboarding-workflows',),
                  'Own workflow, or the HR/Admin list with days-in-step.'),
    'FR-OFF-01': ('H', 'C', 'IMPLEMENTED', ('/api/resignations', '/api/resignations/<int:resignation_id>/acknowledge'),
                  'Resignation is a first-class record that triggers the workflow; it is '
                  'never implied by a status change.'),
    'FR-OFF-02': ('M', 'C', 'IMPLEMENTED', ('/api/offboarding-tasks', '/api/exit-interviews'),
                  'Per-stage tasks with owners, and exit-interview scheduling.'),
    'FR-OFF-03': ('H', 'C', 'IMPLEMENTED', ('/api/offboarding-workflows/<int:offboard_id>/stages/<int:stage>/complete',
                                             '/api/offboarding-workflows/<int:offboard_id>/revoke-access'),
                  'Each stage is a guarded conditional update; stages 2 and 3 run in '
                  'parallel; Finance prepares and approves the settlement separately; the '
                  'nightly job revokes access on the LWD.'),

    # ── FR-PERF: goals and reviews ──────────────────────────────────────
    'FR-PERF-01': ('M', 'R', 'IMPLEMENTED', ('/api/goals', '/api/goals/<int:gid>', '/api/goals/<int:gid>/rate'),
                   'CRUD plus a 1-5 rating that transitions the goal to Completed, all in '
                   'goals.py. POST /api/goals had never worked (a bare VALUES with ten '
                   'placeholders against a nine-column table, so every create was a 500) '
                   'and now uses an explicit column list and takes emp_id from the session. '
                   'PUT /api/goals/<id> was @login_required with no ownership check, so any '
                   'authenticated user could rewrite any goal by guessing a sequential id; '
                   'it is now owner/manager/HR and cannot set status or rating, which is how '
                   'the rating flow used to be skipped. Rating is by the reporting manager or '
                   'HR/Admin and never the owner (the SRS calls that out), through a new '
                   'reporting-line gate because @admin_required excluded the role the '
                   'requirement names. Completed is terminal and the write is conditional.'),
    'FR-PERF-02': ('M', 'R', 'PARTIAL', ('/api/performance-reviews', '/api/performance-reviews/<int:rid>/submit'),
                   'Cycle create/list is HR/Admin-gated. The submit path does not require '
                   'the submitter to be the assigned reviewer, so any user who can reach it '
                   'can submit somebody else\'s review, and the write is unconditional.'),

    # ── FR-EXP: expenses ────────────────────────────────────────────────
    'FR-EXP-01': ('M', 'R', 'PARTIAL', ('/api/expense-categories',),
                  'Six seeded categories and a read endpoint. No CRUD, so the set is '
                  'configurable only by editing rows.'),
    'FR-EXP-02': ('M', 'C', 'PARTIAL', ('/api/expenses',),
                  'emp_id is taken from the session and a body override is rejected with '
                  'a 400, closing the v1.0 impersonation hole; the category must exist '
                  'and the amount must be positive. Missing: receipt validation on upload.'),
    'FR-EXP-03': ('M', 'C', 'IMPLEMENTED', ('/api/expenses', '/api/expenses/<int:eid>/status',),
                  'Strict transition table in expenses.py: Pending -> Approved/Rejected by '
                  "the owner's manager or HR/Admin, Approved -> Paid by Finance/Admin only "
                  '(Appendix A-11), Rejected and Paid final. Self-approval blocked, a '
                  'rejection reason required, every write a conditional UPDATE with a '
                  'before/after audit, and the list reports the actions the caller may '
                  'actually take. Finance holds the expenses module because the SRS names '
                  'it for Paid; the list stays scoped to own + reports, so that grants reach '
                  'rather than company-wide visibility.'),

    # ── FR-TKT: tickets ─────────────────────────────────────────────────
    'FR-TKT-01': ('M', 'C', 'PARTIAL', ('/api/tickets',),
                  'HR/IT queues and a priority on every ticket. The SLA target per '
                  'priority is not configurable and there are no subcategories.'),
    'FR-TKT-02': ('M', 'C', 'PARTIAL', ('/api/tickets',),
                  'Create and list with the owner/assignee/department/admin visibility '
                  'rule. Department notification is partial: the department is notified '
                  'but not Super Admin.'),
    'FR-TKT-03': ('M', 'C', 'IMPLEMENTED', ('/api/tickets/<int:tid>', '/api/tickets/<int:tid>/comment'),
                  'The detail view re-checks visibility server-side rather than trusting '
                  'the list query, and comments bump updated_at.'),
    'FR-TKT-04': ('M', 'C', 'PARTIAL', ('/api/tickets/<int:tid>/status',),
                  'Open -> In Progress -> Resolved -> Closed is enforced with a guarded '
                  'transition. Reopened does not exist: a comment on a closed ticket is '
                  'accepted but leaves it Closed.'),

    # ── FR-DOC: documents ───────────────────────────────────────────────
    'FR-DOC-01': ('M', 'R', 'IMPLEMENTED', ('/api/documents',),
                  'Scoped to the owner unless HR/Admin.'),
    'FR-DOC-02': ('H', 'C', 'PARTIAL', ('/api/upload',),
                  'Multipart upload, a size cap and an extension allow-list. The MIME '
                  'type is taken from the filename extension rather than sniffed from '
                  'the content, and there is no malware scan. A renamed .exe passes.'),
    'FR-DOC-03': ('M', 'C', 'PARTIAL', ('/api/documents/<int:did>/download', '/api/documents/<int:did>'),
                  'Owner or HR/Admin for download, Admin-only delete (the v1.0 hole is '
                  'closed). The download is a direct file response, not a presigned URL, '
                  'and it is not audited.'),

    # ── FR-HOL: holidays ────────────────────────────────────────────────
    'FR-HOL-01': ('M', 'C', 'PARTIAL', ('/api/holidays',),
                  'National/Optional types, per-location applicability and search. The '
                  'location field is stored but not filtered on.'),
    'FR-HOL-02': ('H', 'C', 'PARTIAL', ('/api/holidays', '/api/holidays/<int:hid>'),
                  'CRUD for HR/Admin with a duplicate (name, date) check. The check is '
                  'in the handler, not a unique constraint, so it races under '
                  'concurrency. No year-to-year copy, so Feb-29 handling is absent.'),
    'FR-HOL-03': ('H', 'C', 'PARTIAL', (),
                  'holiday_optins exists in the canonical schema with a unique index. No '
                  'route writes an opt-in and there is no HR approval queue, so Optional '
                  'holidays cannot actually be opted into.'),

    # ── FR-JOB: scheduled work ──────────────────────────────────────────
    'FR-JOB-01': ('H', 'C', 'IMPLEMENTED', (),
                  'Nightly finalisation classifies every active employee in the required '
                  'priority order, groups per shift date so night shifts finalize '
                  'correctly, replaces the target date transactionally, and recomputes '
                  'on regularization approval.'),
    'FR-JOB-02': ('H', 'C', 'PARTIAL', (),
                  'Hourly purge of expired reset and idempotency tokens. The orphaned-break '
                  'auto-close is not implemented (see FR-AUTH-14).'),
    'FR-JOB-03': ('S', 'R', 'NOT_STARTED', (),
                  'No quarterly job opens the next performance review cycle.'),
    'FR-JOB-04': ('H', 'C', 'IMPLEMENTED', ('/api/admin/offboarding/revoke',),
                  'The nightly job closes sessions, clears permissions, disables login and '
                  'marks the employee Inactive on their last working day.'),
    'FR-JOB-05': ('H', 'C', 'NOT_STARTED', (),
                  'No leader election. The scheduler starts in the gunicorn master, which '
                  'is the usual single-instance answer, but a multi-pod deployment would '
                  'run every cron job once per pod.'),

    # ── FR-ANL / FR-RPT: analytics and reports ──────────────────────────
    'FR-ANL-01': ('M', 'C', 'PARTIAL', ('/api/analytics/headcount', '/api/analytics/leave-trends',
                                          '/api/analytics/expense-summary', '/api/analytics/performance-summary'),
                  'All four metrics ship and match the v1.0 figures. They are computed '
                  'live per request, not served from materialized views refreshed on a '
                  'schedule.'),
    'FR-ANL-02': ('M', 'C', 'PARTIAL', ('/api/analytics/attrition-risk',),
                  'The four factors are all present. The weights are hard-coded rather '
                  'than configuration (FR-ANL-04), and the score is not versioned.'),
    'FR-ANL-04': ('M', 'C', 'NOT_STARTED', ('/api/analytics/attrition-risk',),
                  'The weights (0.4, 1.5, 0.8, 3) are literals in the handler. Changing '
                  'them needs a code change and redeploy.'),
    'FR-RPT-01': ('M', 'C', 'PARTIAL', ('/api/reports',),
                  'The self-service view substitutes the caller scope. It ignores a '
                  'supplied department only in some paths; the admin/HR view is gated by '
                  'the policy.'),
    'FR-RPT-02': ('M', 'C', 'PARTIAL', ('/api/reports/export', '/api/reports/pdf', '/api/reports/department-summary'),
                  'Excel and PDF export plus the department summary, all synchronous. No '
                  'async job for ranges beyond a month or 200 employees.'),

    # ── FR-AUD: audit ───────────────────────────────────────────────────
    'FR-AUD-01': ('H', 'C', 'IMPLEMENTED', ('/api/audit-log',),
                  'actor/action/entity/entity_id/before/after/ip/request_id/created_at on '
                  'every mutating action, including from a scheduler thread (which used '
                  'to raise and be swallowed, so those rows never existed).'),
}


def by_status(status: str) -> list[str]:
    """Requirement IDs with a given status, sorted."""
    return sorted(rid for rid, row in TRACEABILITY.items() if row[2] == status)


def counts() -> dict[str, int]:
    """Requirement count per status."""
    out: dict[str, int] = {}
    for row in TRACEABILITY.values():
        out[row[2]] = out.get(row[2], 0) + 1
    return out


def rows():
    """(id, priority, delta, status, routes, note) sorted by id, for rendering."""
    for rid, row in sorted(TRACEABILITY.items()):
        yield (rid, *row)
