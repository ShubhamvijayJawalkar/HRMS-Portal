import os
import sys
import json
import tempfile
import bcrypt
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

os.environ['SECRET_KEY'] = 'test-secret-key'
os.environ['DB_FILE'] = os.path.join(tempfile.gettempdir(), f'hrms_test_{datetime.now().timestamp()}.duckdb')
os.environ['FLASK_DEBUG'] = '0'

import pytest
from app import app, get_db, hash_password, gen_id
from hrms.helpers import now_ist


@pytest.fixture
def client():
    app.config['TESTING'] = True
    app.config['SERVER_NAME'] = 'localhost'
    with app.test_client() as c:
        with app.app_context():
            yield c


@pytest.fixture
def auth_client(client):
    client.post('/login', json={'emp_id': 'EMP001', 'password': 'pass123'})
    return client


def cleanup():
    from hrms.db import close_db
    close_db()
    try:
        os.remove(os.environ['DB_FILE'])
    except OSError:
        pass


# ── Basic Tests ─────────────────────────────────────────────────

def test_index_redirect(client):
    resp = client.get('/')
    assert resp.status_code == 302


def test_login_page(client):
    resp = client.get('/login')
    assert resp.status_code == 200
    assert b'HRMS Portal' in resp.data


def test_login_missing_credentials(client):
    resp = client.post('/login', json={})
    assert resp.status_code == 400
    assert b'Missing' in resp.data or b'error' in resp.data


def test_login_invalid_emp(client):
    resp = client.post('/login', json={'emp_id': 'NONEXIST', 'password': 'x'})
    assert resp.status_code == 401


def test_login_admin_success(client):
    resp = client.post('/login', json={'emp_id': 'EMP001', 'password': 'pass123'})
    data = resp.get_json()
    assert resp.status_code == 200
    assert data.get('redirect') == '/dashboard'


# ── Authentication Tests ────────────────────────────────────────

def test_dashboard_requires_login(client):
    resp = client.get('/dashboard')
    assert resp.status_code == 302


def test_admin_dashboard_redirect(auth_client):
    resp = auth_client.get('/dashboard')
    assert resp.status_code == 200


def test_api_users_requires_admin(client):
    resp = client.get('/api/users')
    assert resp.status_code in (302, 401)


# ── Swagger Tests ───────────────────────────────────────────────

def test_swagger_docs(client):
    resp = client.get('/docs/')
    assert resp.status_code in (200, 302)


def test_apispec(client):
    resp = client.get('/apispec.json')
    assert resp.status_code in (200, 302)


# ── Database Tests ──────────────────────────────────────────────

def test_audit_log_table_exists(client):
    conn = get_db()
    tables = conn.execute("SELECT table_name FROM information_schema.tables WHERE table_name='audit_log'").fetchall()
    conn.close()
    assert len(tables) > 0


def test_leave_tables_exist(client):
    conn = get_db()
    for t in ['leave_requests', 'leave_balance', 'password_reset_tokens']:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name=?", [t]
        ).fetchall()
        assert len(rows) > 0, f"Table {t} not found"
    conn.close()


def test_leave_types(client):
    conn = get_db()
    types = conn.execute("SELECT break_type FROM break_types").fetchall()
    conn.close()
    assert len(types) >= 3


def test_seed_data_has_multiple_entries_per_model(client):
    conn = get_db()
    tables = [
        'users', 'user_sessions', 'break_types', 'breaks', 'audit_log',
        'leave_requests', 'leave_balance', 'password_reset_tokens',
        'employee_documents', 'dependents', 'holidays', 'notifications',
        'regularization_requests', 'assets', 'job_postings', 'candidates',
        'interviews', 'offer_letters', 'onboarding_tasks', 'offboarding_tasks',
        'exit_interviews', 'offboarding_workflow', 'salary_structures', 'payroll_runs', 'payroll_items',
        'goals', 'performance_reviews', 'feedback_360', 'expense_categories',
        'expense_claims', 'tickets', 'ticket_comments', 'documents'
    ]
    for table in tables:
        count = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        assert count >= 2, f'{table} should have at least 2 seeded rows, found {count}'
    conn.close()


# ── Helper Tests ────────────────────────────────────────────────

def test_password_hashing():
    h = hash_password('test123')
    assert h.startswith('$2')
    assert bcrypt.checkpw(b'test123', h.encode())


# ── Authenticated API Tests (use session_transaction) ──────────

def test_profile_api(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99999
    with client.session_transaction() as sess:
        assert sess.get('emp_id') == 'EMP001'
    resp = client.get('/api/profile')
    assert resp.status_code == 200, f'Expected 200, got {resp.status_code}'
    data = resp.get_json()
    assert data['emp_id'] == 'EMP001'


def test_change_password(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99998
    resp = client.post('/api/change-password', json={
        'current_password': 'pass123',
        'new_password': 'newpass123'
    })
    assert resp.status_code == 200, f'Expected 200, got {resp.status_code}'
    resp = client.post('/api/change-password', json={
        'current_password': 'newpass123',
        'new_password': 'pass123'
    })
    assert resp.status_code == 200
    resp = client.post('/api/change-password', json={
        'current_password': 'wrong',
        'new_password': 'test123'
    })
    assert resp.status_code == 400


def test_active_users_endpoint_filters_inactive_employees(client):
    conn = get_db()
    conn.execute("UPDATE users SET status = 'Blocked' WHERE emp_id = ?", ['EMP002'])
    conn.commit()
    conn.close()
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99998
    resp = client.get('/api/users?active=1')
    assert resp.status_code == 200
    data = resp.get_json()
    assert all(item['status'] == 'Active' for item in data['data'])



def test_break_types_api(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99997
    resp = client.get('/api/break-types')
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) >= 1


def test_user_breaks(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99948
    resp = client.get('/api/user-breaks')
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)


def test_break_approvals_request(client):
    """Employee requests Lunch break approval"""
    conn = get_db()
    conn.execute("DELETE FROM break_approvals WHERE emp_id = 'EMP002'")
    conn.close()
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP002'
        sess['name'] = 'Sachin Bhakte'
        sess['role'] = 'Employee'
        sess['session_id'] = 99947
    resp = client.post('/api/break-approvals', json={
        'break_type': 'Lunch', 'reason': 'Need lunch break'
    })
    assert resp.status_code == 201
    data = resp.get_json()
    assert 'approval_id' in data


def test_break_approvals_non_lunch_rejected(client):
    """Non-Lunch break approvals should be rejected"""
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP002'
        sess['name'] = 'Sachin Bhakte'
        sess['role'] = 'Employee'
        sess['session_id'] = 99946
    resp = client.post('/api/break-approvals', json={
        'break_type': 'Tea', 'reason': 'Tea break'
    })
    assert resp.status_code == 400


def test_break_approvals_list(client):
    """Admin can see all break approvals"""
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO break_approvals (approval_id, emp_id, break_type, break_date, reason, status) VALUES (?, ?, ?, ?, ?, 'Pending')",
                 [999001, 'EMP002', 'Lunch', now_ist().date(), 'Test approval'])
    conn.close()
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99945
    resp = client.get('/api/break-approvals')
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)
    assert len(data) >= 1


