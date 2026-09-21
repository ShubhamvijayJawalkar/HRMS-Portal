import atexit
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

os.environ['SECRET_KEY'] = 'test-secret-key'
os.environ['DB_FILE'] = os.path.join(tempfile.gettempdir(), f'hrms_wf_{datetime.now().timestamp()}.duckdb')
os.environ['FLASK_DEBUG'] = '0'

import pytest

from app import app


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


@pytest.fixture
def emp_client(client):
    client.post('/login', json={'emp_id': 'EMP002', 'password': 'pass123'})
    return client


def cleanup():
    from hrms.db import close_db
    close_db()
    try:
        os.remove(os.environ['DB_FILE'])
    except OSError:
        pass


atexit.register(cleanup)


# ── PERFORMANCE MODULE ─────────────────────────────────────────────

def test_goals_list(auth_client):
    resp = auth_client.get('/api/goals')
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)


def test_goals_create(auth_client):
    resp = auth_client.post('/api/goals', json={
        'title': 'Development',
        'description': 'Learn Python',
        'target_date': '2026-12-31'
    })
    assert resp.status_code == 201
    data = resp.get_json()
    assert 'id' in data


def test_goals_create_requires_auth(client):
    resp = client.post('/api/goals', data={})
    assert resp.status_code == 302


def test_goal_rate(auth_client):
    r = auth_client.post('/api/goals', json={'title': 'Rate Me', 'description': 'Goal for rating'})
    assert r.status_code == 201
    gid = r.get_json()['id']
    r = auth_client.put(f'/api/goals/{gid}/rate', json={'rating': 4, 'comment': 'Good progress'})
    assert r.status_code == 200


def test_performance_reviews_list(auth_client):
    resp = auth_client.get('/api/performance-reviews')
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)


def test_performance_reviews_create(auth_client):
    resp = auth_client.post('/api/performance-reviews', json={
        'emp_id': 'EMP002',
        'reviewer_id': 'EMP001',
        'review_period': 'H1 2026'
    })
    assert resp.status_code == 201
    data = resp.get_json()
    assert 'id' in data


def test_performance_review_submit(auth_client):
    r = auth_client.post('/api/performance-reviews', json={
        'emp_id': 'EMP002', 'reviewer_id': 'EMP001', 'review_period': 'H1 2026'
    })
    assert r.status_code == 201
    rid = r.get_json()['id']
    r = auth_client.put(f'/api/performance-reviews/{rid}/submit', json={
        'rating': 4, 'comments': 'Good performance overall'
    })
    assert r.status_code == 200


def test_feedback_360_create(auth_client):
    resp = auth_client.post('/api/feedback-360', json={
        'emp_id': 'EMP002',
        'rating': 5,
        'comment': 'Great work'
    })
    assert resp.status_code == 201
    data = resp.get_json()
    assert data['message'] == 'Feedback submitted'


# ── ATS MODULE ─────────────────────────────────────────────────────

def test_jobs_list(auth_client):
    resp = auth_client.get('/api/jobs')
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)


def test_jobs_create(auth_client):
    resp = auth_client.post('/api/jobs', json={
        'title': 'Software Engineer',
        'department': 'Engineering',
        'location': 'Remote'
    })
    assert resp.status_code == 201
    data = resp.get_json()
    assert 'id' in data


def test_candidates_list(auth_client):
    resp = auth_client.get('/api/candidates')
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)


def test_candidates_create(auth_client):
    r = auth_client.post('/api/jobs', json={'title': 'Engineer'})
    job_id = r.get_json()['id']
    resp = auth_client.post('/api/candidates', json={
        'job_id': job_id,
        'name': 'John Doe',
        'email': 'john@test.com'
    })
    assert resp.status_code == 201
    data = resp.get_json()
    assert 'id' in data


def test_candidate_convert_to_employee(auth_client):
    r = auth_client.post('/api/jobs', json={'title': 'Engineer Convert'})
    job_id = r.get_json()['id']
    r = auth_client.post('/api/candidates', json={
        'job_id': job_id, 'name': 'Convert Me', 'email': 'convert@wf.com'
    })
    cand_id = r.get_json()['id']
    r = auth_client.put(f'/api/candidates/{cand_id}/status', json={'status': 'Hired'})
    assert r.status_code == 200
    r = auth_client.post(f'/api/candidates/{cand_id}/convert', json={
        'department': 'Engineering',
        'designation': 'Software Engineer',
        'date_of_joining': '2026-08-01'
    })
    assert r.status_code in (200, 201)
    if r.status_code == 201:
        data = r.get_json()
        assert 'emp_id' in data


