import atexit
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

os.environ['SECRET_KEY'] = 'test-secret-key'
os.environ['DB_FILE'] = os.path.join(tempfile.gettempdir(), f'hrms_api_{datetime.now().timestamp()}.duckdb')
os.environ['FLASK_DEBUG'] = '0'

import pytest

from app import app, gen_id, get_db
from hrms.helpers import UPLOAD_FOLDER, now_ist


@pytest.fixture
def client():
    app.config['TESTING'] = True
    app.config['SERVER_NAME'] = 'localhost'
    with app.test_client() as c:
        with app.app_context():
            yield c


@pytest.fixture
def auth_client(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Shubham Jawalkar'
        sess['role'] = 'Super Admin'
        sess['department'] = 'MIS'
        sess['session_id'] = 999001
    return client


@pytest.fixture
def emp_client(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP002'
        sess['name'] = 'Sachin Bhakte'
        sess['role'] = 'Employee'
        sess['department'] = 'Operations'
        sess['session_id'] = 999002
    return client


@pytest.fixture
def hr_client(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP003'
        sess['name'] = 'Priya Sharma'
        sess['role'] = 'HR'
        sess['department'] = 'HR'
        sess['session_id'] = 999003
    return client


def cleanup():
    from hrms.db import close_db
    close_db()
    try:
        os.remove(os.environ['DB_FILE'])
    except OSError:
        pass


atexit.register(cleanup)


# ── ASSETS ─────────────────────────────────────────────────────────

def test_my_assets(auth_client):
    resp = auth_client.get('/api/my-assets')
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)


def test_my_assets_requires_auth(client):
    resp = client.get('/api/my-assets')
    assert resp.status_code == 302


def test_assets_list_admin(auth_client):
    resp = auth_client.get('/api/assets')
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)


def test_assets_list_requires_admin(emp_client):
    resp = emp_client.get('/api/assets')
    assert resp.status_code == 302


def test_assets_create(auth_client):
    resp = auth_client.post('/api/assets', json={'emp_id': 'EMP002', 'asset_type': 'Laptop'})
    assert resp.status_code == 201
    data = resp.get_json()
    assert 'id' in data


def test_assets_create_missing_fields(auth_client):
    resp = auth_client.post('/api/assets', json={})
    assert resp.status_code == 400
    assert b'emp_id and asset_type required' in resp.data


def test_assets_create_inactive_employee(auth_client):
    resp = auth_client.post('/api/assets', json={'emp_id': 'EMP999', 'asset_type': 'Laptop'})
    assert resp.status_code == 400
    assert b'Employee not found or inactive' in resp.data


def test_assets_return(auth_client):
    aid = gen_id()
    conn = get_db()
    conn.execute(
        "INSERT INTO assets (asset_id, emp_id, asset_type, issued_date, status) VALUES (?, ?, ?, ?, 'Issued')",
        [aid, 'EMP002', 'Monitor', now_ist().date()]
    )
    conn.close()
    resp = auth_client.post(f'/api/assets/{aid}/return')
    assert resp.status_code == 200
    assert b'Asset returned' in resp.data
    conn = get_db()
    status = conn.execute("SELECT status FROM assets WHERE asset_id = ?", [aid]).fetchone()[0]
    conn.close()
    assert status == 'Returned'


# ── EXPENSES ───────────────────────────────────────────────────────

def test_expense_categories(auth_client):
    resp = auth_client.get('/api/expense-categories')
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)
    assert len(data) >= 1


def test_expense_categories_requires_auth(client):
    resp = client.get('/api/expense-categories')
    assert resp.status_code == 302


def test_expenses_list(auth_client):
    resp = auth_client.get('/api/expenses')
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)


def test_expenses_create(auth_client):
    resp = auth_client.post('/api/expenses', json={'cat_id': 1, 'amount': 500})
    assert resp.status_code == 201
    data = resp.get_json()
    assert 'id' in data


def test_expenses_create_missing_fields(auth_client):
    resp = auth_client.post('/api/expenses', json={'amount': 500})
    assert resp.status_code == 400
    assert b'cat_id and amount required' in resp.data


