# SRS v2.0 requirement traceability

_Generated 2026-09-30 by `scripts/generate_traceability.py` from `traceability.py`.
**Do not hand-edit this file** — edit the data and re-run the script._

Every FR-* requirement in `HRMS_SRS_v2.0.pdf` is listed with the routes that
implement it and an honest verdict. The verdicts are deliberately unforgiving.

| Verdict | Meaning |
|---|---|
| `IMPLEMENTED` | the behaviour is enforced server-side **and** covered by a test |
| `PARTIAL` | a usable subset ships; the note names exactly what is missing |
| `NOT_STARTED` | nothing enforces it, even where a table or column exists |
| `RETIRED` | folded into another requirement by Appendix A |

A requirement is only `IMPLEMENTED` where the code *enforces* it, not where a
handler merely exists. Four tests in `tests/test_app.py` keep this honest: the
matrix must cover exactly the SRS id set, every route it names must exist in the
live `url_map`, an `IMPLEMENTED` row with no route must explain itself, and a
`PARTIAL` row must say what is missing. A renamed route turns the build red
rather than quietly invalidating this document.

## Summary

| Verdict | Count | Share |
|---|---:|---:|
| `IMPLEMENTED` | 53 | 51% |
| `PARTIAL` | 39 | 38% |
| `NOT_STARTED` | 11 | 11% |
| `RETIRED` | 1 | 1% |
| **total** | **104** | |

### IMPLEMENTED (53)