def test_candidate_status_update(auth_client):
    r = auth_client.post('/api/jobs', json={'title': 'Engineer Status'})
    job_id = r.get_json()['id']
    r = auth_client.post('/api/candidates', json={
        'job_id': job_id, 'name': 'Status Cand', 'email': 'status@wf.com'
    })
    cand_id = r.get_json()['id']
    r = auth_client.put(f'/api/candidates/{cand_id}/status', json={'status': 'Screened'})
    assert r.status_code == 200


def test_interviews_list(auth_client):
    resp = auth_client.get('/api/interviews')
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)


def test_interviews_create(auth_client):
    r = auth_client.post('/api/jobs', json={'title': 'Interview Job'})
    job_id = r.get_json()['id']
    r = auth_client.post('/api/candidates', json={
        'job_id': job_id, 'name': 'Interview Cand', 'email': 'interview@wf.com'
    })
    cand_id = r.get_json()['id']
    resp = auth_client.post('/api/interviews', json={
        'candidate_id': cand_id,
        'interviewer': 'EMP001',
        'scheduled_at': '2026-08-15T10:00:00',
        'mode': 'Video Call'
    })
    assert resp.status_code == 201
    data = resp.get_json()
    assert 'id' in data


def test_interview_feedback(auth_client):
    r = auth_client.post('/api/jobs', json={'title': 'Feedback Job'})
    job_id = r.get_json()['id']
    r = auth_client.post('/api/candidates', json={
        'job_id': job_id, 'name': 'Feedback Cand', 'email': 'feedback@wf.com'
    })
    cand_id = r.get_json()['id']
    r = auth_client.post('/api/interviews', json={
        'candidate_id': cand_id, 'scheduled_at': '2026-08-15T10:00:00',
        'interviewer': 'EMP001', 'mode': 'Video Call'
    })
    assert r.status_code == 201
    iid = r.get_json()['id']
    r = auth_client.put(f'/api/interviews/{iid}/feedback', json={'feedback': 'Excellent', 'rating': 5})
    assert r.status_code == 200


def test_offers_list(auth_client):
    resp = auth_client.get('/api/offers')
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)


def test_offers_create(auth_client):
    r = auth_client.post('/api/jobs', json={'title': 'Offer Job'})
    job_id = r.get_json()['id']
    r = auth_client.post('/api/candidates', json={
        'job_id': job_id, 'name': 'Offer Cand', 'email': 'offer@wf.com'
    })
    cand_id = r.get_json()['id']
    resp = auth_client.post('/api/offers', json={
        'candidate_id': cand_id,
        'offered_salary': 50000
    })
    assert resp.status_code == 201
    data = resp.get_json()
    assert 'id' in data


def test_offer_accept(auth_client):
    r = auth_client.post('/api/jobs', json={'title': 'Accept Job'})
    job_id = r.get_json()['id']
    r = auth_client.post('/api/candidates', json={
        'job_id': job_id, 'name': 'Accept Cand', 'email': 'accept@wf.com'
    })
    cand_id = r.get_json()['id']
    r = auth_client.put(f'/api/candidates/{cand_id}/status', json={'status': 'Offered'})
    assert r.status_code == 200
    r = auth_client.post('/api/offers', json={
        'candidate_id': cand_id, 'offered_salary': 60000
    })
    assert r.status_code == 201
    offer_id = r.get_json()['id']
    r = auth_client.post(f'/api/offers/{offer_id}/accept')
    assert r.status_code == 200


# ── ONBOARDING MODULE ─────────────────────────────────────────────

def test_onboarding_tasks_list(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 50001
    resp = client.get('/api/onboarding-tasks')
    assert resp.status_code == 200


def test_onboarding_tasks_create(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 50002
    resp = client.post('/api/onboarding-tasks', json={
        'emp_id': 'EMP002',
        'task_name': 'Submit ID Proof',
        'due_date': '2026-08-01'
    })
    assert resp.status_code == 201
    data = resp.get_json()
    assert 'id' in data


def test_onboarding_task_complete(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 50003
    r = client.post('/api/onboarding-tasks', json={
        'emp_id': 'EMP002', 'task_name': 'Background Check', 'due_date': '2026-08-01'
    })
    assert r.status_code == 201
    tid = r.get_json()['id']
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 50004
    r = client.post(f'/api/onboarding-tasks/{tid}/complete')
    assert r.status_code == 200


def test_onboarding_checklist_get(client):
    with client.session_transaction() as sess:
        sess['emp_id'] = 'EMP001'
        sess['name'] = 'Admin'
        sess['role'] = 'Admin'
        sess['session_id'] = 50005
    resp = client.get('/api/onboarding-checklist')
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)
