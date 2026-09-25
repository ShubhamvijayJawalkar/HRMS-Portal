# HRMS v2.0 — Data Dictionary

_Generated 2026-09-25T09:14:08.929021+00:00 by `scripts/generate_data_dictionary.py` from `localhost:55432/hrms`. **Do not hand-edit** — regenerate (SRS §15)._

Corresponds to SRS v2.0 §7 (Data model & database constraints). 
Retention classes per §7.4: `transactional`, `statutory-7y` (7 years per §11.4), `audit-indefinite-with-review`. PII columns are permission-gated behind `pii_reveal`.

## Tables overview

| Table | Rows | Retention | PII cols |
|-------|-----:|-----------|----------|
| `alembic_version` | 1 | transactional | — |
| `approval_delegations` | 0 | transactional | — |
| `assets` | 2 | transactional | — |
| `attendance_days` | 0 | statutory-7y | — |
| `audit_log` | 2 | audit-indefinite-with-review | — |
| `break_approvals` | 0 | transactional | — |
| `break_types` | 3 | transactional | — |
| `breaks` | 2 | statutory-7y | — |
| `candidates` | 2 | transactional | — |
| `dependents` | 2 | transactional | — |
| `documents` | 2 | transactional | — |
| `employee_documents` | 2 | transactional | — |
| `exit_interviews` | 2 | transactional | — |
| `expense_categories` | 6 | transactional | — |
| `expense_claims` | 2 | statutory-7y | — |
| `feedback_360` | 2 | transactional | — |
| `goals` | 2 | transactional | — |
| `holiday_optins` | 0 | transactional | — |
| `holidays` | 5 | transactional | — |
| `idempotency_keys` | 0 | transactional | — |
| `interviews` | 2 | transactional | — |
| `job_postings` | 2 | transactional | — |
| `leave_balance` | 6 | statutory-7y | — |
| `leave_policy_assignments` | 0 | transactional | — |
| `leave_requests` | 2 | statutory-7y | — |
| `mfa_credentials` | 0 | transactional | — |
| `monthly_leave_grants` | 0 | transactional | — |
| `notifications` | 2 | transactional | — |
| `offboarding_approvals` | 0 | transactional | — |
| `offboarding_settlements` | 0 | transactional | — |
| `offboarding_tasks` | 2 | transactional | — |
| `offboarding_workflow` | 0 | transactional | — |
| `offer_letters` | 2 | transactional | — |
| `onboarding_checklist` | 0 | transactional | — |
| `onboarding_tasks` | 2 | transactional | — |
| `onboarding_workflow` | 0 | transactional | — |
| `outbox_events` | 0 | audit-indefinite-with-review | — |
| `password_reset_tokens` | 2 | transactional | — |
| `payroll_approvals` | 0 | statutory-7y | — |
| `payroll_items` | 2 | statutory-7y | — |
| `payroll_runs` | 2 | statutory-7y | — |
| `performance_reviews` | 2 | transactional | — |
| `regularization_requests` | 2 | statutory-7y | — |
| `resignations` | 0 | transactional | — |
| `salary_structures` | 2 | statutory-7y | — |
| `shift_assignments` | 2 | transactional | — |
| `ticket_comments` | 2 | transactional | — |
| `tickets` | 2 | transactional | — |
| `user_permissions` | 0 | transactional | — |
| `user_sessions` | 2 | transactional | — |
| `users` | 2 | transactional | — |

---

## `alembic_version`

- Retention class: **transactional**
- Primary key: `version_num`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `version_num` | character varying | N | `` |  |

## `approval_delegations`

- Retention class: **transactional**
- Primary key: `delegation_id`
- Foreign keys: `delegate_id` → `users.emp_id`; `delegator_id` → `users.emp_id`
- Unique indexes: none
- Exclusion constraints: `no_overlapping_delegation` (delegator_id, daterange(starts_on, ends_on, '[]'::text))

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `delegation_id` | bigint | N | `` |  |
| `delegator_id` | character varying | N | `` |  |
| `delegate_id` | character varying | N | `` |  |
| `starts_on` | date | N | `` |  |
| `ends_on` | date | N | `` |  |
| `created_at` | timestamp with time zone | N | `now()` |  |

## `assets`

