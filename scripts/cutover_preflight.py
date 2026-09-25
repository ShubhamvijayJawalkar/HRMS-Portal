#!/usr/bin/env python3
"""Read-only Phase 5 cutover preflight for the PostgreSQL ``public`` schema.

The final cutover is intentionally an explicit operations action. This script
only inspects the target/legacy schemas and emits a report; it never changes
data, drops a schema, or switches traffic.

Typical maintenance-window sequence::

    alembic -c migrations/alembic.ini upgrade head
    python scripts/cutover_preflight.py --require-legacy-read-only \
        --duckdb-file /data/hrms.duckdb --report reports/cutover-preflight.json

The operator then starts the public image, performs the health check, and
switches traffic only after the report has no failed checks. The DuckDB file
and legacy schema remain available for the defined audit-fallback period.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NATURAL_KEYS = {
    ("users", "emp_id"),
    ("break_types", "break_type"),
    ("idempotency_keys", "key"),
    ("alembic_version", "version_num"),
}
_REQUIRED_PUBLIC_TABLES = {
    "users",
    "job_postings",
    "candidates",
    "offer_letters",
    "onboarding_workflow",
    "onboarding_checklist",
    "resignations",
    "offboarding_workflow",
    "offboarding_approvals",
    "offboarding_settlements",
    "payroll_runs",
    "payroll_items",
    "attendance_days",
    "outbox_events",
}
_COMMON_TABLES = (
    "users",
    "job_postings",
    "candidates",
    "interviews",
    "offer_letters",
    "payroll_runs",
    "payroll_items",
    "leave_requests",
    "attendance_days",
)


def _psycopg_dsn(value: str) -> str:
    return re.sub(r"^(postgres(?:ql)?)\+[A-Za-z0-9_.]+", r"\1", value)


def _validate_schema(schema: str) -> str:
    if not _IDENTIFIER.fullmatch(schema):
        raise ValueError(f"invalid schema name: {schema!r}")
    return schema


def _qualified(schema: str, table: str) -> str:
    return f'"{_validate_schema(schema)}"."{_validate_schema(table)}"'


def _table_names(conn: psycopg.Connection, schema: str) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_type = 'BASE TABLE'",
            (schema,),
        ).fetchall()
    }


def _table_count(conn: psycopg.Connection, schema: str, table: str) -> int | None:
    if table not in _table_names(conn, schema):
        return None
    return int(conn.execute(f"SELECT COUNT(*) FROM {_qualified(schema, table)}").fetchone()[0])


def _identity_violations(conn: psycopg.Connection, schema: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT t.relname, a.attname
        FROM pg_class t
        JOIN pg_namespace n ON n.oid = t.relnamespace AND n.nspname = %s
        JOIN pg_index i ON i.indrelid = t.oid AND i.indisprimary
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(i.indkey)
        WHERE t.relkind = 'r' AND a.attidentity = ''
        ORDER BY t.relname, a.attname
        """,
        (schema,),
    ).fetchall()
    return [
        f"{table}.{column}"
        for table, column in rows
        if (table, column) not in _NATURAL_KEYS
    ]


def _sequence_violations(conn: psycopg.Connection, schema: str) -> list[str]:
    """Return identity sequences that are behind explicit legacy IDs."""
    rows = conn.execute(
        """
        SELECT t.relname, a.attname
        FROM pg_class t
        JOIN pg_namespace n ON n.oid = t.relnamespace AND n.nspname = %s
        JOIN pg_index i ON i.indrelid = t.oid AND i.indisprimary
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(i.indkey)
        WHERE t.relkind = 'r' AND a.attidentity IN ('a', 'd')
        ORDER BY t.relname, a.attname
        """,
        (schema,),
    ).fetchall()
    violations: list[str] = []
    for table, column in rows:
        sequence = conn.execute(
            "SELECT pg_get_serial_sequence(%s, %s)",
            (f"{schema}.{table}", column),
        ).fetchone()[0]
        if not sequence:
            violations.append(f"{table}.{column}: backing sequence missing")
            continue
        last_value = int(conn.execute(f"SELECT last_value FROM {sequence}").fetchone()[0])
        max_value = int(conn.execute(
            f"SELECT COALESCE(MAX({column}), 0) FROM {_qualified(schema, table)}"
        ).fetchone()[0])
        if last_value < max_value:
            violations.append(
                f"{table}.{column}: sequence {last_value} is behind MAX {max_value}"
            )
    return violations


