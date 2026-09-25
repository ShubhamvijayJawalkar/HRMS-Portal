#!/usr/bin/env python3
"""Phase 1 (SRS v2.0 §14) — one-time ETL: DuckDB (v1.0) → PostgreSQL (v2.0 target).

What this does, in order:
  1. Creates the PART A schema (tables, FKs, plain indexes) on the target.
  2. Copies every v1.0 table across, converting naive IST timestamps to UTC
     TIMESTAMPTZ (CC-02), types to the v2.0 shape, and handling FK orphans
     conservatively: nullable FKs are NULLed, NOT NULL orphans are skipped
     and reported (never invented).
  3. Phase-1 reconciliation: row counts + checksums of loaded rows vs the
     target, before any cleanup. Any mismatch here is an ETL bug → exit 1.
  4. Data cleanup for the v2.0 constraints (the SRS's "manual cleanup pass"):
     reported by default; applied only with --apply-cleanup.
  5. Applies PART B (the CC-05 invariants: partial uniques, exclusion
     constraints, CHECK split_sums_100, email uniqueness).
  6. Phase-2 reconciliation: final counts + cleanup ledger; advances identity
     sequences (setval) so runtime inserts can't collide with migrated ids.

Usage:
  python scripts/migrate_duckdb_to_postgres.py [--apply-cleanup] [--reset]

Environment:
  DUCKDB_FILE    source DuckDB file     (default: hrms.duckdb)
  DATABASE_URL   target Postgres URL    (default: postgresql+psycopg://postgres:postgres@localhost:55432/hrms)

Exit codes: 0 success · 1 phase-1 reconciliation failed · 2 constraints need
cleanup (re-run with --apply-cleanup) · 3 usage/environment error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
from sqlalchemy import create_engine, text

try:
    from alembic import command
    from alembic.config import Config
except ImportError:  # alembic optional; stamping skipped with a warning
    command = None
    Config = None

IST = ZoneInfo("Asia/Kolkata")  # CC-02: business timezone; storage is UTC

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_FILE = REPO_ROOT / "db" / "postgres_schema.sql"
PART_B_MARKER = "--  PART B"
REPORT_DIR = REPO_ROOT / "reports"

# Parent key column per referenced table (for FK orphan checks).
PARENT_KEY = {
    "users": "emp_id",
    "break_types": "break_type",
    "job_postings": "job_id",
    "candidates": "candidate_id",
    "expense_categories": "cat_id",
    "tickets": "ticket_id",
    "payroll_runs": "run_id",
    "resignations": "resignation_id",
    "onboarding_workflow": "workflow_id",
    "offboarding_workflow": "offboard_id",
}

# child_table -> list of (fk_column, parent_table, nullable)
#   nullable=True  → orphaned value becomes NULL (recorded)
#   nullable=False → the whole row is skipped (recorded)
FKS: dict[str, list[tuple[str, str, bool]]] = {
    "users": [
        ("manager_emp_id", "users", True),
        # A pre-hire points back to the candidate that produced it.  The
        # candidate parent is loaded before users in REGISTRY.
        ("candidate_id", "candidates", True),
    ],
    "user_sessions": [("emp_id", "users", False)],
    "break_approvals": [
        ("emp_id", "users", False),
        ("break_type", "break_types", False),
        ("approved_by", "users", True),
    ],
    "breaks": [("emp_id", "users", False), ("break_type", "break_types", False)],
    "audit_log": [("emp_id", "users", True)],
    "leave_requests": [("emp_id", "users", False), ("approved_by", "users", True)],
    "leave_balance": [("emp_id", "users", False)],
    "password_reset_tokens": [("emp_id", "users", False)],
    "employee_documents": [("emp_id", "users", False)],
    "dependents": [("emp_id", "users", False)],
    "notifications": [("emp_id", "users", False)],
    "regularization_requests": [("emp_id", "users", False), ("approved_by", "users", True)],
    "assets": [("emp_id", "users", False)],
    "candidates": [("job_id", "job_postings", True)],
    "interviews": [("candidate_id", "candidates", False)],
    "offer_letters": [("candidate_id", "candidates", False)],
    # assigned_to is a symbolic owner (HR/IT/Finance/Manager) or an employee
    # id, not a required users FK in the lifecycle task tables.
    "onboarding_tasks": [("emp_id", "users", False)],
    "onboarding_workflow": [
        ("emp_id", "users", False),
        ("candidate_id", "candidates", True),
    ],
    "onboarding_checklist": [
        ("workflow_id", "onboarding_workflow", False),
        ("reviewed_by", "users", True),
    ],
    "offboarding_tasks": [("emp_id", "users", False)],
    "resignations": [
        ("emp_id", "users", False),
        ("initiated_by", "users", False),
    ],
    "offboarding_workflow": [
        ("resignation_id", "resignations", False),
        ("emp_id", "users", False),
    ],
    "offboarding_approvals": [
        ("offboard_id", "offboarding_workflow", False),
        ("actor_emp_id", "users", False),
    ],
    "offboarding_settlements": [
        ("offboard_id", "offboarding_workflow", False),
        ("prepared_by", "users", False),
        ("approved_by", "users", True),
    ],
    "exit_interviews": [
        ("emp_id", "users", False),
        ("offboard_id", "offboarding_workflow", True),
    ],
    "salary_structures": [("emp_id", "users", False)],
    "payroll_runs": [
        ("submitted_by", "users", True), ("approved_by", "users", True),
        ("adjustment_of_run_id", "payroll_runs", True),
    ],
    "payroll_approvals": [("run_id", "payroll_runs", False), ("actor_emp_id", "users", False)],
    "payroll_items": [("run_id", "payroll_runs", False), ("emp_id", "users", False)],
    "goals": [("emp_id", "users", False)],
    "performance_reviews": [("emp_id", "users", False), ("reviewer_id", "users", False)],
    "feedback_360": [("emp_id", "users", False), ("reviewer_id", "users", False)],
    "expense_claims": [
        ("emp_id", "users", False),
        ("cat_id", "expense_categories", False),
        ("approved_by", "users", True),
    ],
    "tickets": [("emp_id", "users", False), ("assigned_to", "users", True)],
    "ticket_comments": [("ticket_id", "tickets", False), ("emp_id", "users", False)],
    "documents": [("emp_id", "users", False)],
    "shift_assignments": [("emp_id", "users", False)],
}


# ───────────────────────────────────────────────────────────────────────────
# Value conversion helpers
# ───────────────────────────────────────────────────────────────────────────

def to_pg(v):
    """Naive datetime → aware UTC (source timestamps are IST, CC-02)."""
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=IST)
        return v.astimezone(UTC)
    return v


def _to_bool(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    return bool(int(v))


def _to_time(v):
    if isinstance(v, time):
        return v
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() == "24x7":
        return None
    try:
        parts = s.split(":")
        h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        return time(h % 24, m)
    except (ValueError, IndexError):
        return None


def canon(v) -> str:
    """Canonical text for a value, identical for source and target representations."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "t" if v else "f"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return f"{v:.6f}"
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=IST)
        return v.astimezone(UTC).replace(tzinfo=None).isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, time):
        return v.isoformat()
    return str(v)


