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
from io import BytesIO
from pathlib import Path

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


def _login(app_mod, dsn, emp_id: str):
    """Sign in and walk the FR-AUTH-11 second factor when the account has one.

    The probe logs in as an Admin as well as an Employee, and MFA is compulsory
    for the former, so a bare password leaves the admin client half-authenticated
    and every admin-scoped flow below reports 401. Walking the real flow rather
    than exempting the probe is the point: this is exactly the surface a public
    flip has to keep working.

    An account with no factor takes the one-step path and is untouched by any of
    this, so the ordinary case costs nothing.
    """
    import pyotp

    cl = app_mod.test_client()
    tok = cl.get("/api/csrf-token").get_json()["csrf_token"]
    r = cl.post("/login", json={"emp_id": emp_id, "password": "pass123"},
                headers={"X-CSRF-Token": tok})
    if r.status_code != 200 or not (r.is_json and r.get_json().get("mfa_required")):
        return cl, tok, r.status_code

    step = r.get_json()["mfa_required"]
    if step == "enrol_required":
        # Mint the secret through the same route the UI uses, then read it back
        # out of the database: the API deliberately never returns a stored secret
        # a second time, so this is the only place it is available. The read goes
        # through psycopg on `dsn` rather than the app's `get_db`, because this
        # function is handed the Flask *app*, which has no `get_db` attribute.
        e = cl.post("/api/mfa/enrol", headers={"X-CSRF-Token": tok})
        if e.status_code != 200:
            return cl, tok, e.status_code
        secret = _stored_secret(dsn, emp_id)
        if secret is None:
            return cl, tok, 500
        # The QR is only served while the enrolment is in progress, so this is the
        # one moment it can be checked against the v2.0 target. After the confirm
        # below the same route answers 404 by design.
        if cl.get("/api/mfa/qr").status_code != 200:
            return cl, tok, 500
        ok = cl.post("/api/mfa/confirm", json={"code": pyotp.TOTP(secret).now()},
                     headers={"X-CSRF-Token": tok, "Content-Type": "application/json"})
    else:
        secret = _stored_secret(dsn, emp_id)
        if secret is None:
            return cl, tok, 500
        ok = cl.post(
            "/api/mfa/challenge",
            json={"code": pyotp.TOTP(secret).now()},
            headers={"X-CSRF-Token": tok, "Content-Type": "application/json"},
        )
    return cl, tok, ok.status_code


def _stored_secret(dsn, emp_id: str):
    """The decrypted authenticator secret for an enrolled employee, or None."""
    import mfa as _mfa

    with psycopg.connect(dsn, autocommit=True) as pc:
        row = pc.execute(
            "SELECT secret_encrypted FROM mfa_credentials WHERE emp_id = %s", [emp_id],
        ).fetchone()
    if not row:
        return None
    return _mfa.decrypt_secret(row[0])


def _post(cl, tok, url, body=None):
    headers = {"X-CSRF-Token": tok}
    if body is not None:
        headers["Content-Type"] = "application/json"
    return cl.post(url, json=body, headers=headers)


def _put(cl, tok, url, body=None):
    headers = {"X-CSRF-Token": tok}
    if body is not None:
        headers["Content-Type"] = "application/json"
    return cl.put(url, json=body, headers=headers)


def _delete_any(pc, statement: str, values) -> None:
    if values:
        pc.execute(statement, [list(values)])


def _remove_probe_upload_files(paths: set[Path]) -> None:
    """Best-effort removal of pre-boarding files owned by probe workflows."""
    for path in sorted(paths, key=str):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # A stale upload must not turn an otherwise clean database probe red.
            continue