def _alembic_version(conn: psycopg.Connection, schema: str) -> str | None:
    if "alembic_version" not in _table_names(conn, schema):
        return None
    row = conn.execute(
        f"SELECT version_num FROM {_qualified(schema, 'alembic_version')} LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def _read_only_file(path: str | None) -> dict[str, Any]:
    if not path:
        return {"checked": False, "ready": None, "path": None}
    file_path = Path(path)
    if not file_path.exists():
        return {"checked": True, "ready": False, "path": str(file_path), "reason": "missing"}
    mode = file_path.stat().st_mode
    writable = bool(mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    return {
        "checked": True,
        "ready": not writable,
        "path": str(file_path),
        "mode": oct(stat.S_IMODE(mode)),
        "writable": writable,
    }


def _schema_report(conn: psycopg.Connection, schema: str) -> dict[str, Any]:
    schema = _validate_schema(schema)
    tables = _table_names(conn, schema)
    counts = {table: _table_count(conn, schema, table) for table in _COMMON_TABLES}
    return {
        "schema": schema,
        "exists": bool(tables),
        "alembic_version": _alembic_version(conn, schema),
        "table_count": len(tables),
        "counts": counts,
        "identity_violations": _identity_violations(conn, schema) if tables else [],
        "sequence_violations": _sequence_violations(conn, schema) if tables else [],
    }


def _run(args: argparse.Namespace) -> int:
    target = _validate_schema(args.schema)
    legacy = _validate_schema(args.legacy_schema)
    failures: list[str] = []
    with psycopg.connect(_psycopg_dsn(args.database_url), autocommit=True) as conn:
        public = _schema_report(conn, target)
        legacy_report = _schema_report(conn, legacy) if legacy != target else None
        public_tables = set(
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = %s",
                (target,),
            ).fetchall()
        )
        missing = sorted(_REQUIRED_PUBLIC_TABLES - public_tables)
        if missing:
            failures.append(f"public missing required tables: {', '.join(missing)}")
        if public["alembic_version"] != args.expected_head:
            failures.append(
                f"public Alembic version is {public['alembic_version']!r}; "
                f"expected {args.expected_head!r}"
            )
        if public["identity_violations"]:
            failures.append(
                "public has non-identity surrogate keys: "
                + ", ".join(public["identity_violations"])
            )
        if public["sequence_violations"]:
            failures.append(
                "public identity sequences are behind data: "
                + ", ".join(public["sequence_violations"])
            )
        null_splits = conn.execute(
            f"SELECT COUNT(*) FROM {_qualified(target, 'offer_letters')} "
            "WHERE basic_pct IS NULL OR hra_pct IS NULL OR allowances_pct IS NULL"
        ).fetchone()[0] if "offer_letters" in public_tables else None
        if null_splits:
            failures.append(f"public has {null_splits} offer rows with NULL percentage splits")
        active_duplicates = conn.execute(
            f"SELECT COUNT(*) FROM ("
            f"SELECT candidate_id FROM {_qualified(target, 'offer_letters')} "
            "WHERE status IN ('Pending', 'Accepted') GROUP BY candidate_id HAVING COUNT(*) > 1"
            ") AS duplicates"
        ).fetchone()[0] if "offer_letters" in public_tables else None
        if active_duplicates:
            failures.append(f"public has {active_duplicates} candidates with multiple active offers")

    duckdb = _read_only_file(args.duckdb_file)
    deltas = _count_deltas(public, legacy_report)
    if args.require_legacy_read_only and duckdb["ready"] is not True:
        failures.append("DuckDB fallback is not present and read-only")
    if args.fail_on_count_delta:
        changed = [table for table, values in deltas.items() if values["delta"]]
        if changed:
            failures.append("public/legacy row-count deltas require review: " + ", ".join(changed))

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": public,
        "legacy": legacy_report,
        "duckdb_fallback": duckdb,
        "count_deltas": deltas,
        "failures": failures,
        "ready": not failures,
    }
    encoded = json.dumps(report, indent=2, sort_keys=True, default=str)
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if not failures else 1


def _count_deltas(public: dict[str, Any], legacy: dict[str, Any] | None) -> dict[str, Any]:
    if not legacy:
        return {}
    result: dict[str, Any] = {}
    for table in _COMMON_TABLES:
        public_count = public["counts"].get(table)
        legacy_count = legacy["counts"].get(table)
        if public_count is not None and legacy_count is not None:
            result[table] = {
                "public": public_count,
                "legacy": legacy_count,
                "delta": public_count - legacy_count,
            }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:55432/hrms"),
        help="PostgreSQL DSN (default: DATABASE_URL)",
    )
    parser.add_argument("--schema", default="public", help="cutover target schema")
    parser.add_argument("--legacy-schema", default="legacy", help="fallback schema to compare")
    parser.add_argument(
        "--expected-head",
        default="0003_lifecycle_hardening",
        help="required Alembic head on the target schema",
    )
    parser.add_argument("--duckdb-file", help="optional legacy DuckDB fallback path")
    parser.add_argument("--require-legacy-read-only", action="store_true")
    parser.add_argument(
        "--fail-on-count-delta",
        action="store_true",
        help="fail when common-table public/legacy counts differ",
    )
    parser.add_argument("--report", help="write the JSON report to this path")
    args = parser.parse_args()
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