def checksum(rows) -> str:
    payload = "\n".join(sorted("|".join(canon(c) for c in row) for row in rows))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _quote_identifier(identifier: str) -> str:
    """Quote a source identifier used in a generated DuckDB SELECT."""
    return '"' + identifier.replace('"', '""') + '"'


def _source_catalog(src) -> dict[str, set[str]]:
    """Return the DuckDB main-schema table/column catalog.

    The corrected lifecycle columns and tables were added after the original
    v1.0 ETL.  Introspecting the source lets this migration preserve them when
    present while still accepting an older DuckDB file.
    """
    catalog: dict[str, set[str]] = {}
    rows = src.execute("""
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'main'
    """).fetchall()
    for table_name, column_name in rows:
        catalog.setdefault(str(table_name).lower(), set()).add(str(column_name).lower())
    return catalog


def _source_select(entry: dict, available_columns: set[str]) -> str:
    """Build a source query, using safe defaults for optional legacy columns.

    Entries without ``source_names`` retain their hand-written query.  For
    lifecycle-aware entries, ``source_names`` is aligned with ``columns`` and
    ``source_defaults`` supplies a SQL expression when a source column is
    absent (or NULL when present).  This is deliberately data-shape aware,
    rather than relying on a hard-coded old v1.0 SELECT.
    """
    source_names = entry.get("source_names")
    if not source_names:
        return entry["select"]

    columns = entry["columns"]
    if len(source_names) != len(columns):
        raise ValueError(f"{entry['table']}: source_names/columns length mismatch")

    defaults = entry.get("source_defaults", {})
    expressions = []
    for target_column, source_name in zip(columns, source_names):
        default = defaults.get(target_column, "NULL")
        if source_name is None or source_name.lower() not in available_columns:
            expression = default
        elif default == "NULL":
            expression = _quote_identifier(source_name)
        else:
            expression = f"COALESCE({_quote_identifier(source_name)}, {default})"
        expressions.append(f"{expression} AS {_quote_identifier(target_column)}")

    order_by = ""
    first_source = source_names[0]
    if first_source and first_source.lower() in available_columns:
        order_by = f" ORDER BY {_quote_identifier(first_source)}"
    return (
        f"SELECT {', '.join(expressions)} FROM {_quote_identifier(entry['table'])}{order_by}"
    )


def split_sql_statements(sql: str) -> list[str]:
    """Split a SQL script on top-level semicolons, ignoring comments, string
    literals and quoted identifiers (so `-- ... load directly; ` doesn't cut)."""
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
        if ch == "/" and nxt == "*":
            end = sql.find("*/", i + 2)
            if end == -1:
                buf.append(sql[i:])
                break
            buf.append(sql[i:end + 2])
            i = end + 2
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            while i < n:
                if sql[i] == quote and i + 1 < n and sql[i + 1] == quote:  # escaped quote
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


# ───────────────────────────────────────────────────────────────────────────
# Table registry (load order = FK parents first)
# Each entry: table, columns (insert = fetch = checksum), select, row_fn
# ───────────────────────────────────────────────────────────────────────────

def _row_fn_none(row):
    return tuple(row)


def _offer_row_fn(row):
    """Keep percentage values at the target NUMERIC(5,2) scale."""
    percentages = tuple(
        Decimal(str(value)).quantize(Decimal("0.01")) if value is not None else None
        for value in row[3:6]
    )
    return (*row[:3], *percentages, *row[6:])