- Retention class: **transactional**
- Primary key: `asset_id`
- Foreign keys: `emp_id` → `users.emp_id`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `asset_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `asset_type` | character varying | N | `` |  |
| `asset_tag` | character varying | Y | `` |  |
| `brand` | character varying | Y | `` |  |
| `model` | character varying | Y | `` |  |
| `serial_number` | character varying | Y | `` |  |
| `issued_date` | date | N | `` |  |
| `return_date` | date | Y | `` |  |
| `status` | character varying | N | `'Issued'::character varying` |  |
| `notes` | character varying | Y | `` |  |

## `attendance_days`

- Retention class: **statutory-7y**
- Primary key: `attendance_id`
- Foreign keys: `emp_id` → `users.emp_id`
- Unique indexes: `uq_attendance_emp_date` (emp_id, attendance_date)
- Exclusion constraints: `uq_attendance_emp_date` ()

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `attendance_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `attendance_date` | date | N | `` |  |
| `status` | character varying | N | `` |  |
| `shift_hours` | numeric | Y | `` |  |
| `source` | character varying | N | `'job'::character varying` |  |
| `version` | integer | N | `1` |  |
| `updated_at` | timestamp with time zone | N | `now()` |  |

## `audit_log`

- Retention class: **audit-indefinite-with-review**
- Primary key: `log_id`
- Foreign keys: `emp_id` → `users.emp_id`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `log_id` | bigint | N | `` |  |
| `emp_id` | character varying | Y | `` |  |
| `actor` | character varying | Y | `` |  |
| `action` | character varying | N | `` |  |
| `entity` | character varying | Y | `` |  |
| `entity_id` | character varying | Y | `` |  |
| `details` | character varying | Y | `` |  |
| `before` | jsonb | Y | `` |  |
| `after` | jsonb | Y | `` |  |
| `ip_address` | character varying | Y | `` |  |
| `request_id` | character varying | Y | `` |  |
| `created_at` | timestamp with time zone | N | `now()` |  |

## `break_approvals`

- Retention class: **transactional**
- Primary key: `approval_id`
- Foreign keys: `approved_by` → `users.emp_id`; `break_type` → `break_types.break_type`; `emp_id` → `users.emp_id`
- Unique indexes: `uq_pending_lunch_approval` (emp_id, break_type, break_date) WHERE ((status)::text = 'Pending'::text)

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `approval_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `break_type` | character varying | N | `` |  |
| `break_date` | date | N | `` |  |
| `reason` | character varying | Y | `` |  |
| `status` | character varying | N | `'Pending'::character varying` |  |
| `approved_by` | character varying | Y | `` |  |
| `created_at` | timestamp with time zone | N | `now()` |  |

## `break_types`

- Retention class: **transactional**
- Primary key: `break_type`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `break_type` | character varying | N | `` |  |
| `daily_limit_minutes` | integer | Y | `` |  |
| `description` | character varying | Y | `` |  |

## `breaks`

- Retention class: **statutory-7y**
- Primary key: `break_id`
- Foreign keys: `break_type` → `break_types.break_type`; `emp_id` → `users.emp_id`
- Unique indexes: `uq_one_active_break` (emp_id) WHERE ((status)::text = 'Active'::text)

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `break_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `break_type` | character varying | N | `` |  |
| `start_time` | timestamp with time zone | N | `` |  |
| `end_time` | timestamp with time zone | Y | `` |  |
| `duration_minutes` | integer | Y | `` |  |
| `break_date` | date | Y | `` |  |
| `status` | character varying | N | `'Active'::character varying` |  |
| `ended_reason` | character varying | Y | `` |  |

## `candidates`

- Retention class: **transactional**
- Primary key: `candidate_id`
- Foreign keys: `job_id` → `job_postings.job_id`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `candidate_id` | bigint | N | `` |  |
| `job_id` | bigint | Y | `` |  |
| `name` | character varying | N | `` | ⚠ |
| `email` | character varying | N | `` | ⚠ |
| `phone` | character varying | Y | `` | ⚠ |
| `resume_text` | character varying | Y | `` | ⚠ |
| `status` | character varying | N | `'Applied'::character varying` |  |
| `applied_at` | timestamp with time zone | N | `now()` |  |

## `dependents`