def test_break_approvals_approve(client):
    """Admin approves a Lunch break request"""
    conn = get_db()
    conn.execute("DELETE FROM break_approvals WHERE emp_id = 'EMP002'")
    aid = 999002
    conn.execute("INSERT INTO break_approvals (approval_id, emp_id, break_type, break_date, reason, status) VALUES (?, ?, ?, ?, ?, 'Pending')",
                 [aid, 'EMP002', 'Lunch', now_ist().date(), 'Approve test'])
    conn.close()
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99944
    resp = client.post(f'/api/break-approvals/{aid}/approve')
    assert resp.status_code == 200
    assert b'Break approved' in resp.data


def test_break_approvals_reject(client):
    """Admin rejects a Lunch break request"""
    conn = get_db()
    aid = 999003
    conn.execute("INSERT OR REPLACE INTO break_approvals (approval_id, emp_id, break_type, break_date, reason, status) VALUES (?, ?, ?, ?, ?, 'Pending')",
                 [aid, 'EMP002', 'Lunch', now_ist().date(), 'Reject test'])
    conn.close()
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99943
    resp = client.post(f'/api/break-approvals/{aid}/reject')
    assert resp.status_code == 200
    assert b'Break rejected' in resp.data


def test_break_approvals_not_found(client):
    """Approve non-existent approval returns 404"""
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99942
    resp = client.post('/api/break-approvals/99999999/approve')
    assert resp.status_code == 404


def test_admin_breaks(client):
    """Admin breaks endpoint returns break data"""
    conn = get_db()
    # Ensure an active break exists for today
    shift_date = now_ist().date()
    exists = conn.execute("SELECT 1 FROM breaks WHERE emp_id = 'EMP001' AND status = 'Active' AND break_date = ?", [shift_date]).fetchone()
    if not exists:
        conn.execute("INSERT INTO breaks (break_id, emp_id, break_type, start_time, break_date, status) VALUES (?, ?, ?, ?, ?, 'Active')",
                     [999100, 'EMP001', 'Tea', now_ist(), shift_date])
    conn.close()
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99941
    resp = client.get('/api/admin/breaks')
    assert resp.status_code == 200
    data = resp.get_json()
    assert 'active_breaks' in data
    assert 'disposed_breaks' in data


def test_admin_dispose_break(client):
    """Admin can dispose (end) an active break"""
    conn = get_db()
    # Ensure an active break exists
    shift_date = now_ist().date()
    conn.execute("DELETE FROM breaks WHERE emp_id = 'EMP001' AND status = 'Active'")
    bid = 999101
    conn.execute("INSERT INTO breaks (break_id, emp_id, break_type, start_time, break_date, status) VALUES (?, ?, ?, ?, ?, 'Active')",
                 [bid, 'EMP001', 'Tea', now_ist(), shift_date])
    conn.close()
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99940
    resp = client.post(f'/api/admin/dispose-break/{bid}')
    assert resp.status_code == 200
    assert b'Break ended by admin' in resp.data


def test_admin_dispose_break_not_found(client):
    """Dispose non-existent break returns 404"""
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99939
    resp = client.post('/api/admin/dispose-break/99999999')
    assert resp.status_code == 404


