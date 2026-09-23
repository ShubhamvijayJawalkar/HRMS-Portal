#!/usr/bin/env python3
"""CC-01 rule checker — DB-generated identity keys (SRS v2.0 §14 Phase 3b).

Asserts the cross-cutting key invariant for the v2.0 target schema:

  * every *surrogate* primary key is a real PostgreSQL identity column
    (``BY DEFAULT`` today so migrated legacy IDs load; ``ALWAYS`` after the
    service-layer flip);
  * the only non-identity PKs allowed are explicit NATURAL keys and the
    Alembic bookkeeping table;
  * every identity sequence sits ahead of its table's ``MAX(id)`` — the ETL
    advances sequences with ``setval`` (scripts/migrate_duckdb_to_postgres.py)
    and nothing may regress that (a runtime insert would then collide).

Usage:
    APP_DB=postgres DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/hrms \
        python scripts/check_cc_rules.py [--schema public]

Exit code 0 = compliant, 1 = violations found, 2 = usage/connection error.
"""

from __future__ import annotations

import argparse
import os
import sys

import psycopg

# Natural (business) key PKs — allowed to be non-identity by design.
NATURAL_KEYS = {
    ("users", "emp_id"),               # business key (employee id)
    ("break_types", "break_type"),     # business key (break type name)
    ("idempotency_keys", "key"),       # CC-07: caller-supplied idempotency key
    ("alembic_version", "version_num"),# Alembic bookkeeping
}

_QUERY_PKS = """
    SELECT t.relname, a.attname, a.attidentity
    FROM pg_class t
    JOIN pg_namespace n ON n.oid = t.relnamespace AND n.nspname = %(schema)s
    JOIN pg_index i ON i.indrelid = t.oid AND i.indisprimary
    JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(i.indkey)
    WHERE t.relkind = 'r'
    ORDER BY t.relname, a.attnum
"""

_IDENTITY_TAG = {"a": "ALWAYS", "d": "BY DEFAULT"}


def _dsn() -> str:
    url = os.getenv("DATABASE_URL") or "postgresql+psycopg://postgres:postgres@localhost:5432/hrms"
    return url.replace("postgresql+psycopg://", "postgresql://")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=_dsn())
    parser.add_argument("--schema", default="public")
    args = parser.parse_args()

    problems: list[str] = []
    try:
        with psycopg.connect(args.dsn, autocommit=True) as conn:
            pks = conn.execute(_QUERY_PKS, {"schema": args.schema}).fetchall()

            if not pks:
                print(f"!! no primary keys found in schema '{args.schema}'")
                return 1

            identity = [r for r in pks if r[2] in _IDENTITY_TAG]
            non_identity = [r for r in pks if r[2] not in _IDENTITY_TAG]
            unexpected = [
                (t, c) for t, c, _ in non_identity
                if (t, c) not in NATURAL_KEYS
            ]
            if unexpected:
                problems.append(
                    "non-identity surrogate PKs: " + ", ".join(f"{t}.{c}" for t, c in unexpected)
                )

            print(f"schema '{args.schema}': {len(pks)} primary keys "
                  f"({len(identity)} identity, {len(non_identity)} natural)")

            for t, c, i in non_identity:
                tag = {"a": "ALWAYS", "d": "BY DEFAULT"}.get(i, "none")
                note = " (natural key)" if (t, c) in NATURAL_KEYS else "  <-- UNEXPECTED"
                print(f"  natural PK  {t}.{c}  [{tag}]{note}")
            for t, c, i in identity:
                print(f"  identity    {t}.{c}  [{_IDENTITY_TAG[i]}]")

            # -- Sequences must sit ahead of the highest migrated id -------
            for t, c, i in identity:
                seq = conn.execute(
                    "SELECT pg_get_serial_sequence(%s, %s)", (f"{args.schema}.{t}", c)
                ).fetchone()
                if not seq or not seq[0]:
                    problems.append(f"{t}.{c}: no backing sequence found")
                    continue
                last = conn.execute(f"SELECT last_value FROM {seq[0]}").fetchone()[0]
                high = conn.execute(
                    f"SELECT COALESCE(MAX({c}), 0) FROM {args.schema}.{t}"
                ).fetchone()[0]
                if last < high:
                    problems.append(
                        f"{t}.{c}: sequence ({last}) behind MAX({c}) ({high}) — "
                        "run setval or OVERRIDING SYSTEM VALUE, a runtime insert would collide"
                    )
                elif last == high:
                    print(f"  seq ok      {t}.{c}: last_value == MAX = {high}")
                else:
                    print(f"  seq ok      {t}.{c}: last_value {last} > MAX {high}")

    except psycopg.Error as exc:
        print(f"!! database error: {exc}")
        return 2

    if problems:
        print("\nCC-01 VIOLATIONS:")
        for p in problems:
            print(f"  ✗ {p}")
        return 1

    print("\nCC-01 OK — every surrogate key is identity; sequences ahead of data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())