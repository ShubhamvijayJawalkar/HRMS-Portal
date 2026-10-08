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
    'FR-USR-02': ('M', 'C', 'PARTIAL',
                  ('/api/users', '/api/admin/users/<emp_id>/password'),
                  'emp_id/email/role/department validated, case-insensitive email '
                  'uniqueness, default status Active + allow_login. A 24 h single-use '
                  'welcome token is issued (credentials.issued) but its only delivery '
                  'is the outbox, so on a deployment with no SMTP the employee never '
                  'receives it - which is why an admin-set password '
                  '(POST /api/admin/users/<emp_id>/password) is the delivery-independent '
                  'path for both the welcome and the forgotten-password case. It closes '
                  'every session, clears any FR-AUTH-03 lockout (otherwise the admin '
                  'action appears to work and the employee is still locked out for 15 '
                  'minutes), refuses a blocked/archived/inactive target with a 409 that '
                  'names the action which would help, and requires the current password '
                  'when the target is the caller - otherwise a hijacked admin SESSION '
                  'becomes permanent ownership of the account. No password or hash is '
                  'written to audit_log, which is retained for years. Creating a user '
                  'no longer reports email_sent: true unconditionally, which on a '
                  'no-SMTP deployment meant an admin was told the welcome email went out '
                  'when nothing had been sent. Missing: balances seeded from the '
                  'grade/location policy rather than the default matrix (leave_policy '
                  'derives on read instead).'),
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
                  '(emp_id kept, free text left) are recorded in docs/ANONYMISATION.md §8. '
                  'Two deviations found by auditing this row against the requirement text. '
                  'The SRS spells the replacement as name -> "Former Employee #id"; the code '
                  'substitutes a salted HMAC pseudonym (ANON-<16 hex>), which is stable per '
                  'employee without restating an identifier in every row. And the SRS names '
                  'bank details among the identifiers to scrub - no bank or account column '
                  'exists anywhere in the schema, so that clause is vacuous today; a test now '
                  'fails if a personal-looking column is ever added without being taught to '
                  'the eraser.'),
    'FR-USR-06a': ('H', 'N', 'IMPLEMENTED', ('/api/anonymisation/<int:request_id>/confirm',),
                   'Two-person state machine proposed -> confirmed -> applied; the '
                   'confirmer must be a different user, and only an archived account qualifies.'),
    'FR-USR-07': ('H', 'R', 'IMPLEMENTED', ('/api/users/bulk',),
                  'All three clauses, and the delegation is the design rather than an '
                  'implementation detail. The SRS asks for Bulk POST /api/users/bulk '
                  '{action, emp_ids[]}: SELF EXCLUDED; per-row result reported, and '
                  'PARTIAL FAILURE DOES NOT FAIL THE BATCH. Every row delegates to '
                  '_set_user_access_status, the same function the single block/unblock/'
                  'archive/restore routes call, so the batch has no behaviour of its own '
                  'to get wrong: it cannot be more permissive about a 409, cannot forget '
                  'to close sessions, and cannot skip the audit row. Every refusal the '
                  'single routes make - already archived, archived users must be restored '
                  'first, already blocked - is inherited per row with the same wording. '
                  'Self is a per-row FAILURE rather than a silent skip, with the single '
                  'routes exact 409 message: silently dropping the row would leave an '
                  'administrator who selected thirty people seeing 29 archived and no '
                  'reason the thirtieth was different. Status codes state which outcome '
                  'happened - 200 all succeeded, 207 mixed, 400 none - because a 200 that '
                  'hid a failure or a 400 that hid twenty successes is the reason the SRS '
                  'asks for per-row results. The action vocabulary is data (BULK_USER_'
                  'ACTIONS) mapping each action to its status and allow_login value, '
                  'because those two decide whether sessions are closed. Validation runs '
                  'before anything is touched, ids are normalised and blank-filtered up '
                  'front so [""] gets the same answer as [], and the batch is bounded at 500 '
                  'so one request cannot hold locks across the directory. The admin UI '
                  'has a selection column and a bulk bar whose result is rendered as the '
                  'per-row list rather than a single done, with select-all scoped to the '
                  'current page and saying so. No schema change and no Alembic revision: '
                  'the operations already existed and only the batch entry point was '
                  'missing.'),
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
                  'scoped by emp_id as well. Create and delete are both audited, '
                  'because policy.PII_FIELDS classifies dependents as PII - a third '
                  'party with no statutory retention of their own - and a silent erase '
                  'of one was the gap the audit pass found. The create also used a '
                  'bare INSERT INTO dependents VALUES (...), now a named column list.'),
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
    'FR-AUTH-01': ('M', 'C', 'IMPLEMENTED', ('/login', '/api/forgot-password',
                                              '/api/reset-password'),
                   'Employee code is trimmed and matched case-insensitively. '
                   'PER-ACCOUNT *AND* PER-IP, which is the whole requirement and was '
                   'measured rather than asserted. Two per-IP-only limiters existed: '
                   'LOGIN_RATE_LIMIT at 20/min (10x below the SRS shift-start burst of '
                   '1,000 logins in 5 minutes = 200/min from one address, so it '
                   'refused 90% of a legitimate shift start) and the global '
                   'DEFAULT_RATE_LIMIT at 200/min keyed by remote address - measured '
                   'at 15 distinct employees behind one egress address and 25 reads '
                   'each: 200 of 375 served, 175 locked out with 429, because the '
                   'first users consumed a bucket shared by the whole company. With '
                   '500 concurrent sessions behind one corporate NAT that is 0.4 req/min '
                   'per user. The global key is now the employee identity once '
                   'authenticated and the remote address only while anonymous, so '
                   'authenticated browsing is fair and NAT-safe; anonymous traffic '
                   '(the surface actually worth limiting) stays per address; and '
                   'login gains the per-account dimension from lockout.py (10 '
                   'consecutive failures in 15 minutes, FR-AUTH-03), which is what '
                   'makes a looser address limit safe. login/forgot-password/'
                   'reset-password are all env-overridable. The shared-NAT case is '
                   'reproduced by scripts/shared_nat_check.py, and two ratchet tests '
                   'fail if a default drifts back below the SRS figures.'),
    'FR-AUTH-02': ('M', 'N', 'IMPLEMENTED', ('/login',),
                   'Every refusal is the same 401 {"error":"invalid_credentials"}: '
                   'unknown account, wrong password, blocked, archived, pre-hire, '
                   'allow_login=false and locked out are indistinguishable, and the '
                   'password is verified before any state is considered so the '
                   'response time says nothing either. This row previously claimed '
                   'IMPLEMENTED while the route answered two different 401 messages '
                   'and two 403s ("Account is blocked", "Login is not allowed") - the '
                   'state leak the requirement exists to prevent. Found by reading '
                   'the handler while building FR-AUTH-03.'),
    'FR-AUTH-03': ('M', 'N', 'IMPLEMENTED', ('/login', '/api/admin/users/<emp_id>/unlock'),
                   'lockout.py, to the SRS numbers: 10 consecutive failures inside a '
                   '15-minute window lock the account for 15 minutes and notify the '
                   'user by email (plus in-app, because send_email only logs when no '
                   'SMTP host is configured, and a lockout nobody can see is a silent '
                   'denial). A successful sign-in breaks the streak - without that, '
                   'four typos spread over a week lock an employee out - and the '
                   'window is sliding, so ten failures across a month are not ten '
                   'consecutive failures. An admin can clear a lockout immediately '
                   'without touching users.status, because a lockout is a temporary '
                   'consequence of failed sign-ins while Blocked is a sanctioned '
                   'account state; folding them together would write an HR record '
                   'against a fifteen-minute nuisance. Deliberate deviation: the SRS '
                   'flow diagram puts the counter in Redis and it is stored on users '
                   'instead, because this app treats Redis as optional and a lockout '
                   'that silently stops existing when Redis is unreachable has failed '
                   'open rather than degraded.'),
    'FR-AUTH-04': ('M', 'C', 'PARTIAL', ('/logout',),
                   'Server-side Redis sessions (opaque cookie, 8 h TTL), HttpOnly, '
                   'SameSite=Lax, Secure in production; logout deletes the server copy. '
                   'There is no 24 h *absolute* timeout distinct from the 8 h idle one.'),
    'FR-AUTH-05': ('M', 'R', 'IMPLEMENTED', ('/logout',),
                   'Logout closes the active user_sessions row with total_hours = '
                   'logout - login and audits the action.'),
    'FR-AUTH-06': ('M', 'R', 'IMPLEMENTED', ('/',), 'Redirects by session state.'),
    'FR-AUTH-07': ('M', 'R', 'IMPLEMENTED', ('/dashboard',),
                   'Admin vs self dashboard chosen by policy.sees_admin_surface(). '
                   'Unauthenticated gets 302 for a page and 401 for JSON - and the '
                   'Accept header the SRS names is now honoured, so this row was '
                   'previously describing behaviour the handler did not have: '
                   '_wants_json checked is_json and the /api/ path but not Accept, so '
                   'GET /dashboard with Accept: application/json answered 302 to the '
                   'login page - HTML for a caller that asked for JSON, which then '
                   'follows the redirect and cannot parse what it got. Same failure the '
                   'multipart case was fixed for, one trigger earlier. A combined Accept '
                   'still redirects, so a browser mentioning JSON among other types is '
                   'unaffected.'),
    'FR-AUTH-08': ('H', 'C', 'IMPLEMENTED', ('/api/forgot-password', '/reset-password'),
                   'Always 202 with the SRS\'s own sentence and never a token, so the '
                   'request has nothing to compare between a real and an unknown '
                   'account; a test asserts all four request shapes answer '
                   'byte-identically. Delivery moved to the outbox, which is where the '
                   'SRS puts it ("email {host}/reset-password?token=... (queued via '
                   'outbox)") and what makes an empty response possible. This row '
                   'previously claimed IMPLEMENTED while the route answered 404 {"error": '
                   '"No matching user found"} for an unknown account and 200 *carrying '
                   'the working token* for a real one - not a weakened control but its '
                   'inversion, since a caller could confirm any employee ID and obtain a '
                   'credential without touching the account. /reset-password now exists '
                   'too: the emailed URL used to 404, so the journey was reachable only '
                   'by calling the API.'),
    'FR-AUTH-09': ('H', 'C', 'IMPLEMENTED', ('/api/reset-password', '/reset-password'),
                   'token_urlsafe(32), stored as a SHA-256 digest and never in the '
                   'clear, 1 h expiry, single use via a write conditional on used = 0, '
                   'and every other live token for the account invalidated on a '
                   'successful reset - without that last part an attacker who requested '
                   'their own reset while a legitimate one was live keeps a working '
                   'credential after the legitimate user resets. Purged hourly. The '
                   'queued link carries the token encrypted (the digest cannot be '
                   'reversed, so the dispatcher needs a copy), which is unreadable '
                   'without the app Fernet key, so a database read still cannot mint a '
                   'reset. Two corrections to this row: it previously said the token was '
                   '"stored unhashed", which was half true and arguably worse - hashing '
                   'existed on the write path while /api/reset-password looked tokens up '
                   'as token IN (raw, digest) to accommodate two plaintext tokens the '
                   'boot seed wrote, so a database read still yielded working '
                   'credentials; and it said the expiry should be 24 h, which is wrong - '
                   'that is FR-USR-02\'s welcome-email token, a different flow, and '
                   'FR-AUTH-09 specifies 1 h, which the code already used.'),
    'FR-AUTH-10': ('M', 'C', 'IMPLEMENTED',
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
    'FR-AUTH-11': ('H', 'N', 'IMPLEMENTED',
                   ('/api/mfa/enrol', '/api/mfa/confirm', '/api/mfa/challenge',
                    '/api/mfa/status', '/api/mfa/qr', '/api/mfa/disable',
                    '/api/admin/users/<emp_id>/mfa/reset'),
                   'mfa.py: TOTP (RFC 6238) for Admin/Super Admin/HR/Finance and '
                   'self-service opt-in for everyone else. Two-phase enrolment — the '
                   'row is written with enabled=0 and only a valid code promotes it, '
                   'so a stolen password cannot enrol an attacker\'s own authenticator '
                   'against the account. The password step parks the identity in '
                   'session["mfa_pending"] and deliberately does NOT set session["emp_id"], '
                   'so a half-authenticated session is refused by every existing gate by '
                   'construction rather than by each route remembering to check. Five '
                   'wrong codes abandon the parked login (429); one step of clock drift '
                   'is tolerated; the pending state expires after ten minutes. Secrets are '
                   'Fernet-encrypted under a dedicated MFA_ENCRYPTION_KEY and the feature '
                   'refuses with 503 rather than storing them in the clear — key '
                   'separation from SECRET_KEY, mirroring ANONYMISATION_SALT. Recovery is '
                   'an audited admin reset only, which is the weaker of the two answers '
                   'the SRS allows: there are no recovery codes, so the mitigation is that '
                   'the reset answers identically whether or not the target was enrolled '
                   '(so it cannot be used to find out who is protected) and writes a '
                   'before/after audit row. A mandatory role cannot disable its own factor.'),
    'FR-AUTH-12': ('M', 'C', 'IMPLEMENTED', ('/api/csrf-token',),
                   'A per-session synchroniser token, compared with hmac.compare_digest '
                   'on every mutating request. Note the shape differs from the SRS, which '
                   'names "a csrf_token cookie plus X-CSRF-Token header (double-submit)": '
                   'the token lives in the session rather than in a separate cookie, and '
                   'enforcement is app-wide rather than scoped to /api/* - both stricter '
                   'than asked, and session storage is the stronger of the two patterns. '
                   'An HTML response gets a script that attaches the header to fetch, and '
                   'native forms carry a hidden field. /api/preboarding/* is exempt because '
                   'those requests authenticate with a signed, expiring token of their own '
                   'and cannot be forged cross-site. Asserted end to end with server-side '
                   'sessions too.'),
    'FR-AUTH-13': ('L', 'C', 'PARTIAL', ('/api/credentials',),
                   'Restricted by the permission policy and no longer returns '
                   'passwords or hashes. Missing the 5-minute re-authentication and '
                   'the audit row for a credential read.'),
    'FR-AUTH-14': ('H', 'C', 'IMPLEMENTED', ('/api/admin/users/<emp_id>/unlock',),
                  'Hourly job does both duties the SRS names in one sentence: purge '
                  'expired reset tokens AND idempotency keys (CC-07), and auto-close '
                  'breaks left Active for more than 12 hours. The sweep is '
                  'orphan_breaks.close_orphaned_breaks: the write is conditional on '
                  'status = Active so a repeat pass or a racing pod is a no-op rather '
                  'than a double notification, and it sets status Orphaned plus '
                  'ended_reason orphan_timeout - the canonical schema already carried '
                  'that column and documented the vocabulary on it '
                  '(orphan_timeout|admin_dispose|auto_end_new_break) with no writer '
                  'anywhere, so a break could be closed four ways and there was no way '
                  'to tell which. Duration is derived from the break TYPE daily limit, '
                  'capped by elapsed time, and never from the 12 hours itself: that '
                  'number is a threshold for when a row can no longer be believed to be '
                  'running, not a length of break, and closing at start_time + 12h '
                  'would manufacture an absence and a loss-of-pay deduction out of a '
                  'forgotten button press. Every closure is audited with actor SYSTEM '
                  '(before/after) and notified to the employee, because the recorded '
                  'duration is a guess and they are the only party who knows when they '
                  'came back - FR-ATT-16 admin disposal is how a wrong record is '
                  'corrected, and an employee never told cannot ask.'),
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
                  'from the timestamps, action audited. The audit half was missing while '
                  'this row claimed it: a break is the origin of an attendance record that '
                  'feeds the payroll LOP calculation, so closing one wrote no record of '
                  'who closed it or when. It does now, with before/after. start_break '
                  'audits too, including the auto-end of a previous break.'),
    'FR-ATT-04': ('M', 'R', 'IMPLEMENTED', ('/api/user-breaks',),
                  'Own breaks only, UNIONed with any Active break from another date '
                  'so a forgotten break-end is still visible.'),
    'FR-ATT-05': ('H', 'C', 'IMPLEMENTED', ('/api/break-approvals',),
                  'Lunch only; one Pending per employee per shift date is enforced by a '
                  'unique **partial** index (`uq_pending_lunch_approval`, predicate '
                  "`status = 'Pending'`) on *both* schemas, not just an app-level SELECT "
                  'that two simultaneous requests can both pass. The canonical schema has '
                  'had the index since the baseline; `init_db` now creates the same index '
                  'on the compatibility shape (PostgreSQL is the only backend since the '
                  'Phase-6 decommission, so the "DuckDB cannot build a partial index" '
                  'limitation that forced conditional INSERTs elsewhere no longer applies), '
                  'and the route translates a UniqueViolation race into the same 409 '
                  'instead of a 500. A decided (Approved/Rejected) row frees the slot, '
                  'because the rule is "one *Pending*" - tests assert the partial-ness in '
                  'both directions.'),
    'FR-ATT-06': ('H', 'C', 'IMPLEMENTED', ('/api/break-approvals/<int:aid>/approve', '/api/break-approvals/<int:aid>/reject'),
                  'Manager/HR/Admin - or an active delegate of that manager (FR-LEA-08a) - '
                  'may approve, the update is conditional (CC-04), the action is audited '
                  'and the employee is notified. Every one of those clauses was false while '
                  'this row asserted them. The gate was @admin_required, so a Team Leader '
                  'could not approve their own report - the third instance of that bug here '
                  'after FR-EXP-03 and FR-PERF-01 - and the requirement was unreachable for '
                  'the role the SRS names. The approve write was unconditional, so two '
                  'approvers both won. Nothing was audited, and the employee was never '
                  'notified. reject additionally answered 200 {"message": "Break rejected"} '
                  'whether or not it rejected anything, the same always-200 lie the '
                  'regularization routes had. All fixed, reusing the reporting_line_required '
                  'gate from FR-PERF-01, and the decision itself goes through the shared '
                  'delegations.can_approve_for/approval_note rule the leave and '
                  'regularization routes use - so the SRS wording "including an active '
                  'delegate, FR-LEA-08a" is literally the actor rule, not a comment.'),
    'FR-ATT-07': ('L', 'R', 'PARTIAL', ('/api/break-types', '/api/user-breaks',),
                  'Minutes used and the approval flag are exposed. The per-type summary '
                  'is assembled by the client from two calls rather than served as one '
                  'projection.'),
    'FR-ATT-08': ('M', 'R', 'IMPLEMENTED', ('/api/login-hours',),
                  'First login to last logout, scoped to a shift date.'),
    'FR-ATT-09': ('H', 'C', 'IMPLEMENTED', ('/api/user/shift-summary',),
                  'shift_hours = last_logout - first_login (NOT the sum of sessions) '
                  'when both exist, else now - first_login for an open shift, CAPPED at '
                  'the scheduled shift length +25% and flagged estimated:true. The cap and '
                  'the flag were both missing, and the miss was the second instance of '
                  'the FR-LEA-09 defect class: _attendance_worked_hours already implemented '
                  'the cap and /api/user/shift-summary did not, so an employee who forgot '
                  'to log out saw a figure that grew without limit on their own dashboard '
                  'while payroll was credited the capped one - a support call every time, '
                  'and an employee whose screen disagrees with their payslip has no reason '
                  'to trust either. shift_hours.py is now the single rule, called from both, '
                  'and a test walks the AST of app.py so a third implementation cannot '
                  'appear. The cap is on the OPEN-SHIFT branch only: the SRS attaches it '
                  'there (to avoid a forgotten-logout skewing the figure) and a closed shift '
                  'is real recorded data - capping it would under-credit genuine overtime, '
                  'which is a payroll decision rather than a data-quality one. The payroll '
                  'credit ceiling stays on top of the shared rule for the same reason. '
                  'Reported: estimated (any open shift, since its end is by definition not '
                  'yet observed - a superset of the SRS wording that serves its intent), '
                  'capped (the 25% allowance actually bound, i.e. a logout was probably '
                  'forgotten), scheduled_hours, and shift_configured. Those last two exist '
                  'because an employee with NO configured shift resolves to midnight-to-'
                  'midnight = a 24h scheduled span, so the +25% cap would be 30 hours and the '
                  'skew this requirement prevents would arrive by another route; the module '
                  "s documented 8h default applies instead and shift_configured says so. A "
                  'night shift whose end is earlier than its start has its negative span '
                  'rolled forward a day rather than clamped to zero, which would otherwise '
                  'make every overnight shift look instantaneous and cap it at zero hours.'),
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
                  'One console call for active/disposed/summary; dispose ends any Active '
                  'break with an audited reason. Neither half existed: the route neither '
                  'audited nor accepted a reason, so the row claimed an audited reason for '
                  'a handler that had neither. The reason is not cosmetic - this ends '
                  "another employee's break, shortening their recorded attendance and so "
                  'their pay, and "no reason given" is indistinguishable from a mistake '
                  'once the row is written. A reason is now required rather than '
                  'defaulted, recorded in the audit row, returned to the caller, and sent '
                  'to the employee as a notification.'),
    'FR-ATT-17': ('M', 'C', 'IMPLEMENTED', (),
                  'shift_assignments, effective-dated; get_shift/set_shift resolve the '
                  'model per backend+schema and init_db no longer re-adds the removed '
                  'columns to v2.0 users.'),

    # ── FR-REG: regularization ──────────────────────────────────────────
    'FR-REG-01': ('M', 'R', 'IMPLEMENTED', ('/api/regularization',),
                  'pending_my_approval, status and month filters all parse and apply, and '
                  'the company-wide/self split is decided by policy.can_view_all. The '
                  'manager view includes delegated reports (FR-LEA-08a): the '
                  'pending_my_approval filter is the shared _pending_my_approval_clause '
                  'that also feeds the leave list and the approve routes, so the view '
                  'cannot promise a request the decision refuses - one rule in the two '
                  'shapes (delegations.approvable_employees / can_approve_for).'),
    'FR-REG-02': ('M', 'C', 'PARTIAL', ('/api/regularization',),
                  'Corrected times are captured and future dates are refused. The '
                  '"a specific corrected time is required" rule is not enforced: a '
                  'reason-only request is accepted.'),
    'FR-REG-03': ('H', 'C', 'IMPLEMENTED', ('/api/regularization/<int:rid>/approve',),
                  'Approval writes the corrected time, is conditional on status = '
                  'Pending, and triggers the FR-JOB-01 recompute for that employee and '
                  'date. This row was IMPLEMENTED with a note mentioning none of that, and '
                  'reading the handler found every outcome was a 200: the success return '
                  'sat outside the if that did the work, so approving a non-existent '
                  'request, approving one already decided, and rejecting an '
                  'already-approved request all answered 200 - and the rejection answered '
                  '{"message": "Rejected"} while the row still said Approved, so a client '
                  'checking status_code believed a decision it had not made. Now a 404 for '
                  'an unknown request and a 409 naming the state found. Neither route '
                  'audited anything either, so an attendance correction that feeds payroll '
                  'left no trail; both now write a before/after row. The gate is a '
                  'deliberate widening that landed with the delegation slice: it was '
                  '@admin_required, so FR-REG-01\'s "manager view includes delegated '
                  'reports" was a dead end - a queue a manager was shown but only an admin '
                  'could decide. It is now reporting_line_required (manager, HR, Admin, '
                  'or an active delegate of that manager), and _approval_denial still '
                  'refuses the applicant their own request.'),
    'FR-REG-04': ('M', 'R', 'NOT_STARTED', (), 'No regularization Excel export.'),

    # ── FR-LEA: leave ───────────────────────────────────────────────────
    'FR-LEA-01': ('M', 'C', 'IMPLEMENTED', ('/api/leaves',),
                  'pending_my_approval / admin filter / self filters all ship, the split '
                  'decided by policy.can_view_all, and delegated-manager visibility is '
                  'real: the pending_my_approval filter is the shared '
                  '_pending_my_approval_clause, so a delegate sees the delegator\'s '
                  'reports exactly as the approve route decides them (FR-LEA-08a).'),
    'FR-LEA-02': ('M', 'C', 'IMPLEMENTED', ('/api/leaves',),
                  'Dates parsed and swapped if reversed, and days_requested is now the '
                  'working-day count the SRS names (days_requested = working days in range '
                  'per the employees weekly-off), shared with payroll and reports per '
                  'FR-LEA-09 rather than counted in calendar days as it was. The `session` '
                  'field (Full | First-half | Second-half) is IMPLEMENTED: the column '
                  'existed on the canonical schema with no writer and no reader, so a '
                  'half-day leave could not be expressed at all and an employee on a '
                  'four-hour shift had to book a whole day. An unknown session is a 400 listing '
                  'the three values rather than being defaulted. The apply response reports '
                  'days_requested, calendar_days and session, so an employee can see what '
                  'they were charged and reconcile it against their own calendar.'),
    'FR-LEA-03': ('M', 'R', 'PARTIAL', ('/api/leaves/export',),
                  'Excel export ships, synchronously. The async variant for large ranges '
                  'is not implemented.'),
    'FR-LEA-04': ('H', 'C', 'IMPLEMENTED', ('/api/leaves/<int:leave_id>/approve',),
                  'Approve and reject are one actor rule, and the "not the applicant" half '
                  'was previously asserted rather than enforced: the gate was admin-only, '
                  'so an employee could not reach the route, but nothing refused the wrong '
                  'approver on the way in. The decision is now '
                  'delegations.can_approve_for, consulted on every request: the applicant '
                  'is never their own approver (refused first, whatever their role), the '
                  'actor must be the employee\'s manager, an active delegate of that '
                  'manager (FR-LEA-08a), or HR/Admin. The update is conditional on '
                  'status = Pending (CC-04), so two approvers racing give one winner, and '
                  'leave_balance.used_days and reserved move in the same transaction. A '
                  'delegate\'s decision is audited as "approved by delegate for manager X" '
                  '(delegations.approval_note).'),
    'FR-LEA-05': ('H', 'C', 'IMPLEMENTED', ('/api/leaves/<int:leave_id>/reject',
                                             '/api/leaves/<int:leave_id>/cancel'),
                   'Reject is conditional and releases the reservation. Cancel '
                   'now exists: Pending only, or Approved before it starts, by owner or admin '
                   '(leave_policy.check_cancel), and the ledger reversal differs by state - a '
                   'Pending request releases the reservation, an Approved one takes the days '
                   'back out of used_days, which is why the decision and the reversal are one '
                   'function (leave_policy.cancel). Cancelling twice or a settled request is a '
                   '409, a started leave is a 409, and the action taken is in the response and '
                   'the audit row.'),
    'FR-LEA-06': ('M', 'C', 'IMPLEMENTED', ('/api/leave-balance',),
                  'total/used/reserved/remaining derived per type per year, with the '
                  'source reported so a number can be traced to a policy or a default.'),
    'FR-LEA-07': ('H', 'R', 'IMPLEMENTED', ('/api/leave-grants',),
                  'All five clauses. HR/Admin can add days to ONE OR MORE employees'
                  'balances for a type/month/year; every grant is audited as LEAVE_GRANT '
                  'with before/after totals; the employee is notified; and a GET returns '
                  'the grant history, so a balance that differs from the policy is '
                  'explainable rather than mysterious. The gate is '
                  'hr_or_admin_required, because the requirement names both and '
                  '@admin_required would have excluded HR - the same gate-versus-'
                  'requirement mismatch this codebase has now found in four places. THE '
                  'DESIGN DECISION: a grant is NOT written to '
                  'leave_balance.total_days. That column is DERIVED - ensure_balances '
                  'recomputes it from the policy on every read and overwrites it - so a '
                  'grant written there would be silently erased the next time anybody '
                  'opened the balance, surviving only until the next page load with no '
                  'audit row able to explain where it went. The grant is a ROW '
                  '(leave_grants, Alembic 0011) and entitlement_days adds the years grants '
                  'to the policy figure; the entitlement was split into a wrapper plus '
                  '_policy_entitlement_days precisely because the policy function has '
                  'four early returns and adding the grant to each is how a future branch '
                  'would silently forget it. entitlement gains a +grants source suffix so '
                  'a number that came from an administrator is self-describing. '
                  'Before/after totals are READ FROM THE BALANCE, not computed as '
                  'before + days: a grant can push an employee past the carry-forward '
                  'cap, so the arithmetic sum states a ceiling they do not have, in the '
                  'record an administrator reads to decide whether to grant again. A '
                  'grant may be NEGATIVE because the same route is how a mis-keyed one is '
                  'corrected and the correction stays in the same ledger as the mistake. '
                  'Each employee in a batch commits and audits independently and partial '
                  'success answers 207: one mistyped id in a list of fifty would '
                  'otherwise cost the other forty-nine their adjustment. reason is '
                  'required. An archived or blocked employee is refused with the reason, '
                  'because a grant produces a number nobody can spend.'),
    'FR-LEA-08': ('M', 'N', 'IMPLEMENTED', ('/api/users/<emp_id>/leave-policy', '/api/accrual/run',),
                  'Effective-dated per employee, entitlement derived and accrued month '
                  'by month from the rate, capped by the carry-forward cap, the ledger '
                  'posted by cron, on demand, or from the admin UI.'),
    'FR-LEA-08a': ('S', 'N', 'IMPLEMENTED', ('/api/delegations', '/api/delegations/<int:delegation_id>'),
                   'A manager can delegate approval authority to another employee for a '
                   'date range; the no-overlap rule is enforced in the application and by '
                   'the canonical no_overlapping_delegation exclusion (the compat schema '
                   'answers through the same predicate in create). Delegates appear in '
                   'pending_my_approval views - the shared _pending_my_approval_clause, '
                   'which the leave and regularization lists and all six approve/reject '
                   'routes answer from the same delegations.approvable_employees / '
                   'can_approve_for rule - and a delegate\'s approval is audited as '
                   '"approved by delegate for manager X" (approval_note, fired only when '
                   'the delegation is what authorised the decision). Self-delegation is a '
                   '409, an employee cannot file a delegation in someone else\'s name '
                   '(403, CC-10), delegating requires actually holding authority (the '
                   'delegator must manage somebody, or be an admin), and only the '
                   'delegator or an admin may revoke - revoke also means an admin can '
                   'withdraw a delegation the delegator cannot reach (e.g. on leave '
                   'themselves).'),
    'FR-LEA-09': ('M', 'N', 'IMPLEMENTED',
                  ('/api/leaves', '/api/leaves/<int:leave_id>/approve',
                   '/api/leaves/<int:leave_id>/reject',
                   '/api/leaves/<int:leave_id>/cancel', '/api/leaves/export',
                   '/api/exit-interviews'),
                  'One function, called from one place (working_days.py), for leave days, '
                  'payroll loss-of-pay and the reports figure - the requirement as worded. '
                  'FOUR rules were live and they disagreed. Leave counted '
                  '(end - start).days + 1, so Friday-to-Monday cost four days of a '
                  'twelve-day allowance, two of them a weekend. Payroll counted '
                  'attendance_days rows with status Absent OR Half-day, so an employee marked '
                  'half-present lost a FULL day of pay - FR-JOB-01 classification made the '
                  'distinction and the money threw it away. Reports had no working-day figure '
                  'at all, reporting days-with-a-login from user_sessions, which is a fifth '
                  'rule answering a different question. leave_policy.days_between was a '
                  'sixth copy of the calendar rule and the dangerous one: apply reserved '
                  'working days while reject and cancel gave back calendar days, so every '
                  'REJECTED leave silently INCREASED the balance. All six now route through '
                  'the module; a test walks the AST of app.py and fails if an inline '
                  'day-count expression or a new module call site appears. Working days are '
                  'PER EMPLOYEE via get_weekly_off_pattern, because this application has no '
                  'company-wide Mon-Fri week and never did (FR-ATT-17) - a night-shift '
                  'operator is not off on Saturday. Holidays are deducted through '
                  '_is_attendance_holiday, so National applies to everyone and Optional only to '
                  'an approved opt-in (FR-HOL-03). A range with no working days is refused '
                  'with a 400 naming the reason rather than recorded as a Pending request '
                  'reserving zero. The figure is now STORED on the request (Alembic 0010, '
                  'nullable and deliberately unbackfilled, because a request approved under '
                  'the old rule has no honest value to reconstruct), so approve and cancel '
                  'move exactly what apply reserved - a holiday added in between would '
                  'otherwise make approve release a different number of days with every audit '
                  'row still honest, which is the FR-LEA-06 ledger defect reappearing one '
                  'layer down. Behaviour change recorded: leave and payroll now count working '
                  'days, so existing balances shift by however many weekends a request '
                  'spanned.'),
    # ── FR-PAY: payroll ─────────────────────────────────────────────────
    # All nine were absent from the matrix until the DSR extraction found them:
    # §5 prints their ids as `FR-PAY -01`, and plain-text extraction (correctly)
    # refused to read that as an id, so the module went untracked entirely. The
    # verdicts below come from reading the handlers, not from the presence of a
    # route: see each note.
    'FR-PAY-01': ('M', 'C', 'PARTIAL',
                  ('/api/salary-structures', '/api/payroll-runs'),
                  'The pages and APIs exist, but the gate is `finance_or_admin`, which '
                  'admits Admin and Finance only - the SRS also grants HR READ access, '
                  'and an HR user gets 403 on both routes. Missing: any surface at all '
                  'for payroll RATES, which is FR-PAY-03 and has no table to read from.'),
    'FR-PAY-02': ('M', 'C', 'PARTIAL', ('/api/salary-structures',),
                  'CRUD ships and the non-overlapping effective ranges are a real '
                  'exclusion constraint (no_overlapping_structure), not an app check. '
                  'Missing: the preview endpoint, which is the half the requirement '
                  'spends its second sentence on - it must call the SAME function as '
                  'FR-PAY-05, "one function, not two" (Appendix A-07), and there is no '
                  'preview route to be sharing it with yet.'),
    'FR-PAY-03': ('M', 'C', 'NOT_STARTED', (),
                  'No rates table exists in the canonical schema or anywhere in the '
                  'code, so there is nothing versioned, effective-dated or editable. '
                  'Every rate the calculation needs is a literal inside '
                  'calc_payroll_item (app.py): PF 0.12 / ESI 0.0075 / pf_max 1800 / '
                  'esi_max 21000 / pt 200 / pt_threshold 10000. A run therefore cannot '
                  '"use the rates effective on the run period end date" because there '
                  'are no rates to date, and changing one is a code change.'),
    'FR-PAY-04': ('M', 'C', 'PARTIAL',
                  ('/api/payroll-runs', '/api/payroll-runs/<int:rid>/items'),
                  'Gross, PF, ESI and PT are computed from the hard-coded literals named '
                  'in FR-PAY-03 rather than from configured slabs. Three columns the '
                  'requirement needs exist but are never written - tds, lop_amount and '
                  'reimbursements all stay at their 0 default - so net pay silently '
                  'omits TDS, omits the FR-LEA-09 loss-of-pay deduction (which would '
                  'duplicate the arithmetic FR-LEA-09 exists to single-source) and omits '
                  'approved unpaid reimbursements. calc_tds hard-codes its own slab table '
                  'and is used only by the TDS report, so the report and the payslip can '
                  'disagree.'),
    'FR-PAY-05': ('M', 'C', 'PARTIAL', ('/api/payroll-runs',),
                  'Creation, Active/Onboarding coverage (Appendix A-08) and the Draft '
                  'initial status all ship. Two things are missing: the duplicate '
                  '(month, year, not-Cancelled) check is an app-level SELECT with no '
                  'unique constraint behind it, so two concurrent requests both pass it; '
                  'and the run executes synchronously on the request thread with no '
                  'background job and no progress to poll, so a large payroll holds a '
                  'worker for its whole duration.'),
    'FR-PAY-06': ('H', 'N', 'IMPLEMENTED',
                  ('/api/payroll-runs/<int:rid>/submit',
                   '/api/payroll-runs/<int:rid>/approve',
                   '/api/payroll-runs/<int:rid>/finalize'),
                  'Draft -> Submitted -> Approved -> Finalized, Finance/Admin only, the '
                  'submitter cannot approve their own run, every transition conditional '
                  'and audited into payroll_approvals, and finalization enqueues '
                  'payroll.finalized in the same transaction. Finalized items are '
                  'immutable; corrections go through an adjustment run referencing the '
                  'original. v1.0 had no approval step at all (Appendix A-09). Maker-'
                  'checker, self-approval and state-transition tests cover it on both '
                  'backends plus the v2.0 probe.'),
    'FR-PAY-07': ('H', 'C', 'PARTIAL',
                  ('/api/payslip/<int:run_id>/<emp_id>',
                   '/api/payroll-runs/<int:rid>/payslip-pdf/<emp_id>'),
                  'JSON and PDF both serve, self-or-admin/Finance access is enforced, '
                  'and payslip_generated is set on first PDF generation. Missing: the '
                  'PDF is written to local disk and handed back with send_file - there '
                  'is no object storage and no presigned URL, so the last sentence of '
                  'the requirement does not apply to this deployment.'),
    'FR-PAY-08': ('H', 'C', 'IMPLEMENTED',
                  ('/api/payroll-runs/<int:rid>/bank-file',
                   '/api/payroll-runs/<int:rid>/tds-report'),
                  'Bank file CSV (employee id, name, net salary, account, IFSC) and the '
                  'TDS report, both behind finance_or_admin and both refused with a 409 '
                  'unless the run is Finalized. Covered by a unit test and by two probe '
                  'flows that exercise both endpoints on the v2.0 target.'),
    'FR-PAY-09': ('S', 'R', 'NOT_STARTED', (),
                  'The SRS sentence is printed under Payroll but is a performance-review '
                  'requirement, cross-referenced by FR-JOB-03 (which names the same '
                  'quarterly job). Neither half exists: there is no review_cycles table - '
                  'performance_reviews rows carry a review_period string, which is a '
                  'label on a review, not a cycle to report progress against - so the '
                  'progress query has nothing to be progress OF, and no quarterly job '
                  'opens the next cycle. The flat GET /api/performance-reviews that '
                  'exists answers a different question.'),
    'FR-NOT-01': ('M', 'C', 'IMPLEMENTED', ('/api/notifications',
                                               '/api/send-notification-email'),
                  'Last-50 list with an unread count, and delivery through the '
                  'transactional outbox rather than a direct SMTP call on the request '
                  'thread. That was an availability defect, not a style preference: '
                  'SMTP is a network call to a third party and the old code had no '
                  'timeout at all, so a slow provider held a web worker for as long as '
                  'it chose - and one of the three call sites was the LOGIN path, so a '
                  'hanging provider would hold the worker meant to be refusing the '
                  'attempt. All three call sites (lockout notice, admin compose, '
                  'welcome mail on user creation) now enqueue a notification.email '
                  'event. The admin compose endpoint answers 202 with an event id '
                  'rather than 200 and a delivered claim it can no longer make, and its '
                  'audit row records queued plus the event id instead of a delivery that '
                  'did not happen. User creation reports email_queued and no longer '
                  'carries email_sent at all: the value was hardcoded True, and '
                  'reporting False would be no better now because the route does not '
                  'know either - what it says is that the credentials were not '
                  'delivered by this request, and names the recovery route.'),
    'FR-NOT-02': ('M', 'C', 'IMPLEMENTED', ('/api/notifications/read',),
                  'Sets is_read and keeps the row, so a read notification does not vanish.'),
    'FR-NOT-03': ('S', 'R', 'PARTIAL',
                  ('/api/notification-preferences',),
                  'Per-category {in_app, email} preferences with default true, own-row '
                  'only, partial update, validated, audited. The gap was structural, not '
                  'just missing: the stored categories and the SRS taxonomy had NOTHING in '
                  'common. Every leave notification was stored as `Leave` where the SRS '
                  'says `Leaves`, so a preference keyed on `Leaves` would never have '
                  'matched one, and tickets, goals, reviews and holiday opt-ins all fell '
                  'through to `General` - `Tickets` had no producer at all, so a '
                  'preference screen built on the old substring derivation would have been '
                  'switches that did nothing. notifications.category_for is now the single '
                  'derivation (exact table then longest prefix), a test parses the real '
                  'add_notification call sites and fails if any type has no mapping, and '
                  'the outbox no longer hardcodes a category. Two documented deviations: '
                  '`Performance` and `Holiday` are added because the app emits goal '
                  'ratings, reviews and holiday opt-ins and forcing them into General '
                  'would be worse than naming them; `Tickets-SLA` is kept even though '
                  'FR-TKT-01 has no producer yet, and the API reports has_producer per '
                  'category rather than presenting a dead switch. The `email` channel now '
                  'HAS a consumer: the outbox notification.email handler reads '
                  'notifications.wants_email before sending, and a muted category is '
                  'retired as DELIVERED rather than failed - returning False would retry, '
                  'and if the employee re-enabled the switch mid-backoff the mail would '
                  'then go, which is the opposite of what they asked for, and it would burn '
                  'five attempts and dead-letter a notification nobody was meant to '
                  'receive. The preference is read at DISPATCH time rather than enqueue '
                  'time, so turning a category off after an event was queued does not '
                  'mail it; the converse is accepted and stated, since such an event was '
                  'legitimately queued. Two deliberate exceptions, both documented at the '
                  'call site: a broken preference lookup sends anyway and logs, because '
                  'failing closed would silently drop a possible account-security notice '
                  'and an unwanted email is recoverable; and the admin compose endpoint '
                  'forces the send, because suppressing an explicit instruction would '
                  'leave the admin believing mail went out. The lockout notice also forces, '
                  'because the SRS pairs the lock with a notification precisely so the '
                  'login response cannot become a status oracle. STILL PARTIAL: no SMTP '
                  'provider is configured yet, so on the current deployment nothing is '
                  'actually delivered.'),
    'FR-AST-01': ('M', 'C', 'IMPLEMENTED', ('/api/assets', '/api/my-assets', '/api/assets/<int:aid>/return'),
                  'Issue/return with return_date set on return, own-assets view, and '
                  'outstanding counts feeding the offboarding clearance gate.'),
    'FR-ATS-01': ('M', 'C', 'IMPLEMENTED', ('/api/candidates/<int:cid>/status', '/api/pipeline'),
                  'Applied -> Screened -> Interviewed -> Offered -> Hired, with Rejected '
                  'and Withdrawn reachable before Hired; direct Hired is 409; the '
                  'pipeline aggregates counts per stage.'),
    'FR-ATS-02': ('M', 'C', 'IMPLEMENTED', ('/api/jobs', '/api/candidates', '/api/jobs/<int:jid>/close', '/api/candidates/<int:cid>'),
                  'Jobs and candidates CRUD for HR/Admin; status changes go through the '
                  'same guarded stage machine.'),
    'FR-ATS-03': ('M', 'C', 'IMPLEMENTED', ('/api/offers/<int:oid>/accept',),
                  'The only path to Hired is offer acceptance, which atomically creates '
                  'the pre-hire user, salary structure, onboarding workflow and checklist.'),
    'FR-ATS-04': ('M', 'C', 'IMPLEMENTED', ('/api/offers',),
                  'Offers require a 100% salary split, basic_pct/hra_pct/allowances_pct '
                  'are stored, and a unique constraint keeps two active checklists per '
                  'employee from existing.'),

    # ── FR-ONB / FR-OFF: lifecycle ──────────────────────────────────────
    'FR-ONB-01': ('M', 'C', 'IMPLEMENTED', ('/api/onboarding-workflows',),
                  'All five steps with their guards, task owners and per-step timestamps.'),
    'FR-ONB-02': ('M', 'C', 'IMPLEMENTED', ('/api/onboarding-workflows', '/api/onboarding-workflows/<int:workflow_id>'),
                  'HR/Admin see everyone; the candidate view is token-scoped through '
                  '/api/preboarding/<token>.'),
    'FR-ONB-03': ('M', 'C', 'IMPLEMENTED', ('/api/offers/<int:oid>/accept',),
                  'The checklist is created by the offer-acceptance transaction, not by '
                  'a separate call a caller could forget.'),
    'FR-ONB-04': ('M', 'C', 'PARTIAL', ('/api/preboarding/<token>/documents/<doc_type>', '/api/onboarding-checklist/<int:item_id>/review'),
                  'Upload, review, and mandatory rejection notes all ship. The shared '
                  'pipeline only checks the file extension (FR-DOC-02 is partial).'),
    'FR-ONB-05': ('M', 'C', 'IMPLEMENTED', ('/api/onboarding-workflows/<int:workflow_id>/steps/<int:step>/complete',),
                  'Advancement is guard-based; HR only confirms the physical/logistics steps.'),
    'FR-ONB-06': ('M', 'R', 'IMPLEMENTED', ('/api/onboarding-workflows',),
                  'Own workflow, or the HR/Admin list with days-in-step.'),
    'FR-OFF-01': ('M', 'C', 'IMPLEMENTED', ('/api/resignations', '/api/resignations/<int:resignation_id>/acknowledge'),
                  'Resignation is a first-class record that triggers the workflow; it is '
                  'never implied by a status change.'),
    'FR-OFF-02': ('M', 'R', 'IMPLEMENTED', ('/api/offboarding-tasks', '/api/exit-interviews'),
                  'Per-stage tasks with owners, and exit-interview scheduling.'),
    'FR-OFF-03': ('M', 'C', 'IMPLEMENTED', ('/api/offboarding-workflows/<int:offboard_id>/stages/<int:stage>/complete',
                                             '/api/offboarding-workflows/<int:offboard_id>/revoke-access'),
                  'Each stage is a guarded conditional update; stages 2 and 3 run in '
                  'parallel; Finance prepares and approves the settlement separately; the '
                  'nightly job revokes access on the LWD.'),

    # ── FR-PERF: goals and reviews ──────────────────────────────────────
    'FR-PERF-01': ('M', 'C', 'IMPLEMENTED', ('/api/goals', '/api/goals/<int:gid>', '/api/goals/<int:gid>/rate'),
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
    'FR-PERF-02': ('M', 'C', 'IMPLEMENTED', ('/api/performance-reviews',
                                             '/api/performance-reviews/<int:rid>/submit',
                                             '/api/feedback-360'),
                   'reviews.py. Cycle create/list stays HR/Admin-gated; a self-review is '
                   'refused at creation (409) because a review whose subject is also its '
                   'reviewer has nobody to sign it, and both employees must exist. Submit '
                   'requires the assigned reviewer and nothing else - HR and Admin get no '
                   'bypass, deliberately, because the rule exists to stop a review being '
                   'signed by somebody who did not write it (Appendix A-18). The rating is '
                   'bounded 1-5, the write is conditional on Draft so a signed review is '
                   'final, and the before/after is audited and the subject notified. 360° '
                   'feedback refuses self-feedback and takes a fixed category set.'),

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
    'FR-TKT-02': ('M', 'R', 'PARTIAL', ('/api/tickets',),
                  'Create and list with one shared visibility rule in tickets.can_view: '
                  'owner, assignee, or a role policy.can_view_all admits. The reporter is '
                  'always the session user, so a ticket cannot be filed in a colleague\'s '
                  'name. Still missing: the SRS also lists "matching department" scoping, '
                  'and Super Admin is not notified of a new ticket.'),
    'FR-TKT-03': ('M', 'C', 'IMPLEMENTED', ('/api/tickets', '/api/tickets/<int:tid>',
                                             '/api/tickets/<int:tid>/comment',
                                             '/api/tickets/<int:tid>/status'),
                  'One visibility rule (tickets.can_view) now serves the list, the detail '
                  'view, commenting and status changes, so the "defence in depth" the '
                  'requirement asks for is real rather than half of it. The comment route '
                  'had only an existence check, so a user refused a ticket with 403 could '
                  'still write into its history; the status route had no check at all. '
                  'Comments bump updated_at.'),
    'FR-TKT-04': ('M', 'C', 'IMPLEMENTED', ('/api/tickets/<int:tid>/status',
                                             '/api/tickets/<int:tid>/comment',
                                             '/api/tickets/<int:tid>/assign'),
                  'tickets.py holds the chain Open -> In Progress -> Resolved -> Closed, '
                  'enforced strictly and with a conditional write, so Open -> Closed is a '
                  '409 naming what is allowed. Reopened is a real status: a Closed ticket '
                  'reopens when its *reporter* comments within seven days of closing, and '
                  'nobody else can reopen it that way; an older closure stays closed. '
                  'Resolved -> In Progress and Reopened -> In Progress are the ways back. '
                  'Assignment is audited, which FR-TKT-04 asks for and nothing did; a ghost '
                  'assignee is a 404 and the assignee is notified.'),

    # ── FR-DOC: documents ───────────────────────────────────────────────
    'FR-DOC-01': ('M', 'R', 'IMPLEMENTED', ('/api/documents',),
                  'Scoped to the owner unless HR/Admin. The record and the delete are '
                  'both audited. The create also used a bare INSERT INTO '
                  'employee_documents VALUES (...) - not a live failure, since both '
                  'schemas have five columns in that order today, but the same latent '
                  'shape that made POST /api/goals return 500 on every backend and '
                  'mis-targeted add_holiday against v2.0 sixth column. It names its '
                  'columns now, and a test reads the canonical schema and fails if the '
                  'route list and the schema stop agreeing.'),
    'FR-DOC-02': ('M', 'C', 'PARTIAL', ('/api/upload',),
                  'Multipart upload, a size cap and an extension allow-list. The MIME '
                  'type is taken from the filename extension rather than sniffed from '
                  'the content, and there is no malware scan. A renamed .exe passes.'),
    'FR-DOC-03': ('M', 'C', 'PARTIAL', ('/api/documents/<int:did>/download', '/api/documents/<int:did>'),
                  'Owner or HR/Admin for download, Admin-only delete (the v1.0 hole is '
                  'closed), and the download now writes a DOCUMENT_DOWNLOAD audit row - a '
                  'document read that leaves no trail is the one that matters after an '
                  'incident. Still missing: a presigned URL rather than a direct file '
                  'response, so the object store is never reachable directly.'),

    # ── FR-HOL: holidays ────────────────────────────────────────────────
    'FR-HOL-01': ('M', 'R', 'IMPLEMENTED', ('/api/holidays',),
                  'National/Optional types, per-location applicability and search. The '
                  'location column is now writable, reported, and filtered on: a location '
                  'filter includes org-wide holidays rather than hiding them, and the '
                  'applied filters are echoed so a client can tell an empty result from an '
                  'over-narrow one. Creation is audited, which is worth stating because '
                  'the *edit* already was: a holiday could be added to the company '
                  'calendar with no record of it, then edited with one.'),
    'FR-HOL-02': ('M', 'R', 'IMPLEMENTED',
                  ('/api/holidays', '/api/holidays/<int:hid>',
                   '/api/holidays/copy-year', '/api/holidays/export',
                   '/api/holidays/import', '/api/holidays/ical'),
                  'Full CRUD for HR/Admin (the update did not exist) plus copy year-to-year, '
                  'CSV import/export and an iCal feed. The duplicate rule is a unique '
                  'constraint, not a handler check, on (name, holiday_date, '
                  "COALESCE(location, '')) - a plain UNIQUE would accept duplicate "
                  'org-wide holidays, because NULL is distinct from NULL in SQL. Feb-29 on '
                  'copy is skipped and named in the response rather than shifted onto '
                  'another day (holiday_calendar.copy_plan). The iCal feed uses '
                  'DTSTART;VALUE=DATE and folds every content line to the 75-octet RFC 5545 '
                  'limit. Delete is refused while opt-ins reference the holiday. Import '
                  'reports per-row reasons and converges on a re-run.'),
    'FR-HOL-03': ('M', 'R', 'IMPLEMENTED',
                  ('/api/holidays/<int:hid>/opt-in', '/api/holidays/opt-ins/mine',
                   '/api/holidays/opt-ins', '/api/holidays/opt-ins/<int:oid>/approve',
                   '/api/holidays/opt-ins/<int:oid>/cancel'),
                  'The table existed and nothing wrote to it, which made FR-JOB-01 wrong '
                  'rather than merely incomplete: an Optional holiday is an attendance '
                  'holiday only for an employee with an Approved opt-in, no employee could '
                  'ever obtain one, and the nightly finalisation recorded the seeded Diwali '
                  'as Weekly-off. An employee requests, HR approves or rejects from a queue, '
                  'and the owner may withdraw; all of it is holidays_optin.check_request / '
                  'check_cancel / check_review. Only Optional holidays can be opted into, a '
                  'passed holiday cannot (its attendance is finalised), and the review is a '
                  'conditional write so two reviewers give one winner and one 409. '
                  '"One active opt-in per employee per holiday" is a partial unique index '
                  '(Alembic 0006) because a withdrawn or rejected request must not stop the '
                  'employee asking again - the baseline uq_optin was a plain UNIQUE, which '
                  'made opt-out irreversible, and the compatibility schema enforces the same '
                  'definition in a conditional INSERT because DuckDB has no partial index.'),

    # ── FR-JOB: scheduled work ──────────────────────────────────────────
    'FR-JOB-01': ('M', 'N', 'IMPLEMENTED', (),
                  'Nightly finalisation classifies every active employee in the required '
                  'priority order, groups per shift date so night shifts finalize '
                  'correctly, replaces the target date transactionally, and recomputes '
                  'on regularization approval.'),
    'FR-JOB-02': ('H', 'R', 'IMPLEMENTED', ('/api/health',),
                  'Hourly: purge expired reset tokens; close orphaned breaks '
                  '(FR-AUTH-14). Both now run in the same job the SRS describes, so this '
                  'row and FR-AUTH-14 were the same unimplemented requirement counted '
                  'twice. The job runs inside an application context - a scheduler '
                  'thread has none, and audit_log degrades for *request* metadata only, '
                  'so without it the audit rows raised, were swallowed by audit_log own '
                  'except, and silently did not exist: the third instance of that shape '
                  'in this codebase.'),
    'FR-JOB-03': ('S', 'R', 'NOT_STARTED', (),
                  'No quarterly job opens the next performance review cycle.'),
    'FR-JOB-04': ('M', 'N', 'IMPLEMENTED', ('/api/admin/offboarding/revoke',),
                  'The nightly job closes sessions, clears permissions, disables login and '
                  'marks the employee Inactive on their last working day.'),
    'FR-JOB-05': ('M', 'N', 'IMPLEMENTED', ('/api/health',),
                  'Redis lease election (scheduler_leader.py): one key, SET NX with a TTL, '
                  'renewed at a third of the TTL by a scheduler job that shuts the scheduler '
                  'down if the lease is lost. Renewal is fenced by token via a Lua '
                  'compare-then-extend, so a stale leader cannot resurrect its term and two '
                  'pods cannot both believe they are leader. The previous heuristic - start in '
                  'the gunicorn master - was correct for one instance and silently wrong for '
                  'several, because every pod has its own gunicorn master, so an N-pod '
                  'deployment ran every cron job N times. Duplication was mostly absorbed by '
                  'idempotency built for other reasons (accrual grants, outbox claims), which '
                  'is why it went unnoticed. A configured-but-unreachable Redis refuses to '
                  'start the scheduler rather than running every job unowned; no Redis at all '
                  'falls back to the single-process heuristic and logs the multi-pod '
                  'restriction. The SRS chaos test is implemented literally: three competing '
                  'OS processes race for the lease and exactly one wins, with three distinct '
                  'identities. A wiring defect found while verifying the FR-ATT-05 slice: the '
                  'renewal job was installed unconditionally after should_start_scheduler() - '
                  'but on the no-Redis fallback renew() correctly returns False (no lease '
                  'store), so the fallback scheduler shut itself down on its first ~20 s tick '
                  'and the clause above was untrue in effect while its test passed. The boot '
                  'block now installs renewal only under scheduler_leader.renewal_required(); '
                  'a truth-table test and an AST wiring test hold it.'),

    # ── FR-ANL / FR-RPT: analytics and reports ──────────────────────────
    'FR-ANL-01': ('M', 'C', 'PARTIAL', ('/api/analytics/headcount', '/api/analytics/leave-trends',
                                          '/api/analytics/expense-summary', '/api/analytics/performance-summary'),
                  'All four metrics ship and match the v1.0 figures. They are computed '
                  'live per request, not served from materialized views refreshed on a '
                  'schedule.'),
    'FR-ANL-02': ('S', 'C', 'PARTIAL', ('/api/analytics/attrition-risk',),
                  'The four factors are all present. The weights are hard-coded rather '
                  'than configuration (FR-ANL-04), and the score is not versioned.'),
    'FR-ANL-04': ('S', 'C', 'NOT_STARTED', ('/api/analytics/attrition-risk',),
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
    'FR-AUD-01': ('M', 'C', 'PARTIAL', ('/api/audit-log',),
                  'actor/action/entity/entity_id/before/after/ip/request_id/created_at, '
                  'written from a scheduler thread without raising (which it used to, and its '
                  'own except swallowed it, so those rows never existed), and the log is never '
                  'purged by the application. Every mutating handler now writes an audit row '
                  '**except one deliberate exemption**, so the SRS "every mutating action" holds '
                  'in substance; the row stays PARTIAL only because the SRS also asks for the '
                  'row to be written *via the transactional outbox* (CC-09) and audit_log() '
                  'writes straight to the table. The exemption is mark_notifications_read - a '
                  'read receipt on the caller own notifications, where a row per click would be '
                  'noise that makes the real entries harder to find. Getting here took three '
                  'grouped passes and fixed a great deal on the way: regularization '
                  'approve/reject returned 200 whatever happened (including {"message": '
                  '"Rejected"} for a request still Approved) and audited nothing; document '
                  '*deletion* wrote no row while document *download* did, which is backwards '
                  'since deleting removes the row and the file; deleting a dependent erased '
                  'policy-classified PII with no trail; the break lifecycle claimed auditing '
                  'that did not exist, an unconditional approve, and an admin dispose with '
                  'neither a reason nor a row; returning an asset updated unconditionally and '
                  'answered 200 whether or not it returned anything. Two of those were the '
                  'same always-200 lie in unrelated routes. A ratchet test holds the position: '
                  'it fails if a new mutating route skips the audit log, and fails if the '
                  'exemption is removed or a fixed handler left behind on the list. A second '
                  'test reads db/postgres_schema.sql and fails if a route insert stops matching '
                  'the canonical columns - three bare INSERT INTO <table> VALUES (...) were '
                  'fixed on the way.'),
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