def _cleanup_lifecycle_probe_residue(pc) -> set[Path]:
    """Remove prior lifecycle journeys before exercising the same journey again.

    Cleanup is driven by the probe candidate/user markers, then expanded through
    their workflow IDs. This also reaches children whose marker relationship was
    lost in an interrupted earlier run. Returned file paths are removed only
    after the surrounding database transaction commits.
    """
    candidate_ids = {
        row[0]
        for row in pc.execute(
            "SELECT candidate_id FROM candidates WHERE email LIKE 'probe-cand-%'"
        ).fetchall()
    }
    user_ids = {
        row[0]
        for row in pc.execute(
            """
            SELECT u.emp_id
            FROM users u
            WHERE u.candidate_id IN (
                SELECT candidate_id FROM candidates WHERE email LIKE 'probe-cand-%'
            ) OR (u.name = 'Probe Candidate' AND u.email LIKE 'probe-cand-%')
            """
        ).fetchall()
    }

    workflow_rows = pc.execute(
        "SELECT workflow_id, candidate_id, emp_id FROM onboarding_workflow"
    ).fetchall()
    workflow_ids = {
        row[0]
        for row in workflow_rows
        if row[1] in candidate_ids or row[2] in user_ids
    }
    user_ids.update(
        row[2]
        for row in workflow_rows
        if row[1] in candidate_ids and row[2]
    )
    offer_ids = {
        row[0]
        for row in pc.execute("SELECT offer_id, candidate_id FROM offer_letters").fetchall()
        if row[1] in candidate_ids
    }
    resignation_rows = pc.execute(
        "SELECT resignation_id, emp_id, reason FROM resignations"
    ).fetchall()
    resignation_ids = {
        row[0]
        for row in resignation_rows
        if row[1] in user_ids or row[2] == "public lifecycle probe"
    }
    user_ids.update(
        row[1]
        for row in resignation_rows
        if row[2] == "public lifecycle probe" and row[1]
    )
    offboarding_rows = pc.execute(
        "SELECT offboard_id, resignation_id, emp_id FROM offboarding_workflow"
    ).fetchall()
    offboard_ids = {
        row[0]
        for row in offboarding_rows
        if row[1] in resignation_ids or row[2] in user_ids
    }
    user_ids.update(
        row[2]
        for row in offboarding_rows
        if row[1] in resignation_ids and row[2]
    )
    checklist_ids = {
        row[0]
        for row in pc.execute(
            "SELECT item_id, workflow_id FROM onboarding_checklist"
        ).fetchall()
        if row[1] in workflow_ids
    }
    onboarding_task_ids = {
        row[0]
        for row in pc.execute(
            "SELECT task_id, emp_id FROM onboarding_tasks"
        ).fetchall()
        if row[1] in user_ids
    }
    offboarding_task_ids = {
        row[0]
        for row in pc.execute(
            "SELECT task_id, emp_id FROM offboarding_tasks"
        ).fetchall()
        if row[1] in user_ids
    }
    exit_interview_ids = {
        row[0]
        for row in pc.execute(
            "SELECT interview_id, emp_id, offboard_id FROM exit_interviews"
        ).fetchall()
        if row[1] in user_ids or row[2] in offboard_ids
    }
    interview_ids = {
        row[0]
        for row in pc.execute(
            "SELECT interview_id, candidate_id FROM interviews"
        ).fetchall()
        if row[1] in candidate_ids
    }
    payroll_run_ids = {
        row[0]
        for row in pc.execute(
            "SELECT run_id FROM payroll_runs WHERE year >= 2099"
        ).fetchall()
    }

    # Capture both the metadata path and the workflow-id prefix before deleting
    # lifecycle rows. The basename-only join prevents a database path from
    # escaping the repository's upload directory.
    upload_root = Path(__file__).resolve().parents[1] / "uploads"
    upload_paths: set[Path] = set()
    for workflow_id in workflow_ids:
        upload_paths.update(upload_root.glob(f"preboarding_{workflow_id}_*"))
    for emp_id, file_path in pc.execute(
        "SELECT emp_id, file_path FROM documents WHERE file_path LIKE 'preboarding_%'"
    ).fetchall():
        name = Path(str(file_path)).name
        workflow_prefixes = tuple(f"preboarding_{workflow_id}_" for workflow_id in workflow_ids)
        if emp_id in user_ids or name.startswith(workflow_prefixes):
            upload_paths.add(upload_root / name)

    # Outbox rows have no FK to their aggregate. Delete them by both aggregate
    # and payload, including PRE-prefixed lifecycle events left without parents.
    outbox_conditions = [
        "(event_type = 'offer.created' AND payload->>'email' LIKE ANY(%s))",
        "(event_type IN ('offer.accepted', 'candidate.hired', 'credentials.issued') "
        "AND payload->>'emp_id' LIKE ANY(%s))",
    ]
    outbox_params = [["probe-cand-%"], ["PRE%"]]
    for aggregate, payload_key, values in (
        ("candidates", "candidate_id", candidate_ids),
        ("offer_letters", "offer_id", offer_ids),
        ("users", "emp_id", user_ids),
        ("onboarding_workflow", "workflow_id", workflow_ids),
        ("payroll_runs", "run_id", payroll_run_ids),
    ):
        if values:
            string_values = [str(value) for value in values]
            outbox_conditions.append(
                f"(aggregate = '{aggregate}' AND aggregate_id = ANY(%s))"
            )
            outbox_params.append(string_values)
            outbox_conditions.append(
                f"(payload->>'{payload_key}' = ANY(%s))"
            )
            outbox_params.append(string_values)
    pc.execute(
        "DELETE FROM outbox_events WHERE " + " OR ".join(outbox_conditions),
        outbox_params,
    )

    notification_conditions = []
    notification_params = []
    if user_ids:
        notification_conditions.append("emp_id = ANY(%s)")
        notification_params.append(list(user_ids))
    if candidate_ids:
        notification_conditions.append(
            "type = 'Onboarding' AND message = ANY(%s)"
        )
        notification_params.append([
            f"Candidate {candidate_id} accepted — onboarding workflow started"
            for candidate_id in candidate_ids
        ])
    if payroll_run_ids:
        notification_conditions.append(
            "type = 'Payroll' AND message LIKE ANY(%s)"
        )
        notification_params.append([
            f"Salary for run {run_id} credited:%" for run_id in payroll_run_ids
        ])
    if notification_conditions:
        pc.execute(
            "DELETE FROM notifications WHERE " + " OR ".join(notification_conditions),
            notification_params,
        )

    audit_conditions = []
    audit_params = []
    if user_ids:
        audit_conditions.extend(("emp_id = ANY(%s)", "actor = ANY(%s)"))
        audit_params.extend((list(user_ids), list(user_ids)))
    for entity, values in (
        ("users", user_ids),
        ("candidates", candidate_ids),
        ("interviews", interview_ids),
        ("offer_letters", offer_ids),
        ("onboarding_workflow", workflow_ids),
        ("onboarding_checklist", checklist_ids),
        ("onboarding_tasks", onboarding_task_ids),
        ("resignations", resignation_ids),
        ("offboarding_workflow", offboard_ids),
        ("offboarding_tasks", offboarding_task_ids),
        ("exit_interviews", exit_interview_ids),
    ):
        if values:
            audit_conditions.append("(entity = %s AND entity_id = ANY(%s))")
            audit_params.extend((entity, [str(value) for value in values]))
    # This detail is unique to the probe and catches an audit whose parent row
    # was lost before this cleanup version could associate it by ID.
    audit_conditions.append("details = 'Added candidate Probe Candidate'")
    pc.execute(
        "DELETE FROM audit_log WHERE " + " OR ".join(audit_conditions),
        audit_params,
    )

    # Child-first order is required by the canonical lifecycle foreign keys.
    _delete_any(
        pc,
        "DELETE FROM onboarding_checklist WHERE workflow_id = ANY(%s)",
        workflow_ids,
    )
    _delete_any(
        pc,
        "DELETE FROM offboarding_settlements WHERE offboard_id = ANY(%s)",
        offboard_ids,
    )
    _delete_any(
        pc,
        "DELETE FROM offboarding_approvals WHERE offboard_id = ANY(%s)",
        offboard_ids,
    )
    _delete_any(
        pc,
        "DELETE FROM exit_interviews WHERE interview_id = ANY(%s)",
        exit_interview_ids,
    )
    _delete_any(
        pc,
        "DELETE FROM offboarding_tasks WHERE task_id = ANY(%s)",
        offboarding_task_ids,
    )
    _delete_any(
        pc,
        "DELETE FROM offboarding_workflow WHERE offboard_id = ANY(%s)",
        offboard_ids,
    )
    _delete_any(
        pc,
        "DELETE FROM resignations WHERE resignation_id = ANY(%s)",
        resignation_ids,
    )
    _delete_any(
        pc,
        "DELETE FROM onboarding_tasks WHERE task_id = ANY(%s)",
        onboarding_task_ids,
    )
    _delete_any(
        pc,
        "DELETE FROM onboarding_workflow WHERE workflow_id = ANY(%s)",
        workflow_ids,
    )
    _delete_any(pc, "DELETE FROM salary_structures WHERE emp_id = ANY(%s)", user_ids)
    _delete_any(pc, "DELETE FROM documents WHERE emp_id = ANY(%s)", user_ids)
    _delete_any(pc, "DELETE FROM assets WHERE emp_id = ANY(%s)", user_ids)
    _delete_any(pc, "DELETE FROM user_sessions WHERE emp_id = ANY(%s)", user_ids)
    _delete_any(pc, "DELETE FROM employee_documents WHERE emp_id = ANY(%s)", user_ids)
    _delete_any(pc, "DELETE FROM user_permissions WHERE emp_id = ANY(%s)", user_ids)
    _delete_any(pc, "DELETE FROM password_reset_tokens WHERE emp_id = ANY(%s)", user_ids)
    _delete_any(pc, "DELETE FROM shift_assignments WHERE emp_id = ANY(%s)", user_ids)
    _delete_any(pc, "DELETE FROM users WHERE emp_id = ANY(%s)", user_ids)
    _delete_any(pc, "DELETE FROM interviews WHERE interview_id = ANY(%s)", interview_ids)
    _delete_any(pc, "DELETE FROM offer_letters WHERE offer_id = ANY(%s)", offer_ids)
    _delete_any(pc, "DELETE FROM candidates WHERE candidate_id = ANY(%s)", candidate_ids)

    return {path for path in upload_paths if path.is_file() or path.is_symlink()}


# A password the FR-AUTH-10 policy accepts. The probe only needs a value that
# is not the seed's demo password, which the policy refuses because it is on the
# breach corpus.
PROBE_PASSWORD = "jade-marlin-quilt-77"


def _leave_casual(cl, tok):
    """EMP002's Casual balance, read through the API rather than the database."""
    import json as _json
    rows = _json.loads(cl.get("/api/leave-balance").data or b"[]")
    return next(r for r in rows if r["leave_type"] == "Casual")