| ID | Pri | Δ | Routes | Notes |
|---|---|:---:|---|---|
| `FR-AST-01` | M | R | `/api/assets`<br>`/api/my-assets`<br>`/api/assets/<int:aid>/return` | Issue/return with return_date set on return, own-assets view, and outstanding counts feeding the offboarding clearance gate. |
| `FR-ATS-01` | M | C | `/api/candidates/<int:cid>/status`<br>`/api/pipeline` | Applied -> Screened -> Interviewed -> Offered -> Hired, with Rejected and Withdrawn reachable before Hired; direct Hired is 409; the pipeline aggregates counts per stage. |
| `FR-ATS-02` | M | C | `/api/jobs`<br>`/api/candidates`<br>`/api/jobs/<int:jid>/close`<br>`/api/candidates/<int:cid>` | Jobs and candidates CRUD for HR/Admin; status changes go through the same guarded stage machine. |
| `FR-ATS-03` | H | C | `/api/offers/<int:oid>/accept` | The only path to Hired is offer acceptance, which atomically creates the pre-hire user, salary structure, onboarding workflow and checklist. |
| `FR-ATS-04` | H | C | `/api/offers` | Offers require a 100% salary split, basic_pct/hra_pct/allowances_pct are stored, and a unique constraint keeps two active checklists per employee from existing. |
| `FR-ATT-03` | M | R | `/api/end-break/<int:break_id>` | Ownership checked against the session employee, duration computed from the timestamps, action audited. |
| `FR-ATT-04` | M | R | `/api/user-breaks` | Own breaks only, UNIONed with any Active break from another date so a forgotten break-end is still visible. |
| `FR-ATT-08` | M | R | `/api/login-hours` | First login to last logout, scoped to a shift date. |
| `FR-ATT-11` | H | C | `/api/user/calendar` | Sessions, breaks, day-expanded leaves, holidays and the finalised FR-JOB-01 status per day. |
| `FR-ATT-12` | L | R | `/api/live-monitoring` | Active-break monitoring. |
| `FR-ATT-13` | L | R | `/api/break-summary` | Per-employee counts and minutes. |
| `FR-ATT-14` | L | R | `/api/disposed-breaks` | Last hour. |
| `FR-ATT-16` | H | R | `/api/admin/breaks`<br>`/api/admin/dispose-break/<int:break_id>` | One console call for active/disposed/summary; dispose ends any Active break with an audited reason. |
| `FR-ATT-17` | M | C | — | shift_assignments, effective-dated; get_shift/set_shift resolve the model per backend+schema and init_db no longer re-adds the removed columns to v2.0 users. |
| `FR-AUD-01` | H | C | `/api/audit-log` | actor/action/entity/entity_id/before/after/ip/request_id/created_at on every mutating action, including from a scheduler thread (which used to raise and be swallowed, so those rows never existed). |
| `FR-AUTH-02` | M | N | `/login` | One response for every failure mode, so an attacker cannot tell an unknown account from a wrong password. |
| `FR-AUTH-05` | M | R | `/logout` | Logout closes the active user_sessions row with total_hours = logout - login and audits the action. |
| `FR-AUTH-06` | M | R | `/` | Redirects by session state. |
| `FR-AUTH-07` | M | N | `/dashboard` | Admin vs self dashboard chosen by policy.sees_admin_surface(); unauthenticated gets 302 for a page and 401 for JSON. |
| `FR-AUTH-08` | M | N | `/api/forgot-password` | Always 202 with the same message, so the endpoint cannot be used to enumerate accounts. |
| `FR-AUTH-10` | M | N | `/api/users`<br>`/api/change-password`<br>`/api/reset-password` | passwords.py: a 10-character minimum (Appendix A-01 calls 6 a defect) and a breach-corpus check, enforced at every point a password is set. The corpus is a bundled offline list, extended optionally by the HIBP k-anonymity range API; leet variants and known-password-plus-suffix are caught too. Deliberately no complexity rules and no expiry, per NIST SP 800-63B, and a test parses the module to keep them out. The old shared default of 'pass123' is gone: a user created without a password gets a generated compliant one, returned once. The rejection message is generic, so it is not an oracle for confirming a guess. |
| `FR-AUTH-12` | M | C | `/api/csrf-token` | Double-submit on every mutating /api request; a fetch wrapper attaches the header and native forms carry the hidden field. Asserted end to end with server-side sessions too. |
| `FR-DOC-01` | M | R | `/api/documents` | Scoped to the owner unless HR/Admin. |
| `FR-EXP-03` | M | C | `/api/expenses`<br>`/api/expenses/<int:eid>/status` | Strict transition table in expenses.py: Pending -> Approved/Rejected by the owner's manager or HR/Admin, Approved -> Paid by Finance/Admin only (Appendix A-11), Rejected and Paid final. Self-approval blocked, a rejection reason required, every write a conditional UPDATE with a before/after audit, and the list reports the actions the caller may actually take. Finance holds the expenses module because the SRS names it for Paid; the list stays scoped to own + reports, so that grants reach rather than company-wide visibility. |
| `FR-JOB-01` | H | C | — | Nightly finalisation classifies every active employee in the required priority order, groups per shift date so night shifts finalize correctly, replaces the target date transactionally, and recomputes on regularization approval. |
| `FR-JOB-04` | H | C | `/api/admin/offboarding/revoke` | The nightly job closes sessions, clears permissions, disables login and marks the employee Inactive on their last working day. |
| `FR-LEA-04` | H | C | `/api/leaves/<int:leave_id>/approve` | Not the applicant, conditional update, used_days incremented and the reservation consumed. |
| `FR-LEA-05` | M | C | `/api/leaves/<int:leave_id>/reject`<br>`/api/leaves/<int:leave_id>/cancel` | Reject is conditional and releases the reservation. Cancel now exists: Pending only, or Approved before it starts, by owner or admin (leave_policy.check_cancel), and the ledger reversal differs by state - a Pending request releases the reservation, an Approved one takes the days back out of used_days, which is why the decision and the reversal are one function (leave_policy.cancel). Cancelling twice or a settled request is a 409, a started leave is a 409, and the action taken is in the response and the audit row. |
| `FR-LEA-06` | H | C | `/api/leave-balance` | total/used/reserved/remaining derived per type per year, with the source reported so a number can be traced to a policy or a default. |
| `FR-LEA-08` | H | C | `/api/users/<emp_id>/leave-policy`<br>`/api/accrual/run` | Effective-dated per employee, entitlement derived and accrued month by month from the rate, capped by the carry-forward cap, the ledger posted by cron, on demand, or from the admin UI. |
| `FR-NOT-02` | M | R | `/api/notifications/read` | Sets is_read and keeps the row, so a read notification does not vanish. |
| `FR-OFF-01` | H | C | `/api/resignations`<br>`/api/resignations/<int:resignation_id>/acknowledge` | Resignation is a first-class record that triggers the workflow; it is never implied by a status change. |
| `FR-OFF-02` | M | C | `/api/offboarding-tasks`<br>`/api/exit-interviews` | Per-stage tasks with owners, and exit-interview scheduling. |
| `FR-OFF-03` | H | C | `/api/offboarding-workflows/<int:offboard_id>/stages/<int:stage>/complete`<br>`/api/offboarding-workflows/<int:offboard_id>/revoke-access` | Each stage is a guarded conditional update; stages 2 and 3 run in parallel; Finance prepares and approves the settlement separately; the nightly job revokes access on the LWD. |
| `FR-ONB-01` | H | C | `/api/onboarding-workflows` | All five steps with their guards, task owners and per-step timestamps. |
| `FR-ONB-02` | H | C | `/api/onboarding-workflows`<br>`/api/onboarding-workflows/<int:workflow_id>` | HR/Admin see everyone; the candidate view is token-scoped through /api/preboarding/<token>. |
| `FR-ONB-03` | H | C | `/api/offers/<int:oid>/accept` | The checklist is created by the offer-acceptance transaction, not by a separate call a caller could forget. |
| `FR-ONB-05` | H | C | `/api/onboarding-workflows/<int:workflow_id>/steps/<int:step>/complete` | Advancement is guard-based; HR only confirms the physical/logistics steps. |
| `FR-ONB-06` | L | C | `/api/onboarding-workflows` | Own workflow, or the HR/Admin list with days-in-step. |
| `FR-PERF-01` | M | R | `/api/goals`<br>`/api/goals/<int:gid>`<br>`/api/goals/<int:gid>/rate` | CRUD plus a 1-5 rating that transitions the goal to Completed, all in goals.py. POST /api/goals had never worked (a bare VALUES with ten placeholders against a nine-column table, so every create was a 500) and now uses an explicit column list and takes emp_id from the session. PUT /api/goals/<id> was @login_required with no ownership check, so any authenticated user could rewrite any goal by guessing a sequential id; it is now owner/manager/HR and cannot set status or rating, which is how the rating flow used to be skipped. Rating is by the reporting manager or HR/Admin and never the owner (the SRS calls that out), through a new reporting-line gate because @admin_required excluded the role the requirement names. Completed is terminal and the write is conditional. |
| `FR-PERF-02` | M | R | `/api/performance-reviews`<br>`/api/performance-reviews/<int:rid>/submit`<br>`/api/feedback-360` | reviews.py. Cycle create/list stays HR/Admin-gated; a self-review is refused at creation (409) because a review whose subject is also its reviewer has nobody to sign it, and both employees must exist. Submit requires the assigned reviewer and nothing else - HR and Admin get no bypass, deliberately, because the rule exists to stop a review being signed by somebody who did not write it (Appendix A-18). The rating is bounded 1-5, the write is conditional on Draft so a signed review is final, and the before/after is audited and the subject notified. 360° feedback refuses self-feedback and takes a fixed category set. |
| `FR-REG-03` | H | C | `/api/regularization/<int:rid>/approve` | Approval writes the corrected time and triggers the FR-JOB-01 recompute for that day. |
| `FR-TKT-03` | M | C | `/api/tickets`<br>`/api/tickets/<int:tid>`<br>`/api/tickets/<int:tid>/comment`<br>`/api/tickets/<int:tid>/status` | One visibility rule (tickets.can_view) now serves the list, the detail view, commenting and status changes, so the "defence in depth" the requirement asks for is real rather than half of it. The comment route had only an existence check, so a user refused a ticket with 403 could still write into its history; the status route had no check at all. Comments bump updated_at. |
| `FR-TKT-04` | M | C | `/api/tickets/<int:tid>/status`<br>`/api/tickets/<int:tid>/comment`<br>`/api/tickets/<int:tid>/assign` | tickets.py holds the chain Open -> In Progress -> Resolved -> Closed, enforced strictly and with a conditional write, so Open -> Closed is a 409 naming what is allowed. Reopened is a real status: a Closed ticket reopens when its *reporter* comments within seven days of closing, and nobody else can reopen it that way; an older closure stays closed. Resolved -> In Progress and Reopened -> In Progress are the ways back. Assignment is audited, which FR-TKT-04 asks for and nothing did; a ghost assignee is a 404 and the assignee is notified. |
| `FR-USR-01` | M | R | `/api/users` | page/per_page parsed as ints, per_page capped at 200, sort on an allow-list; invalid input is 400, never a silent default. |
| `FR-USR-03` | M | R | `/api/users/<emp_id>` | Field allow-list, true partial update, role changes audited with a before/after diff, email re-verifies uniqueness. |
| `FR-USR-04` | H | C | `/api/users/<emp_id>/block`<br>`/api/users/<emp_id>/unblock` | Blocking closes the DB sessions and revokes the Redis sessions; asserted in tests/test_redis_sessions.py. |
| `FR-USR-05` | H | R | `/api/users/<emp_id>/archive`<br>`/api/users/<emp_id>/restore` | Status-based archive/restore, self-action 409, payroll and audit records retained. The legacy DELETE route is archive-compatible. |
| `FR-USR-06` | H | C | `/api/users/<emp_id>/anonymise` | Purge replaced by anonymisation: a dry-run plan, a required salt, and an audit history scrubbed by value substitution. Trade-offs (emp_id kept, free text left) are recorded in docs/ANONYMISATION.md §8. |
| `FR-USR-06a` | H | N | `/api/anonymisation/<int:request_id>/confirm` | Two-person state machine proposed -> confirmed -> applied; the confirmer must be a different user, and only an archived account qualifies. |
| `FR-USR-09` | H | C | `/api/users/<emp_id>/permissions` | Full replace of the override set, audited with a real before/after diff, anti-lockout guard, module mapped into policy.PERMISSION_MODULES. |
| `FR-USR-11` | M | C | `/api/dependents`<br>`/api/dependents/<int:did>` | emp_id always from the session, never the payload; delete is scoped by emp_id as well. |
| `FR-USR-15` | M | C | — | policy.navigation_for() is the same predicate the route gates use, injected into every template; five tests assert the navbar and the gate of the linked route never disagree. |

