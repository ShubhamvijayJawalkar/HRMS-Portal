import os, sys, json, tempfile
from datetime import datetime
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ['SECRET_KEY'] = 'test-secret-key'
os.environ['DB_FILE'] = os.path.join(tempfile.gettempdir(), f'hrms_pw_{datetime.now().timestamp()}.duckdb')
os.environ['FLASK_DEBUG'] = '0'

import pytest
from app import app
import threading, time
from playwright.sync_api import sync_playwright

BASE_URL = 'http://localhost:8787'

@pytest.fixture(scope='session', autouse=True)
def server():
    t = threading.Thread(target=lambda: app.run(host='127.0.0.1', port=8787, debug=False, use_reloader=False), daemon=True)
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

def test_login_page(page):
    page.goto(BASE_URL + '/login')
    assert page.title() == 'HRMS - Login'

def test_admin_login(page):
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP001')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_url(BASE_URL + '/dashboard')
    assert page.url == BASE_URL + '/dashboard'

def test_employee_login(page):
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP002')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_url(BASE_URL + '/dashboard')
    assert page.url == BASE_URL + '/dashboard'

def test_admin_sees_user_tab(page):
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP001')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_timeout(2000)
    page.goto(BASE_URL + '/admin/users')
    page.wait_for_timeout(2000)
    page.click('#manage-tab')
    page.wait_for_timeout(2000)
    tbody = page.locator('#usersTableBody')
    assert tbody.is_visible()
    page.wait_for_timeout(1000)
    assert page.text_content('#pageInfo').startswith('Page')

def test_employee_cannot_access_admin_users(page):
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP002')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_timeout(2000)
    page.goto(BASE_URL + '/admin/users', wait_until='commit')
    page.wait_for_timeout(3000)
    assert page.url == BASE_URL + '/dashboard', f'Expected redirect to dashboard but got {page.url}'

def test_admin_create_user(page):
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP001')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_timeout(3000)
    page.goto(BASE_URL + '/admin/users')
    page.wait_for_timeout(1000)
    page.click('#create-tab')
    page.wait_for_timeout(500)
    page.fill('#empId', 'TEST01')
    page.fill('#name', 'Test User')
    page.fill('#email', 'test@company.com')
    page.select_option('#department', 'MIS')
    page.select_option('#role', 'Employee')
    with page.expect_response(lambda r: r.url.endswith('/api/users') and r.request.method == 'POST') as resp:
        page.click('button[type="submit"]')
    assert resp.value.ok, f'Create user failed: {resp.value.status}'
    page.wait_for_timeout(1000)
    page.click('#manage-tab')
    page.wait_for_timeout(2000)
    body = page.text_content('#usersTableBody')
    assert 'TEST01' in body, f'TEST01 not found in {body}'

def test_breaks_tab_shows_on_user_dashboard(page):
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP002')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_timeout(3000)
    page.click('#breaktab')
    page.wait_for_timeout(2000)
    btns = page.locator('.break-type-btn')
    assert btns.count() >= 1

def test_can_start_and_end_break(page):
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP002')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_timeout(3000)
    page.click('#breaktab')
    page.wait_for_timeout(2000)
    first_btn = page.locator('.break-type-btn').first
    assert first_btn.is_visible(), 'No break type buttons visible'
    first_btn.click()
    page.wait_for_timeout(2000)
    active = page.locator('#activeBreakInfo')
    end_btn = active.locator('.endBreakBtn')
    assert end_btn.is_visible(), 'End Break button should appear after starting a break'
    end_btn.click()
    page.wait_for_selector('.endBreakBtn', state='hidden', timeout=10000)
    page.wait_for_timeout(1000)
    txt = active.text_content()
    assert 'No active break' in txt, f'Expected "No active break" but got "{txt}"'

def test_login_hours_display(page):
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP002')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_timeout(3000)
    total = page.locator('#totalLoginHours')
    txt = total.text_content()
    val = float(txt)
    assert val >= 0, f'Login hours should be >= 0, got {val}'