- Retention class: **transactional**
- Primary key: `dependent_id`
- Foreign keys: `emp_id` → `users.emp_id`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `dependent_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `name` | character varying | N | `` | ⚠ |
| `relationship` | character varying | N | `` | ⚠ |
| `date_of_birth` | date | Y | `` | ⚠ |

## `documents`

- Retention class: **transactional**
- Primary key: `doc_id`
- Foreign keys: `emp_id` → `users.emp_id`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `doc_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `name` | character varying | N | `` | ⚠ |
| `category` | character varying | N | `'Other'::character varying` |  |
| `file_path` | character varying | Y | `` |  |
| `file_size` | integer | Y | `` |  |
| `uploaded_at` | timestamp with time zone | N | `now()` |  |

## `employee_documents`

- Retention class: **transactional**
- Primary key: `doc_id`
- Foreign keys: `emp_id` → `users.emp_id`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `doc_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `doc_type` | character varying | N | `` |  |
| `file_name` | character varying | Y | `` | ⚠ |
| `uploaded_at` | timestamp with time zone | N | `now()` |  |

## `exit_interviews`

- Retention class: **transactional**
- Primary key: `interview_id`
- Foreign keys: `emp_id` → `users.emp_id`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `interview_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `reason` | character varying | N | `` |  |
| `feedback` | character varying | Y | `` |  |
| `exit_date` | date | N | `` |  |
| `created_at` | timestamp with time zone | N | `now()` |  |
| `offboard_id` | bigint | Y | `` |  |

## `expense_categories`

- Retention class: **transactional**
- Primary key: `cat_id`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `cat_id` | bigint | N | `` |  |
| `name` | character varying | N | `` |  |
| `description` | character varying | Y | `` |  |

## `expense_claims`

- Retention class: **statutory-7y**
- Primary key: `claim_id`
- Foreign keys: `approved_by` → `users.emp_id`; `cat_id` → `expense_categories.cat_id`; `emp_id` → `users.emp_id`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `claim_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `cat_id` | bigint | N | `` |  |
| `amount` | numeric | N | `` |  |
| `description` | character varying | Y | `` |  |
| `receipt_path` | character varying | Y | `` |  |
| `status` | character varying | N | `'Pending'::character varying` |  |
| `approved_by` | character varying | Y | `` |  |
| `paid_at` | timestamp with time zone | Y | `` |  |
| `created_at` | timestamp with time zone | N | `now()` |  |

## `feedback_360`

- Retention class: **transactional**
- Primary key: `feedback_id`
- Foreign keys: `emp_id` → `users.emp_id`; `reviewer_id` → `users.emp_id`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `feedback_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `reviewer_id` | character varying | N | `` |  |
| `category` | character varying | Y | `` |  |
| `rating` | integer | Y | `` |  |
| `comment` | character varying | Y | `` |  |
| `submitted_at` | timestamp with time zone | N | `now()` |  |

## `goals`

- Retention class: **transactional**
- Primary key: `goal_id`
- Foreign keys: `emp_id` → `users.emp_id`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `goal_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `title` | character varying | N | `` |  |
| `description` | character varying | Y | `` |  |
| `target_date` | date | Y | `` |  |
| `weight` | integer | N | `1` |  |
| `rating` | integer | Y | `` |  |
| `status` | character varying | N | `'Active'::character varying` |  |
| `created_at` | timestamp with time zone | N | `now()` |  |

## `holiday_optins`

- Retention class: **transactional**
- Primary key: `optin_id`
- Foreign keys: `emp_id` → `users.emp_id`; `holiday_id` → `holidays.holiday_id`
- Unique indexes: `uq_optin` (emp_id, holiday_id)
- Exclusion constraints: `uq_optin` ()

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `optin_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `holiday_id` | bigint | N | `` |  |
| `status` | character varying | N | `'Pending'::character varying` |  |
| `created_at` | timestamp with time zone | N | `now()` |  |

## `holidays`

- Retention class: **transactional**
- Primary key: `holiday_id`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `holiday_id` | bigint | N | `` |  |
| `name` | character varying | N | `` |  |
| `holiday_date` | date | N | `` |  |
| `year` | integer | N | `` |  |
| `type` | character varying | N | `'National'::character varying` |  |
| `location` | character varying | Y | `` |  |

## `idempotency_keys`

- Retention class: **transactional**
- Primary key: `key`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `key` | character varying | N | `` |  |
| `route` | character varying | N | `` |  |
| `request_hash` | character varying | N | `` |  |
| `response_status` | integer | N | `` |  |
| `response_body` | jsonb | Y | `` |  |
| `created_at` | timestamp with time zone | N | `now()` |  |
| `expires_at` | timestamp with time zone | N | `` |  |