def _write_flows(app_mod, dsn) -> dict[str, tuple[str, str]]:
    """Fire the core state-changing flows against ``public`` exactly as the
    legacy browser tests do; bucket OK (2xx) vs guarded (4xx, route served and
    business rule fired) vs failed (5xx/EXC)."""
    from datetime import date, datetime, timedelta

    # FR-AUTH-03's threshold, read from the module rather than repeated here: a
    # probe flow that hardcoded "10" would keep passing after the policy changed,
    # which is exactly the drift this script exists to catch.
    import lockout as _lockout_mod
    _LOCKOUT_ATTEMPTS = _lockout_mod.MAX_FAILED_ATTEMPTS

    out: dict[str, tuple[str, str]] = {}
    today = date.today()
    days_until_monday = (7 - today.weekday()) % 7 or 7
    attendance_date = today + timedelta(days=days_until_monday)
    state: dict = {}

    # Clear residue from prior probe runs so dedupe guards don't mask results.
    pg_dsn = dsn.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(pg_dsn) as pc:
        upload_paths = _cleanup_lifecycle_probe_residue(pc)
        pc.execute("DELETE FROM regularization_requests WHERE reason = 'public write probe'")
        pc.execute("DELETE FROM leave_requests WHERE reason IN ('public write probe', 'idempotency probe')")
        pc.execute("DELETE FROM goals WHERE title = 'public write probe'")
        # Comments first: ticket_comments has a foreign key to tickets, so
        # deleting the ticket before its comments is a constraint violation.
        pc.execute("DELETE FROM ticket_comments WHERE comment IN "
                   "('still broken', 'not mine', 'probe comment')")
        pc.execute("DELETE FROM tickets WHERE subject = 'public write probe'")
        pc.execute("DELETE FROM performance_reviews WHERE review_period = 'public write probe'")
        pc.execute("DELETE FROM feedback_360 WHERE comment = 'public write probe'")
        # The `auth(reset-password)` flow below changes EMP002's password, so a
        # second probe run against the same database would fail at login. Restoring
        # the seeded hash keeps the probe idempotent across runs rather than only
        # against a freshly created database.
        from security import hash_password as _seed_hash
        pc.execute("UPDATE users SET password = %s WHERE emp_id = 'EMP002'",
                   [_seed_hash("pass123")])
        pc.execute("DELETE FROM break_approvals WHERE reason = 'public write probe'")
        pc.execute("UPDATE breaks SET status = 'Completed' WHERE emp_id = 'EMP002' AND status = 'Active'")
        pc.execute("DELETE FROM ticket_comments WHERE comment = 'probe comment'")
        pc.execute("DELETE FROM tickets WHERE subject = 'public write probe'")
        pc.execute("DELETE FROM payroll_approvals WHERE run_id IN "
                   "(SELECT run_id FROM payroll_runs WHERE year >= 2099)")
        pc.execute("DELETE FROM payroll_items WHERE run_id IN "
                   "(SELECT run_id FROM payroll_runs WHERE year >= 2099)")
        pc.execute("DELETE FROM payroll_runs WHERE year >= 2099")
        pc.execute("DELETE FROM password_reset_tokens WHERE emp_id = 'EMP002'")
        pc.execute("DELETE FROM idempotency_keys WHERE key LIKE 'probe-%'")
        # FR-LEA-08: the probe's own leave policy and accrual ledger, so a second
        # run posts the same grants rather than skipping them as already posted.
        pc.execute("DELETE FROM monthly_leave_grants WHERE emp_id = 'EMP002'")
        pc.execute("DELETE FROM leave_policy_assignments WHERE emp_id = 'EMP002'")
        pc.execute("DELETE FROM leave_balance WHERE emp_id = 'EMP002'")
        # FR-AUTH-03: a previous run that died between the lock and the unlock
        # would leave EMP002 locked, and the next run's lockout flow would then
        # start from "already locked" and assert the wrong thing. The flow unlocks
        # at the end, but a run that fails part way through never gets there.
        pc.execute(
            "UPDATE users SET failed_attempts = 0, last_failed_login = NULL, "
            "locked_until = NULL WHERE emp_id = 'EMP002'"
        )
        pc.execute(
            "DELETE FROM attendance_days WHERE emp_id = 'EMP002' AND attendance_date = %s",
            [attendance_date],
        )
        pc.execute(
            "DELETE FROM user_sessions WHERE emp_id = 'EMP002' AND session_date = %s",
            [attendance_date],
        )
        # FR-HOL-03: the probe's own Optional holiday, its opt-ins and the
        # attendance it produced, so a second run re-requests cleanly. The opt-ins
        # go first: holiday_optins references holidays.
        pc.execute("DELETE FROM holiday_optins WHERE holiday_id IN "
                   "(SELECT holiday_id FROM holidays WHERE name = 'Probe Optional')")
        pc.execute("DELETE FROM attendance_days WHERE attendance_date IN "
                   "(SELECT holiday_date FROM holidays WHERE name = 'Probe Optional')")
        pc.execute("DELETE FROM holidays WHERE name = 'Probe Optional'")
        # FR-HOL-02: the probe's own calendar fixtures, matched by name across
        # *every* year. Keying on a year was wrong: the flow dates most of its
        # holidays relative to today, so they land in the current year and only the
        # leap-day fixture lands in 2036 — a year-scoped cleanup left the rest
        # behind and every later run collided on the duplicate rule. Children first.
        pc.execute("DELETE FROM holiday_optins WHERE holiday_id IN "
                   "(SELECT holiday_id FROM holidays WHERE name LIKE 'Probe Cal%')")
        pc.execute("DELETE FROM holidays WHERE name LIKE 'Probe Cal%'")
        # FR-NOT-03: the probe's own preference rows, so a second run reads the
        # defaults rather than the previous run's switches.
        pc.execute("DELETE FROM notification_preferences WHERE emp_id = 'EMP002'")
    _remove_probe_upload_files(upload_paths)

    def run(name, fn):
        try:
            status = fn()
            label = "OK" if 200 <= status < 300 else ("GUARDED" if 400 <= status < 500 else "FAIL")
            out[name] = (label, f"status={status}")
        except Exception as exc:
            out[name] = ("FAIL", f"{type(exc).__name__}: {str(exc).splitlines()[0][:220]}")

    # ── employee flows ─────────────────────────────────────────────
    cl, tok, lc = _login(app_mod, dsn, "EMP002")
    if lc != 200:
        out["login-EMP002"] = ("FAIL", f"login status={lc}")
        return out
    out["login-EMP002"] = ("OK", "status=200")

    def start_tea():
        r = _post(cl, tok, "/api/start-break", {"break_type": "Tea"})
        if r.status_code == 201 and r.is_json:
            state["break_id"] = (r.get_json() or {}).get("break_id")
        return r.status_code
    run("start-break(Tea)", start_tea)

    def end_break():
        bid = state.get("break_id")
        if not bid:
            return 409
        return _post(cl, tok, f"/api/end-break/{bid}").status_code
    run("end-break", end_break)

    def regularization():
        return _post(cl, tok, "/api/regularization",
                     {"date": (today + timedelta(days=30)).isoformat(), "reason": "public write probe"}).status_code
    run("regularization(submit)", regularization)

    def leave_apply():
        return _post(cl, tok, "/api/leaves",
                     {"leave_type": "Casual",
                      "start_date": (today + timedelta(days=30)).isoformat(),
                      "end_date": (today + timedelta(days=31)).isoformat(),
                      "reason": "public write probe"}).status_code
    run("leaves(apply)", leave_apply)

    run("notifications( read)", lambda: _post(cl, tok, "/api/notifications/read").status_code)

    def lunch_request():
        return _post(cl, tok, "/api/break-approvals",
                     {"break_type": "Lunch", "reason": "public write probe"}).status_code
    run("break-approvals(request Lunch)", lunch_request)

    # ── admin flows ────────────────────────────────────────────────
    cl_a, tok_a, lc_a = _login(app_mod, dsn, "EMP001")
    if lc_a != 200:
        out["login-EMP001"] = ("FAIL", f"login status={lc_a}")
        return out
    out["login-EMP001"] = ("OK", "status=200")
    cl_f, tok_f, lc_f = _login(app_mod, dsn, "EMP003")
    if lc_f != 200:
        out["login-EMP003"] = ("FAIL", f"login status={lc_f}")
        return out
    out["login-EMP003"] = ("OK", "status=200")

    # FR-USR-01: employee IDs are ``EMP`` + >=3 digits (app.py ``_EMP_ID_RE``).
    uniq = f"EMP{int(date.today().strftime('%m%d'))}{os.getpid() % 10000:04d}"
    # Clear residue from an earlier probe run so the create flow stays a 201
    # (and so the permission override rows below never accumulate).
    with psycopg.connect(pg_dsn) as probe_pc:
        probe_pc.execute("DELETE FROM user_permissions WHERE emp_id = %s", [uniq])
        probe_pc.execute("DELETE FROM audit_log WHERE entity_id = %s", [uniq])
        probe_pc.execute("DELETE FROM users WHERE emp_id = %s", [uniq])

    def create_user():
        return _post(cl_a, tok_a, "/api/users",
                     {"emp_id": uniq, "name": "Probe Tester", "email": f"{uniq.lower()}@company.com",
                      "department": "MIS", "role": "Employee",
                      "password": PROBE_PASSWORD}).status_code
    run("users(create)", create_user)

    # FR-USR-09: user_permissions is an identity key with a BOOLEAN flag on
    # v2.0 (INTEGER + no sequence on the compat schema) and the route writes
    # ints, so this exercises the allocator and the boolean adapter together.
    def put_permissions():
        return _put(cl_a, tok_a, f"/api/users/{uniq}/permissions",
                    {"modules": {"tickets": False, "goals": True}}).status_code
    run("user-permissions(put)", put_permissions)

    def get_permissions():
        body = cl_a.get(f"/api/users/{uniq}/permissions").get_json() or {}
        overrides = body.get("overrides") or {}
        effective = body.get("effective") or {}
        if (overrides.get("tickets") is False and effective.get("tickets") is False
                and effective.get("breaks") is True):
            return 200
        return 409
    run("user-permissions(get)", get_permissions)

    def approve_lunch():
        rows = cl_a.get("/api/break-approvals").get_json() or []
        target = next((r for r in rows if r.get("emp_id") == "EMP002" and r.get("status") == "Pending"), None)
        if not target:
            return 409
        return _post(cl_a, tok_a, f"/api/break-approvals/{target['approval_id']}/approve").status_code
    run("break-approvals(approve)", approve_lunch)

    # ── service-layer rewrite: shifts resolve from shift_assignments, and
    #    init_db must NOT have re-added users.shift_start/shift_end ──────────
    def shift_write():
        cur = cl_a.get("/api/users/EMP002").get_json()
        if not cur:
            return 409
        body = {"name": cur["name"], "email": cur["email"], "role": "Employee",
                "department": cur.get("department") or "Operations", "status": "Active",
                "shift_start": "10:00", "shift_end": "19:00"}
        rc = _put(cl_a, tok_a, "/api/users/EMP002", body).status_code
        if rc != 200:
            return rc
        with psycopg.connect(dsn.replace("postgresql+psycopg://", "postgresql://"), autocommit=True) as pc:
            row = pc.execute(
                "SELECT shift_type, to_char(shift_start, 'HH24:MI'), to_char(shift_end, 'HH24:MI') "
                "FROM shift_assignments WHERE emp_id = 'EMP002'",
            ).fetchone()
            cols = [r[0] for r in pc.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'users'",
            )]
        ok = row is not None and row[0] == 'Fixed' and row[1] == '10:00' and row[2] == '19:00'
        return 200 if ok and 'shift_start' not in cols else 409
    run("shifts(assignment write)", shift_write)

    # ── FR-LEA-08: monthly accrual on the v2.0 ledger ────────────────────────
    # `monthly_leave_grants` exists in the canonical target with an identity key
    # and `granted_by` as a foreign key to users(emp_id), so a system-driven
    # accrual must leave it NULL rather than invent an actor id.
    def leave_accrual():
        rc = _put(cl_a, tok_a, "/api/users/EMP002/leave-policy",
                  {"accrual_rate": "1.0",
                   "effective_from": today.replace(month=1, day=1).isoformat()}).status_code
        if rc != 200:
            return rc
        rc = _post(cl_a, tok_a, "/api/accrual/run", {}).status_code
        if rc != 200:
            return rc
        with psycopg.connect(pg_dsn, autocommit=True) as pc:
            rows = pc.execute(
                "SELECT month, days, granted_by FROM monthly_leave_grants "
                "WHERE emp_id = 'EMP002' AND leave_type = 'Annual' ORDER BY month",
            ).fetchall()
            balance = pc.execute(
                "SELECT total_days FROM leave_balance WHERE emp_id = 'EMP002' "
                "AND leave_type = 'Annual' AND year = %s",
                [today.year],
            ).fetchone()
        expected = list(range(1, today.month + 1))
        posted = [r[0] for r in rows]
        derived = balance[0] if balance else None
        ok = (posted == expected and all(r[1] > 0 and r[2] is None for r in rows)
              and derived is not None and int(derived) == sum(r[1] for r in rows))
        return 200 if ok else 409
    # ── FR-PERF-01: goals, end to end on the v2.0 identity key ───────────────
    # `POST /api/goals` used to be a bare `INSERT INTO goals VALUES (...)` with
    # ten placeholders against a nine-column table, so it returned 500 on every
    # backend and no probe flow covered it. This one creates a goal as EMP002,
    # has their manager rate it, and reads the ledger back.
    def goals_lifecycle():
        # `cl` is EMP002, the goal's owner, so they are who legitimately edits it.
        created = _post(cl, tok, "/api/goals",
                        {"title": "public write probe", "weight": 3})
        if created.status_code != 201:
            return created.status_code
        gid = (created.get_json() or {}).get("id")
        if not gid:
            return 409
        # An edit may not smuggle the status past the rating flow, not even by the
        # owner, and the body may not name somebody else (CC-10).
        for payload in ({"status": "Completed"}, {"emp_id": "EMP001", "title": "x"}):
            blocked = _put(cl, tok, f"/api/goals/{gid}", payload)
            if blocked.status_code != 400:
                return 409
        with psycopg.connect(pg_dsn, autocommit=True) as pc:
            pc.execute(
                "UPDATE users SET manager_emp_id = 'EMP001' WHERE emp_id = 'EMP002'")
        rated = _put(cl_a, tok_a, f"/api/goals/{gid}/rate", {"rating": 4})
        if rated.status_code != 200:
            return rated.status_code
        with psycopg.connect(pg_dsn, autocommit=True) as pc:
            row = pc.execute(
                "SELECT rating, status FROM goals WHERE goal_id = %s", [gid],
            ).fetchone()
        return 200 if row == (4, "Completed") else 409
    run("goals(create -> rate)", goals_lifecycle)
    # ── FR-PERF-02: only the assigned reviewer may sign a review ──────────────
    # Appendix A-18 records this as a v1.0 gap: "Submit PUT … (auth any – no role
    # check gap – record)". The route was `@login_required` with an id from the
    # path, so any authenticated user could sign off anybody's performance review.
    def review_signoff():
        with psycopg.connect(pg_dsn, autocommit=True) as pc:
            pc.execute("UPDATE users SET manager_emp_id = 'EMP001' WHERE emp_id = 'EMP002'")
        # A self-review has nobody to sign it, so it cannot even be opened.
        self_review = _post(cl_a, tok_a, "/api/performance-reviews",
                            {"emp_id": "EMP002", "reviewer_id": "EMP002",
                             "review_period": "public write probe"})
        if self_review.status_code != 409:
            return 409
        opened = _post(cl_a, tok_a, "/api/performance-reviews",
                       {"emp_id": "EMP002", "reviewer_id": "EMP001",
                        "review_period": "public write probe"})
        if opened.status_code != 201:
            return opened.status_code
        rid = (opened.get_json() or {}).get("id")
        if not rid:
            return 409
        # Neither the subject nor an unrelated third party may sign it. The
        # "admin who merely opened the cycle" case is covered by the unit test;
        # here the opener and the reviewer are the same client, so conflating them
        # would assert the opposite of what is meant.
        for client, token in ((cl, tok), (cl_f, tok_f)):
            if _put(client, token, f"/api/performance-reviews/{rid}/submit",
                    {"rating": 5}).status_code != 403:
                return 409
        # The assigned reviewer can, and the signed review is then final.
        if _put(cl_a, tok_a, f"/api/performance-reviews/{rid}/submit",
                {"rating": 4}).status_code != 200:
            return 409
        if _put(cl_a, tok_a, f"/api/performance-reviews/{rid}/submit",
                {"rating": 1}).status_code != 409:
            return 409
        with psycopg.connect(pg_dsn, autocommit=True) as pc:
            row = pc.execute(
                "SELECT overall_rating, status FROM performance_reviews WHERE review_id = %s",
                [rid],
            ).fetchone()
        return 200 if row == (4, "Submitted") else 409
    run("performance-reviews(reviewer signoff)", review_signoff)
    # ── FR-TKT-03/04: visibility on the writes, and the chain ────────────────
    # The list and the detail view enforced the visibility rule; the two write
    # paths did not, so a user refused a ticket with 403 could still comment on it
    # and close it. The status route also had no state machine at all.
    def ticket_lifecycle():
        created = _post(cl, tok, "/api/tickets", {"subject": "public write probe",
                                                 "priority": "High"})
        if created.status_code != 201:
            return created.status_code
        tid = (created.get_json() or {}).get("id")
        if not tid:
            return 409
        # A third party can neither comment on it nor move it.
        if _post(cl_f, tok_f, f"/api/tickets/{tid}/comment", {"comment": "not mine"}).status_code != 403:
            return 409
        if _put(cl_f, tok_f, f"/api/tickets/{tid}/status", {"status": "In Progress"}).status_code != 403:
            return 409
        # The chain is strict: Open cannot jump to Closed.
        if _put(cl, tok, f"/api/tickets/{tid}/status", {"status": "Closed"}).status_code != 409:
            return 409
        for step in ("In Progress", "Resolved", "Closed"):
            if _put(cl, tok, f"/api/tickets/{tid}/status", {"status": step}).status_code != 200:
                return 409
        # A Closed ticket reopens when its *reporter* comments, and only then.
        reopened = _post(cl, tok, f"/api/tickets/{tid}/comment", {"comment": "still broken"})
        if reopened.status_code != 201 or not (reopened.get_json() or {}).get("reopened"):
            return 409
        with psycopg.connect(pg_dsn, autocommit=True) as pc:
            row = pc.execute(
                "SELECT status, resolved_at FROM tickets WHERE ticket_id = %s", [tid],
            ).fetchone()
        return 200 if row == ("Reopened", None) else 409
    run("tickets(visibility + chain + reopen)", ticket_lifecycle)
    # ── FR-LEA-05: cancelling gives the reservation back ─────────────────────
    # There was no cancel route, so a Pending request reserved days that nothing
    # could ever release. This applies, cancels, and reads the ledger back to
    # prove the days came home.
    def leave_cancel():
        days = 3
        start = (today + timedelta(days=40)).isoformat()
        end = (today + timedelta(days=40 + days - 1)).isoformat()
        applied = _post(cl, tok, "/api/leaves",
                        {"leave_type": "Casual", "start_date": start,
                         "end_date": end, "reason": "public write probe"})
        if applied.status_code != 201:
            return applied.status_code
        lid = (applied.get_json() or {}).get("leave_id")
        if not lid:
            return 409
        # Read *after* the apply, and assert the *delta* rather than a global zero:
        # an earlier flow in this run holds a reservation of its own, so
        # `reserved_days == 0` afterwards would be measuring somebody else's leave.
        held = _leave_casual(cl, tok)
        if held["reserved_days"] < days:
            return 409
        cancelled = _post(cl, tok, f"/api/leaves/{lid}/cancel", {})
        if cancelled.status_code != 200:
            return cancelled.status_code
        if (cancelled.get_json() or {}).get("ledger") != "release":
            return 409
        after = _leave_casual(cl, tok)
        ok = (after["reserved_days"] == held["reserved_days"] - days
              and after["remaining"] == held["remaining"] + days)
        return 200 if ok else 409
    run("leaves(cancel releases the reservation)", leave_cancel)

    # ── FR-HOL-03: an Optional holiday becomes an attendance holiday only for an
    # employee with an Approved opt-in. Nothing could ever obtain one before this
    # slice, so the seeded Diwali was finalised as something other than a holiday.
    def holiday_optin():
        # The holiday is an HR artefact and the opt-in is the employee's, so this
        # flow needs both sessions — which is also the shape of the approval
        # workflow it is checking.
        hl, htok, hstatus = _login(app_mod, dsn, "EMP001")
        if hstatus != 200:
            return hstatus
        created = _post(hl, htok, "/api/holidays", {
            "name": "Probe Optional",
            "date": (today + timedelta(days=60)).isoformat(),
            "type": "Optional",
        })
        if created.status_code != 201:
            return created.status_code
        hid = (created.get_json() or {}).get("id")
        if not hid:
            return 409
        # A National holiday cannot be opted into, and the seed has one in range.
        catalog = hl.get("/api/holidays").get_json() or {}
        listed = catalog.get("holidays") if isinstance(catalog, dict) else catalog
        national = [h for h in (listed or []) if h.get("type") == "National"]
        if national:
            refused = _post(hl, htok, f"/api/holidays/{national[0]['id']}/opt-in", {})
            if refused.status_code != 409:
                return 409
        requested = _post(cl, tok, f"/api/holidays/{hid}/opt-in", {})
        if requested.status_code != 201:
            return requested.status_code
        # One active opt-in per employee per holiday.
        if _post(cl, tok, f"/api/holidays/{hid}/opt-in", {}).status_code != 409:
            return 409
        # The queue is not the employee's to read.
        if cl.get("/api/holidays/opt-ins").status_code != 403:
            return 409
        optin_id = (requested.get_json() or {}).get("optin_id")
        if not optin_id:
            return 409
        queue = hl.get("/api/holidays/opt-ins").get_json() or {}
        if optin_id not in [o.get("optin_id") for o in queue.get("optins", [])]:
            return 409
        approved = _post(hl, htok, f"/api/holidays/opt-ins/{optin_id}/approve", {})
        if approved.status_code != 200:
            return approved.status_code
        # A settled request is not reviewable twice.
        if _post(hl, htok, f"/api/holidays/opt-ins/{optin_id}/approve", {}).status_code != 409:
            return 409
        # The point of the whole flow: the canonical holiday_optins row exists, and
        # attendance for that date is now a Holiday for this employee.
        with psycopg.connect(pg_dsn) as vpc:
            row = vpc.execute(
                "SELECT status FROM holiday_optins WHERE optin_id = %s", (optin_id,)
            ).fetchone()
            if not row or row[0] != 'Approved':
                return 409
            when = vpc.execute(
                "SELECT holiday_date FROM holidays WHERE holiday_id = %s", (hid,)
            ).fetchone()
            if not when:
                return 409
        from app import finalize_attendance_for_date
        finalize_attendance_for_date(when[0], employee_ids=['EMP002'])
        with psycopg.connect(pg_dsn) as vpc:
            status = vpc.execute(
                "SELECT status FROM attendance_days WHERE emp_id = 'EMP002' "
                "AND attendance_date = %s", (when[0],)
            ).fetchone()
        return 200 if (status and status[0] == 'Holiday') else 409
    run("holidays(optional opt-in drives attendance)", holiday_optin)

    # ── FR-HOL-01/02: the calendar itself ─────────────────────────────────────
    # Location scoping, the per-location duplicate rule (a unique constraint on
    # v2.0), a year-to-year copy that skips rather than shifts 29 February, the CSV
    # round trip, and the iCal feed's all-day form.
    def holiday_calendar():
        hl, htok, hstatus = _login(app_mod, dsn, "EMP001")
        if hstatus != 200:
            return hstatus
        when = (today + timedelta(days=300)).isoformat()
        created = _post(hl, htok, "/api/holidays", {
            "name": "Probe Cal Day", "date": when, "type": "National", "location": "Pune",
        })
        if created.status_code != 201:
            return created.status_code
        hid = (created.get_json() or {}).get("id")
        if not hid:
            return 409
        # Duplicate (name, date) per location. A plain UNIQUE would accept the
        # org-wide case, which is why the index is on COALESCE(location, '').
        if _post(hl, htok, "/api/holidays", {
            "name": "Probe Cal Day", "date": when, "location": "Pune",
        }).status_code != 409:
            return 409
        if _post(hl, htok, "/api/holidays", {
            "name": "Probe Cal Day", "date": when, "location": "Mumbai",
        }).status_code != 201:
            return 409
        org = (today + timedelta(days=301)).isoformat()
        if _post(hl, htok, "/api/holidays", {"name": "Probe Cal Org", "date": org}).status_code != 201:
            return 409
        if _post(hl, htok, "/api/holidays", {
            "name": "Probe Cal Org", "date": org, "location": "",
        }).status_code != 409:
            return 409
        # A location filter includes the org-wide holiday, not just its own.
        pune = hl.get("/api/holidays?location=Pune").get_json() or {}
        listed = pune.get("holidays") if isinstance(pune, dict) else pune
        names = {h.get("name") for h in (listed or [])}
        if "Probe Cal Day" not in names or "Probe Cal Org" not in names:
            return 409
        # The "U" of CRUD, with an unknown field refused.
        edited = hl.put(f"/api/holidays/{hid}", json={"name": "Probe Cal Day Renamed"},
                        headers={"X-CSRF-Token": htok, "Content-Type": "application/json"})
        if edited.status_code != 200 or not (edited.get_json() or {}).get("name"):
            return 409
        typo = hl.put(f"/api/holidays/{hid}", json={"name": "X", "bogus": 1},
                      headers={"X-CSRF-Token": htok, "Content-Type": "application/json"})
        if typo.status_code != 400:
            return 409
        # A copy that must skip a leap day rather than shift it.
        leap = "2036-02-29"
        if _post(hl, htok, "/api/holidays", {
            "name": "Probe Cal Leap", "date": leap, "type": "Optional",
        }).status_code != 201:
            return 409
        copied = _post(hl, htok, "/api/holidays/copy-year",
                       {"from_year": 2036, "to_year": 2037})
        if copied.status_code != 200:
            return copied.status_code
        body = copied.get_json() or {}
        skipped = {s.get("name") for s in body.get("skipped", [])}
        if "Probe Cal Leap" not in skipped:
            return 409
        with psycopg.connect(pg_dsn) as vpc:
            # 29 February does not exist in 2037, and nothing was invented.
            row = vpc.execute(
                "SELECT COUNT(*) FROM holidays WHERE year = 2037 "
                "AND name LIKE 'Probe Cal%' AND EXTRACT(MONTH FROM holiday_date) = 2 "
                "AND EXTRACT(DAY FROM holiday_date) IN (28, 29)"
            ).fetchone()
        if row and row[0]:
            return 409
        # The CSV export and the iCal feed, both for 2036 — the year this flow
        # actually put a holiday in. Asking a year with no holidays in it for an
        # `DTSTART` proves nothing: the feed is correctly empty, which is how the
        # first version of this flow reported a 409 for a working endpoint.
        exported = hl.get("/api/holidays/export?year=2036")
        if exported.status_code != 200:
            return exported.status_code
        text = exported.data.decode()
        if not text.startswith("name,date,type,location") or "Probe Cal Leap" not in text:
            return 409
        # The iCal feed: an all-day DTSTART, or clients show a time.
        feed = hl.get("/api/holidays/ical?year=2036")
        if feed.status_code != 200 or feed.mimetype != "text/calendar":
            return 409
        ics = feed.data.decode()
        if "DTSTART;VALUE=DATE:20360229" not in ics or "DTSTART:2" in ics:
            return 409
        if "SUMMARY:Probe Cal Leap (Optional)" not in ics:
            return 409
        # Delete is refused while an opt-in references the holiday (a foreign key).
        if hl.delete(f"/api/holidays/{hid}",
                     headers={"X-CSRF-Token": htok}).status_code != 200:
            return 409
        return 200
    run("holidays(calendar CRUD, copy, export, iCal)", holiday_calendar)

    # ── FR-NOT-03: preferences, and the taxonomy they key on ────────────────
    # The stored categories and the SRS's preference taxonomy had nothing in common
    # before this: leave notifications were stored as `Leave` where the SRS says
    # `Leaves`, and tickets, goals, reviews and holiday opt-ins all fell through to
    # `General`. This flow reads a notification back and checks the category it
    # carries is a key a preference can actually be stored against.
    def notification_preferences():
        cl, tok, lc = _login(app_mod, dsn, "EMP002")
        if lc != 200:
            return lc
        before = cl.get("/api/notification-preferences").get_json() or {}
        prefs = before.get("preferences") or {}
        # Every category defaults on with no stored row.
        for category, channels in prefs.items():
            if channels != {"in_app": True, "email": True}:
                return 409
        # A category with nothing behind it yet is reported as such rather than
        # being presented as a working switch.
        taxonomy = {row["category"]: row for row in before.get("taxonomy", [])}
        if not taxonomy or "Leaves" not in taxonomy:
            return 409
        if taxonomy["Leaves"].get("in_srs") is not True:
            return 409
        # Read the categories off the rows themselves, through the v2.0 table.
        with psycopg.connect(pg_dsn) as vpc:
            stored = vpc.execute(
                "SELECT DISTINCT category FROM notifications WHERE emp_id = 'EMP002' "
                'AND category IS NOT NULL'
            ).fetchall()
        for (category,) in stored:
            if category != "General" and category not in prefs:
                return 409
        # Turn a category off and confirm the canonical row is written.
        updated = _put(cl, tok, "/api/notification-preferences",
                       {"Leaves": {"in_app": False}})
        if updated.status_code != 200:
            return updated.status_code
        if (updated.get_json() or {}).get("changed") != ["Leaves"]:
            return 409
        with psycopg.connect(pg_dsn) as vpc:
            row = vpc.execute(
                "SELECT in_app, email FROM notification_preferences "
                "WHERE emp_id = 'EMP002' AND category = 'Leaves'"
            ).fetchone()
        if not row or row[0] is not False or row[1] is not True:
            return 409
        # A partial update must not reset the others.
        second = _put(cl, tok, "/api/notification-preferences",
                      {"Expenses": {"email": False}})
        if second.status_code != 200:
            return second.status_code
        after = (second.get_json() or {}).get("preferences") or {}
        if (after.get("Leaves") or {}).get("in_app") is not False:
            return 409
        if (after.get("Tickets") or {}).get("in_app") is not True:
            return 409
        # And a bad payload is refused with a 400, not a silent no-op.
        if _put(cl, tok, "/api/notification-preferences",
                {"Nope": {"in_app": True}}).status_code != 400:
            return 409
        if _put(cl, tok, "/api/notification-preferences",
                {"Leaves": "yes"}).status_code != 400:
            return 409
        return 200
    run("notification-preferences(taxonomy + per-category switch)", notification_preferences)










    run("leave-accrual(monthly ledger)", leave_accrual)

    # ── FR-JOB-01: nightly finalisation against the v2.0 identity key and
    #    effective-dated shift/weekly-off assignment ─────────────────────────
    def attendance_finalize():
        from app import finalize_attendance_for_date

        start = datetime.combine(attendance_date, datetime.strptime("10:00", "%H:%M").time())
        end = datetime.combine(attendance_date, datetime.strptime("19:00", "%H:%M").time())
        with psycopg.connect(dsn.replace("postgresql+psycopg://", "postgresql://"), autocommit=True) as pc:
            pc.execute(
                "INSERT INTO user_sessions "
                "(emp_id, login_time, logout_time, total_hours, session_date) "
                "VALUES ('EMP002', %s, %s, 9, %s)",
                [start, end, attendance_date],
            )
        result = finalize_attendance_for_date(
            attendance_date, employee_ids=["EMP002"], as_of=datetime.combine(attendance_date, datetime.strptime("20:00", "%H:%M").time()),
        )
        with psycopg.connect(dsn.replace("postgresql+psycopg://", "postgresql://"), autocommit=True) as pc:
            row = pc.execute(
                "SELECT status, shift_hours, source, version FROM attendance_days "
                "WHERE emp_id = 'EMP002' AND attendance_date = %s",
                [attendance_date],
            ).fetchone()
        return 200 if (
            result.get("processed") == 1
            and row == ("Present", 9, "job", 1)
        ) else 409
    run("attendance(finalize)", attendance_finalize)

    # ── password-reset journey (public POSTs, no session required) ─────────
    def forgot_password():
        with psycopg.connect(dsn.replace("postgresql+psycopg://", "postgresql://"), autocommit=True) as pc:
            row = pc.execute("SELECT email FROM users WHERE emp_id = 'EMP002'").fetchone()
        if not row:
            return 409
        r = _post(cl, tok, "/api/forgot-password",
                  {"emp_id": "EMP002", "email": row[0]})
        if r.status_code == 200 and r.is_json:
            state["reset_token"] = (r.get_json() or {}).get("token")
        return r.status_code
    run("auth(forgot-password)", forgot_password)

    def reset_password_flow():
        t = state.get("reset_token")
        if not t:
            return 409
        return _post(cl, tok, "/api/reset-password",
                     {"token": t, "new_password": PROBE_PASSWORD}).status_code
    run("auth(reset-password)", reset_password_flow)

    # ── help-desk journey: employee creates + comments, admin resolves ─────
    def ticket_create():
        r = _post(cl, tok, "/api/tickets",
                  {"subject": "public write probe", "description": "ticket probe",
                   "category": "IT", "priority": "Medium"})
        if r.status_code == 201 and r.is_json:
            state["ticket_id"] = (r.get_json() or {}).get("id")
        return r.status_code
    run("tickets(create)", ticket_create)

    def ticket_comment():
        tid = state.get("ticket_id")
        if not tid:
            return 409
        return _post(cl, tok, f"/api/tickets/{tid}/comment", {"comment": "probe comment"}).status_code
    run("tickets(comment)", ticket_comment)

    def ticket_resolve():
        tid = state.get("ticket_id")
        if not tid:
            return 409
        # FR-TKT-04 is a chain, so Open -> Resolved is refused. This step used to
        # jump straight there and the probe never noticed because there was no
        # state machine to notice with.
        if _put(cl_a, tok_a, f"/api/tickets/{tid}/status", {"status": "In Progress"}).status_code != 200:
            return 409
        return _put(cl_a, tok_a, f"/api/tickets/{tid}/status", {"status": "Resolved"}).status_code
    run("tickets(resolve)", ticket_resolve)

    # ── ATS journey: Applied → Screened → Interviewed → Offered → accepted ─
    cand_marker = f"probe-cand-{os.getpid()}@company.com"

    def candidate_create():
        r = _post(cl_a, tok_a, "/api/candidates",
                  {"name": "Probe Candidate", "email": cand_marker,
                   "phone": "9999900000", "resume_text": "public write probe"})
        if r.status_code == 201 and r.is_json:
            state["candidate_id"] = (r.get_json() or {}).get("id")
        return r.status_code
    run("ats(candidate create)", candidate_create)

    def direct_hired_guard():
        cid = state.get("candidate_id")
        if not cid:
            return 409
        response = _put(cl_a, tok_a, f"/api/candidates/{cid}/status", {"status": "Hired"})
        return 200 if response.status_code == 409 else response.status_code
    run("ats(direct Hired guard)", direct_hired_guard)

    def candidate_screened():
        cid = state.get("candidate_id")
        if not cid:
            return 409
        return _put(cl_a, tok_a, f"/api/candidates/{cid}/status", {"status": "Screened"}).status_code
    run("ats(candidate screened)", candidate_screened)

    def candidate_interviewed():
        cid = state.get("candidate_id")
        if not cid:
            return 409
        return _put(cl_a, tok_a, f"/api/candidates/{cid}/status", {"status": "Interviewed"}).status_code
    run("ats(candidate interviewed)", candidate_interviewed)

    def offer_send():
        cid = state.get("candidate_id")
        if not cid:
            return 409
        r = _post(cl_a, tok_a, "/api/offers", {
            "candidate_id": cid, "offered_salary": 600000,
            "basic_pct": 50, "hra_pct": 30, "allowances_pct": 20,
        })
        if r.status_code == 201 and r.is_json:
            state["offer_id"] = (r.get_json() or {}).get("id")
        return r.status_code
    run("ats(offer send)", offer_send)

    def offer_accept():
        oid = state.get("offer_id")
        if not oid:
            return 409
        response = _post(cl_a, tok_a, f"/api/offers/{oid}/accept")
        if response.status_code != 200:
            return response.status_code
        state["prehire_id"] = (response.get_json() or {}).get("emp_id")
        state["workflow_id"] = (response.get_json() or {}).get("workflow_id")
        state["preboarding_token"] = (response.get_json() or {}).get("preboarding_token")
        cid = state.get("candidate_id")
        with psycopg.connect(dsn.replace("postgresql+psycopg://", "postgresql://"), autocommit=True) as pc:
            row = pc.execute(
                "SELECT c.status, u.allow_login, u.status, w.completed, "
                "(SELECT COUNT(*) FROM onboarding_checklist x WHERE x.workflow_id = w.workflow_id), "
                "(SELECT COUNT(*) FROM salary_structures s WHERE s.emp_id = u.emp_id) "
                "FROM candidates c JOIN users u ON u.candidate_id = c.candidate_id "
                "JOIN onboarding_workflow w ON w.candidate_id = c.candidate_id WHERE c.candidate_id = %s",
                [cid],
            ).fetchone()
        return 200 if row == ('Hired', False, 'Pre-hire', False, 5, 1) else 409
    run("ats(offer accept)", offer_accept)

    def preboarding_view():
        token = state.get("preboarding_token")
        if not token:
            return 409
        return cl_a.get(f"/api/preboarding/{token}").status_code
    run("onboarding(preboarding token)", preboarding_view)

    def preboarding_documents():
        token = state.get("preboarding_token")
        if not token:
            return 409
        for doc_type in ("ID%20Proof", "Address%20Proof", "Education", "Certification", "Bank%20Details"):
            response = cl_a.post(
                f"/api/preboarding/{token}/documents/{doc_type}",
                data={"file": (BytesIO(b"%PDF-1.4\npublic probe"), "document.pdf", "application/pdf")},
                headers={"X-CSRF-Token": tok_a},
            )
            if response.status_code != 201:
                return response.status_code
        return cl_a.post(f"/api/preboarding/{token}/submit").status_code
    run("onboarding(documents + submit)", preboarding_documents)

    def preboarding_review():
        wid = state.get("workflow_id")
        if not wid:
            return 409
        with psycopg.connect(dsn.replace("postgresql+psycopg://", "postgresql://"), autocommit=True) as pc:
            items = pc.execute(
                "SELECT item_id FROM onboarding_checklist WHERE workflow_id = %s", [wid]
            ).fetchall()
        for (item_id,) in items:
            response = _post(cl_a, tok_a, f"/api/onboarding-checklist/{item_id}/review",
                             {"status": "Approved", "note": "probe approved"})
            if response.status_code != 200:
                return response.status_code
        with psycopg.connect(dsn.replace("postgresql+psycopg://", "postgresql://"), autocommit=True) as pc:
            row = pc.execute(
                "SELECT step2_status, step3_status FROM onboarding_workflow WHERE workflow_id = %s", [wid]
            ).fetchone()
        return 200 if row == ('Completed', 'InProgress') else 409
    run("onboarding(HR document review)", preboarding_review)

    def onboarding_task_completion():
        emp_id = state.get("prehire_id")
        if not emp_id:
            return 409
        tasks = cl_a.get("/api/onboarding-tasks").get_json() or []
        tasks = sorted((task for task in tasks if task.get("emp_id") == emp_id), key=lambda task: task.get("stage") or 0)
        for task in tasks:
            response = _post(cl_a, tok_a, f"/api/onboarding-tasks/{task['id']}/complete")
            if response.status_code != 200:
                return response.status_code
        return 200
    run("onboarding(task guards + steps)", onboarding_task_completion)

    # ── corrected offboarding: resignation → parallel clearance → settlement
    #    maker-checker → LWD access revocation ────────────────────────────────
    def resignation_create():
        emp_id = state.get("prehire_id")
        if not emp_id:
            return 409
        r = _post(cl_a, tok_a, "/api/resignations", {
            "emp_id": emp_id,
            "notice_date": today.isoformat(),
            "last_working_day": today.isoformat(),
            "reason": "public lifecycle probe",
        })
        if r.status_code == 201 and r.is_json:
            state["offboard_id"] = (r.get_json() or {}).get("offboard_id")
            state["resignation_id"] = (r.get_json() or {}).get("resignation_id")
        return r.status_code
    run("offboarding(resignation)", resignation_create)

    def resignation_ack():
        rid = state.get("resignation_id")
        if not rid:
            return 409
        return _post(cl_a, tok_a, f"/api/resignations/{rid}/acknowledge").status_code
    run("offboarding(acknowledge)", resignation_ack)

    def offboarding_parallel_clearance():
        oid = state.get("offboard_id")
        emp_id = state.get("prehire_id")
        if not oid or not emp_id:
            return 409
        first = _post(cl_a, tok_a, f"/api/offboarding-workflows/{oid}/stages/2/complete")
        if first.status_code != 200:
            return first.status_code
        with psycopg.connect(dsn.replace("postgresql+psycopg://", "postgresql://"), autocommit=True) as pc:
            asset_id = pc.execute(
                "INSERT INTO assets (emp_id, asset_type, issued_date, status) VALUES (%s, 'Laptop', %s, 'Issued') RETURNING asset_id",
                [emp_id, today],
            ).fetchone()[0]
        blocked = _post(cl_a, tok_a, f"/api/offboarding-workflows/{oid}/stages/3/complete")
        with psycopg.connect(dsn.replace("postgresql+psycopg://", "postgresql://"), autocommit=True) as pc:
            pc.execute(
                "UPDATE assets SET status = 'Returned', return_date = %s WHERE asset_id = %s",
                [today, asset_id],
            )
        second = _post(cl_a, tok_a, f"/api/offboarding-workflows/{oid}/stages/3/complete")
        return 200 if blocked.status_code == 409 and second.status_code == 200 else 409
    run("offboarding(parallel stages 2/3)", offboarding_parallel_clearance)

    def settlement_prepare():
        oid = state.get("offboard_id")
        if not oid:
            return 409
        return _post(cl_f, tok_f, f"/api/offboarding-workflows/{oid}/stage/4/prepare").status_code
    run("offboarding(settlement prepare)", settlement_prepare)

    def settlement_approve():
        oid = state.get("offboard_id")
        if not oid:
            return 409
        return _post(cl_a, tok_a, f"/api/offboarding-workflows/{oid}/stage/4/approve").status_code
    run("offboarding(settlement approve)", settlement_approve)

    def lwd_revoke():
        r = _post(cl_a, tok_a, "/api/admin/offboarding/revoke", {"date": today.isoformat()})
        if r.status_code != 200:
            return r.status_code
        emp_id = state.get("prehire_id")
        with psycopg.connect(dsn.replace("postgresql+psycopg://", "postgresql://"), autocommit=True) as pc:
            row = pc.execute("SELECT status, allow_login FROM users WHERE emp_id = %s", [emp_id]).fetchone()
        return 200 if row == ('Inactive', False) else 409
    run("offboarding(LWD revoke)", lwd_revoke)

    # ── payroll maker-checker: create → submit (Finance) → approve (Admin)
    #    → finalize, then bank-file + TDS exports ───────────────────────────
    pay_year = 2099 + (os.getpid() % 50)

    def payroll_create():
        r = _post(cl_a, tok_a, "/api/payroll-runs", {"month": 1, "year": pay_year})
        if r.status_code != 201:
            return r.status_code
        with psycopg.connect(dsn.replace("postgresql+psycopg://", "postgresql://"), autocommit=True) as pc:
            row = pc.execute(
                "SELECT run_id FROM payroll_runs WHERE month = 1 AND year = %s", [pay_year],
            ).fetchone()
        if not row:
            return 409
        state["run_id"] = row[0]
        return 201
    run("payroll(run create)", payroll_create)

    def payroll_submit():
        rid = state.get("run_id")
        if not rid:
            return 409
        return _post(cl_f, tok_f, f"/api/payroll-runs/{rid}/submit").status_code
    run("payroll(submit)", payroll_submit)

    def payroll_approve():
        rid = state.get("run_id")
        if not rid:
            return 409
        return _post(cl_a, tok_a, f"/api/payroll-runs/{rid}/approve").status_code
    run("payroll(approve)", payroll_approve)

    def payroll_finalize():
        rid = state.get("run_id")
        if not rid:
            return 409
        status = _post(cl_a, tok_a, f"/api/payroll-runs/{rid}/finalize").status_code
        if status != 200:
            return status
        with psycopg.connect(dsn.replace("postgresql+psycopg://", "postgresql://"), autocommit=True) as pc:
            trail = pc.execute(
                "SELECT COUNT(*) FROM payroll_approvals WHERE run_id = %s", [rid],
            ).fetchone()[0]
        return 200 if trail == 3 else 409
    run("payroll(finalize)", payroll_finalize)

    def bank_file():
        rid = state.get("run_id")
        if not rid:
            return 409
        return cl_a.get(f"/api/payroll-runs/{rid}/bank-file").status_code
    run("payroll(bank-file)", bank_file)

    def tds_report():
        rid = state.get("run_id")
        if not rid:
            return 409
        return cl_a.get(f"/api/payroll-runs/{rid}/tds-report").status_code
    run("payroll(tds-report)", tds_report)

    # ── CC-07 idempotency (same key twice -> stored response replay) ─────
    run("users(create) verify GETs", lambda: cl_a.get(f"/api/users/{uniq}").status_code)

    def idem_replay():
        url = "/api/leaves"
        body = {
            "leave_type": "Casual",
            "start_date": (today + timedelta(days=40)).isoformat(),
            "end_date": (today + timedelta(days=40)).isoformat(),
            "reason": "idempotency probe",
        }
        hdrs = {"X-CSRF-Token": tok_a, "Content-Type": "application/json",
                "Idempotency-Key": "probe-ik-1"}
        r1 = cl_a.post(url, json=body, headers=hdrs)
        if r1.status_code != 201:
            return r1.status_code
        r2 = cl_a.post(url, json=body, headers=hdrs)
        if r2.status_code != 201 or r2.get_json() != r1.get_json():
            return 409  # no replay stored on the v2.0 idempotency_keys (JSONB)
        with psycopg.connect(dsn.replace("postgresql+psycopg://", "postgresql://"), autocommit=True) as pc:
            n = pc.execute(
                "SELECT count(*) FROM leave_requests WHERE emp_id = %s AND reason = 'idempotency probe'",
                ["EMP001"],
            ).fetchone()[0]
        return 200 if n == 1 else 409  # duplicate applied despite replay
    run("idempotency(replay leaves x2)", idem_replay)

    # ── FR-AUTH-03 lockout: the new `users` columns on the v2.0 target ─────────
    # Last, deliberately. It locks EMP002 for real, and every flow above signs in
    # as EMP002, so anything after it would fail for the wrong reason.
    def lockout_lock():
        # A throwaway client per attempt, because a parked MFA login is not what
        # this is testing and a reused session would carry state between tries.
        last = None
        for _ in range(_LOCKOUT_ATTEMPTS):
            lc = app_mod.test_client()
            lt = lc.get("/api/csrf-token").get_json()["csrf_token"]
            last = lc.post(
                "/login",
                json={"emp_id": "EMP002", "password": "definitely-wrong"},
                headers={"X-CSRF-Token": lt},
            )
        # The tenth failure locks, and the body is the uniform FR-AUTH-02 answer —
        # a distinct "locked" reply here would be an enumeration channel.
        if last is None or last.status_code != 401 \
                or last.get_json() != {"error": "invalid_credentials"}:
            return last.status_code if last else 500
        with psycopg.connect(dsn, autocommit=True) as pc:
            row = pc.execute(
                "SELECT failed_attempts, locked_until FROM users WHERE emp_id = 'EMP002'"
            ).fetchone()
        if not row or row[1] is None:
            return 409  # locked_until never written on the v2.0 target
        # And the correct password is refused while the lock stands.
        ok = app_mod.test_client()
        kt = ok.get("/api/csrf-token").get_json()["csrf_token"]
        refused = ok.post(
            "/login", json={"emp_id": "EMP002", "password": "pass123"},
            headers={"X-CSRF-Token": kt},
        )
        # 200 means "every assertion in this flow held". Returning the 401 that the
        # lock legitimately produces would book the flow as *guarded*, which the
        # summary counts as not-served — the harness reserves a 4xx for "the guard
        # I expected did not fire", and here it fired exactly as intended.
        return 200 if refused.status_code == 401 else refused.status_code
    run("auth(lockout after 10 failures)", lockout_lock)

    def lockout_unlock():
        r = _post(cl_a, tok_a, "/api/admin/users/EMP002/unlock")
        if r.status_code != 200:
            return r.status_code
        if not (r.get_json() or {}).get("removed"):
            return 409  # there was no lock to clear, so this flow proved nothing
        with psycopg.connect(dsn, autocommit=True) as pc:
            row = pc.execute(
                "SELECT failed_attempts, locked_until FROM users WHERE emp_id = 'EMP002'"
            ).fetchone()
        if row[0] or row[1] is not None:
            return 409  # unlock did not clear the v2.0 columns
        # The account works again, which is the whole point of clearing it.
        #
        # `PROBE_PASSWORD`, not "pass123": the `auth(reset-password)` flow earlier
        # in this run changed EMP002's password, and the seeded hash is only restored
        # by the *next* run's cleanup. Signing in with the seeded password here
        # returns 401 — which is also this flow's own lockout working, so it is a
        # confusing way to fail.
        cl2 = app_mod.test_client()
        lt2 = cl2.get("/api/csrf-token").get_json()["csrf_token"]
        st = cl2.post(
            "/login", json={"emp_id": "EMP002", "password": PROBE_PASSWORD},
            headers={"X-CSRF-Token": lt2},
        ).status_code
        return 200 if st == 200 else st
    run("auth(admin unlock + sign-in again)", lockout_unlock)

    return out