### PARTIAL (39)

| ID | Pri | Δ | Routes | Notes |
|---|---|:---:|---|---|
| `FR-ANL-01` | M | C | `/api/analytics/headcount`<br>`/api/analytics/leave-trends`<br>`/api/analytics/expense-summary`<br>`/api/analytics/performance-summary` | All four metrics ship and match the v1.0 figures. They are computed live per request, not served from materialized views refreshed on a schedule. |
| `FR-ANL-02` | M | C | `/api/analytics/attrition-risk` | The four factors are all present. The weights are hard-coded rather than configuration (FR-ANL-04), and the score is not versioned. |
| `FR-ATT-01` | M | C | `/api/break-types` | The three seeded types with their limits ship and Lunch requires approval. The read endpoint exposes no per-location configuration and there is no CRUD route, so the types are configurable only by editing rows. |
| `FR-ATT-02` | M | C | `/api/start-break` | allow_breaks and the daily quota are enforced, the Lunch approval is required, an existing Active break is auto-ended, and the write is idempotent. The "one transaction" and "partial unique index" parts are not met on the compatibility schema: the auto-end plus insert is two statements, and only v2.0 carries the index. |
| `FR-ATT-05` | H | C | `/api/break-approvals` | Lunch only, one Pending per employee enforced in the handler. The partial unique index that would enforce it under concurrency exists only in the v2.0 schema. |
| `FR-ATT-06` | H | C | `/api/break-approvals/<int:aid>/approve`<br>`/api/break-approvals/<int:aid>/reject` | Manager/HR/Admin may approve, the update is conditional, the action is audited and the employee is notified. Delegated approvers (FR-LEA-08a) are not consulted, because delegation is unimplemented. |
| `FR-ATT-07` | L | R | `/api/break-types`<br>`/api/user-breaks` | Minutes used and the approval flag are exposed. The per-type summary is assembled by the client from two calls rather than served as one projection. |
| `FR-ATT-09` | H | C | `/api/user/shift-summary` | last_logout - first_login, productive_hours and efficiency all ship. The 25% cap on an open shift and the estimated flag are missing, so a forgotten logout inflates the figure. |
| `FR-ATT-15` | M | C | `/api/dashboard-stats` | The five keys are stable. They are recomputed per request, with no Redis 15 s cache and no worker refresh, so the cost grows with the employee count. |
| `FR-AUTH-01` | M | C | `/login` | Employee code is trimmed and matched case-insensitively, with a rate limit (LOGIN_RATE_LIMIT, default 20/min). The limit is per remote address rather than per account *and* per IP. |
| `FR-AUTH-04` | M | C | `/logout` | Server-side Redis sessions (opaque cookie, 8 h TTL), HttpOnly, SameSite=Lax, Secure in production; logout deletes the server copy. There is no 24 h *absolute* timeout distinct from the 8 h idle one. |
| `FR-AUTH-09` | M | N | `/api/reset-password` | Single-use token, invalidated after a successful reset, purged hourly. Missing: the token is stored unhashed, and the expiry is 1 h where the SRS asks for 24 h. |
| `FR-AUTH-13` | H | N | `/api/credentials` | Restricted by the permission policy and no longer returns passwords or hashes. Missing the 5-minute re-authentication and the audit row for a credential read. |
| `FR-AUTH-14` | H | N | — | An hourly job purges expired reset and idempotency tokens. It does not auto-close breaks Active for more than 12 hours, so a forgotten break-end leaves a row Active indefinitely. |
| `FR-DOC-02` | H | C | `/api/upload` | Multipart upload, a size cap and an extension allow-list. The MIME type is taken from the filename extension rather than sniffed from the content, and there is no malware scan. A renamed .exe passes. |
| `FR-DOC-03` | M | C | `/api/documents/<int:did>/download`<br>`/api/documents/<int:did>` | Owner or HR/Admin for download, Admin-only delete (the v1.0 hole is closed), and the download now writes a DOCUMENT_DOWNLOAD audit row - a document read that leaves no trail is the one that matters after an incident. Still missing: a presigned URL rather than a direct file response, so the object store is never reachable directly. |
| `FR-EXP-01` | M | R | `/api/expense-categories` | Six seeded categories and a read endpoint. No CRUD, so the set is configurable only by editing rows. |
| `FR-EXP-02` | M | C | `/api/expenses` | emp_id is taken from the session and a body override is rejected with a 400, closing the v1.0 impersonation hole; the category must exist and the amount must be positive. Missing: receipt validation on upload. |
| `FR-HOL-01` | M | C | `/api/holidays` | National/Optional types, per-location applicability and search. The location field is stored but not filtered on. |
| `FR-HOL-02` | H | C | `/api/holidays`<br>`/api/holidays/<int:hid>` | CRUD for HR/Admin with a duplicate (name, date) check. The check is in the handler, not a unique constraint, so it races under concurrency. No year-to-year copy, so Feb-29 handling is absent. |
| `FR-HOL-03` | H | C | — | holiday_optins exists in the canonical schema with a unique index. No route writes an opt-in and there is no HR approval queue, so Optional holidays cannot actually be opted into. |
| `FR-JOB-02` | H | C | — | Hourly purge of expired reset and idempotency tokens. The orphaned-break auto-close is not implemented (see FR-AUTH-14). |
| `FR-LEA-01` | M | R | `/api/leaves` | Filters and the scope split ship. Missing: delegated-manager visibility. |
| `FR-LEA-02` | M | C | `/api/leaves` | Dates are swapped if reversed and the session is recorded. Working-day deduction ignores holidays — FR-LEA-09 asks for one shared function and there is none. |
| `FR-LEA-03` | M | N | `/api/leaves/export` | Excel export ships, synchronously. The async variant for large ranges is not implemented. |
| `FR-NOT-01` | M | C | `/api/notifications` | Last-50 list with an unread count. Delivery is a direct SMTP call on the request thread rather than an outbox enqueue, so a slow provider can block the request that triggered it. |
| `FR-ONB-04` | H | C | `/api/preboarding/<token>/documents/<doc_type>`<br>`/api/onboarding-checklist/<int:item_id>/review` | Upload, review, and mandatory rejection notes all ship. The shared pipeline only checks the file extension (FR-DOC-02 is partial). |
| `FR-REG-01` | M | R | `/api/regularization` | Filters and the company-wide/self split ship, the split decided by policy.can_view_all. Delegated reports are not included. |
| `FR-REG-02` | M | C | `/api/regularization` | Corrected times are captured and future dates are refused. The "a specific corrected time is required" rule is not enforced: a reason-only request is accepted. |
| `FR-RPT-01` | M | C | `/api/reports` | The self-service view substitutes the caller scope. It ignores a supplied department only in some paths; the admin/HR view is gated by the policy. |
| `FR-RPT-02` | M | C | `/api/reports/export`<br>`/api/reports/pdf`<br>`/api/reports/department-summary` | Excel and PDF export plus the department summary, all synchronous. No async job for ranges beyond a month or 200 employees. |
| `FR-TKT-01` | M | C | `/api/tickets` | HR/IT queues and a priority on every ticket. The SLA target per priority is not configurable and there are no subcategories. |
| `FR-TKT-02` | M | C | `/api/tickets` | Create and list with one shared visibility rule in tickets.can_view: owner, assignee, or a role policy.can_view_all admits. The reporter is always the session user, so a ticket cannot be filed in a colleague's name. Still missing: the SRS also lists "matching department" scoping, and Super Admin is not notified of a new ticket. |
| `FR-USR-02` | M | C | `/api/users` | emp_id/email/role/department validated, case-insensitive email uniqueness, default status Active + allow_login. Missing: the welcome email with a 24 h single-use reset token, and balances seeded from the grade/location policy rather than the default matrix (leave_policy derives on read instead). |
| `FR-USR-08` | L | R | `/api/users/<emp_id>` | Missing the meta endpoint, the async CSV export and the last-50 sessions history. The list endpoint carries the pagination meta. |
| `FR-USR-10` | H | C | `/api/users/import`<br>`/api/users/import/<int:job_id>` | CSV via pandas as a background job (202 + job id), per-row validation, {imported, skipped, errors, job_id}. The progress endpoint is /api/users/import/<job_id>, not the /api/imports/... path the SRS names, and errors are capped at 20 rather than 50. |
| `FR-USR-12` | L | C | `/api/upload` | Employee uploads go through the same route as admin uploads, so one validation pipeline exists, and it does sniff the content. It inherits the FR-DOC-02 gaps: no per-category size cap and no real scanner. |
| `FR-USR-13` | M | C | `/api/profile` | Profile read/write is self-scoped and routed through the PII helper. The field allow-list is not a declared strict subset: an employee cannot change their own role, but the boundary is implied by the handler rather than asserted by a test. |
| `FR-USR-14` | M | R | `/api/change-password` | The current password is required. The session token is not re-issued on change, so an existing cookie keeps working. |