## `interviews`

- Retention class: **transactional**
- Primary key: `interview_id`
- Foreign keys: `candidate_id` → `candidates.candidate_id`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `interview_id` | bigint | N | `` |  |
| `candidate_id` | bigint | N | `` |  |
| `scheduled_at` | timestamp with time zone | N | `` |  |
| `interviewer` | character varying | Y | `` |  |
| `mode` | character varying | N | `'In-person'::character varying` |  |
| `feedback` | character varying | Y | `` |  |
| `status` | character varying | N | `'Scheduled'::character varying` |  |

## `job_postings`

- Retention class: **transactional**
- Primary key: `job_id`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `job_id` | bigint | N | `` |  |
| `title` | character varying | N | `` |  |
| `department` | character varying | Y | `` |  |
| `location` | character varying | Y | `` |  |
| `description` | character varying | Y | `` |  |
| `requirements` | character varying | Y | `` |  |
| `status` | character varying | N | `'Open'::character varying` |  |
| `created_at` | timestamp with time zone | N | `now()` |  |

## `leave_balance`

- Retention class: **statutory-7y**
- Primary key: `balance_id`
- Foreign keys: `emp_id` → `users.emp_id`
- Unique indexes: `uq_balance` (emp_id, leave_type, year)
- Exclusion constraints: `uq_balance` ()

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `balance_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `leave_type` | character varying | N | `` |  |
| `total_days` | integer | N | `0` |  |
| `used_days` | integer | N | `0` |  |
| `reserved` | integer | N | `0` |  |
| `year` | integer | N | `` |  |

## `leave_policy_assignments`

- Retention class: **transactional**
- Primary key: `assignment_id`
- Foreign keys: `emp_id` → `users.emp_id`
- Unique indexes: none
- Exclusion constraints: `no_overlapping_policy` (emp_id, daterange(effective_from, effective_to, '[)'::text))

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `assignment_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `location` | character varying | Y | `` |  |
| `grade` | character varying | Y | `` |  |
| `accrual_rate` | numeric | Y | `` |  |
| `carry_forward_cap` | integer | Y | `` |  |
| `encashment_rule` | character varying | Y | `` |  |
| `weekly_off_pattern` | character varying | Y | `` |  |
| `effective_from` | date | N | `` |  |
| `effective_to` | date | Y | `` |  |

## `leave_requests`

- Retention class: **statutory-7y**
- Primary key: `leave_id`
- Foreign keys: `approved_by` → `users.emp_id`; `emp_id` → `users.emp_id`
- Unique indexes: none
- Exclusion constraints: `no_overlapping_leave` (emp_id, daterange(start_date, end_date, '[]'::text)) WHERE ((status)::text = ANY ((ARRAY['Pending'::character varying, 'Approved'::character varying])::text[]))

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `leave_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `leave_type` | character varying | N | `` |  |
| `start_date` | date | N | `` |  |
| `end_date` | date | N | `` |  |
| `year` | integer | N | `0` |  |
| `session` | character varying | N | `'Full'::character varying` |  |
| `reason` | character varying | Y | `` |  |
| `status` | character varying | N | `'Pending'::character varying` |  |
| `approved_by` | character varying | Y | `` |  |
| `created_at` | timestamp with time zone | N | `now()` |  |
| `updated_at` | timestamp with time zone | Y | `` |  |
| `version` | integer | N | `1` |  |

## `mfa_credentials`

- Retention class: **transactional**
- Primary key: `cred_id`
- Foreign keys: `emp_id` → `users.emp_id`
- Unique indexes: `uq_mfa_emp` (emp_id)

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `cred_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `secret_encrypted` | character varying | N | `` |  |
| `enabled` | boolean | N | `true` |  |
| `created_at` | timestamp with time zone | N | `now()` |  |
| `last_used_at` | timestamp with time zone | Y | `` |  |

## `monthly_leave_grants`

