#!/usr/bin/env python3
"""Public-flip readiness probe (Phase 3b / CC-01).

Measures how much of the *existing* v1.0 app already works against the pure
v2.0 target schema (``public``) on a throwaway database — a quantified
cutover backlog instead of a leap of faith.

What it does
------------
1. Builds/uses the v2.0 ``public`` schema (run ``alembic upgrade head`` first).
2. Pre-seeds ``users``/``leave_balance`` with correct v2.0 column types so the
   app's boot-time seed can short-circuit; wraps DB access so any *other*
   legacy seed INSERTs rejected by the v2.0 schema are recorded, not fatal.
3. Boots the app (``APP_DB_SCHEMA=public``), logs in, then hits every
   parameterless authenticated GET ``/api/*`` route and reports a matrix.

``APP_DB_SCHEMA=public`` must point at the throwaway DB (NOT the ETL copy).
The ETL database (``hrms``) is deliberately never written here.

Usage::

    docker exec hrms-pg psql -U postgres -c "DROP DATABASE IF EXISTS hrms_probe" \\
        -c "CREATE DATABASE hrms_probe"
    APP_DB=postgres APP_DB_SCHEMA=public \\
      DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/hrms_probe \\
      python scripts/probe_public_flip.py

Exit: 0 = every measured route served, 1 = boot or login failure,
2 = route failures present.
"""

from __future__ import annotations

import os
import sys
import traceback
from collections import Counter

import psycopg

# ── tolerant-boot shim ──────────────────────────────────────────────────
_BOOT = True
_SEED_FAILURES: list[tuple[str, str]] = []


class _SentinelResult:
    def fetchone(self):
        return [0]

    def fetchall(self):
        return []

    @property
    def rowcount(self):
        return 0


class _TolerantConn:
    def __init__(self, real):
        self._real = real

    def execute(self, sql, params=None):
        if _BOOT and (sql or "").strip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            try:
                return self._real.execute(sql, params)
            except Exception as exc:
                _SEED_FAILURES.append((sql.strip()[:160], str(exc)))
                return _SentinelResult()
        return self._real.execute(sql, params)

    def executemany(self, sql, seq):
        if _BOOT and (sql or "").strip().upper().startswith("INSERT"):
            try:
                return self._real.executemany(sql, seq)
            except Exception as exc:
                _SEED_FAILURES.append((sql.strip()[:160], str(exc)))
                return None
        return self._real.executemany(sql, seq)

    def close(self):
        self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


def _install_tolerant_boot() -> None:
    import db_backend

    real = db_backend.connect

    def wrapped():
        return _TolerantConn(real())

    db_backend.connect = wrapped


def main() -> int:
    if os.getenv("APP_DB", "duckdb").lower() not in ("postgres", "postgresql", "pg"):
        print("should run with APP_DB=postgres and APP_DB_SCHEMA=public")
        return 1
    schema = os.getenv("APP_DB_SCHEMA", "public")
    dsn = (os.getenv("DATABASE_URL") or "").replace("postgresql+psycopg://", "postgresql://")
    print(f"probe target: {dsn}  schema={schema}")

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

    # Supress the ADAPTER's own boolean rewrite introspection noise: none needed.
    import db_backend  # noqa: F401
    from security import hash_password

    with psycopg.connect(dsn, autocommit=True) as pconn:
        if pconn.execute("SELECT count(*) FROM users").fetchone()[0] == 0:
            ph = hash_password("pass123")
            pconn.execute(
                "INSERT INTO users (emp_id, name, email, password, role, department,"
                " designation, phone, date_of_joining, status, allow_login, allow_breaks,"
                " first_login, created_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                ["EMP001", "Probe Admin", "probe@company.com", ph, "Admin", "MIS", "Tech Lead",
                 "9876543210", "2024-01-01", "Active", True, True, "2024-01-01", "2024-01-01"],
            )
        if pconn.execute("SELECT count(*) FROM leave_balance").fetchone()[0] == 0:
            pconn.execute(
                "INSERT INTO leave_balance (emp_id, leave_type, total_days, used_days,"
                " reserved, year) VALUES (%s,%s,%s,%s,%s,%s)",
                ["EMP001", "Casual", 12, 0, 0, 2026],
            )

    _install_tolerant_boot()
    try:
        from app import app
    except Exception:
        print("BOOT FAILED:")
        traceback.print_exc()
        return 1

    global _BOOT
    _BOOT = False

    c = app.test_client()
    tok = c.get("/api/csrf-token").get_json()["csrf_token"]
    login = c.post("/login", json={"emp_id": "EMP001", "password": "pass123"},
                   headers={"X-CSRF-Token": tok})
    print("login:", login.status_code)
    if login.status_code != 200:
        return 1

    app.config["PROPAGATE_EXCEPTIONS"] = True
    statuses = {}
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        if not rule.rule.startswith("/api/") or "GET" not in rule.methods:
            continue
        if any(a not in (rule.defaults or {}) for a in rule.arguments):
            continue
        try:
            statuses[rule.rule] = c.get(rule.rule).status_code
        except Exception:
            statuses[rule.rule] = "EXC"

    by = Counter(statuses.values())
    print("\n=== route status distribution ===")
    for k, v in by.most_common():
        print(f"  {k}: {v}")
    fails = [u for u, s in statuses.items() if s != 200]
    print(f"\n{len(statuses) - len(fails)}/{len(statuses)} authenticated GET routes served from {schema}")

    print("\n=== seed-level deltas (init_db inserts still rejected by v2.0) ===")
    for sql, err in _SEED_FAILURES:
        table = sql.split(" ")[2] if len(sql.split(" ")) > 2 else "?"
        print(f"  {table}: {err[:140].splitlines()[0]}")

    print("\n=== unresolved route failures ===")
    for u in fails:
        print(f"  {u}  ({statuses[u]})")

    if not fails:
        print("\nREADINESS: all measured GET routes green on v2.0 public.")
    return 0 if not fails else 2


if __name__ == "__main__":
    sys.exit(main())