def test_expense_status_update(auth_client):
    eid = gen_id()
    conn = get_db()
    conn.execute(
        "INSERT INTO expense_claims (claim_id, emp_id, cat_id, amount, description, status, created_at) VALUES (?, ?, ?, ?, ?, 'Pending', ?)",
        [eid, 'EMP002', 1, 1000, 'Test expense', now_ist()]
    )
    conn.close()
    resp = auth_client.put(f'/api/expenses/{eid}/status', json={'status': 'Approved'})
    assert resp.status_code == 200
    assert b'Expense approved' in resp.data


def test_expense_status_invalid(auth_client):
    resp = auth_client.put('/api/expenses/99999/status', json={'status': 'Approved'})
    assert resp.status_code == 404
    assert b'Expense not found' in resp.data


def test_expense_status_bad_status(auth_client):
    eid = gen_id()
    conn = get_db()
    conn.execute(
        "INSERT INTO expense_claims (claim_id, emp_id, cat_id, amount, description, status, created_at) VALUES (?, ?, ?, ?, ?, 'Pending', ?)",
        [eid, 'EMP002', 1, 1000, 'Test bad status', now_ist()]
    )
    conn.close()
    resp = auth_client.put(f'/api/expenses/{eid}/status', json={'status': 'BadStatus'})
    assert resp.status_code == 400
    assert b'Invalid status' in resp.data


# ── NOTIFICATIONS ──────────────────────────────────────────────────

def test_get_notifications(auth_client):
    resp = auth_client.get('/api/notifications')
    assert resp.status_code == 200
    data = resp.get_json()
    assert 'unread' in data
    assert 'data' in data


def test_get_notifications_requires_auth(client):
    resp = client.get('/api/notifications')
    assert resp.status_code == 302


def test_mark_read(auth_client):
    resp = auth_client.post('/api/notifications/read')
    assert resp.status_code == 200
    assert b'Cleared' in resp.data


def test_send_notification_email(auth_client):
    resp = auth_client.post('/api/send-notification-email', json={
        'to': 'test@test.com', 'subject': 'Test', 'body': 'Test body'
    })
    assert resp.status_code == 200


# ── REGULARIZATION ─────────────────────────────────────────────────

def test_regularization_approve(auth_client):
    conn = get_db()
    rid = gen_id()
    conn.execute(
        "INSERT INTO regularization_requests (request_id, emp_id, request_date, reason, status) VALUES (?, ?, ?, ?, 'Pending')",
        [rid, 'EMP002', (now_ist() - timedelta(days=1)).date(), 'Test approval']
    )
    conn.close()
    resp = auth_client.post(f'/api/regularization/{rid}/approve')
    assert resp.status_code == 200
    assert b'Approved' in resp.data


def test_regularization_approve_not_found(auth_client):
    resp = auth_client.post('/api/regularization/99999/approve')
    assert resp.status_code == 404
    assert b'Request not found' in resp.data


def test_regularization_reject(auth_client):
    conn = get_db()
    rid = gen_id()
    conn.execute(
        "INSERT INTO regularization_requests (request_id, emp_id, request_date, reason, status) VALUES (?, ?, ?, ?, 'Pending')",
        [rid, 'EMP002', (now_ist() - timedelta(days=1)).date(), 'Test rejection']
    )
    conn.close()
    resp = auth_client.post(f'/api/regularization/{rid}/reject')
    assert resp.status_code == 200
    assert b'Rejected' in resp.data


def test_regularization_cancel(auth_client):
    conn = get_db()
    rid = gen_id()
    conn.execute(
        "INSERT INTO regularization_requests (request_id, emp_id, request_date, reason, status) VALUES (?, ?, ?, ?, 'Pending')",
        [rid, 'EMP002', (now_ist() - timedelta(days=1)).date(), 'Test cancel']
    )
    conn.close()
    resp = auth_client.post(f'/api/regularization/{rid}/cancel')
    assert resp.status_code == 200
    assert b'Cancelled' in resp.data