- Retention class: **transactional**
- Primary key: `grant_id`
- Foreign keys: `emp_id` → `users.emp_id`; `granted_by` → `users.emp_id`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `grant_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `leave_type` | character varying | N | `` |  |
| `days` | integer | N | `` |  |
| `month` | integer | N | `` |  |
| `year` | integer | N | `` |  |
| `granted_by` | character varying | Y | `` |  |
| `granted_at` | timestamp with time zone | N | `now()` |  |

## `notifications`

- Retention class: **transactional**
- Primary key: `notification_id`
- Foreign keys: `emp_id` → `users.emp_id`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `notification_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `type` | character varying | N | `` |  |
| `category` | character varying | Y | `'General'::character varying` |  |
| `message` | character varying | N | `` |  |
| `related_link` | character varying | Y | `` |  |
| `is_read` | boolean | N | `false` |  |
| `created_at` | timestamp with time zone | N | `now()` |  |

## `offboarding_approvals`

- Retention class: **transactional**
- Primary key: `approval_id`
- Foreign keys: `actor_emp_id` → `users.emp_id`; `offboard_id` → `offboarding_workflow.offboard_id`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `approval_id` | bigint | N | `` |  |
| `offboard_id` | bigint | N | `` |  |
| `actor_emp_id` | character varying | N | `` |  |
| `action` | character varying | N | `` |  |
| `from_status` | character varying | N | `` |  |
| `to_status` | character varying | N | `` |  |
| `created_at` | timestamp with time zone | N | `now()` |  |

## `offboarding_settlements`

- Retention class: **transactional**
- Primary key: `settlement_id`
- Foreign keys: `approved_by` → `users.emp_id`; `offboard_id` → `offboarding_workflow.offboard_id`; `prepared_by` → `users.emp_id`
- Unique indexes: `offboarding_settlements_offboard_id_key` (offboard_id)
- Exclusion constraints: `offboarding_settlements_offboard_id_key` ()

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `settlement_id` | bigint | N | `` |  |
| `offboard_id` | bigint | N | `` |  |
| `pending_payroll` | numeric | N | `0` |  |
| `lop_adjustment` | numeric | N | `0` |  |
| `leave_encashment` | numeric | N | `0` |  |
| `deductions` | numeric | N | `0` |  |
| `asset_damage` | numeric | N | `0` |  |
| `total_amount` | numeric | N | `0` |  |
| `status` | character varying | N | `'Prepared'::character varying` |  |
| `prepared_by` | character varying | N | `` |  |
| `prepared_at` | timestamp with time zone | N | `now()` |  |
| `approved_by` | character varying | Y | `` |  |
| `approved_at` | timestamp with time zone | Y | `` |  |

## `offboarding_tasks`

- Retention class: **transactional**
- Primary key: `task_id`
- Foreign keys: `emp_id` → `users.emp_id`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `task_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `task_name` | character varying | N | `` |  |
| `assigned_to` | character varying | N | `` |  |
| `status` | character varying | N | `'Pending'::character varying` |  |
| `due_date` | date | Y | `` |  |
| `completed_at` | timestamp with time zone | Y | `` |  |
| `stage` | integer | N | `1` |  |

## `offboarding_workflow`

- Retention class: **transactional**
- Primary key: `offboard_id`
- Foreign keys: `emp_id` → `users.emp_id`; `resignation_id` → `resignations.resignation_id`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `offboard_id` | bigint | N | `` |  |
| `resignation_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `stage1_status` | character varying | N | `'Pending'::character varying` |  |
| `stage2_status` | character varying | N | `'Pending'::character varying` |  |
| `stage3_status` | character varying | N | `'Pending'::character varying` |  |
| `stage4_status` | character varying | N | `'Pending'::character varying` |  |
| `stage5_status` | character varying | N | `'Pending'::character varying` |  |
| `completed` | boolean | N | `false` |  |
| `completed_at` | timestamp with time zone | Y | `` |  |
| `created_at` | timestamp with time zone | N | `now()` |  |

## `offer_letters`

- Retention class: **transactional**
- Primary key: `offer_id`
- Foreign keys: `candidate_id` → `candidates.candidate_id`
- Unique indexes: `uq_active_offer_candidate` (candidate_id) WHERE ((status)::text = ANY ((ARRAY['Pending'::character varying, 'Accepted'::character varying])::text[]))

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `offer_id` | bigint | N | `` |  |
| `candidate_id` | bigint | N | `` |  |
| `offered_salary` | numeric | Y | `` |  |
| `basic_pct` | numeric | Y | `` |  |
| `hra_pct` | numeric | Y | `` |  |
| `allowances_pct` | numeric | Y | `` |  |
| `offer_date` | date | N | `` |  |
| `status` | character varying | N | `'Pending'::character varying` |  |
| `accepted_at` | timestamp with time zone | Y | `` |  |
| `notes` | character varying | Y | `` |  |