REGISTRY: list[dict] = [
    # Load candidates before users: users.candidate_id is the durable link from
    # an accepted offer to its pre-hire account.
    dict(
        table="job_postings",
        columns=["job_id", "title", "department", "location", "description", "requirements", "status", "created_at"],
        select="SELECT job_id, title, department, location, description, requirements, status, created_at FROM job_postings ORDER BY job_id",
        row_fn=_row_fn_none,
        pk="job_id",
    ),
    dict(
        table="candidates",
        columns=["candidate_id", "job_id", "name", "email", "phone", "resume_text", "status", "applied_at"],
        select="SELECT candidate_id, job_id, name, email, phone, resume_text, status, applied_at FROM candidates ORDER BY candidate_id",
        row_fn=_row_fn_none,
        pk="candidate_id",
    ),
    dict(
        table="users",
        columns=[
            "emp_id", "name", "email", "password", "role", "department", "designation",
            "manager_emp_id", "phone", "date_of_birth", "date_of_joining", "address",
            "emergency_contact_name", "emergency_contact_phone", "status", "allow_login",
            "allow_breaks", "first_login", "created_at", "is_super_admin", "candidate_id",
        ],
        select="""
            SELECT emp_id, name, email, password, role, department, designation,
                   manager_emp_id, phone, date_of_birth, date_of_joining, address,
                   emergency_contact_name, emergency_contact_phone, status, allow_login,
                   allow_breaks, first_login, created_at, is_super_admin, candidate_id
            FROM users ORDER BY emp_id
        """,
        source_names=[
            "emp_id", "name", "email", "password", "role", "department", "designation",
            "manager_emp_id", "phone", "date_of_birth", "date_of_joining", "address",
            "emergency_contact_name", "emergency_contact_phone", "status", "allow_login",
            "allow_breaks", "first_login", "created_at", "is_super_admin", "candidate_id",
        ],
        source_defaults={
            "role": "'Employee'",
            "status": "'Active'",
            "allow_login": "TRUE",
            "allow_breaks": "TRUE",
            "created_at": "CAST(CURRENT_TIMESTAMP AS TIMESTAMP)",
            "is_super_admin": "FALSE",
            "candidate_id": "NULL",
        },
        row_fn=lambda r: (
            *r[:15], _to_bool(r[15]), _to_bool(r[16]), *r[17:19],
            _to_bool(r[19]) if r[19] is not None else False, r[20],
        ),
        pk="emp_id",
    ),
    dict(
        table="expense_categories",
        columns=["cat_id", "name", "description"],
        select="SELECT cat_id, name, description FROM expense_categories ORDER BY cat_id",
        row_fn=_row_fn_none,
        pk="cat_id",
    ),
    dict(
        table="break_types",
        columns=["break_type", "daily_limit_minutes", "description"],
        select="SELECT break_type, daily_limit_minutes, description FROM break_types ORDER BY break_type",
        row_fn=_row_fn_none,
        pk="break_type",
    ),
    # v1.0 keeps shifts on users; v2.0 moves them to effective-dated shift_assignments (FR-ATT-17).
    # One row per employee with effective_from=1970-01-01 (no overlap possible), weekly-off
    # defaults to Sat,Sun until real patterns are collected (SRS R-04).
    dict(
        table="shift_assignments",
        columns=["emp_id", "shift_type", "shift_start", "shift_end", "weekly_off_pattern", "effective_from", "effective_to"],
        select="SELECT emp_id, shift_start, shift_end FROM users WHERE shift_start IS NOT NULL ORDER BY emp_id",
        row_fn=lambda r: (
            r[0],
            "24x7" if str(r[1]).lower() == "24x7" or str(r[2]).lower() == "24x7" else "Fixed",
            _to_time(r[1]) or time(9, 0),
            _to_time(r[2]) or time(18, 0),
            "Sat,Sun",
            date(1970, 1, 1),
            None,
        ),
        pk="emp_id",
    ),
    dict(
        table="user_sessions",
        columns=["session_id", "emp_id", "login_time", "logout_time", "total_hours", "session_date"],
        select="SELECT session_id, emp_id, login_time, logout_time, total_hours, session_date FROM user_sessions ORDER BY session_id",
        row_fn=_row_fn_none,
        pk="session_id",
    ),
    dict(
        table="break_approvals",
        columns=["approval_id", "emp_id", "break_type", "break_date", "reason", "status", "approved_by", "created_at"],
        select="SELECT approval_id, emp_id, break_type, break_date, reason, status, approved_by, created_at FROM break_approvals ORDER BY approval_id",
        row_fn=_row_fn_none,
        pk="approval_id",
    ),
    dict(
        table="breaks",
        columns=["break_id", "emp_id", "break_type", "start_time", "end_time", "duration_minutes", "break_date", "status", "ended_reason"],
        select="SELECT break_id, emp_id, break_type, start_time, end_time, duration_minutes, break_date, status FROM breaks ORDER BY break_id",
        row_fn=lambda r: (*r, None),
        pk="break_id",
    ),
    # v2.0 audit row = {actor, action, entity, entity_id, before, after, ip, request_id, created_at} (FR-AUD-01).
    dict(
        table="audit_log",
        columns=["log_id", "emp_id", "actor", "action", "entity", "entity_id", "details", "before", "after", "ip_address", "request_id", "created_at"],
        select="SELECT log_id, emp_id, action, details, ip_address, created_at FROM audit_log ORDER BY log_id",
        row_fn=lambda r: (r[0], r[1], r[1], r[2], None, None, r[3], None, None, r[4], None, r[5]),
        pk="log_id",
    ),
    dict(
        table="leave_requests",
        columns=["leave_id", "emp_id", "leave_type", "start_date", "end_date", "year", "session", "reason", "status", "approved_by", "created_at", "updated_at", "version"],
        select="SELECT leave_id, emp_id, leave_type, start_date, end_date, year, reason, status, approved_by, created_at, updated_at FROM leave_requests ORDER BY leave_id",
        row_fn=lambda r: (*r[:6], "Full", *r[6:], 1),
        pk="leave_id",
    ),
    dict(
        table="leave_balance",
        columns=["balance_id", "emp_id", "leave_type", "total_days", "used_days", "reserved", "year"],
        select="SELECT balance_id, emp_id, leave_type, total_days, used_days, year FROM leave_balance ORDER BY balance_id",
        row_fn=lambda r: (r[0], r[1], r[2], r[3], r[4], 0, r[5]),
        pk="balance_id",
    ),
    dict(
        table="password_reset_tokens",
        columns=["token_id", "emp_id", "token", "expires_at", "used", "created_at"],
        select="SELECT token_id, emp_id, token, expires_at, used, created_at FROM password_reset_tokens ORDER BY token_id",
        row_fn=lambda r: (*r[:4], _to_bool(r[4]), r[5]),
        pk="token_id",
    ),
    dict(
        table="employee_documents",
        columns=["doc_id", "emp_id", "doc_type", "file_name", "uploaded_at"],
        select="SELECT doc_id, emp_id, doc_type, file_name, uploaded_at FROM employee_documents ORDER BY doc_id",
        row_fn=_row_fn_none,
        pk="doc_id",
    ),
    dict(
        table="dependents",
        columns=["dependent_id", "emp_id", "name", "relationship", "date_of_birth"],
        select="SELECT dependent_id, emp_id, name, relationship, date_of_birth FROM dependents ORDER BY dependent_id",
        row_fn=_row_fn_none,
        pk="dependent_id",
    ),
    dict(
        table="holidays",
        columns=["holiday_id", "name", "holiday_date", "year", "type", "location"],
        select="SELECT holiday_id, name, holiday_date, year, type FROM holidays ORDER BY holiday_id",
        row_fn=lambda r: (*r, None),
        pk="holiday_id",
    ),
    # v1.0 `type` is the v2.0 preference category (FR-NOT-03).
    dict(
        table="notifications",
        columns=["notification_id", "emp_id", "type", "category", "message", "related_link", "is_read", "created_at"],
        select="SELECT notification_id, emp_id, type, message, related_link, is_read, created_at FROM notifications ORDER BY notification_id",
        row_fn=lambda r: (r[0], r[1], r[2], r[2], r[3], r[4], _to_bool(r[5]), r[6]),
        pk="notification_id",
    ),
    dict(
        table="regularization_requests",
        columns=["request_id", "emp_id", "request_date", "reason", "requested_login_time", "requested_logout_time", "status", "approved_by", "created_at", "updated_at", "version"],
        select="SELECT request_id, emp_id, request_date, reason, status, approved_by, created_at, updated_at FROM regularization_requests ORDER BY request_id",
        row_fn=lambda r: (*r[:4], None, None, *r[4:], 1),
        pk="request_id",
    ),
    dict(
        table="assets",
        columns=["asset_id", "emp_id", "asset_type", "asset_tag", "brand", "model", "serial_number", "issued_date", "return_date", "status", "notes"],
        select="SELECT asset_id, emp_id, asset_type, asset_tag, brand, model, serial_number, issued_date, return_date, status, notes FROM assets ORDER BY asset_id",
        row_fn=_row_fn_none,
        pk="asset_id",
    ),
    dict(
        table="interviews",
        columns=["interview_id", "candidate_id", "scheduled_at", "interviewer", "mode", "feedback", "status"],
        select="SELECT interview_id, candidate_id, scheduled_at, interviewer, mode, feedback, status FROM interviews ORDER BY interview_id",
        row_fn=_row_fn_none,
        pk="interview_id",
    ),
    # Old v1.0 offers had no percentage columns (and its fixed split was
    # 50/20/20).  Corrected source offers are read as-is; only absent/NULL
    # legacy values receive the validated 50/30/20 compatibility split.
    dict(
        table="offer_letters",
        columns=["offer_id", "candidate_id", "offered_salary", "basic_pct", "hra_pct", "allowances_pct", "offer_date", "status", "accepted_at", "notes"],
        select="""
            SELECT offer_id, candidate_id, offered_salary, basic_pct, hra_pct,
                   allowances_pct, offer_date, status, accepted_at, notes
            FROM offer_letters ORDER BY offer_id
        """,
        source_names=[
            "offer_id", "candidate_id", "offered_salary", "basic_pct", "hra_pct",
            "allowances_pct", "offer_date", "status", "accepted_at", "notes",
        ],
        source_defaults={
            "basic_pct": "CAST(50.00 AS DECIMAL(5,2))",
            "hra_pct": "CAST(30.00 AS DECIMAL(5,2))",
            "allowances_pct": "CAST(20.00 AS DECIMAL(5,2))",
            "offer_date": "CURRENT_DATE",
            "status": "'Pending'",
        },
        row_fn=_offer_row_fn,
        pk="offer_id",
    ),
    dict(
        table="onboarding_tasks",
        columns=["task_id", "emp_id", "task_name", "assigned_to", "status", "due_date", "completed_at", "stage"],
        select="""
            SELECT task_id, emp_id, task_name, assigned_to, status, due_date,
                   completed_at, stage
            FROM onboarding_tasks ORDER BY task_id
        """,
        source_names=["task_id", "emp_id", "task_name", "assigned_to", "status", "due_date", "completed_at", "stage"],
        source_defaults={"assigned_to": "'HR'", "status": "'Pending'", "stage": "1"},
        row_fn=_row_fn_none,
        pk="task_id",
    ),
    dict(
        table="offboarding_tasks",
        columns=["task_id", "emp_id", "task_name", "assigned_to", "status", "due_date", "completed_at", "stage"],
        select="""
            SELECT task_id, emp_id, task_name, assigned_to, status, due_date,
                   completed_at, stage
            FROM offboarding_tasks ORDER BY task_id
        """,
        source_names=["task_id", "emp_id", "task_name", "assigned_to", "status", "due_date", "completed_at", "stage"],
        source_defaults={"assigned_to": "'HR'", "status": "'Pending'", "stage": "1"},
        row_fn=_row_fn_none,
        pk="task_id",
    ),
    # Corrected lifecycle tables are optional for old v1.0 DuckDB files.  When
    # present, every source row and its status/timestamp is carried forward.
    dict(
        table="onboarding_workflow",
        columns=[
            "workflow_id", "emp_id", "candidate_id", "current_step", "step_started_at",
            "step1_status", "step2_status", "step3_status", "step4_status", "step5_status",
            "completed", "completed_at", "created_at",
        ],
        select="""
            SELECT workflow_id, emp_id, candidate_id, current_step, step_started_at,
                   step1_status, step2_status, step3_status, step4_status, step5_status,
                   completed, completed_at, created_at
            FROM onboarding_workflow ORDER BY workflow_id
        """,
        source_names=[
            "workflow_id", "emp_id", "candidate_id", "current_step", "step_started_at",
            "step1_status", "step2_status", "step3_status", "step4_status", "step5_status",
            "completed", "completed_at", "created_at",
        ],
        source_defaults={
            "current_step": "1",
            "step_started_at": "CAST(CURRENT_TIMESTAMP AS TIMESTAMP)",
            "step1_status": "'InProgress'",
            "step2_status": "'Pending'",
            "step3_status": "'Pending'",
            "step4_status": "'Pending'",
            "step5_status": "'Pending'",
            "completed": "FALSE",
            "created_at": "CAST(CURRENT_TIMESTAMP AS TIMESTAMP)",
        },
        row_fn=lambda r: (*r[:10], _to_bool(r[10]), *r[11:]),
        pk="workflow_id",
        source_optional=True,
    ),
    dict(
        table="onboarding_checklist",
        columns=["item_id", "workflow_id", "doc_type", "status", "uploaded_at", "reviewed_by", "review_note", "reviewed_at"],
        select="""
            SELECT item_id, workflow_id, doc_type, status, uploaded_at,
                   reviewed_by, review_note, reviewed_at
            FROM onboarding_checklist ORDER BY item_id
        """,
        source_names=["item_id", "workflow_id", "doc_type", "status", "uploaded_at", "reviewed_by", "review_note", "reviewed_at"],
        source_defaults={"status": "'Pending'"},
        row_fn=_row_fn_none,
        pk="item_id",
        source_optional=True,
    ),
    dict(
        table="resignations",
        columns=["resignation_id", "emp_id", "notice_date", "last_working_day", "reason", "initiated_by", "status", "created_at", "version"],
        select="""
            SELECT resignation_id, emp_id, notice_date, last_working_day, reason,
                   initiated_by, status, created_at, version
            FROM resignations ORDER BY resignation_id
        """,
        source_names=["resignation_id", "emp_id", "notice_date", "last_working_day", "reason", "initiated_by", "status", "created_at", "version"],
        source_defaults={
            "notice_date": "CURRENT_DATE",
            "last_working_day": "CURRENT_DATE",
            "status": "'Pending'",
            "created_at": "CAST(CURRENT_TIMESTAMP AS TIMESTAMP)",
            "version": "1",
        },
        row_fn=_row_fn_none,
        pk="resignation_id",
        source_optional=True,
    ),
    dict(
        table="offboarding_workflow",
        columns=[
            "offboard_id", "resignation_id", "emp_id", "stage1_status", "stage2_status",
            "stage3_status", "stage4_status", "stage5_status", "completed",
            "completed_at", "created_at",
        ],
        select="""
            SELECT offboard_id, resignation_id, emp_id, stage1_status, stage2_status,
                   stage3_status, stage4_status, stage5_status, completed,
                   completed_at, created_at
            FROM offboarding_workflow ORDER BY offboard_id
        """,
        source_names=[
            "offboard_id", "resignation_id", "emp_id", "stage1_status", "stage2_status",
            "stage3_status", "stage4_status", "stage5_status", "completed",
            "completed_at", "created_at",
        ],
        source_defaults={
            "stage1_status": "'Pending'",
            "stage2_status": "'Pending'",
            "stage3_status": "'Pending'",
            "stage4_status": "'Pending'",
            "stage5_status": "'Pending'",
            "completed": "FALSE",
            "created_at": "CAST(CURRENT_TIMESTAMP AS TIMESTAMP)",
        },
        row_fn=lambda r: (*r[:8], _to_bool(r[8]), *r[9:]),
        pk="offboard_id",
        source_optional=True,
    ),
    dict(
        table="offboarding_approvals",
        columns=["approval_id", "offboard_id", "actor_emp_id", "action", "from_status", "to_status", "created_at"],
        select="""
            SELECT approval_id, offboard_id, actor_emp_id, action, from_status,
                   to_status, created_at
            FROM offboarding_approvals ORDER BY approval_id
        """,
        source_names=["approval_id", "offboard_id", "actor_emp_id", "action", "from_status", "to_status", "created_at"],
        source_defaults={"created_at": "CAST(CURRENT_TIMESTAMP AS TIMESTAMP)"},
        row_fn=_row_fn_none,
        pk="approval_id",
        source_optional=True,
    ),
    dict(
        table="offboarding_settlements",
        columns=[
            "settlement_id", "offboard_id", "pending_payroll", "lop_adjustment",
            "leave_encashment", "deductions", "asset_damage", "total_amount", "status",
            "prepared_by", "prepared_at", "approved_by", "approved_at",
        ],
        select="""
            SELECT settlement_id, offboard_id, pending_payroll, lop_adjustment,
                   leave_encashment, deductions, asset_damage, total_amount, status,
                   prepared_by, prepared_at, approved_by, approved_at
            FROM offboarding_settlements ORDER BY settlement_id
        """,
        source_names=[
            "settlement_id", "offboard_id", "pending_payroll", "lop_adjustment",
            "leave_encashment", "deductions", "asset_damage", "total_amount", "status",
            "prepared_by", "prepared_at", "approved_by", "approved_at",
        ],
        source_defaults={
            "pending_payroll": "CAST(0 AS DECIMAL(14,2))",
            "lop_adjustment": "CAST(0 AS DECIMAL(14,2))",
            "leave_encashment": "CAST(0 AS DECIMAL(14,2))",
            "deductions": "CAST(0 AS DECIMAL(14,2))",
            "asset_damage": "CAST(0 AS DECIMAL(14,2))",
            "total_amount": "CAST(0 AS DECIMAL(14,2))",
            "status": "'Prepared'",
            "prepared_at": "CAST(CURRENT_TIMESTAMP AS TIMESTAMP)",
        },
        row_fn=_row_fn_none,
        pk="settlement_id",
        source_optional=True,
    ),
    dict(
        table="exit_interviews",
        columns=["interview_id", "emp_id", "reason", "feedback", "exit_date", "created_at", "offboard_id"],
        select="""
            SELECT interview_id, emp_id, reason, feedback, exit_date, created_at,
                   offboard_id
            FROM exit_interviews ORDER BY interview_id
        """,
        source_names=["interview_id", "emp_id", "reason", "feedback", "exit_date", "created_at", "offboard_id"],
        source_defaults={"offboard_id": "NULL"},
        row_fn=_row_fn_none,
        pk="interview_id",
    ),
    dict(
        table="salary_structures",
        columns=["struct_id", "emp_id", "basic", "hra", "allowances", "deductions", "effective_from", "effective_to"],
        select="SELECT struct_id, emp_id, basic, hra, allowances, deductions, effective_from FROM salary_structures ORDER BY struct_id",
        row_fn=lambda r: (*r, None),
        pk="struct_id",
    ),
    dict(
        table="payroll_runs",
        columns=["run_id", "month", "year", "processed_at", "status", "submitted_by", "submitted_at", "approved_by", "approved_at", "finalized_at", "adjustment_of_run_id"],
        select="SELECT run_id, month, year, processed_at, status FROM payroll_runs ORDER BY run_id",
        row_fn=lambda r: (*r, None, None, None, None, None, None),
        pk="run_id",
    ),
    dict(
        table="payroll_approvals",
        columns=["approval_id", "run_id", "actor_emp_id", "action", "from_status", "to_status", "created_at"],
        select="SELECT approval_id, run_id, actor_emp_id, action, from_status, to_status, created_at FROM payroll_approvals ORDER BY approval_id",
        row_fn=_row_fn_none,
        pk="approval_id",
    ),
    dict(
        table="payroll_items",
        columns=["item_id", "run_id", "emp_id", "gross_salary", "deductions_total", "net_salary", "pf", "esi", "pt", "tds", "lop_amount", "reimbursements", "payslip_generated"],
        select="SELECT item_id, run_id, emp_id, gross_salary, deductions_total, net_salary, pf, esi, pt, payslip_generated FROM payroll_items ORDER BY item_id",
        row_fn=lambda r: (*r[:9], Decimal("0.00"), Decimal("0.00"), Decimal("0.00"), _to_bool(r[9])),
        pk="item_id",
    ),
    dict(
        table="goals",
        columns=["goal_id", "emp_id", "title", "description", "target_date", "weight", "rating", "status", "created_at"],
        select="SELECT goal_id, emp_id, title, description, target_date, weight, rating, status, created_at FROM goals ORDER BY goal_id",
        row_fn=_row_fn_none,
        pk="goal_id",
    ),
    dict(
        table="performance_reviews",
        columns=["review_id", "emp_id", "reviewer_id", "review_period", "overall_rating", "comments", "status", "created_at", "submitted_at"],
        select="SELECT review_id, emp_id, reviewer_id, review_period, overall_rating, comments, status, created_at, submitted_at FROM performance_reviews ORDER BY review_id",
        row_fn=_row_fn_none,
        pk="review_id",
    ),
    dict(
        table="feedback_360",
        columns=["feedback_id", "emp_id", "reviewer_id", "category", "rating", "comment", "submitted_at"],
        select="SELECT feedback_id, emp_id, reviewer_id, category, rating, comment, submitted_at FROM feedback_360 ORDER BY feedback_id",
        row_fn=_row_fn_none,
        pk="feedback_id",
    ),
    dict(
        table="expense_claims",
        columns=["claim_id", "emp_id", "cat_id", "amount", "description", "receipt_path", "status", "approved_by", "paid_at", "created_at"],
        select="SELECT claim_id, emp_id, cat_id, amount, description, receipt_path, status, approved_by, created_at FROM expense_claims ORDER BY claim_id",
        row_fn=lambda r: (*r[:8], None, r[8]),
        pk="claim_id",
    ),
    dict(
        table="tickets",
        columns=["ticket_id", "emp_id", "subject", "description", "queue", "category", "priority", "status", "assigned_to", "created_at", "updated_at", "resolved_at"],
        select="SELECT ticket_id, emp_id, subject, description, category, priority, status, assigned_to, created_at, updated_at, resolved_at FROM tickets ORDER BY ticket_id",
        row_fn=lambda r: (r[0], r[1], r[2], r[3], r[4] or "IT", *r[4:]),
        pk="ticket_id",
    ),
    dict(
        table="ticket_comments",
        columns=["comment_id", "ticket_id", "emp_id", "comment", "created_at"],
        select="SELECT comment_id, ticket_id, emp_id, comment, created_at FROM ticket_comments ORDER BY comment_id",
        row_fn=_row_fn_none,
        pk="comment_id",
    ),
    dict(
        table="documents",
        columns=["doc_id", "emp_id", "name", "category", "file_path", "file_size", "uploaded_at"],
        select="SELECT doc_id, emp_id, name, category, file_path, file_size, uploaded_at FROM documents ORDER BY doc_id",
        row_fn=_row_fn_none,
        pk="doc_id",
    ),
]


