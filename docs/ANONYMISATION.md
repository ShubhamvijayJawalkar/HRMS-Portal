# Two-person anonymisation — design for review (FR-USR)

**Status:** proposal. Nothing is implemented yet. This note exists so the
irreversible parts are decided by a human before any code is written.

## 1. The problem

`archive_user` stops someone using their account and keeps their records. That
is *not* anonymisation. After an archive the row still holds a name, an email, a
phone, an address, a date of birth, an emergency contact and their dependents,
and the audit log holds the old values in its `before`/`after` JSON.

Under GDPR-style rules a person who has asked to be forgotten still has
statutory records we are required to keep (SRS §7.4 marks `leave_requests`,
`payroll_runs`, `payroll_items`, `payroll_approvals`, `salary_structures`,
`expense_claims`, `breaks`, `regularization_requests` and `attendance_days` as
**statutory-7y**). So the answer cannot be "delete the person" — it has to be
"make the person unidentifiable while keeping the money and the days".

## 2. What would change, field by field

Three categories, and the distinction is the whole design:

| Category | Fields | Treatment |
|---|---|---|
| **Erase** (direct identifiers) | `name`, `email`, `phone`, `address`, `emergency_contact_name`, `emergency_contact_phone` | overwritten with a fixed placeholder, original never stored anywhere |
| **Pseudonymise** (join keys) | `emp_id`, `password` | `emp_id` → `ANON-<stable hash>`; password set to a random unusable value |
| **Keep** (the statutory record) | `emp_id` (new value), `department`, `designation`, `grade`, `date_of_joining`, all payroll/leave/attendance rows, all monetary amounts | untouched |

Notes on the tricky ones:

- **`emp_id` must stay a stable pseudonymous value, not a random one.** Payroll
  items, attendance days and leave requests join on it, and those rows are kept
  for seven years. If the pseudonym were random per run, two anonymisations
  could not be told apart and a report could not be reproduced. A salted hash of
  the original id (salt stored once, in config) keeps the join intact and makes
  the mapping unrecoverable from the database alone.
- **A back-reference must not be created.** Do not keep the old `emp_id` in a
  "was EMP123" column — that would defeat the whole exercise. If HR needs to
  prove a specific record belonged to a specific person, that proof has to come
  from the request ticket, not from the database.
- **`date_of_birth` is a quasi-identifier.** On its own it identifies people
  when combined with department and grade. It should be in the *erase* category
  unless your retention rules require it; `date_of_joining` is the
  employment-relevant one and stays.
- **Dependents and documents are separate tables** and are in scope: a
  dependent's name and date of birth are another person's personal data.
  Documents are binary artefacts (offer letters, ID proofs) — see §5.
- **`manager_emp_id` and every `approved_by` / `reviewed_by` / `granted_by` /
  `assigned_to` value** point at a person. If the *approver* is anonymised
  these must be rewritten to the pseudonym too, or they leak the relationship.

## 3. The two-person rule

The requirement is that no single actor can erase someone.

Proposed rules, each of which needs confirming:

1. **Two distinct approvers.** The requester proposes; a second person with the
   `policy_admin` capability confirms. The two must be different `emp_id`s. The
   requester may be an Admin, but an Admin cannot confirm their own request.
2. **Three states, not two.** `proposed → confirmed → applied`. Until
   `confirmed` nothing has changed. `applied` is performed by the system, never
   by a person, so the operation is reproducible and auditable.
3. **Anonymising an approver is refused** while pending requests exist, to
   avoid a pending confirmation becoming unconfirmable.
4. **Reversible only in one direction.** There is no "un-anonymise". If a
   request was made in error, the remedy is restoring from a backup taken
   before the operation, under a separate, audited decision.

## 4. What the audit trail records — and what it must not

The audit row is the *only* durable record that erasure happened, so it has to
be meaningful; it must also not defeat the erasure.

- **Record:** who proposed, who confirmed, when, which categories were applied
  (erase / pseudonymise / keep), the counts of affected rows per table, and the
  request reference.
- **Must not record:** the original name, email, phone, address, emergency
  contact, date of birth, the original `emp_id`, or any before/after JSON
  containing them. `audit_log(..., before=..., after=...)` is exactly the
  mechanism that would leak, so this operation writes its audit row with
  `before=None` and a summary payload only.
- **Consequence to accept explicitly:** the `audit_log` rows written *before*
  the anonymisation (every `USER_UPDATE` with a before/after diff, the
  `PII_REVEAL` rows, the import error summaries) still contain the old values.
  Anonymising the user without scrubbing history leaves the personal data
  sitting in the audit table. Either that history is in scope for a scrub, or
  the design must state that audit history is deliberately exempt. **This is
  the single biggest open question in this note** and it needs your answer,
  because scrubbing the audit log is itself an irreversible, statutorily
  sensitive act.

## 5. Related but separate

- **Uploaded documents** (`documents`, `employee_documents`) hold offer letters
  and ID proofs. Deleting the file is a different decision from anonymising the
  row (a pre-hire contract has its own retention expectation). Proposed default:
  out of scope, listed as a follow-up.
- **`monthly_leave_grants`, `notifications`, `tickets`, `expense_claims`** hold
  free-text that may contain personal data (a ticket subject, an expense
  description). Free text cannot be reliably scrubbed, so either it is left as
  is (defensible: it is the operational record) or it needs redaction tooling.
  Proposed default: left as is, called out in the audit row.
- **A dry-run report** should exist before the real thing: `POST …/anonymise
  --dry-run` returning exactly which rows and fields *would* change, with no
  writes. That is the safety net that makes this feature reviewable.

## 6. If you accept this shape, the implementation is

1. `anonymise.py` — the rules above as pure functions, with the field
   categories as data.
2. `anonymisation_requests` table + Alembic revision (the canonical target is
   frozen, so this needs a new revision and a preflight head bump, like
   `0004_import_jobs`).
3. `POST /api/users/<id>/anonymise` (propose, optional `--dry-run`) and
   `POST /api/anonymisation/<id>/confirm`, both under `policy_admin`.
4. A scheduler sweep that retries `confirmed` requests whose application failed,
   the same pattern as the import jobs — so the operation is idempotent and
   survives a crash halfway.
5. An admin view listing requests and their state.
6. Tests for: the two-approver rule, the self-confirm refusal, the audit row
   containing no erased value, the pseudonym being stable, the dry-run writing
   nothing, and a re-run after partial application converging.

## 7. Questions for you

1. **Audit history** (§4): scrub the pre-existing audit rows, or exempt them?
2. **`date_of_birth`**: erase, or keep as employment-relevant?
3. **Dependents and free text** (§5): in scope or deferred?
4. **Is a stable pseudonym acceptable**, given it is still a join key across
   seven years of payroll and attendance?
5. **Who are the two approvers in practice** — is `policy_admin` the right
   capability, or does this need a dedicated one?
