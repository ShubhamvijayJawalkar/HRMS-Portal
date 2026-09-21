import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

os.environ['SECRET_KEY'] = 'test-secret-key'
os.environ['DB_FILE'] = os.path.join(tempfile.gettempdir(), f'hrms_conn_{datetime.now().timestamp()}.duckdb')
os.environ['FLASK_DEBUG'] = '0'

import pytest

from app import app, get_db
from hrms.db import close_db, health_status
from hrms.helpers import now_ist
from hrms.schema import init_db


@pytest.fixture
def client():
    app.config['TESTING'] = True
    app.config['SERVER_NAME'] = 'localhost'
    with app.test_client() as c:
        with app.app_context():
            yield c


def cleanup():
    close_db()
    try:
        os.remove(os.environ['DB_FILE'])
    except OSError:
        pass


# ── Connection Tests ──────────────────────────────────────────────

def test_health_endpoint(client):
    resp = client.get('/api/__health')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['db_ok'] is True
    assert 'db_file' in data
    assert data['db_file'] == os.environ['DB_FILE']
    assert 'connected' in data


def test_connection_lifecycle(client):
    conn1 = get_db()
    conn2 = get_db()
    assert conn1 is conn2, 'get_db() should return the same global connection'

    close_db()
    conn3 = get_db()
    assert conn3 is not conn1, 'After close_db(), get_db() should return a new connection'
    row = conn3.execute("SELECT 1").fetchone()
    assert row[0] == 1


def test_close_db_releases_file(client):
    conn = get_db()
    assert conn is not None
    close_db()
    status = health_status()
    assert status['connected'] is False, 'After close_db(), no live connections should remain'

    new_conn = get_db()
    assert new_conn is not None
    assert new_conn is not conn
    row = new_conn.execute("SELECT 'fresh'").fetchone()
    assert row[0] == 'fresh'


def test_schema_idempotency(client):
    init_db()
    init_db()


def test_concurrent_db_access(client):
    import duckdb
    results = []

    def db_worker(n):
        try:
            conn = duckdb.connect(os.environ['DB_FILE'])
            row = conn.execute("SELECT 1").fetchone()
            assert row[0] == 1
            conn.execute(
                "INSERT INTO audit_log (log_id, emp_id, action, details, ip_address, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [999000 + n, 'EMP001', 'CONCURRENT_TEST', f'Thread {n}', '127.0.0.1', now_ist()]
            )
            conn.close()
            return f'worker_{n}_ok'
        except Exception as e:
            return f'worker_{n}_error: {e}'

    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = [ex.submit(db_worker, i) for i in range(5)]
        for f in futures:
            result = f.result()
            results.append(result)
            assert '_ok' in result, f'Concurrent worker failed: {result}'

    assert len(results) == 5


def test_health_before_db_init(client):
    resp = client.get('/api/__health')
    assert resp.status_code == 200
    data = resp.get_json()
    assert 'db_ok' in data
    assert 'db_file' in data
    assert 'connected' in data


def test_db_file_path(client):
    from hrms.db import DB_FILE
    expected = os.environ['DB_FILE']
    assert DB_FILE == expected, f'DB_FILE mismatch: {DB_FILE} != {expected}'
    assert os.path.isabs(DB_FILE)