# ───────────────────────────────────────────────────────────────────────────
# Cleanup rules for the v2.0 constraints (SRS §14 Phase 1, "manual cleanup pass")
# ───────────────────────────────────────────────────────────────────────────

CLEANUP_RULES = {
    "dup_pending_break_approval": (
        "uq_pending_lunch_approval (FR-ATT-05): one Pending break approval per "
        "employee/break_type/date — later duplicates become Rejected"
    ),
    "dup_pending_regularization": (
        "uq_pending_regularization (FR-REG-02): one Pending regularization per "
        "employee/date — later duplicates become Cancelled"
    ),
    "overlapping_leave": (
        "no_overlapping_leave (FR-LEA-02): overlapping Pending/Approved leaves — "
        "earliest is kept, later overlapping requests become Cancelled"
    ),
    "dup_leave_balance": (
        "uq_balance (FR-LEA-06): duplicate balance rows are merged into the "
        "earliest row and removed"
    ),
    "dup_holiday": (
        "duplicate holidays (name+date+location): earliest row kept, exact "
        "duplicates removed"
    ),
    "multi_active_break": (
        "uq_one_active_break (FR-ATT-02): one Active break per employee — the "
        "latest start is kept Active, stale rows become Orphaned"
    ),
    "overlap_salary_structure": (
        "no_overlapping_structure (FR-PAY-02): v1.0 never closed a prior salary "
        "structure when a new one started (A-10). Earlier open-ended/overlapping "
        "rows are closed at (next effective_from − 1 day); the latest stays open."
    ),
    "dup_payroll_run": (
        "uq_payroll_period (FR-PAY-05): duplicate runs for the same month/year — "
        "earliest kept, later runs become Cancelled"
    ),
    "dup_active_offer": (
        "uq_active_offer_candidate (FR-ATS-04): one active offer per candidate — "
        "an Accepted offer wins, then the lowest offer_id wins ties; later rows "
        "are removed"
    ),
    "dup_email": (
        "uq_users_email_ci (FR-USR-02): duplicate email addresses — REPORT ONLY, "
        "needs a human decision (rename or merge) before Part B can be applied"
    ),
}


