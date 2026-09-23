"""baseline — HRMS v2.0 target schema (PostgreSQL 17+)

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-23
"""
import os
from pathlib import Path
from typing import Sequence, Union

from alembic import op

revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Canonical DDL lives with the repo, not inside the migration, so a fresh
# environment and a migrated environment cannot drift (SRS §7.2, §14 Phase 0).
_SCHEMA_FILE = Path(__file__).resolve().parents[2] / "db" / "postgres_schema.sql"
_PART_B_MARKER = "--  PART B"


def _load_schema_sql() -> str:
    sql = _SCHEMA_FILE.read_text(encoding="utf-8")
    if not os.path.exists(_SCHEMA_FILE):
        raise FileNotFoundError(f"Target schema not found: {_SCHEMA_FILE}")
    if _PART_B_MARKER not in sql:
        raise ValueError(f"PART B marker not found in {_SCHEMA_FILE}")
    return sql


def _split_sql_statements(sql: str) -> list[str]:
    """Split on top-level semicolons, ignoring comments and quoted strings."""
    statements: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    in_line_comment = False

    def flush():
        stmt = "".join(buf).strip()
        if stmt:
            statements.append(stmt)
        buf.clear()

    while i < n:
        ch, nxt = sql[i], sql[i + 1] if i + 1 < n else ""
        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if ch == "-" and nxt == "-":
            in_line_comment = True
            buf.append(ch)
            buf.append(nxt)
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            while i < n:
                if sql[i] == quote and i + 1 < n and sql[i + 1] == quote:
                    buf.append(sql[i])
                    buf.append(sql[i + 1])
                    i += 2
                    continue
                if sql[i] == quote:
                    buf.append(ch)
                    i += 1
                    break
                buf.append(sql[i])
                i += 1
            continue
        if ch == ";":
            flush()
            i += 1
            continue
        buf.append(ch)
        i += 1
    flush()
    return statements


def upgrade() -> None:
    # The file wraps everything in BEGIN/COMMIT; Alembic already runs inside a
    # transaction, so those are dropped. Statements execute one at a time so a
    # failure surfaces the exact offending DDL.
    sql = _load_schema_sql()
    for stmt in _split_sql_statements(sql):
        if stmt.upper().startswith(("BEGIN", "COMMIT")):
            continue
        op.execute(stmt)


def downgrade() -> None:
    # Reverse of PART A creation order (children first, then users last).
    op.execute(
        """
        DROP TABLE IF EXISTS
            idempotency_keys,
            outbox_events,
            user_permissions,
            mfa_credentials,
            payroll_approvals,
            payroll_items,
            payroll_runs,
            salary_structures,
            expense_claims,
            expense_categories,
            ticket_comments,
            tickets,
            feedback_360,
            performance_reviews,
            goals,
            documents,
            employee_documents,
            dependents,
            notifications,
            holiday_optins,
            holidays,
            regularization_requests,
            attendance_days,
            monthly_leave_grants,
            approval_delegations,
            leave_policy_assignments,
            leave_balance,
            leave_requests,
            password_reset_tokens,
            user_sessions,
            break_approvals,
            breaks,
            break_types,
            exit_interviews,
            offboarding_workflow,
            offboarding_tasks,
            resignations,
            onboarding_checklist,
            onboarding_workflow,
            onboarding_tasks,
            offer_letters,
            interviews,
            candidates,
            job_postings,
            assets,
            audit_log,
            shift_assignments,
            users
        CASCADE
        """
    )