def test_dashboard_stats_requires_admin(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99996
    resp = client.get('/api/dashboard-stats')
    assert resp.status_code == 200


def test_export_report(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99995
    resp = client.get('/api/reports/export?format=csv')
    assert resp.status_code == 200


# ── Leave Management Tests ──────────────────────────────────────

def test_leave_balance(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99994
    resp = client.get('/api/leave-balance')
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) >= 1
    types = {b['leave_type'] for b in data}
    assert 'Casual' in types


def test_apply_leave(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99993
    resp = client.post('/api/leaves', json={
        'leave_type': 'Casual',
        'start_date': '2026-07-10',
        'end_date': '2026-07-11',
        'reason': 'Test leave'
    })
    assert resp.status_code == 201


# ── Auth: Password Reset / Credentials / CSRF / Logout ──────────

def test_logout(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99950
    conn = get_db()
    from hrms.helpers import now_ist
    conn.execute("INSERT INTO user_sessions (session_id, emp_id, login_time, session_date) VALUES (?, ?, ?, ?)",
                 [99950, 'EMP001', now_ist(), now_ist().date()])
    conn.close()
    resp = client.get('/logout')
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert 'emp_id' not in sess


def test_forgot_password(client):
    resp = client.post('/api/forgot-password', json={
        'emp_id': 'EMP001', 'email': 'shubham@company.com'
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert 'reset link has been generated' in data['message'].lower()


def test_forgot_password_no_match(client):
    resp = client.post('/api/forgot-password', json={
        'emp_id': 'EMP001', 'email': 'wrong@example.com'
    })
    assert resp.status_code == 404
    assert b'No matching user found' in resp.data


def test_reset_password(client):
    import secrets
    token = secrets.token_urlsafe(32)
    conn = get_db()
    from hrms.helpers import now_ist
    from datetime import timedelta
    conn.execute(
        "INSERT INTO password_reset_tokens (token_id, emp_id, token, expires_at) VALUES (?, ?, ?, ?)",
        [999950, 'EMP001', token, now_ist() + timedelta(hours=1)]
    )
    conn.close()
    resp = client.post('/api/reset-password', json={
        'token': token, 'new_password': 'newpwd123'
    })
    assert resp.status_code == 200
    # Verify password changed
    from hrms.helpers import check_password
    conn = get_db()
    stored = conn.execute("SELECT password FROM users WHERE emp_id = 'EMP001'").fetchone()[0]
    conn.close()
    assert check_password('newpwd123', stored)
    # Restore original
    from hrms.helpers import hash_password
    conn = get_db()
    conn.execute("UPDATE users SET password = ? WHERE emp_id = 'EMP001'", [hash_password('pass123')])
    conn.close()


def test_reset_password_bad_token(client):
    resp = client.post('/api/reset-password', json={
        'token': 'invalidtoken123', 'new_password': 'test123'
    })
    assert resp.status_code == 400
    assert b'Invalid or expired token' in resp.data


def test_reset_password_short(client):
    resp = client.post('/api/reset-password', json={
        'token': 'sometoken', 'new_password': 'ab'
    })
    assert resp.status_code == 400
    assert b'at least 6 characters' in resp.data


def test_csrf_token(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99951
    resp = client.get('/api/csrf-token')
    assert resp.status_code == 200
    data = resp.get_json()
    assert 'csrf_token' in data
    assert len(data['csrf_token']) == 64


def test_csrf_token_requires_auth(client):
    resp = client.get('/api/csrf-token')
    assert resp.status_code == 401
    assert b'Authentication required' in resp.data


def test_credentials(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99952
    resp = client.get('/api/credentials')
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)
    assert len(data) >= 1
    assert data[0]['emp_id'] == 'EMP001'


def test_credentials_requires_auth(client):
    resp = client.get('/api/credentials')
    assert resp.status_code == 401


def test_credentials_requires_admin(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP002'
        sess['name'] = 'Employee'
        sess['role'] = 'Employee'
        sess['session_id'] = 99953
    resp = client.get('/api/credentials')
    assert resp.status_code == 403


# ── ATS / Phase 1 Tests ─────────────────────────────────────────

def test_offer_reject_happy(auth_client):
    r = auth_client.post('/api/jobs', json={'title': 'Test Job'})
    assert r.status_code == 201
    job_id = r.get_json()['id']

    r = auth_client.post('/api/candidates', json={'name': 'Test Cand', 'email': 'cand@test.com', 'job_id': job_id})
    assert r.status_code == 201
    cand_id = r.get_json()['id']

    r = auth_client.put(f'/api/candidates/{cand_id}/status', json={'status': 'Offered'})
    assert r.status_code == 200

    r = auth_client.post('/api/offers', json={'candidate_id': cand_id, 'offered_salary': 500000})
    assert r.status_code == 201
    offer_id = r.get_json()['id']

    r = auth_client.post(f'/api/offers/{offer_id}/reject')
    assert r.status_code == 200
    assert b'Offer rejected' in r.data


def test_offer_reject_nonexistent(auth_client):
    r = auth_client.post('/api/offers/9999999/reject')
    assert r.status_code == 200


def test_convert_candidate_happy(auth_client):
    r = auth_client.post('/api/jobs', json={'title': 'Engineer'})
    job_id = r.get_json()['id']

    r = auth_client.post('/api/candidates', json={'name': 'Convert Me', 'email': 'convert@test.com', 'job_id': job_id})
    cand_id = r.get_json()['id']

    r = auth_client.put(f'/api/candidates/{cand_id}/status', json={'status': 'Offered'})
    r = auth_client.post('/api/offers', json={'candidate_id': cand_id, 'offered_salary': 600000})
    offer_id = r.get_json()['id']

    r = auth_client.post(f'/api/offers/{offer_id}/accept')
    assert r.status_code == 200

    r = auth_client.post(f'/api/candidates/{cand_id}/convert', json={
        'department': 'Engineering',
        'designation': 'Software Engineer',
        'date_of_joining': '2026-08-01'
    })
    assert r.status_code == 201
    data = r.get_json()
    assert 'emp_id' in data

    conn = get_db()
    user = conn.execute("SELECT status, allow_login FROM users WHERE emp_id = ?", [data['emp_id']]).fetchone()
    conn.close()
    assert user is not None
    assert user[0] == 'Pre-hire'
    assert user[1] == 0


def test_convert_candidate_not_hired(auth_client):
    r = auth_client.post('/api/jobs', json={'title': 'Test'})
    job_id = r.get_json()['id']

    r = auth_client.post('/api/candidates', json={'name': 'Not Hired', 'email': 'not@test.com', 'job_id': job_id})
    cand_id = r.get_json()['id']

    r = auth_client.post(f'/api/candidates/{cand_id}/convert', json={})
    assert r.status_code == 400
    assert b'Candidate must have Hired status' in r.data


def test_convert_candidate_nonexistent(auth_client):
    r = auth_client.post('/api/candidates/9999999/convert', json={})
    assert r.status_code == 404


def test_onboarding_initiate_requires_admin(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP002'
        sess['name'] = 'Employee'
        sess['role'] = 'Employee'
        sess['session_id'] = 99991
    r = client.post('/api/onboarding-checklist', json={'emp_id': 'EMP002'})
    assert r.status_code == 403
    assert b'admin access required' in r.data


def test_admin_can_initiate_onboarding(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99990
    conn = get_db()
    exists = conn.execute("SELECT COUNT(*) FROM onboarding_workflow WHERE emp_id = 'EMP005'").fetchone()[0]
    conn.close()
    if exists:
        conn = get_db()
        conn.execute("DELETE FROM onboarding_workflow WHERE emp_id = 'EMP005'")
        conn.close()

    r = client.post('/api/onboarding-checklist', json={'emp_id': 'EMP005'})
    assert r.status_code == 201
    assert b'Onboarding initiated' in r.data


# ── Phase 2: Offboarding Workflow Tests ─────────────────────────

def test_offboarding_workflow_get(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99990
    resp = client.get('/api/offboarding-workflow')
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)


def test_offboarding_workflow_proceed(client):
    conn = get_db()
    conn.execute("DELETE FROM offboarding_workflow WHERE emp_id = 'EMP005'")
    conn.execute(
        "INSERT INTO offboarding_workflow (workflow_id, emp_id, step1_done) VALUES (?, ?, ?)",
        [int(datetime.now().timestamp() * 1000000) % 2147483647, 'EMP005', 0]
    )
    conn.close()
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99989
    resp = client.post('/api/offboarding-proceed', json={'emp_id': 'EMP005'})
    assert resp.status_code == 200
    data = resp.get_json()
    assert 'message' in data
    conn = get_db()
    row = conn.execute("SELECT step1_done FROM offboarding_workflow WHERE emp_id = 'EMP005'").fetchone()
    conn.close()
    assert row[0] == 1


def test_offboarding_workflow_complete(client):
    conn = get_db()
    conn.execute("DELETE FROM offboarding_workflow WHERE emp_id = 'EMP005'")
    wid = int(datetime.now().timestamp() * 1000000) % 2147483647
    conn.execute(
        "INSERT INTO offboarding_workflow (workflow_id, emp_id, step1_done, step2_done, step3_done, step4_done) VALUES (?, ?, 1, 1, 1, 1)",
        [wid, 'EMP005']
    )
    conn.close()
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99988
    resp = client.post('/api/offboarding-proceed', json={'emp_id': 'EMP005'})
    assert resp.status_code == 200
    conn = get_db()
    row = conn.execute("SELECT step5_done, completed FROM offboarding_workflow WHERE emp_id = 'EMP005'").fetchone()
    conn.close()
    assert row[0] == 1
    assert row[1] == 1
    conn = get_db()
    user = conn.execute("SELECT status FROM users WHERE emp_id = 'EMP005'").fetchone()
    conn.execute("UPDATE users SET status = 'Active' WHERE emp_id = 'EMP005'")
    conn.close()
    assert user[0] == 'Inactive'


# ── Phase 2: Document Unification Tests ─────────────────────────

def test_unified_documents_insert(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP002'
        sess['name'] = 'Employee'
        sess['role'] = 'Employee'
        sess['session_id'] = 99987
    resp = client.post('/api/employee-documents', json={'doc_type': 'Test Doc', 'file_name': 'test.pdf'})
    assert resp.status_code == 201
    data = resp.get_json()
    assert 'id' in data
    conn = get_db()
    row = conn.execute("SELECT context, doc_type, name FROM documents WHERE doc_id = ?", [data['id']]).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == 'Personal'
    assert row[1] == 'Test Doc'
    assert row[2] == 'test.pdf'


def test_documents_context_filter(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99986
    resp = client.get('/api/documents?context=Personal')
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)
    for d in data:
        assert d.get('context') == 'Personal'


def test_audit_log(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99992
    resp = client.get('/api/audit-log')
    assert resp.status_code == 200
    data = resp.get_json()
    assert 'data' in data
    assert 'total' in data


# ── Phase 3: Approval Routing Tests ─────────────────────────────

def test_approval_routing_with_manager(client):
    """EMP002 (manager_emp_id=EMP001) submits leave, EMP001 can approve it"""
    conn = get_db()
    conn.execute("UPDATE users SET manager_emp_id = 'EMP001' WHERE emp_id = 'EMP002'")
    conn.close()
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP002'
        sess['name'] = 'Sachin Bhakte'
        sess['role'] = 'Employee'
        sess['department'] = 'Operations'
        sess['session_id'] = 99981
    resp = client.post('/api/leaves', json={
        'leave_type': 'Casual', 'start_date': '2026-09-01', 'end_date': '2026-09-02', 'reason': 'Test manager approval'
    })
    assert resp.status_code == 201
    leave_id = resp.get_json()['leave_id']

    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Shubham Jawalkar'
        sess['role'] = 'Super Admin'
        sess['department'] = 'MIS'
        sess['session_id'] = 99982
    resp = client.post(f'/api/leaves/{leave_id}/approve')
    assert resp.status_code == 200, f'Manager approve failed: {resp.get_json()}'


def test_approval_routing_fallback_hr(client):
    """EMP003 (HR) can approve leave for EMP004 who has no manager set"""
    conn = get_db()
    conn.execute("UPDATE users SET manager_emp_id = NULL WHERE emp_id = 'EMP004'")
    conn.close()
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP004'
        sess['name'] = 'Rahul Verma'
        sess['role'] = 'Employee'
        sess['department'] = 'IT'
        sess['session_id'] = 99983
    resp = client.post('/api/leaves', json={
        'leave_type': 'Sick', 'start_date': '2026-09-05', 'end_date': '2026-09-05', 'reason': 'Sick'
    })
    assert resp.status_code == 201
    leave_id = resp.get_json()['leave_id']

    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP003'
        sess['name'] = 'Priya Sharma'
        sess['role'] = 'HR'
        sess['department'] = 'HR'
        sess['session_id'] = 99984
    resp = client.post(f'/api/leaves/{leave_id}/approve')
    assert resp.status_code == 200, f'HR approve failed: {resp.get_json()}'


def test_approval_routing_employee_cannot_approve_other(client):
    """EMP005 (no relation) cannot approve EMP002's leave"""
    conn = get_db()
    conn.execute("UPDATE users SET manager_emp_id = 'EMP001' WHERE emp_id = 'EMP002'")
    conn.close()
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP002'
        sess['name'] = 'Sachin Bhakte'
        sess['role'] = 'Employee'
        sess['department'] = 'Operations'
        sess['session_id'] = 99985
    resp = client.post('/api/leaves', json={
        'leave_type': 'Annual', 'start_date': '2026-09-10', 'end_date': '2026-09-12', 'reason': 'Test'
    })
    assert resp.status_code == 201
    leave_id = resp.get_json()['leave_id']

    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP005'
        sess['name'] = 'Neha Patil'
        sess['role'] = 'Employee'
        sess['department'] = 'Admin'
        sess['session_id'] = 99986
    resp = client.post(f'/api/leaves/{leave_id}/approve')
    assert resp.status_code == 403


# ── Phase 3: Notification Preferences Tests ─────────────────────

def test_notification_preferences_get_set(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99987
    resp = client.get('/api/notification-preferences')
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)
    assert len(data) >= 5
    assert data[0]['category'] == 'Onboarding'

    resp = client.post('/api/notification-preferences', json=[
        {'category': 'Leaves', 'in_app': 0, 'email': 0}
    ])
    assert resp.status_code == 200

    resp = client.get('/api/notification-preferences')
    data = resp.get_json()
    for p in data:
        if p['category'] == 'Leaves':
            assert p['in_app'] == False
            assert p['email'] == False
            break


def test_notification_preferences_email_opt_out(client):
    """With email=0 for Leaves, an in-app notification is still created"""
    # Set EMP002's preferences: in-app ON, email OFF
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP002'
        sess['name'] = 'Sachin Bhakte'
        sess['role'] = 'Employee'
        sess['department'] = 'Operations'
        sess['session_id'] = 99988
    client.post('/api/notification-preferences', json=[
        {'category': 'Leaves', 'in_app': 1, 'email': 0}
    ])

    conn = get_db()
    before = conn.execute("SELECT COUNT(*) FROM notifications WHERE emp_id = 'EMP002'").fetchone()[0]
    conn.close()

    # EMP002 submits own leave
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP002'
        sess['name'] = 'Sachin Bhakte'
        sess['role'] = 'Employee'
        sess['department'] = 'Operations'
        sess['session_id'] = 99989
    resp = client.post('/api/leaves', json={
        'leave_type': 'Casual', 'start_date': '2026-09-15', 'end_date': '2026-09-16', 'reason': 'Test notif'
    })
    assert resp.status_code == 201
    leave_id = resp.get_json()['leave_id']

    # Admin approves
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Shubham Jawalkar'
        sess['role'] = 'Super Admin'
        sess['department'] = 'MIS'
        sess['session_id'] = 99990
    resp = client.post(f'/api/leaves/{leave_id}/approve')
    assert resp.status_code == 200

    conn = get_db()
    after = conn.execute("SELECT COUNT(*) FROM notifications WHERE emp_id = 'EMP002'").fetchone()[0]
    conn.close()
    assert after > before, 'In-app notification should still be created'


# ── Phase 4: Payroll Rates Regression Tests ──────────────────────

def test_calc_payroll_item_matches_rates(client):
    """calc_payroll_item_from_rates must match calc_payroll_item for default rates"""
    from hrms.helpers import calc_payroll_item, calc_payroll_item_from_rates, calc_tds, calc_tds_from_rates
    test_cases = [
        (None, 30000, 9000, 4000, 1500),
        (None, 15000, 5000, 2000, 500),
        (None, 50000, 15000, 10000, 3000),
        (None, 8000, 2000, 1000, 0),
        (None, 25000, 6000, 3000, 1000),
    ]
    for emp_id, basic, hra, allowances, deductions in test_cases:
        r1 = calc_payroll_item(emp_id, basic, hra, allowances, deductions)
        r2 = calc_payroll_item_from_rates(emp_id, basic, hra, allowances, deductions)
        assert r1 == r2, f'Mismatch for basic={basic}: {r1} != {r2}'


def test_calc_tds_matches_rates(client):
    """calc_tds_from_rates must match calc_tds for default rates"""
    from hrms.helpers import calc_tds, calc_tds_from_rates
    test_gross = [200000, 350000, 500000, 750000, 1000000, 1300000, 1600000, 0, 300000, 600000, 900000, 1200000, 1500000]
    for ag in test_gross:
        r1 = calc_tds(ag)
        r2 = calc_tds_from_rates(ag)
        assert r1 == r2, f'TDS mismatch for annual_gross={ag}: {r1} != {r2}'


def test_preview_payslip_endpoint(auth_client):
    """Preview payslip endpoint returns all expected fields"""
    resp = auth_client.post('/api/salary-structures/preview', json={
        'basic': 30000, 'hra': 9000, 'allowances': 4000, 'deductions': 1500
    })
    assert resp.status_code == 200
    d = resp.get_json()
    assert 'gross' in d and d['gross'] == 43000.0
    assert 'pf' in d and float(d['pf']) > 0
    assert 'esi' in d
    assert 'pt' in d
    assert 'net' in d
    assert 'annual_gross' in d and d['annual_gross'] == 43000.0 * 12
    assert 'monthly_tds' in d


def test_preview_payslip_matches_calc(client):
    """Preview endpoint output matches calc_payroll_item directly"""
    from hrms.helpers import calc_payroll_item
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99971
    resp = client.post('/api/salary-structures/preview', json={
        'basic': 45000, 'hra': 12000, 'allowances': 8000, 'deductions': 2000
    })
    assert resp.status_code == 200
    d = resp.get_json()
    gross, total_ded, net, pf, esi, pt = calc_payroll_item(None, 45000, 12000, 8000, 2000)
    assert d['gross'] == gross
    assert d['pf'] == pf
    assert d['esi'] == esi
    assert d['pt'] == pt
    assert d['net'] == net


# ── Phase 4: Payroll Rates CRUD Tests ────────────────────────────

def test_payroll_rates_get(auth_client):
    resp = auth_client.get('/api/payroll-rates')
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) >= 16
    types = {r['rate_type'] for r in data}
    assert 'pf_rate' in types
    assert 'tds_rate_1' in types


def test_payroll_rates_update(auth_client):
    resp = auth_client.get('/api/payroll-rates')
    rates = resp.get_json()
    pf_rate = next(r for r in rates if r['rate_type'] == 'pf_rate')
    orig = pf_rate['value']
    resp = auth_client.put('/api/payroll-rates', json={'id': pf_rate['id'], 'value': 13.0})
    assert resp.status_code == 200
    resp = auth_client.get('/api/payroll-rates')
    updated = next(r for r in resp.get_json() if r['id'] == pf_rate['id'])
    assert updated['value'] == 13.0
    auth_client.put('/api/payroll-rates', json={'id': pf_rate['id'], 'value': orig})


def test_payroll_rates_update_requires_admin(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP002'
        sess['name'] = 'Employee'
        sess['role'] = 'Employee'
        sess['session_id'] = 99972
    resp = client.put('/api/payroll-rates', json={'id': 1, 'value': 10})
    assert resp.status_code == 403


# ── Phase 4: Review Cycle Progress Tests ─────────────────────────

def test_review_cycle_progress_requires_login(client):
    resp = client.get('/api/review-cycle-progress')
    assert resp.status_code in (302, 401)


def test_review_cycle_progress_admin(auth_client):
    resp = auth_client.get('/api/review-cycle-progress')
    assert resp.status_code == 200
    d = resp.get_json()
    assert 'period' in d
    assert 'total' in d
    assert 'completed' in d
    assert 'pending' in d
    assert d['completed'] + d['pending'] == d['total']


# ── Phase 4: Open Review Cycle Scheduler Tests ───────────────────

def test_open_review_cycle_creates_reviews(client):
    from hrms.payroll import open_review_cycle, _current_review_period
    from hrms.db import get_db, _scalar
    conn = get_db()
    period = _current_review_period()
    conn.execute("DELETE FROM performance_reviews WHERE review_period = ?", [period])
    conn.close()
    open_review_cycle()
    conn = get_db()
    count = _scalar("SELECT COUNT(*) FROM performance_reviews WHERE review_period = ?", [period], conn=conn)
    conn.close()
    assert count > 0, f'Expected reviews created for {period}, got {count}'


def test_open_review_cycle_skips_existing(client):
    from hrms.payroll import open_review_cycle, _current_review_period
    from hrms.db import get_db, _scalar
    open_review_cycle()
    open_review_cycle()
    conn = get_db()
    period = _current_review_period()
    count = _scalar("SELECT COUNT(*) FROM performance_reviews WHERE review_period = ?", [period], conn=conn)
    conn.close()
    initial = _scalar("SELECT COUNT(*) FROM performance_reviews")
    open_review_cycle()
    after = _scalar("SELECT COUNT(*) FROM performance_reviews")
    assert after == initial, 'open_review_cycle should not create duplicate reviews'


# ── Phase 5: Analytics & Audit Maturity Tests ───────────────────

@pytest.mark.parametrize('endpoint', [
    '/api/analytics/headcount',
    '/api/analytics/leave-trends',
    '/api/analytics/attrition-risk',
    '/api/analytics/expense-summary',
    '/api/analytics/performance-summary',
])
def test_analytics_endpoints_accept_filters(client, endpoint):
    """All 5 analytics endpoints accept department/date filters without error"""
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99970
    resp = client.get(endpoint + '?department=Engineering&date_from=2025-01-01&date_to=2025-12-31')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data is not None
    # bare call also succeeds
    resp2 = client.get(endpoint)
    assert resp2.status_code == 200


def test_analytics_attrition_includes_new_signals(client):
    """Refined attrition-risk response includes attendance_rate and rating"""
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99971
    resp = client.get('/api/analytics/attrition-risk')
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)
    if data:
        entry = data[0]
        assert 'attendance_rate' in entry
        assert 'rating' in entry
        assert 'early_break' in entry


def test_analytics_headcount_filters_department(client):
    """Headcount with department filter returns filtered department list"""
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99972
    resp = client.get('/api/analytics/headcount')
    all_data = resp.get_json()
    all_depts = len(all_data.get('by_department', []))
    resp = client.get('/api/analytics/headcount?department=Engineering')
    filtered = resp.get_json()
    if filtered['by_department']:
        for d in filtered['by_department']:
            assert d['dept'] == 'Engineering'


def test_audit_modules_endpoint(client):
    """GET /api/audit-modules returns a list of module names"""
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99973
    resp = client.get('/api/audit-modules')
    assert resp.status_code == 200
    modules = resp.get_json()
    assert isinstance(modules, list)
    assert len(modules) > 0


def test_audit_log_filters_by_action(client):
    """Audit-log endpoint respects action filter"""
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99974
    resp = client.get('/api/audit-log?action=LOGIN')
    assert resp.status_code == 200
    data = resp.get_json()
    assert 'data' in data
    for entry in data['data']:
        assert 'LOGIN' in entry['action']


def test_audit_log_filters_by_module(client):
    """Audit-log endpoint respects module filter"""
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99975
    resp = client.get('/api/audit-log?module=USER')
    assert resp.status_code == 200
    data = resp.get_json()
    assert 'data' in data
    for entry in data['data']:
        assert entry['action'].startswith('USER')


def test_payroll_rate_update_logs_audit(client):
    """Updating a payroll rate writes PAYROLL_RATE_UPDATE audit entry"""
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99976
    rates = client.get('/api/payroll-rates').get_json()
    assert len(rates) > 0
    first = rates[0]
    orig = first['value']
    resp = client.put('/api/payroll-rates', json={'id': first['id'], 'value': orig + 1})
    assert resp.status_code == 200
    # restore
    client.put('/api/payroll-rates', json={'id': first['id'], 'value': orig})
    resp2 = client.get('/api/audit-log?action=PAYROLL_RATE_UPDATE')
    assert resp2.status_code == 200
    data = resp2.get_json()
    assert data['total'] > 0, 'Expected audit entries for PAYROLL_RATE_UPDATE'


def test_onboarding_initiate_logs_audit(client):
    """Initiating onboarding writes ONBOARDING_INITIATE audit entry"""
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99980
    resp = client.post('/api/onboarding-checklist', json={'emp_id': 'EMP002'})
    assert resp.status_code == 201
    # verify audit wrote
    resp2 = client.get('/api/audit-log?action=ONBOARDING_INITIATE')
    assert resp2.status_code == 200
    data = resp2.get_json()
    assert data['total'] > 0


def test_ticket_create_logs_audit(client):
    """Creating a ticket writes TICKET_CREATE audit entry"""
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99981
    resp = client.post('/api/tickets', json={'subject': 'Test ticket for audit'})
    assert resp.status_code == 201
    resp2 = client.get('/api/audit-log?action=TICKET_CREATE')
    assert resp2.status_code == 200
    data = resp2.get_json()
    assert data['total'] > 0


def test_ticket_status_update_logs_audit(client):
    """Updating ticket status writes TICKET_STATUS_UPDATE audit entry"""
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99982
    resp = client.post('/api/tickets', json={'subject': 'Ticket for status audit'})
    assert resp.status_code == 201
    tid = resp.get_json()['id']
    resp2 = client.put(f'/api/tickets/{tid}/status', json={'status': 'In Progress'})
    assert resp2.status_code == 200
    resp3 = client.get('/api/audit-log?action=TICKET_STATUS_UPDATE')
    assert resp3.status_code == 200
    data = resp3.get_json()
    assert data['total'] > 0


def test_document_upload_logs_audit(client):
    """Uploading a document writes DOCUMENT_UPLOAD audit entry"""
    import io
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99983
    data = {'file': (io.BytesIO(b'dummy content'), 'test_audit.txt'), 'category': 'Other', 'context': 'General'}
    resp = client.post('/api/upload', data=data, content_type='multipart/form-data')
    assert resp.status_code == 201
    resp2 = client.get('/api/audit-log?action=DOCUMENT_UPLOAD')
    assert resp2.status_code == 200
    assert resp2.get_json()['total'] > 0


def test_break_start_logs_audit(client):
    """Starting a break writes BREAK_START audit entry"""
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99984
    resp = client.post('/api/start-break', json={'break_type': 'Tea'})
    assert resp.status_code == 201
    resp2 = client.get('/api/audit-log?action=BREAK_START')
    assert resp2.status_code == 200
    data = resp2.get_json()
    assert data['total'] > 0


def test_salary_structure_create_logs_audit(client):
    """Creating a salary structure writes SALARY_STRUCTURE_CREATE audit entry"""
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99985
    resp = client.post('/api/salary-structures', json={
        'emp_id': 'EMP002', 'basic': 25000, 'hra': 8000, 'allowances': 3000, 'deductions': 1000
    })
    assert resp.status_code == 201
    resp2 = client.get('/api/audit-log?action=SALARY_STRUCTURE_CREATE')
    assert resp2.status_code == 200
    assert resp2.get_json()['total'] > 0


# ── Leaves: Admin Page, Reject, Export ───────────────────────────

def test_admin_leaves_page(auth_client):
    resp = auth_client.get('/admin/leaves')
    assert resp.status_code == 200


def test_leave_reject(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP002'
        sess['name'] = 'Sachin Bhakte'
        sess['role'] = 'Employee'
        sess['department'] = 'Operations'
        sess['session_id'] = 99930
    resp = client.post('/api/leaves', json={
        'leave_type': 'Casual', 'start_date': '2026-10-01', 'end_date': '2026-10-02', 'reason': 'To reject'
    })
    assert resp.status_code == 201
    leave_id = resp.get_json()['leave_id']
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99931
    resp = client.post(f'/api/leaves/{leave_id}/reject')
    assert resp.status_code == 200
    conn = get_db()
    status = conn.execute("SELECT status FROM leave_requests WHERE leave_id = ?", [leave_id]).fetchone()[0]
    conn.close()
    assert status == 'Rejected'


def test_leave_reject_not_found(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99932
    resp = client.post('/api/leaves/99999999/reject')
    assert resp.status_code == 404


def test_leave_reject_not_pending(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP002'
        sess['name'] = 'Sachin Bhakte'
        sess['role'] = 'Employee'
        sess['department'] = 'Operations'
        sess['session_id'] = 99933
    resp = client.post('/api/leaves', json={
        'leave_type': 'Annual', 'start_date': '2026-11-01', 'end_date': '2026-11-03', 'reason': 'To reject already approved'
    })
    assert resp.status_code == 201
    leave_id = resp.get_json()['leave_id']
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99934
    resp = client.post(f'/api/leaves/{leave_id}/approve')
    assert resp.status_code == 200
    resp = client.post(f'/api/leaves/{leave_id}/reject')
    assert resp.status_code == 400


def test_leave_export(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99935
    resp = client.get('/api/leaves/export')
    assert resp.status_code == 200


# ── Payroll: Runs, Finalize, Items, Payslips ─────────────────────

def test_payroll_runs_list(client):
    conn = get_db()
    run = conn.execute("SELECT run_id, month, year FROM payroll_runs LIMIT 1").fetchone()
    conn.close()
    assert run is not None, 'No seeded payroll runs found'
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99920
    resp = client.get('/api/payroll-runs')
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)
    assert len(data) >= 2


def test_payroll_run_create(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99921
    next_month = now_ist().month + 1
    next_year = now_ist().year
    if next_month > 12:
        next_month = 1
        next_year += 1
    resp = client.post('/api/payroll-runs', json={'month': next_month, 'year': next_year})
    assert resp.status_code == 201
    # clean up
    conn = get_db()
    conn.execute("DELETE FROM payroll_items WHERE run_id IN (SELECT run_id FROM payroll_runs WHERE month = ? AND year = ?)", [next_month, next_year])
    conn.execute("DELETE FROM payroll_runs WHERE month = ? AND year = ?", [next_month, next_year])
    conn.close()


def test_payroll_run_create_missing_params(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99922
    resp = client.post('/api/payroll-runs', json={})
    assert resp.status_code == 400
    assert b'month and year required' in resp.data


def test_payroll_run_finalize(client):
    conn = get_db()
    run = conn.execute("SELECT run_id FROM payroll_runs WHERE status = 'Draft' LIMIT 1").fetchone()
    conn.close()
    assert run is not None, 'No Draft payroll run found'
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99923
    resp = client.post(f'/api/payroll-runs/{run[0]}/finalize')
    assert resp.status_code == 200
    assert b'Payroll finalized' in resp.data


def test_payroll_run_items(client):
    conn = get_db()
    run = conn.execute("SELECT run_id FROM payroll_runs LIMIT 1").fetchone()
    conn.close()
    assert run is not None
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99924
    resp = client.get(f'/api/payroll-runs/{run[0]}/items')
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)


def test_payslip(client):
    conn = get_db()
    item = conn.execute("SELECT p.run_id, p.emp_id FROM payroll_items p LIMIT 1").fetchone()
    conn.close()
    assert item is not None, 'No payroll items found'
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99925
    resp = client.get(f'/api/payslip/{item[0]}/{item[1]}')
    assert resp.status_code == 200
    data = resp.get_json()
    assert 'gross' in data
    assert 'net' in data


def test_payslip_not_found(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99926
    resp = client.get('/api/payslip/99999/NONEXIST')
    assert resp.status_code == 404


def test_my_payslips(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP002'
        sess['name'] = 'Sachin Bhakte'
        sess['role'] = 'Employee'
        sess['session_id'] = 99927
    resp = client.get('/api/my-payslips')
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)


# ── Users: Single-user CRUD, Block/Unblock ───────────────────────

def test_get_single_user(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99910
    resp = client.get('/api/users/EMP001')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['emp_id'] == 'EMP001'
    assert data['name'] == 'Shubham Jawalkar'


def test_get_single_user_not_found(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99911
    resp = client.get('/api/users/NONEXIST')
    assert resp.status_code == 404


def test_block_user(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99912
    resp = client.post('/api/users/EMP006/block')
    assert resp.status_code == 200
    conn = get_db()
    status = conn.execute("SELECT status FROM users WHERE emp_id = 'EMP006'").fetchone()[0]
    conn.close()
    assert status == 'Blocked'
    # restore
    conn = get_db()
    conn.execute("UPDATE users SET status = 'Active' WHERE emp_id = 'EMP006'")
    conn.close()


def test_unblock_user(client):
    conn = get_db()
    conn.execute("UPDATE users SET status = 'Blocked' WHERE emp_id = 'EMP006'")
    conn.close()
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99913
    resp = client.post('/api/users/EMP006/unblock')
    assert resp.status_code == 200
    conn = get_db()
    status = conn.execute("SELECT status FROM users WHERE emp_id = 'EMP006'").fetchone()[0]
    conn.close()
    assert status == 'Active'


def test_update_user(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99914
    resp = client.put('/api/users/EMP005', json={
        'name': 'Updated Name', 'email': 'updated@company.com', 'role': 'Employee',
        'department': 'IT', 'status': 'Active'
    })
    assert resp.status_code == 200
    conn = get_db()
    name = conn.execute("SELECT name FROM users WHERE emp_id = 'EMP005'").fetchone()[0]
    conn.close()
    assert name == 'Updated Name'
    # restore
    conn = get_db()
    conn.execute("UPDATE users SET name = 'Neha Patil', email = 'neha@company.com', department = 'Admin' WHERE emp_id = 'EMP005'")
    conn.close()


def test_delete_user(client):
    conn = get_db()
    conn.execute("DELETE FROM break_approvals WHERE emp_id = 'EMP006'")
    conn.execute("DELETE FROM regularization_requests WHERE emp_id = 'EMP006'")
    conn.execute("DELETE FROM notification_preferences WHERE emp_id = 'EMP006'")
    conn.execute("DELETE FROM notifications WHERE emp_id = 'EMP006'")
    conn.execute("DELETE FROM onboarding_tasks WHERE emp_id = 'EMP006'")
    conn.execute("DELETE FROM offboarding_tasks WHERE emp_id = 'EMP006'")
    conn.execute("DELETE FROM goals WHERE emp_id = 'EMP006'")
    conn.execute("DELETE FROM performance_reviews WHERE emp_id = 'EMP006'")
    conn.execute("DELETE FROM feedback_360 WHERE emp_id = 'EMP006'")
    conn.execute("DELETE FROM expense_claims WHERE emp_id = 'EMP006'")
    conn.execute("DELETE FROM password_reset_tokens WHERE emp_id = 'EMP006'")
    conn.execute("DELETE FROM audit_log WHERE emp_id = 'EMP006'")
    conn.execute("DELETE FROM assets WHERE emp_id = 'EMP006'")
    conn.execute("DELETE FROM employee_documents WHERE emp_id = 'EMP006'")
    conn.execute("DELETE FROM user_sessions WHERE emp_id = 'EMP006'")
    conn.execute("DELETE FROM documents WHERE emp_id = 'EMP006'")
    conn.execute("DELETE FROM interviews WHERE interviewer = 'EMP006'")
    conn.execute("DELETE FROM leave_requests WHERE emp_id = 'EMP006'")
    conn.execute("DELETE FROM leave_balance WHERE emp_id = 'EMP006'")
    conn.execute("DELETE FROM salary_structures WHERE emp_id = 'EMP006'")
    conn.execute("DELETE FROM breaks WHERE emp_id = 'EMP006'")
    conn.execute("DELETE FROM dependents WHERE emp_id = 'EMP006'")
    conn.execute("DELETE FROM payroll_items WHERE emp_id = 'EMP006'")
    conn.close()
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99915
    resp = client.delete('/api/users/EMP006')
    assert resp.status_code == 200
    conn = get_db()
    row = conn.execute("SELECT 1 FROM users WHERE emp_id = 'EMP006'").fetchone()
    conn.close()
    assert row is None


def test_delete_user_not_found(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99916
    resp = client.delete('/api/users/NONEXIST')
    assert resp.status_code == 404


def test_delete_own_account(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99917
    resp = client.delete('/api/users/EMP001')
    assert resp.status_code == 400
    assert b'Cannot delete your own account' in resp.data


def test_profile_page(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99918
    resp = client.get('/profile')
    assert resp.status_code == 200


# ── Error Handler Tests ───────────────────────────────────────────

def test_404(client):
    resp = client.get('/nonexistent-route')
    assert resp.status_code == 404


def test_500(client):
    """Trigger a 500 by posting bad JSON to an endpoint that expects specific data"""
    resp = client.post('/api/reset-password', json={'token': 'x' * 50})
    assert resp.status_code in (400, 500)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