### NOT_STARTED (11)

| ID | Pri | Δ | Routes | Notes |
|---|---|:---:|---|---|
| `FR-ANL-04` | M | C | `/api/analytics/attrition-risk` | The weights (0.4, 1.5, 0.8, 3) are literals in the handler. Changing them needs a code change and redeploy. |
| `FR-AUTH-03` | M | N | — | No consecutive-failure counter and no timed account lock. Only the IP rate limit stands between an attacker and a password spray. |
| `FR-AUTH-11` | M | N | — | No MFA. mfa_credentials exists in the canonical schema with an encrypted secret, but nothing reads or writes it: no enrolment, no challenge, no gate. This is the largest single gap found by the traceability pass. |
| `FR-JOB-03` | S | R | — | No quarterly job opens the next performance review cycle. |
| `FR-JOB-05` | H | C | — | No leader election. The scheduler starts in the gunicorn master, which is the usual single-instance answer, but a multi-pod deployment would run every cron job once per pod. |
| `FR-LEA-07` | H | C | — | No manual grant route and no LEAVE_GRANT audit action. An admin cannot add days to an employee; only the policy and the accrual job can. |
| `FR-LEA-08a` | M | N | — | approval_delegations exists in the canonical schema with a no-overlap exclusion constraint, but no route reads or writes it. A manager going on leave has no way to delegate. |
| `FR-LEA-09` | M | C | — | There is no single working-day/holiday-deduction function. Leave day counting, payroll LOP and the reports each approximate it differently, which is the inconsistency the requirement exists to remove. |
| `FR-NOT-03` | S | R | — | No per-category {in_app, email} preferences. category exists on the notification row, but the preference model and its defaults do not. |
| `FR-REG-04` | M | N | — | No regularization Excel export. |
| `FR-USR-07` | H | R | — | No POST /api/users/bulk. The block/archive routes take a single employee; there is no batch endpoint with per-row results. |

