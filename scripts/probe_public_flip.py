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


def _login(app_mod, emp_id: str):
    cl = app_mod.test_client()
    tok = cl.get("/api/csrf-token").get_json()["csrf_token"]
    r = cl.post("/login", json={"emp_id": emp_id, "password": "pass123"},
                headers={"X-CSRF-Token": tok})
    return cl, tok, r.status_code


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


def _write_flows(app_mod, dsn) -> dict[str, tuple[str, str]]:
    """Fire the core state-changing flows against ``public`` exactly as the
    legacy browser tests do; bucket OK (2xx) vs guarded (4xx, route served and
    business rule fired) vs failed (5xx/EXC)."""
    from datetime import date, datetime, timedelta

    out: dict[str, tuple[str, str]] = {}
    today = date.today()
    days_until_monday = (7 - today.weekday()) % 7 or 7
    attendance_date = today + timedelta(days=days_until_monday)
    state: dict = {}

    # Clear residue from prior probe runs so dedupe guards don't mask results.
    with psycopg.connect(dsn.replace("postgresql+psycopg://", "postgresql://"), autocommit=True) as pc:
        pc.execute("DELETE FROM regularization_requests WHERE reason = 'public write probe'")
        pc.execute("DELETE FROM leave_requests WHERE reason IN ('public write probe', 'idempotency probe')")
        pc.execute("DELETE FROM break_approvals WHERE reason = 'public write probe'")
        pc.execute("UPDATE breaks SET status = 'Completed' WHERE emp_id = 'EMP002' AND status = 'Active'")
        pc.execute("DELETE FROM ticket_comments WHERE comment = 'probe comment'")
        pc.execute("DELETE FROM tickets WHERE subject = 'public write probe'")
        pc.execute("DELETE FROM onboarding_checklist WHERE workflow_id IN ("
                   "SELECT w.workflow_id FROM onboarding_workflow w JOIN candidates c "
                   "ON c.candidate_id = w.candidate_id WHERE c.email LIKE 'probe-cand-%')")
        pc.execute("DELETE FROM offboarding_settlements WHERE offboard_id IN ("
                   "SELECT w.offboard_id FROM offboarding_workflow w JOIN users u ON u.emp_id = w.emp_id "
                   "JOIN candidates c ON c.candidate_id = u.candidate_id WHERE c.email LIKE 'probe-cand-%')")
        pc.execute("DELETE FROM offboarding_approvals WHERE offboard_id IN ("
                   "SELECT w.offboard_id FROM offboarding_workflow w JOIN users u ON u.emp_id = w.emp_id "
                   "JOIN candidates c ON c.candidate_id = u.candidate_id WHERE c.email LIKE 'probe-cand-%')")
        pc.execute("DELETE FROM offboarding_workflow WHERE emp_id IN ("
                   "SELECT u.emp_id FROM users u JOIN candidates c ON c.candidate_id = u.candidate_id "
                   "WHERE c.email LIKE 'probe-cand-%')")
        pc.execute("DELETE FROM resignations WHERE emp_id IN ("
                   "SELECT u.emp_id FROM users u JOIN candidates c ON c.candidate_id = u.candidate_id "
                   "WHERE c.email LIKE 'probe-cand-%')")
        pc.execute("DELETE FROM onboarding_tasks WHERE emp_id IN ("
                   "SELECT u.emp_id FROM users u JOIN candidates c ON c.candidate_id = u.candidate_id "
                   "WHERE c.email LIKE 'probe-cand-%')")
        pc.execute("DELETE FROM offboarding_tasks WHERE emp_id IN ("
                   "SELECT u.emp_id FROM users u JOIN candidates c ON c.candidate_id = u.candidate_id "
                   "WHERE c.email LIKE 'probe-cand-%')")
        pc.execute("DELETE FROM exit_interviews WHERE emp_id IN ("
                   "SELECT u.emp_id FROM users u JOIN candidates c ON c.candidate_id = u.candidate_id "
                   "WHERE c.email LIKE 'probe-cand-%')")
        pc.execute("DELETE FROM onboarding_workflow WHERE candidate_id IN "
                   "(SELECT candidate_id FROM candidates WHERE email LIKE 'probe-cand-%')")
        pc.execute("DELETE FROM salary_structures WHERE emp_id IN ("
                   "SELECT u.emp_id FROM users u JOIN candidates c ON c.candidate_id = u.candidate_id "
                   "WHERE c.email LIKE 'probe-cand-%')")
        pc.execute("DELETE FROM documents WHERE emp_id IN ("
                   "SELECT u.emp_id FROM users u JOIN candidates c ON c.candidate_id = u.candidate_id "
                   "WHERE c.email LIKE 'probe-cand-%')")
        pc.execute("DELETE FROM assets WHERE emp_id IN ("
                   "SELECT u.emp_id FROM users u JOIN candidates c ON c.candidate_id = u.candidate_id "
                   "WHERE c.email LIKE 'probe-cand-%')")
        pc.execute("DELETE FROM notifications WHERE emp_id IN ("
                   "SELECT u.emp_id FROM users u JOIN candidates c ON c.candidate_id = u.candidate_id "
                   "WHERE c.email LIKE 'probe-cand-%')")
        pc.execute("DELETE FROM audit_log WHERE emp_id IN ("
                   "SELECT u.emp_id FROM users u JOIN candidates c ON c.candidate_id = u.candidate_id "
                   "WHERE c.email LIKE 'probe-cand-%')")
        pc.execute("DELETE FROM user_sessions WHERE emp_id IN ("
                   "SELECT u.emp_id FROM users u JOIN candidates c ON c.candidate_id = u.candidate_id "
                   "WHERE c.email LIKE 'probe-cand-%')")
        pc.execute("DELETE FROM employee_documents WHERE emp_id IN ("
                   "SELECT u.emp_id FROM users u JOIN candidates c ON c.candidate_id = u.candidate_id "
                   "WHERE c.email LIKE 'probe-cand-%')")
        pc.execute("DELETE FROM user_permissions WHERE emp_id IN ("
                   "SELECT u.emp_id FROM users u JOIN candidates c ON c.candidate_id = u.candidate_id "
                   "WHERE c.email LIKE 'probe-cand-%')")
        pc.execute("DELETE FROM password_reset_tokens WHERE emp_id IN ("
                   "SELECT u.emp_id FROM users u JOIN candidates c ON c.candidate_id = u.candidate_id "
                   "WHERE c.email LIKE 'probe-cand-%')")
        pc.execute("DELETE FROM shift_assignments WHERE emp_id IN ("
                   "SELECT u.emp_id FROM users u JOIN candidates c ON c.candidate_id = u.candidate_id "
                   "WHERE c.email LIKE 'probe-cand-%')")
        pc.execute("DELETE FROM users WHERE candidate_id IN "
                   "(SELECT candidate_id FROM candidates WHERE email LIKE 'probe-cand-%')")
        pc.execute("DELETE FROM offer_letters WHERE candidate_id IN "
                   "(SELECT candidate_id FROM candidates WHERE email LIKE 'probe-cand-%')")
        pc.execute("DELETE FROM candidates WHERE email LIKE 'probe-cand-%'")
        pc.execute("DELETE FROM payroll_approvals WHERE run_id IN "
                   "(SELECT run_id FROM payroll_runs WHERE year >= 2099)")
        pc.execute("DELETE FROM payroll_items WHERE run_id IN "
                   "(SELECT run_id FROM payroll_runs WHERE year >= 2099)")
        pc.execute("DELETE FROM payroll_runs WHERE year >= 2099")
        pc.execute("DELETE FROM password_reset_tokens WHERE emp_id = 'EMP002'")
        pc.execute("DELETE FROM idempotency_keys WHERE key LIKE 'probe-%'")
        pc.execute(
            "DELETE FROM attendance_days WHERE emp_id = 'EMP002' AND attendance_date = %s",
            [attendance_date],
        )
        pc.execute(
            "DELETE FROM user_sessions WHERE emp_id = 'EMP002' AND session_date = %s",
            [attendance_date],
        )

    def run(name, fn):
        try:
            status = fn()
            label = "OK" if 200 <= status < 300 else ("GUARDED" if 400 <= status < 500 else "FAIL")
            out[name] = (label, f"status={status}")
        except Exception as exc:
            out[name] = ("FAIL", f"{type(exc).__name__}: {str(exc).splitlines()[0][:220]}")

    # ── employee flows ─────────────────────────────────────────────
    cl, tok, lc = _login(app_mod, "EMP002")
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
    cl_a, tok_a, lc_a = _login(app_mod, "EMP001")
    if lc_a != 200:
        out["login-EMP001"] = ("FAIL", f"login status={lc_a}")
        return out
    out["login-EMP001"] = ("OK", "status=200")
    cl_f, tok_f, lc_f = _login(app_mod, "EMP003")
    if lc_f != 200:
        out["login-EMP003"] = ("FAIL", f"login status={lc_f}")
        return out
    out["login-EMP003"] = ("OK", "status=200")

    uniq = f"TEST{int(date.today().strftime('%m%d'))}{os.getpid() % 10000:04d}"

    def create_user():
        return _post(cl_a, tok_a, "/api/users",
                     {"emp_id": uniq, "name": "Probe Tester", "email": f"{uniq.lower()}@company.com",
                      "department": "MIS", "role": "Employee", "password": "pass123"}).status_code
    run("users(create)", create_user)

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
                     {"token": t, "new_password": "pass123"}).status_code
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

    return out


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

    fails = [u for u, s in statuses.items() if s != 200]
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