def main() -> int:
    if os.getenv("APP_DB_SCHEMA", "public") != "public":
        print("should run with APP_DB_SCHEMA=public")
        return 1
    schema = os.getenv("APP_DB_SCHEMA", "public")
    dsn = (os.getenv("DATABASE_URL") or "").replace("postgresql+psycopg://", "postgresql://")
    print(f"probe target: {dsn}  schema={schema}")

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

    # Supress the ADAPTER's own boolean rewrite introspection noise: none needed.
    import db_backend  # noqa: F401
    from security import hash_password

    with psycopg.connect(dsn, autocommit=True) as pconn:
        ph = hash_password("pass123")
        if pconn.execute("SELECT count(*) FROM users").fetchone()[0] == 0:
            pconn.execute(
                "INSERT INTO users (emp_id, name, email, password, role, department,"
                " designation, phone, date_of_joining, status, allow_login, allow_breaks,"
                " first_login, created_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                ["EMP001", "Probe Admin", "probe@company.com", ph, "Admin", "MIS", "Tech Lead",
                 "9876543210", "2024-01-01", "Active", True, True, "2024-01-01", "2024-01-01"],
            )
            # The app's init_db sample-seed writes rows for EMP001 *and* EMP002,
            # and v2.0 enforces the FKs — seed both users here so the sample
            # data can insert.
            pconn.execute(
                "INSERT INTO users (emp_id, name, email, password, role, department,"
                " designation, phone, date_of_joining, manager_emp_id, status, allow_login,"
                " allow_breaks, first_login, created_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                ["EMP002", "Probe User", "probe2@company.com", ph, "Employee", "Operations",
                 "Associate", "9876543211", "2024-01-01", "EMP001", "Active", True, True,
                 "2024-01-01", "2024-01-01"],
            )
        if not pconn.execute("SELECT 1 FROM users WHERE emp_id = 'EMP003'").fetchone():
            pconn.execute(
                "INSERT INTO users (emp_id, name, email, password, role, department,"
                " designation, phone, date_of_joining, status, allow_login, allow_breaks,"
                " first_login, created_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                ["EMP003", "Probe Finance", "probe3@company.com", ph, "Finance", "Finance",
                 "Finance Manager", "9876543212", "2024-01-01", "Active", True, True,
                 "2024-01-01", "2024-01-01"],
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

    # The GET sweep below runs on this client, so it needs a *fully* authenticated
    # session. EMP001 is an Admin and FR-AUTH-11 makes a second factor compulsory
    # for that role, so a bare password parks the login and every route would then
    # answer 401 — which reads as a broken public flip rather than as a login that
    # stopped half way. `_login` walks the second factor.
    c, tok, status = _login(app, dsn, "EMP001")
    print("login:", status)
    if status != 200:
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

    write = _write_flows(app, dsn)

    by = Counter(statuses.values())
    by_w = Counter(v[0] for v in write.values())
    print("\n=== GET route status distribution ===")
    for k, v in by.most_common():
        print(f"  {k}: {v}")
    print("\n=== write-flow status distribution ===")
    for k, v in by_w.most_common():
        print(f"  {k}: {v}")
    for name, (status, detail) in write.items():
        if status != "OK":
            print(f"  {name}: {status}  {detail}")

    # `/api/mfa/qr` is the one route whose *correct* answer depends on state the
    # sweep has already left behind: it serves the provisioning image only while an
    # enrolment is in progress, and answers 404 once confirmed, because re-serving
    # a live bearer secret after the fact would be strictly worse. The sweep runs
    # after a completed login, so 404 is the right answer here — recorded with its
    # reason rather than filtered out silently, and asserted where it *does* apply
    # (during enrolment, in `_login`).
    EXPECTED_GET_404 = {
        "/api/mfa/qr": "only served during an in-progress enrolment; 404 once confirmed",
    }
    fails = [u for u, s in statuses.items() if s != 200 and u not in EXPECTED_GET_404]
    wfails = [n for n, (s, _) in write.items() if s != "OK"]
    print(f"\n{len(statuses) - len(fails)}/{len(statuses)} authenticated GET routes served from {schema}")
    print(f"{len(write) - len(wfails)}/{len(write)} core write flows served from {schema}")

    print("\n=== seed-level deltas (init_db inserts still rejected by v2.0) ===")
    for sql, err in _SEED_FAILURES:
        table = sql.split(" ")[2] if len(sql.split(" ")) > 2 else "?"
        print(f"  {table}: {err[:140].splitlines()[0]}")

    print("\n=== unresolved route failures ===")
    for u in fails:
        print(f"  {u}  ({statuses[u]})")
    print("\n=== unresolved write-flow failures ===")
    for n, (s, detail) in write.items():
        if s != "OK":
            print(f"  {n}: {s}  {detail}")

    if not fails and not wfails:
        print("\nREADINESS: all measured GET routes and core write flows green on v2.0 public.")
    return 0 if not (fails or wfails) else 2


if __name__ == "__main__":
    sys.exit(main())