### RETIRED (1)

| ID | Pri | Δ | Routes | Notes |
|---|---|:---:|---|---|
| `FR-ATT-10` | — | — | — | Folded into FR-ATT-09 by Appendix A. |

## What the gaps have in common

The `NOT_STARTED` rows are not random; they cluster in four places.

**Authentication hardening** — the session, hashing, CSRF and rate-limit work of
Phase 3a is done, but the account-level defences are not. `FR-AUTH-03` (no
consecutive-failure lock, so only the IP rate limit stops a password spray),
`FR-AUTH-10` (no minimum length, no breached-password check) and `FR-AUTH-11`
(no MFA at all) are all absent. MFA is the largest single gap this pass found:
`mfa_credentials` sits in the canonical schema with an encrypted-secret column
and **no code anywhere that reads or writes it** — no enrolment, no challenge,
no gate.

**Schema without routes** — `approval_delegations` (FR-LEA-08a),
`notification_preferences` (FR-NOT-03) and `holiday_optins` (FR-HOL-03) have
their tables and constraints in the canonical schema and no endpoint. The v2.0
target was designed for capabilities the service layer has not caught up with,
which is exactly the kind of drift a traceability matrix is for: without one,
these read as done.

**Synchronous exports** — `FR-LEA-03`, `FR-REG-04` and `FR-RPT-02` all export
inline, so a wide report blocks the request that asked for it. The background-job
machinery the outbox and the CSV importer already prove is available; nothing
reuses it for exports yet.

