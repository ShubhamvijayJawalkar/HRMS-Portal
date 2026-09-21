import json
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ['SECRET_KEY'] = 'test-secret-key'
os.environ['DB_FILE'] = os.path.join(tempfile.gettempdir(), f'hrms_pw2_{datetime.now().timestamp()}.duckdb')
os.environ['FLASK_DEBUG'] = '0'
os.environ['FLASK_ENV'] = 'test'

import threading
import time

import pytest
from playwright.sync_api import sync_playwright

from app import app

BASE_URL = 'http://localhost:8788'

@pytest.fixture(scope='session', autouse=True)
def server():
    t = threading.Thread(target=lambda: app.run(host='127.0.0.1', port=8788, debug=False, use_reloader=False), daemon=True)
    t.start()
    time.sleep(2)
    yield

@pytest.fixture(scope='session')
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()

@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={'width': 1280, 'height': 720})
    p = ctx.new_page()
    yield p
    ctx.close()


def test_health_endpoint(page):
    page.goto(BASE_URL + '/api/__health')
    body = json.loads(page.locator('pre').text_content() if page.locator('pre').count() else page.text_content('body'))
    assert body.get('db_ok') is True


def test_expense_workflow(page):
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP001')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_url(BASE_URL + '/dashboard', wait_until='commit')
    page.goto(BASE_URL + '/expenses')
    page.wait_for_timeout(2000)
    assert '/expenses' in page.url
    result = page.evaluate('''async () => {
        const r = await fetch('/api/expenses', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({cat_id: 1, amount: 500, description: 'E2E test expense'})
        });
        return {status: r.status, json: await r.json()};
    }''')
    assert result['status'] == 201
    list_result = page.evaluate('''async () => {
        const r = await fetch('/api/expenses');
        const data = await r.json();
        return data.length;
    }''')
    assert list_result >= 1


def test_regularization_workflow(page):
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP002')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_url(BASE_URL + '/dashboard', wait_until='commit')
    page.goto(BASE_URL + '/regularization', wait_until='commit')
    page.wait_for_timeout(2000)
    assert '/regularization' in page.url
    body = page.text_content('body')
    assert body is not None


def test_notifications_page(page):
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP001')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_url(BASE_URL + '/dashboard', wait_until='commit')
    page.wait_for_timeout(2000)
    notif_btn = page.locator('#notifBtn')
    assert notif_btn.is_visible()
    result = page.evaluate('''async () => {
        const r = await fetch('/api/notifications');
        return await r.json();
    }''')
    assert 'unread' in result
    assert 'data' in result


def test_break_approve(page):
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP001')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_url(BASE_URL + '/dashboard', wait_until='commit')
    page.wait_for_timeout(2000)
    result = page.evaluate('''async () => {
        const r = await fetch('/api/start-break', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({break_type: 'Tea'})
        });
        return {status: r.status, json: await r.json()};
    }''')
    assert result['status'] == 201
    assert 'break_id' in result['json']
    break_id = result['json']['break_id']
    end_result = page.evaluate(f'''async () => {{
        const r = await fetch('/api/end-break/{break_id}', {{method: 'POST'}});
        return {{status: r.status, json: await r.json()}};
    }}''')
    assert end_result['status'] == 200


def test_document_upload_page(page):
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP001')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_url(BASE_URL + '/dashboard', wait_until='commit')
    page.goto(BASE_URL + '/documents')
    page.wait_for_timeout(2000)
    assert '/documents' in page.url


def test_admin_assets_page(page):
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP001')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_url(BASE_URL + '/dashboard', wait_until='commit')
    page.goto(BASE_URL + '/admin/assets')
    page.wait_for_timeout(2000)
    assert '/admin/assets' in page.url


def test_admin_reports_page(page):
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP001')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_url(BASE_URL + '/dashboard', wait_until='commit')
    page.goto(BASE_URL + '/admin/reports')
    page.wait_for_timeout(2000)
    assert '/admin/reports' in page.url


def test_onboarding_page(page):
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP002')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_url(BASE_URL + '/dashboard', wait_until='commit')
    page.goto(BASE_URL + '/onboarding')
    page.wait_for_timeout(2000)
    assert '/onboarding' in page.url


def test_offboarding_page(page):
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP002')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_url(BASE_URL + '/dashboard', wait_until='commit')
    page.goto(BASE_URL + '/offboarding')
    page.wait_for_timeout(2000)
    assert '/offboarding' in page.url


def test_logout(page):
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP001')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_url(BASE_URL + '/dashboard', wait_until='commit')
    page.goto(BASE_URL + '/logout', wait_until='commit')
    page.wait_for_timeout(2000)
    assert '/login' in page.url