def _execute_cleanup(pg, ledger, apply: bool) -> list[str]:
    """Detect (and optionally fix) data that violates the v2.0 constraints.

    Returns blocking manual-review messages (things --apply-cleanup cannot fix).
    """
    blockers: list[str] = []

    def record(rule: str, table: str, rows: int, applied: bool):
        if rows:
            ledger.append(dict(rule=rule, table=table, rows=rows, applied=applied))

    def run(sql_rule: str, table: str, count_sql: str, fix_sql: str):
        rows = pg.execute(text(count_sql)).scalar() or 0
        if rows:
            record(sql_rule, table, rows, apply)
            if apply:
                pg.execute(text(fix_sql))

    # 1. duplicate Pending break approvals → keep earliest (created_at, approval_id)
    run(
        "dup_pending_break_approval", "break_approvals",
        count_sql="""
            WITH ranked AS (
                SELECT approval_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY emp_id, break_type, break_date
                           ORDER BY created_at, approval_id
                       ) AS rn
                FROM break_approvals WHERE status = 'Pending'
            ) SELECT COUNT(*) FROM ranked WHERE rn > 1
        """,
        fix_sql="""
            WITH ranked AS (
                SELECT approval_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY emp_id, break_type, break_date
                           ORDER BY created_at, approval_id
                       ) AS rn
                FROM break_approvals WHERE status = 'Pending'
            )
            UPDATE break_approvals b
               SET status = 'Rejected'
              FROM ranked r
             WHERE b.approval_id = r.approval_id AND r.rn > 1
        """,
    )

    # 2. duplicate Pending regularizations → keep earliest
    run(
        "dup_pending_regularization", "regularization_requests",
        count_sql="""
            WITH ranked AS (
                SELECT request_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY emp_id, request_date
                           ORDER BY created_at, request_id
                       ) AS rn
                FROM regularization_requests WHERE status = 'Pending'
            ) SELECT COUNT(*) FROM ranked WHERE rn > 1
        """,
        fix_sql="""
            WITH ranked AS (
                SELECT request_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY emp_id, request_date
                           ORDER BY created_at, request_id
                       ) AS rn
                FROM regularization_requests WHERE status = 'Pending'
            )
            UPDATE regularization_requests r
               SET status = 'Cancelled', updated_at = now()
              FROM ranked k
             WHERE r.request_id = k.request_id AND k.rn > 1
        """,
    )

    # 3. overlapping Pending/Approved leaves → greedy keep-earliest, cancel the rest
    leaves = pg.execute(text("""
        SELECT leave_id, emp_id, start_date, end_date, created_at
        FROM leave_requests
        WHERE status IN ('Pending', 'Approved')
        ORDER BY emp_id, created_at, leave_id
    """)).fetchall()
    kept: list[tuple] = []
    overlaps: set[int] = set()
    per_emp: dict[str, list[tuple]] = {}
    for lid, emp, start, end, _c in leaves:
        per_emp.setdefault(emp, []).append((lid, start, end))
    for _emp, rows in per_emp.items():
        accepted: list[tuple] = []
        for lid, start, end in rows:
            clash = any(not (end < a_start or start > a_end) for _, a_start, a_end in accepted)
            if clash:
                overlaps.add(lid)
            else:
                accepted.append((lid, start, end))
    if overlaps:
        record("overlapping_leave", "leave_requests", len(overlaps), apply)
        if apply:
            pg.execute(
                text(
                    "UPDATE leave_requests SET status = 'Cancelled', updated_at = now() "
                    "WHERE leave_id = ANY(:ids)"
                ),
                {"ids": sorted(overlaps)},
            )
    del kept

    # 4. duplicate leave_balance rows → merge into earliest, delete the rest
    dup_balances = pg.execute(text("""
        SELECT leave_type, year,
               array_agg(balance_id ORDER BY balance_id) AS ids,
               SUM(total_days), SUM(used_days)
        FROM leave_balance
        GROUP BY emp_id, leave_type, year
        HAVING COUNT(*) > 1
        ORDER BY 1, 2
    """)).fetchall()
    if dup_balances:
        record("dup_leave_balance", "leave_balance", sum(len(r[2]) - 1 for r in dup_balances), apply)
        if apply:
            for _lt, _yr, ids, total, used in dup_balances:
                keeper, *rest = ids
                pg.execute(text(
                    "UPDATE leave_balance SET total_days = :t, used_days = :u WHERE balance_id = :id"
                ), {"t": int(total), "u": int(used), "id": keeper})
                pg.execute(text("DELETE FROM leave_balance WHERE balance_id = ANY(:ids)"), {"ids": rest})

    # 5. exact duplicate holidays → keep earliest holiday_id
    dup_holidays = pg.execute(text("""
        SELECT array_agg(holiday_id ORDER BY holiday_id) AS ids
        FROM holidays
        GROUP BY name, holiday_date, COALESCE(location, '')
        HAVING COUNT(*) > 1
    """)).fetchall()
    if dup_holidays:
        record("dup_holiday", "holidays", sum(len(r[0]) - 1 for r in dup_holidays), apply)
        if apply:
            for (ids,) in dup_holidays:
                pg.execute(text("DELETE FROM holidays WHERE holiday_id = ANY(:ids)"), {"ids": list(ids)[1:]})

    # 6. multiple Active breaks per employee → keep latest start
    run(
        "multi_active_break", "breaks",
        count_sql="""
            WITH ranked AS (
                SELECT break_id,
                       ROW_NUMBER() OVER (PARTITION BY emp_id ORDER BY start_time DESC, break_id DESC) AS rn
                FROM breaks WHERE status = 'Active'
            ) SELECT COUNT(*) FROM ranked WHERE rn > 1
        """,
        fix_sql="""
            WITH ranked AS (
                SELECT break_id,
                       ROW_NUMBER() OVER (PARTITION BY emp_id ORDER BY start_time DESC, break_id DESC) AS rn
                FROM breaks WHERE status = 'Active'
            )
            UPDATE breaks b
               SET status = 'Orphaned', ended_reason = 'migration_dedupe'
              FROM ranked r
             WHERE b.break_id = r.break_id AND r.rn > 1
        """,
    )

    # 7. duplicate payroll runs for same period → keep earliest, cancel later ones
    run(
        "dup_payroll_run", "payroll_runs",
        count_sql="""
            WITH ranked AS (
                SELECT run_id,
                       ROW_NUMBER() OVER (PARTITION BY month, year ORDER BY processed_at, run_id) AS rn
                FROM payroll_runs WHERE status <> 'Cancelled'
            ) SELECT COUNT(*) FROM ranked WHERE rn > 1
        """,
        fix_sql="""
            WITH ranked AS (
                SELECT run_id,
                       ROW_NUMBER() OVER (PARTITION BY month, year ORDER BY processed_at, run_id) AS rn
                FROM payroll_runs WHERE status <> 'Cancelled'
            )
            UPDATE payroll_runs p
               SET status = 'Cancelled'
              FROM ranked r
             WHERE p.run_id = r.run_id AND r.rn > 1
        """,
    )

    # 8. duplicate active offers per candidate → retain one deterministically.
    #    Prefer an Accepted row, then the lowest offer_id.  This mirrors the
    #    lifecycle migration and removes only the later conflicting records
    #    before the partial unique index is applied in PART B.
    run(
        "dup_active_offer", "offer_letters",
        count_sql="""
            WITH ranked AS (
                SELECT offer_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY candidate_id
                           ORDER BY CASE WHEN status = 'Accepted' THEN 0 ELSE 1 END,
                                    offer_id
                       ) AS rn
                FROM offer_letters
                WHERE status IN ('Pending', 'Accepted')
            ) SELECT COUNT(*) FROM ranked WHERE rn > 1
        """,
        fix_sql="""
            WITH ranked AS (
                SELECT offer_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY candidate_id
                           ORDER BY CASE WHEN status = 'Accepted' THEN 0 ELSE 1 END,
                                    offer_id
                       ) AS rn
                FROM offer_letters
                WHERE status IN ('Pending', 'Accepted')
            )
            DELETE FROM offer_letters o
             USING ranked r
             WHERE o.offer_id = r.offer_id AND r.rn > 1
        """,
    )

    # 9. overlapping/open-ended salary structures per employee (A-10) → close
    #    earlier rows at the next effective_from − 1 day; latest stays open.
    structures = pg.execute(text("""
        SELECT struct_id, emp_id, effective_from, effective_to
        FROM salary_structures
        ORDER BY emp_id, effective_from, struct_id
    """)).fetchall()
    from collections import defaultdict

    per_emp: dict[str, list] = defaultdict(list)
    for sid, emp, ef, et in structures:
        per_emp[emp].append((sid, ef, et))
    close_at: dict[int, date] = {}
    for _emp, rows in per_emp.items():
        for i in range(len(rows) - 1):
            sid, ef, et = rows[i]
            next_ef = rows[i + 1][1]
            if et is None or et >= next_ef:  # overlaps (or open-ended and not latest)
                close_at[sid] = next_ef - timedelta(days=1)
    if close_at:
        record("overlap_salary_structure", "salary_structures", len(close_at), apply)
        if apply:
            for sid, closed_on in close_at.items():
                pg.execute(
                    text("UPDATE salary_structures SET effective_to = :d WHERE struct_id = :id"),
                    {"d": closed_on, "id": sid},
                )

    # 10. duplicate emails (case-insensitive) → report only, blocks Part B
    dup_emails = pg.execute(text("""
        SELECT lower(email), array_agg(emp_id ORDER BY emp_id)
        FROM users
        GROUP BY lower(email)
        HAVING COUNT(*) > 1
    """)).fetchall()
    if dup_emails:
        record("dup_email", "users", sum(len(r[1]) - 1 for r in dup_emails), False)
        for email, emp_ids in dup_emails:
            blockers.append(
                f"users: duplicate email {email!r} held by {', '.join(emp_ids)} — "
                "resolve manually (HR decision) before Part B can be applied."
            )

    return blockers