## `onboarding_checklist`

- Retention class: **transactional**
- Primary key: `item_id`
- Foreign keys: `reviewed_by` → `users.emp_id`; `workflow_id` → `onboarding_workflow.workflow_id`
- Unique indexes: `uq_checklist_item` (workflow_id, doc_type)
- Exclusion constraints: `uq_checklist_item` ()

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `item_id` | bigint | N | `` |  |
| `workflow_id` | bigint | N | `` |  |
| `doc_type` | character varying | N | `` |  |
| `status` | character varying | N | `'Pending'::character varying` |  |
| `uploaded_at` | timestamp with time zone | Y | `` |  |
| `reviewed_by` | character varying | Y | `` |  |
| `review_note` | character varying | Y | `` |  |
| `reviewed_at` | timestamp with time zone | Y | `` |  |

## `onboarding_tasks`

- Retention class: **transactional**
- Primary key: `task_id`
- Foreign keys: `emp_id` → `users.emp_id`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `task_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `task_name` | character varying | N | `` |  |
| `assigned_to` | character varying | N | `` |  |
| `status` | character varying | N | `'Pending'::character varying` |  |
| `due_date` | date | Y | `` |  |
| `completed_at` | timestamp with time zone | Y | `` |  |
| `stage` | integer | N | `1` |  |

## `onboarding_workflow`

- Retention class: **transactional**
- Primary key: `workflow_id`
- Foreign keys: `candidate_id` → `candidates.candidate_id`; `emp_id` → `users.emp_id`
- Unique indexes: `uq_active_onboarding` (emp_id) WHERE (completed = false)

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `workflow_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `candidate_id` | bigint | Y | `` |  |
| `current_step` | integer | N | `1` |  |
| `step1_status` | character varying | N | `'InProgress'::character varying` |  |
| `step2_status` | character varying | N | `'Pending'::character varying` |  |
| `step3_status` | character varying | N | `'Pending'::character varying` |  |
| `step4_status` | character varying | N | `'Pending'::character varying` |  |
| `step5_status` | character varying | N | `'Pending'::character varying` |  |
| `completed` | boolean | N | `false` |  |
| `completed_at` | timestamp with time zone | Y | `` |  |
| `created_at` | timestamp with time zone | N | `now()` |  |
| `step_started_at` | timestamp with time zone | N | `now()` |  |

## `outbox_events`

- Retention class: **audit-indefinite-with-review**
- Primary key: `event_id`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `event_id` | bigint | N | `` |  |
| `event_type` | character varying | N | `` |  |
| `aggregate` | character varying | Y | `` |  |
| `aggregate_id` | character varying | Y | `` |  |
| `payload` | jsonb | Y | `` |  |
| `status` | character varying | N | `'pending'::character varying` |  |
| `attempts` | integer | N | `0` |  |
| `next_attempt_at` | timestamp with time zone | N | `now()` |  |
| `created_at` | timestamp with time zone | N | `now()` |  |
| `delivered_at` | timestamp with time zone | Y | `` |  |

## `password_reset_tokens`

- Retention class: **transactional**
- Primary key: `token_id`
- Foreign keys: `emp_id` → `users.emp_id`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `token_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `token` | character varying | N | `` |  |
| `expires_at` | timestamp with time zone | N | `` |  |
| `used` | boolean | N | `false` |  |
| `created_at` | timestamp with time zone | N | `now()` |  |

## `payroll_approvals`

- Retention class: **statutory-7y**
- Primary key: `approval_id`
- Foreign keys: `actor_emp_id` → `users.emp_id`; `run_id` → `payroll_runs.run_id`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `approval_id` | bigint | N | `` |  |
| `run_id` | bigint | N | `` |  |
| `actor_emp_id` | character varying | N | `` |  |
| `action` | character varying | N | `` |  |
| `from_status` | character varying | N | `` |  |
| `to_status` | character varying | N | `` |  |
| `created_at` | timestamp with time zone | N | `now()` |  |

## `payroll_items`

- Retention class: **statutory-7y**
- Primary key: `item_id`
- Foreign keys: `emp_id` → `users.emp_id`; `run_id` → `payroll_runs.run_id`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `item_id` | bigint | N | `` |  |
| `run_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `gross_salary` | numeric | N | `0` |  |
| `deductions_total` | numeric | N | `0` |  |
| `net_salary` | numeric | N | `0` |  |
| `pf` | numeric | N | `0` |  |
| `esi` | numeric | N | `0` |  |
| `pt` | numeric | N | `0` |  |
| `tds` | numeric | N | `0` |  |
| `lop_amount` | numeric | N | `0` |  |
| `reimbursements` | numeric | N | `0` |  |
| `payslip_generated` | boolean | N | `false` |  |