**Business rules left to the client** — `FR-EXP-03` has no state machine and no
self-approval block, so an employee can approve their own expense claim.
`FR-PERF-01` lets an employee rate their own goal. These are the class of gap
that looks harmless in a demo and is not.

Two `PARTIAL` rows are security properties rather than features, and are worth
reading before any deployment decision:

* **FR-DOC-02** validates the file **extension**, not the content. There is no
  malware scan. A renamed executable passes the allow-list.
* **FR-DOC-03** serves downloads directly rather than by presigned URL, and does
  not audit the read, so document access leaves no trail.

## Next, if you want the numbers to move

Roughly in order of (risk x effort):

1. **FR-DOC-02 content sniffing** — small, and it closes the "renamed .exe
   passes" hole. No new dependency needed: read the first bytes and check the
   magic number, which is what "sniffed from content" means in practice.
2. **FR-EXP-03 self-approval block** — one ownership check, mirroring what
   `approve_leave` already does. Closes an approval-integrity gap.
3. **FR-AUTH-10 password policy** — a minimum length and a small offline
   breached-password corpus. The schema needs nothing.
4. **FR-AUTH-03 account lockout** — needs a failure counter and a `locked_until`
   column, so a migration.
5. **FR-AUTH-11 MFA** — the largest item: enrolment flow, encrypted secret
   handling, a TOTP challenge on the login route, and recovery codes. The schema
   is already there, which is the only reason it is worth doing.