# ───────────────────────────────────────────────────────────────────────────
# Main
# ───────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="DuckDB → PostgreSQL phase-1 ETL with reconciliation")
    ap.add_argument("--apply-cleanup", action="store_true",
                    help="apply (not just report) the v2.0 constraint cleanup rules")
    ap.add_argument("--reset", action="store_true",
                    help="drop and recreate the target schema first (DANGER: destroys target data)")
    ap.add_argument("--no-stamp", action="store_true",
                    help="do not record the migrated schema in the Alembic version table")
    ap.add_argument("--duckdb-file", default=os.getenv("DUCKDB_FILE", "hrms.duckdb"))
    ap.add_argument("--database-url", default=os.getenv(
        "DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:55432/hrms"))
    args = ap.parse_args()

    duck_path = Path(args.duckdb_file)
    if not duck_path.exists():
        print(f"ERROR: source DuckDB file not found: {duck_path}", file=sys.stderr)
        print("       (seed it by running: timeout 10s python -c \"import app\")", file=sys.stderr)
        return 3
    if not SCHEMA_FILE.exists():
        print(f"ERROR: target schema not found: {SCHEMA_FILE}", file=sys.stderr)
        return 3

    sql_full = SCHEMA_FILE.read_text(encoding="utf-8")
    if PART_B_MARKER not in sql_full:
        print(f"ERROR: PART B marker missing in {SCHEMA_FILE}", file=sys.stderr)
        return 3
    part_a, part_b = sql_full.split(PART_B_MARKER, 1)
    # The file wraps everything in BEGIN/COMMIT; the ETL runs each phase in its
    # own transaction instead, so strip the outer transaction statements.
    part_a = part_a.replace("BEGIN;", "").strip()
    part_b = ("--  PART B" + part_b).replace("COMMIT;", "").strip()

    # Identifiers that need quoting (reserved words in PostgreSQL).
    def qi(col: str) -> str:
        return f'"{col}"'

    engine = create_engine(args.database_url)
    src = duckdb.connect(str(duck_path), read_only=True)
    source_catalog = _source_catalog(src)

    report: dict = {
        "started_at": datetime.now(UTC).isoformat(),
        "source": str(duck_path),
        "target": args.database_url.split("@")[-1],  # redact credentials
        "mode": "apply-cleanup" if args.apply_cleanup else "report-only",
        "tables": {},
        "cleanup": [],
        "phase1": {},
        "part_b": {},
        "phase2": {},
    }

    with engine.begin() as pg:
        if args.reset:
            print("--reset: dropping target schema public ...")
            pg.execute(text("DROP SCHEMA public CASCADE"))
            pg.execute(text("CREATE SCHEMA public"))
        print("Applying PART A schema (tables) ...")
        for stmt in split_sql_statements(part_a):
            pg.execute(text(stmt))
        pg.commit()

    loaded_rows: dict[str, list[tuple]] = {}
    skipped_orphans: dict[str, list] = {}
    fk_nulls: dict[str, dict[str, int]] = {}

    with engine.begin() as pg:
        for entry in REGISTRY:
            table, columns, row_fn = entry["table"], entry["columns"], entry["row_fn"]
            source_present = table in source_catalog
            if entry.get("source_optional") and not source_present:
                raw = []
            else:
                select = _source_select(entry, source_catalog.get(table, set()))
                raw = src.execute(select).fetchall()
            rows = [tuple(to_pg(v) for v in row_fn(r)) for r in raw]
            assert all(len(r) == len(columns) for r in rows), f"{table}: row/column length mismatch"

            # FK guard: parents for this table, read from the TARGET (parent rows
            # already loaded there). Self-referencing tables use this batch.
            parent_keys: dict[str, set] = {}
            skipped, nulls = [], {}
            for col, parent, nullable in FKS.get(table, []):
                if parent == table:
                    pk_idx = columns.index(entry["pk"])
                    parent_keys[col] = {r[pk_idx] for r in rows}
                elif parent not in parent_keys:
                    parent_keys[col] = {
                        r[0] for r in pg.execute(
                            text(f"SELECT {qi(PARENT_KEY[parent])} FROM {parent}")
                        ).fetchall()
                    }

            filtered = []
            for r in rows:
                drop = False
                for col, _parent, nullable in FKS.get(table, []):
                    v = r[columns.index(col)]
                    if v is None or v in parent_keys[col]:
                        continue
                    if nullable:
                        nulls[col] = nulls.get(col, 0) + 1
                        r = list(r)
                        r[columns.index(col)] = None
                        r = tuple(r)
                    else:
                        skipped.append(dict(row=repr(r[:3]), column=col, missing=repr(v)))
                        drop = True
                        break
                if not drop:
                    filtered.append(r)

            if skipped:
                skipped_orphans[table] = skipped
            if nulls:
                fk_nulls[table] = nulls

            if filtered:
                col_sql = ", ".join(qi(c) for c in columns)
                placeholders = ", ".join(["%s"] * len(columns))
                insert_sql = f'INSERT INTO {table} ({col_sql}) VALUES ({placeholders})'
                raw_conn = pg.connection.dbapi_connection
                cur = raw_conn.cursor()
                try:
                    cur.executemany(insert_sql, filtered)
                finally:
                    cur.close()

            loaded_rows[table] = filtered
            report["tables"][table] = dict(
                source_table_present=source_present,
                source_count=len(raw),
                loaded_count=len(filtered),
                skipped_orphans=len(skipped),
                fk_nulls=nulls,
            )
            print(f"  {table:<26} source={len(raw):>5}  loaded={len(filtered):>5}"
                  + (f"  skipped={len(skipped)}" if skipped else "")
                  + (f"  fk_nulled={nulls}" if nulls else ""))
        pg.commit()

    # ── Phase-1 reconciliation (pre-cleanup): loaded rows vs target rows ────
    print("\nPhase-1 reconciliation (before cleanup) ...")
    phase1_ok = True
    with engine.connect() as pg:
        for entry in REGISTRY:
            table, columns = entry["table"], entry["columns"]
            col_sql = ", ".join(qi(c) for c in columns)
            target_rows = pg.execute(text(f"SELECT {col_sql} FROM {table}")).fetchall()
            src_ck = checksum(loaded_rows[table])
            tgt_ck = checksum(target_rows)
            match = src_ck == tgt_ck and len(loaded_rows[table]) == len(target_rows)
            report["phase1"][table] = dict(source=src_ck, target=tgt_ck, match=match)
            if not match:
                phase1_ok = False
                print(f"  MISMATCH {table}: loaded={len(loaded_rows[table])} target={len(target_rows)}")
                print(f"           src_ck={src_ck}  tgt_ck={tgt_ck}")
    if not phase1_ok:
        print("PHASE 1 FAILED — data does not survive the ETL unchanged. Investigate before cleanup.",
              file=sys.stderr)
        _write_report(report)
        return 1
    print("  all tables match ✓")
    if skipped_orphans or fk_nulls:
        print(f"  FK handling: skipped_rows={ {k: len(v) for k, v in skipped_orphans.items()} }"
              f"  nulled={fk_nulls}")

    # ── Cleanup + PART B ────────────────────────────────────────────────────
    print(f"\nConstraint cleanup ({report['mode']}) ...")
    with engine.begin() as pg:
        blockers = _execute_cleanup(pg, report["cleanup"], apply=args.apply_cleanup)
        pg.commit()
    for entry in report["cleanup"]:
        state = "applied" if entry["applied"] else "detected (dry-run)"
        print(f"  [{state}] {entry['table']}: {entry['rows']} rows — {CLEANUP_RULES[entry['rule']]}")
    if blockers:
        for b in blockers:
            print(f"  [BLOCKED] {b}", file=sys.stderr)

    pending = [e for e in report["cleanup"] if not e["applied"]]
    if pending and not blockers:
        print("\nConstraint-violating data detected. Re-run with --apply-cleanup to fix it "
              "(or fix manually), then re-run to apply PART B.", file=sys.stderr)
    elif blockers:
        print("\nManual-review items block PART B. Resolve them, then re-run.", file=sys.stderr)

    part_b_ok = False
    if not pending and not blockers:
        print("\nApplying PART B (CC-05 invariants) ...")
        try:
            with engine.begin() as pg:
                for stmt in split_sql_statements(part_b):
                    pg.execute(text(stmt))
                pg.commit()
            part_b_ok = True
            report["part_b"] = dict(status="applied")
            print("  PART B applied ✓")
        except Exception as exc:  # noqa: BLE001 — surface the exact constraint
            report["part_b"] = dict(status="failed", error=str(exc))
            print(f"  PART B FAILED: {exc}", file=sys.stderr)
            print("  Re-run with --apply-cleanup (or resolve the offending data manually).",
                  file=sys.stderr)
            _write_report(report)
            return 2

    # ── Phase-2 reconciliation (post-cleanup) + sequence advance ────────────
    print("\nPhase-2 reconciliation (after cleanup/constraints) ...")
    with engine.begin() as pg:
        phase2 = {}
        for entry in REGISTRY:
            table = entry["table"]
            target_count = pg.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            expected = len(loaded_rows[table])
            # Rows may legitimately differ only by recorded cleanup actions.
            cleanup_delta = sum(
                e["rows"] for e in report["cleanup"] if e["table"] == table and e["applied"]
            )
            phase2[table] = dict(
                after_cleanup=target_count,
                loaded=expected,
                cleanup_rows_touched=cleanup_delta,
            )
            print(f"  {table:<26} final={target_count:>5}  (loaded={expected}, cleanup_rows={cleanup_delta})")

        # Advance identity sequences so future runtime inserts can't collide
        # with migrated legacy ids (pairs with the later CC-01 pass).
        seq_sql = """
            SELECT format(
                'SELECT setval(pg_get_serial_sequence(%L, %L), COALESCE(MAX(%I), 0) + 1, false) FROM %I;',
                c.table_name, c.column_name, c.column_name, c.table_name
            )
            FROM information_schema.columns c
            WHERE c.table_schema = 'public'
              AND c.is_identity = 'YES'
              AND c.identity_generation = 'BY DEFAULT'
            ORDER BY c.table_name
        """
        for (stmt,) in pg.execute(text(seq_sql)).fetchall():
            pg.execute(text(stmt))
        pg.commit()
    report["phase2"] = phase2

    report["finished_at"] = datetime.now(UTC).isoformat()
    report["status"] = "ok" if part_b_ok else "constraints_pending"

    if part_b_ok and not args.no_stamp:
        if command is None:
            print("WARNING: alembic not installed — skipping `alembic stamp head`. Install it from requirements.txt.",
                  file=sys.stderr)
        else:
            try:
                cfg = Config(str(REPO_ROOT / "migrations" / "alembic.ini"))
                cfg.set_main_option("sqlalchemy.url", args.database_url)
                command.stamp(cfg, "head")
                report["alembic"] = {"status": "stamped", "revision": "head"}
                print("Stamped target as Alembic `head` ✓")
            except Exception as exc:  # noqa: BLE001
                report["alembic"] = {"status": "stamp_failed", "error": str(exc)}
                print(f"WARNING: could not stamp Alembic version: {exc}", file=sys.stderr)

    path = _write_report(report)

    print(f"\nResult: {'FULL SUCCESS' if part_b_ok else 'LOADED + RECONCILED, constraints pending'}")
    print(f"Report: {path}")
    return 0 if part_b_ok else 2


def _write_report(report: dict) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = REPORT_DIR / f"migration_report_{ts}.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return path


if __name__ == "__main__":
    sys.exit(main())