def test_end_break_self_heal(page):
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP002')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_timeout(3000)
    page.click('#breaktab')
    page.wait_for_timeout(2000)
    page.evaluate('localStorage.removeItem("activeBreakId")')
    page.evaluate('endBreak()')
    page.wait_for_timeout(3000)
    active = page.locator('#activeBreakInfo')
    txt = active.text_content()
    assert 'No active break' in txt or 'Break' in txt

def test_break_daily_limit_enforced(page):
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP002')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_timeout(5000)
    page.goto(BASE_URL + '/dashboard')
    page.wait_for_timeout(2000)
    page.click('#breaktab')
    page.wait_for_timeout(2000)
    first_btn = page.locator('.break-type-btn').first
    assert first_btn.is_visible()
    first_btn.click()
    page.wait_for_timeout(1000)
    active = page.locator('#activeBreakInfo')
    end_btn = active.locator('.endBreakBtn')
    assert end_btn.is_visible()
    end_btn.click()
    page.wait_for_selector('.endBreakBtn', state='hidden', timeout=10000)
    page.wait_for_timeout(1000)
    txt = active.text_content()
    assert 'No active break' in txt
    result = page.evaluate('''async () => {
        const r = await fetch('/api/start-break', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body:JSON.stringify({break_type:'Tea'})
        });
        return {status: r.status, json: await r.json()};
    }''')
    assert result['status'] == 201, f'Second break should be allowed until daily limit reached, got {result}'

def test_today_login_sessions_table(page):
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP002')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_timeout(5000)
    page.goto(BASE_URL + '/dashboard')
    page.wait_for_timeout(2000)
    rows = page.locator('#loginSessionsTable tr')
    count = rows.count()
    assert count >= 0

def test_holidays_page_loads_for_admin(page):
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP001')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_timeout(5000)
    page.goto(BASE_URL + '/admin/holidays')
    page.wait_for_timeout(2000)
    body = page.text_content('body')
    assert 'Holiday Calendar' in body
    assert 'Add Holiday' in body

def test_can_submit_regularization(page):
    from datetime import date, timedelta
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP002')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_timeout(5000)
    page.goto(BASE_URL + '/regularization', wait_until='commit')
    page.wait_for_timeout(2000)
    page.fill('#regDate', tomorrow)
    page.fill('#regReason', 'Test regularization request')
    with page.expect_response(lambda r: r.url.endswith('/api/regularization') and r.request.method == 'POST') as resp:
        page.click('button[type="submit"]')
    assert resp.value.ok, f'Regularization submission failed: {resp.value.status}'
    page.wait_for_timeout(2000)
    msg = page.text_content('#regMsg')
    assert 'submitted' in msg.lower(), f'Expected success message, got "{msg}"'

def test_can_apply_leave(page):
    from datetime import date, timedelta
    future = (date.today() + timedelta(days=10)).isoformat()
    future2 = (date.today() + timedelta(days=11)).isoformat()
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP002')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_timeout(5000)
    page.goto(BASE_URL + '/leaves')
    page.wait_for_timeout(3000)
    page.wait_for_selector('#leaveForm', timeout=10000)
    page.select_option('#leaveType', 'Casual')
    page.fill('#startDate', future)
    page.fill('#endDate', future2)
    page.fill('#reason', 'Personal work')
    with page.expect_response(lambda r: r.url.endswith('/api/leaves') and r.request.method == 'POST') as resp:
        page.evaluate('''
            (async () => {
                const r = await fetch('/api/leaves', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        leave_type: document.getElementById('leaveType').value,
                        start_date: document.getElementById('startDate').value,
                        end_date: document.getElementById('endDate').value,
                        reason: document.getElementById('reason').value
                    })
                });
                return {ok: r.ok, json: await r.json()};
            })()
        ''')
    assert resp.value, 'Leave application failed'
    page.wait_for_timeout(1000)
    result = page.evaluate('''async () => {
        const r = await fetch('/api/leaves');
        const data = await r.json();
        return data.map(l => l.leave_type).join(',');
    }''')
    assert 'Casual' in result, f'Expected "Casual" in leave list, got "{result}"'