## `payroll_runs`

- Retention class: **statutory-7y**
- Primary key: `run_id`
- Foreign keys: `adjustment_of_run_id` → `payroll_runs.run_id`; `approved_by` → `users.emp_id`; `submitted_by` → `users.emp_id`
- Unique indexes: `uq_payroll_period` (month, year) WHERE ((status)::text <> 'Cancelled'::text)

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `run_id` | bigint | N | `` |  |
| `month` | integer | N | `` |  |
| `year` | integer | N | `` |  |
| `processed_at` | timestamp with time zone | N | `now()` |  |
| `status` | character varying | N | `'Draft'::character varying` |  |
| `submitted_by` | character varying | Y | `` |  |
| `submitted_at` | timestamp with time zone | Y | `` |  |
| `approved_by` | character varying | Y | `` |  |
| `approved_at` | timestamp with time zone | Y | `` |  |
| `finalized_at` | timestamp with time zone | Y | `` |  |
| `adjustment_of_run_id` | bigint | Y | `` |  |

## `performance_reviews`

- Retention class: **transactional**
- Primary key: `review_id`
- Foreign keys: `emp_id` → `users.emp_id`; `reviewer_id` → `users.emp_id`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `review_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `reviewer_id` | character varying | N | `` |  |
| `review_period` | character varying | N | `` |  |
| `overall_rating` | real | Y | `` |  |
| `comments` | character varying | Y | `` |  |
| `status` | character varying | N | `'Draft'::character varying` |  |
| `created_at` | timestamp with time zone | N | `now()` |  |
| `submitted_at` | timestamp with time zone | Y | `` |  |

## `regularization_requests`

- Retention class: **statutory-7y**
- Primary key: `request_id`
- Foreign keys: `approved_by` → `users.emp_id`; `emp_id` → `users.emp_id`
- Unique indexes: `uq_pending_regularization` (emp_id, request_date) WHERE ((status)::text = 'Pending'::text)

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `request_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `request_date` | date | N | `` |  |
| `reason` | character varying | N | `` |  |
| `requested_login_time` | time without time zone | Y | `` |  |
| `requested_logout_time` | time without time zone | Y | `` |  |
| `status` | character varying | N | `'Pending'::character varying` |  |
| `approved_by` | character varying | Y | `` |  |
| `created_at` | timestamp with time zone | N | `now()` |  |
| `updated_at` | timestamp with time zone | Y | `` |  |
| `version` | integer | N | `1` |  |

## `resignations`

- Retention class: **transactional**
- Primary key: `resignation_id`
- Foreign keys: `emp_id` → `users.emp_id`
- Unique indexes: `uq_active_resignation` (emp_id) WHERE ((status)::text <> 'Cancelled'::text)

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `resignation_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `notice_date` | date | N | `` |  |
| `last_working_day` | date | N | `` |  |
| `reason` | character varying | Y | `` |  |
| `initiated_by` | character varying | N | `` |  |
| `status` | character varying | N | `'Pending'::character varying` |  |
| `created_at` | timestamp with time zone | N | `now()` |  |
| `version` | integer | N | `1` |  |

## `salary_structures`

- Retention class: **statutory-7y**
- Primary key: `struct_id`
- Foreign keys: `emp_id` → `users.emp_id`
- Unique indexes: none
- Exclusion constraints: `no_overlapping_structure` (emp_id, daterange(effective_from, effective_to, '[)'::text))

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `struct_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `basic` | numeric | N | `0` |  |
| `hra` | numeric | N | `0` |  |
| `allowances` | numeric | N | `0` |  |
| `deductions` | numeric | N | `0` |  |
| `effective_from` | date | N | `` |  |
| `effective_to` | date | Y | `` |  |

## `shift_assignments`