def test_regularization_cancel_not_owner(auth_client, emp_client):
    resp = emp_client.post('/api/regularization', json={
        'date': (now_ist() - timedelta(days=2)).date().isoformat(),
        'reason': 'Cancel by admin test'
    })
    assert resp.status_code == 201
    rid = resp.get_json()['id']
    resp = auth_client.post(f'/api/regularization/{rid}/cancel')
    assert resp.status_code == 200
    assert b'Cancelled' in resp.data


def test_regularization_cancel_already_processed(auth_client):
    conn = get_db()
    rid = gen_id()
    conn.execute(
        "INSERT INTO regularization_requests (request_id, emp_id, request_date, reason, status) VALUES (?, ?, ?, ?, 'Pending')",
        [rid, 'EMP002', (now_ist() - timedelta(days=1)).date(), 'Test double cancel']
    )
    conn.close()
    resp = auth_client.post(f'/api/regularization/{rid}/cancel')
    assert resp.status_code == 200
    resp = auth_client.post(f'/api/regularization/{rid}/cancel')
    assert resp.status_code == 400
    assert b'Only pending requests' in resp.data


def test_regularization_export(auth_client):
    resp = auth_client.get('/api/regularization/export')
    assert resp.status_code == 200


# ── DOCUMENTS ──────────────────────────────────────────────────────

def test_document_download(auth_client):
    conn = get_db()
    doc_id = gen_id()
    filename = f'test_doc_{doc_id}.txt'
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    with open(filepath, 'w') as f:
        f.write('test content')
    conn.execute(
        "INSERT INTO documents (doc_id, emp_id, name, category, file_path, file_size, uploaded_at, context, doc_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [doc_id, 'EMP002', 'test.txt', 'Other', filename, 12, now_ist(), 'General', 'Test']
    )
    conn.close()
    resp = auth_client.get(f'/api/documents/{doc_id}/download')
    assert resp.status_code == 200
    try:
        os.remove(filepath)
    except OSError:
        pass


def test_document_download_not_found(auth_client):
    resp = auth_client.get('/api/documents/99999/download')
    assert resp.status_code == 404
    assert b'Not found' in resp.data


def test_document_delete(auth_client):
    conn = get_db()
    doc_id = gen_id()
    filename = f'test_doc_del_{doc_id}.txt'
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    with open(filepath, 'w') as f:
        f.write('delete test')
    conn.execute(
        "INSERT INTO documents (doc_id, emp_id, name, category, file_path, file_size, uploaded_at, context, doc_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [doc_id, 'EMP002', 'del.txt', 'Other', filename, 11, now_ist(), 'General', 'Test']
    )
    conn.close()
    resp = auth_client.delete(f'/api/documents/{doc_id}')
    assert resp.status_code == 200
    assert b'Document deleted' in resp.data
    assert not os.path.exists(filepath)


def test_document_delete_not_found(auth_client):
    resp = auth_client.delete('/api/documents/99999')
    assert resp.status_code == 404
    assert b'Not found' in resp.data


def test_document_delete_requires_auth(client):
    resp = client.delete('/api/documents/1')
    assert resp.status_code == 302


# ── LEAVE CANCEL ───────────────────────────────────────────────────

def test_leave_cancel(auth_client):
    resp = auth_client.post('/api/leaves', json={
        'leave_type': 'Casual', 'start_date': '2026-12-01', 'end_date': '2026-12-02', 'reason': 'Cancel test'
    })
    assert resp.status_code == 201
    leave_id = resp.get_json()['leave_id']
    resp = auth_client.post(f'/api/leaves/{leave_id}/cancel')
    assert resp.status_code == 200
    assert b'Leave cancelled' in resp.data


def test_leave_cancel_own(emp_client):
    resp = emp_client.post('/api/leaves', json={
        'leave_type': 'Casual', 'start_date': '2026-12-03', 'end_date': '2026-12-03', 'reason': 'Self cancel'
    })
    assert resp.status_code == 201
    leave_id = resp.get_json()['leave_id']
    resp = emp_client.post(f'/api/leaves/{leave_id}/cancel')
    assert resp.status_code == 200
    assert b'Leave cancelled' in resp.data
