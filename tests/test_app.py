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


# ── Rate Limiting ───────────────────────────────────────────────

def test_rate_limit_headers(client):
    for _ in range(5):
        client.post('/login', json={'emp_id': 'x', 'password': 'x'})
    resp = client.post('/login', json={'emp_id': 'x', 'password': 'x'})
    assert resp.status_code in (401, 429)


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
        'exit_interviews', 'salary_structures', 'payroll_runs', 'payroll_items',
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


def test_org_chart_only_returns_active_employees(client):
    conn = get_db()
    conn.execute("UPDATE users SET status = 'Blocked' WHERE emp_id = ?", ['EMP003'])
    conn.commit()
    conn.close()
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 99997
    resp = client.get('/api/org-chart')
    assert resp.status_code == 200
    data = resp.get_json()
    assert all(item['status'] == 'Active' for item in data)
    assert not any(item['id'] == 'EMP003' for item in data)


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


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