- Retention class: **transactional**
- Primary key: `assignment_id`
- Foreign keys: `emp_id` → `users.emp_id`
- Unique indexes: none
- Exclusion constraints: `no_overlapping_shift` (emp_id, daterange(effective_from, effective_to, '[)'::text))

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `assignment_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `shift_type` | character varying | N | `'Fixed'::character varying` |  |
| `shift_start` | time without time zone | N | `` |  |
| `shift_end` | time without time zone | N | `` |  |
| `weekly_off_pattern` | character varying | Y | `'Sat,Sun'::character varying` |  |
| `effective_from` | date | N | `` |  |
| `effective_to` | date | Y | `` |  |
| `created_at` | timestamp with time zone | N | `now()` |  |

## `ticket_comments`

- Retention class: **transactional**
- Primary key: `comment_id`
- Foreign keys: `emp_id` → `users.emp_id`; `ticket_id` → `tickets.ticket_id`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `comment_id` | bigint | N | `` |  |
| `ticket_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `comment` | character varying | N | `` |  |
| `created_at` | timestamp with time zone | N | `now()` |  |

## `tickets`

- Retention class: **transactional**
- Primary key: `ticket_id`
- Foreign keys: `assigned_to` → `users.emp_id`; `emp_id` → `users.emp_id`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `ticket_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `subject` | character varying | N | `` |  |
| `description` | character varying | Y | `` |  |
| `queue` | character varying | Y | `'IT'::character varying` |  |
| `category` | character varying | Y | `` |  |
| `priority` | character varying | N | `'Medium'::character varying` |  |
| `status` | character varying | N | `'Open'::character varying` |  |
| `assigned_to` | character varying | Y | `` |  |
| `created_at` | timestamp with time zone | N | `now()` |  |
| `updated_at` | timestamp with time zone | Y | `` |  |
| `resolved_at` | timestamp with time zone | Y | `` |  |

## `user_permissions`

- Retention class: **transactional**
- Primary key: `perm_id`
- Foreign keys: `emp_id` → `users.emp_id`
- Unique indexes: `uq_user_permission` (emp_id, module)
- Exclusion constraints: `uq_user_permission` ()

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `perm_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `module` | character varying | N | `` |  |
| `allow` | boolean | N | `true` |  |
| `created_at` | timestamp with time zone | N | `now()` |  |
| `updated_at` | timestamp with time zone | Y | `` |  |

## `user_sessions`

- Retention class: **transactional**
- Primary key: `session_id`
- Foreign keys: `emp_id` → `users.emp_id`
- Unique indexes: none

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `session_id` | bigint | N | `` |  |
| `emp_id` | character varying | N | `` |  |
| `login_time` | timestamp with time zone | N | `` |  |
| `logout_time` | timestamp with time zone | Y | `` |  |
| `total_hours` | numeric | Y | `` |  |
| `session_date` | date | Y | `` |  |

## `users`

- Retention class: **transactional**
- Primary key: `emp_id`
- Foreign keys: `manager_emp_id` → `users.emp_id`
- Unique indexes: `uq_users_email_ci` ((expression))

| Column | Type | Null | Default | PII |
|--------|------|:----:|---------|:---:|
| `emp_id` | character varying | N | `` |  |
| `name` | character varying | N | `` |  |
| `email` | character varying | N | `` |  |
| `password` | character varying | N | `` |  |
| `role` | character varying | N | `'Employee'::character varying` |  |
| `department` | character varying | Y | `` |  |
| `designation` | character varying | Y | `` |  |
| `manager_emp_id` | character varying | Y | `` |  |
| `phone` | character varying | Y | `` | ⚠ |
| `date_of_birth` | date | Y | `` | ⚠ |
| `date_of_joining` | date | Y | `` |  |
| `address` | character varying | Y | `` | ⚠ |
| `emergency_contact_name` | character varying | Y | `` | ⚠ |
| `emergency_contact_phone` | character varying | Y | `` | ⚠ |
| `status` | character varying | N | `'Active'::character varying` |  |
| `allow_login` | boolean | N | `true` |  |
| `allow_breaks` | boolean | N | `true` |  |
| `first_login` | timestamp with time zone | Y | `` |  |
| `created_at` | timestamp with time zone | N | `now()` |  |
| `is_super_admin` | boolean | N | `false` |  |
| `candidate_id` | bigint | Y | `` |  |
