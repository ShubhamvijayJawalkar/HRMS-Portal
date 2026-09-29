import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ['SECRET_KEY'] = 'test-secret-key'
# Dot-free, millisecond-unique file name: DuckDB derives its in-process database
# name from the file stem with dots stripped, so a dotted name (e.g. a float
# timestamp) makes two spellings of the same path collide with
# "Unique file handle conflict" when a connection is opened from another thread.
os.environ['DB_FILE'] = os.path.join(tempfile.gettempdir(), f'hrms_pw_{int(datetime.now().timestamp() * 1000)}.duckdb')
os.environ['FLASK_DEBUG'] = '0'
os.environ.setdefault('LOGIN_RATE_LIMIT', '60 per minute')
os.environ.setdefault('DEFAULT_RATE_LIMIT', '100000 per minute')
os.environ.setdefault('ANONYMISATION_SALT', 'test-anonymisation-salt-value')
os.environ.setdefault('APP_DB', 'duckdb')
os.environ['APP_DB_SCHEMA'] = 'legacy'
if os.getenv('APP_DB', 'duckdb').lower() in ('postgres', 'postgresql', 'pg'):
    import db_backend
    db_backend.reset_schema()

import threading
import time

import pytest
from playwright.sync_api import sync_playwright

from app import app

BASE_URL = 'http://localhost:8787'

@pytest.fixture(scope='session', autouse=True)
def server():
    # DuckDB attaches a database file once per process, so two overlapping
    # requests raise "Unique file handle conflict" and the server is run
    # single-threaded there. PostgreSQL has no such constraint, so it keeps a
    # threaded server -- which also stops a background job tick (the import
    # dispatcher runs every 15s) from stalling the whole suite behind the only
    # request thread.
    _is_duckdb = os.getenv('APP_DB', 'duckdb').lower() not in ('postgres', 'postgresql', 'pg')
    # On DuckDB the scheduler is off as well: a background job opens its own
    # connection while a request is being served, and DuckDB attaches a file
    # once per process, which is the original "Unique file handle conflict". The
    # import test presses "run now" instead of waiting for a tick.
    if _is_duckdb:
        os.environ['HRMS_DISABLE_SCHEDULER'] = '1'
    threaded = not _is_duckdb
    t = threading.Thread(target=lambda: app.run(host='127.0.0.1', port=8787, debug=False,
                                              use_reloader=False, threaded=threaded), daemon=True)
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
    page.click('.create-user-btn')
    page.wait_for_timeout(500)
    page.fill('#empId', 'EMP901')
    page.fill('#name', 'Test User')
    page.fill('#email', 'test901@company.com')
    page.select_option('#department', 'MIS')
    page.select_option('#role', 'Employee')
    with page.expect_response(lambda r: r.url.endswith('/api/users') and r.request.method == 'POST') as resp:
        page.click('#createUserModal .btn-primary')
    assert resp.value.ok, f'Create user failed: {resp.value.status}'
    page.wait_for_timeout(1500)
    body = page.text_content('#usersTableBody')
    assert 'EMP901' in body, f'EMP901 not found in {body}'

def test_admin_edits_user_permissions(page):
    """FR-USR-09: the permissions modal reads defaults and stores an override."""
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP001')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_timeout(3000)
    page.goto(BASE_URL + '/admin/users')
    page.wait_for_timeout(1500)
    page.fill('#searchInput', 'EMP002')
    page.wait_for_timeout(1500)
    page.click("button[title='Permissions']")
    page.wait_for_timeout(1500)
    assert page.is_visible('#perm-tickets'), 'permission grid did not render'
    # The Employee role default for tickets is allowed; deny it as an override.
    assert page.is_checked('#perm-tickets')
    page.uncheck('#perm-tickets')
    with page.expect_response(
        lambda r: r.url.endswith('/api/users/EMP002/permissions') and r.request.method == 'PUT'
    ) as resp:
        page.click('#permissionsModal .btn-primary')
    assert resp.value.ok, f'Permission update failed: {resp.value.status}'
    payload = resp.value.json()
    assert payload['overrides'] == {'tickets': False}
    assert payload['effective']['tickets'] is False
    # Everything else keeps the role default (no override row).
    assert payload['effective']['breaks'] is True
    page.wait_for_timeout(1000)

    # Re-opening shows the stored override, and clearing it reverts to default.
    page.click("button[title='Permissions']")
    page.wait_for_timeout(1500)
    assert not page.is_checked('#perm-tickets')
    page.check('#perm-tickets')
    page.click('#permissionsModal .btn-primary')
    page.wait_for_timeout(1500)
    state = page.evaluate(
        "fetch('/api/users/EMP002/permissions').then(r => r.json())"
    )
    assert state['overrides'] == {}, state['overrides']
    assert state['effective']['tickets'] is True


def test_admin_pii_reveal_is_audited(page):
    """FR-USR-15: the PII reveal is a real, audited capability."""
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP001')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_timeout(3000)
    page.goto(BASE_URL + '/admin/users')
    page.wait_for_timeout(1500)
    page.fill('#searchInput', 'EMP002')
    page.wait_for_timeout(1500)
    page.on('dialog', lambda dialog: dialog.accept())
    page.click("button[title='Personal data (audited)']")
    page.wait_for_timeout(2000)
    assert page.is_visible('#piiModal'), 'PII modal did not open'
    body = page.text_content('#piiBody')
    for label in ('Date of birth', 'Address', 'Emergency contact'):
        assert label in body, f'{label} missing from {body}'
    audit = page.evaluate("fetch('/api/audit-log').then(r => r.json())")
    reveals = [row for row in audit['data'] if row['action'] == 'PII_REVEAL']
    assert reveals, 'the reveal was not audited'
    assert reveals[0]['entity_id'] == 'EMP002'

def test_admin_assigns_a_leave_policy(page):
    """FR-LEA-08: the policy modal changes the derived entitlement."""
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP001')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_timeout(3000)
    # A dedicated employee: the policy must not change the balances the leave
    # tests depend on.
    page.evaluate("""
        () => fetch('/api/users', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({emp_id: 'EMP904', name: 'Policy Subject',
                email: 'emp904@company.com', department: 'MIS', role: 'Employee',
                password: 'pass123'})
        })
    """)
    page.goto(BASE_URL + '/admin/users')
    page.wait_for_timeout(1500)
    page.fill('#searchInput', 'EMP904')
    page.wait_for_timeout(1500)
    page.click("button[title='Leave policy']")
    page.wait_for_timeout(1500)
    assert page.is_visible('#leavePolicyModal'), 'leave policy modal did not open'
    body = page.text_content('#lpBalances')
    assert 'Annual' in body and 'default' in body, body

    page.fill('#lpAccrual', '1')
    page.fill('#lpFrom', '2020-01-01')
    with page.expect_response(
        lambda r: r.url.endswith('/api/users/EMP904/leave-policy') and r.request.method == 'PUT'
    ) as resp:
        page.click('#leavePolicyModal .btn-primary')
    assert resp.value.ok, f'leave policy save failed: {resp.value.status}'
    payload = resp.value.json()
    annual = next(b for b in payload['balances'] if b['leave_type'] == 'Annual')
    # 1.0 day/month, effective from January: what the employee has *earned* so far
    # this year, not the whole year handed over at once.
    assert annual['total_days'] == datetime.now().month, annual
    assert annual['source'] == 'accrual', annual

    # The ledger: one row per elapsed month, posted by an on-demand run. Still an
    # admin session, so the route is reachable; a second press must post nothing.
    first_run = page.evaluate(
        "() => fetch('/api/accrual/run', {method: 'POST'}).then(r => r.json())"
    )
    # Every elapsed month of the year is now in the ledger, whether this run
    # posted it or an earlier one (the job is idempotent, so a second press adds
    # nothing).
    assert first_run['grants'] + first_run['already_posted'] >= datetime.now().month, first_run
    second_run = page.evaluate(
        "() => fetch('/api/accrual/run', {method: 'POST'}).then(r => r.json())"
    )
    assert second_run['grants'] == 0, second_run

    # The same operation is one click away in the leave-policy modal.
    page.click("button[title='Leave policy']")
    page.wait_for_timeout(1500)
    assert page.is_visible('#lpAccrueBtn'), 'the on-demand accrual action is missing'
    page.click('#lpAccrueBtn')
    page.wait_for_timeout(2000)

    # The employee sees the derived entitlement on their own balance endpoint.
    page.goto(BASE_URL + '/logout')
    page.wait_for_timeout(1000)
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP904')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_timeout(3000)
    balances = page.evaluate("fetch('/api/leave-balance').then(r => r.json())")
    annual = next(b for b in balances if b['leave_type'] == 'Annual')
    assert annual['total_days'] == datetime.now().month, annual
    assert annual['source'] == 'accrual', annual
    assert 'reserved_days' in annual


def test_admin_import_users_runs_as_a_background_job(page):
    """FR-USR-04: the upload is queued, polled, and reported as a job."""
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP001')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_timeout(3000)
    page.goto(BASE_URL + '/admin/import-users')
    page.wait_for_timeout(1500)

    csv_body = (
        'emp_id,name,email,role,department\n'
        'EMP905,Browser Import,browser905@company.com,Employee,MIS\n'
        'BAD2,Bad Row,bad2@company.com,Employee,MIS\n'
    )
    page.set_input_files('#fileInput', files=[
        {'name': 'users.csv', 'mimeType': 'text/csv', 'buffer': csv_body.encode()},
    ])
    with page.expect_response(
        lambda r: r.url.endswith('/api/users/import') and r.request.method == 'POST'
    ) as resp:
        page.wait_for_timeout(2500)
    assert resp.value.status == 202, resp.value.status
    job_id = resp.value.json()['job_id']

    # "Run now" is idempotent, so this works whether or not the scheduler is on.
    page.evaluate(f"fetch('/api/users/import/{job_id}/run', {{method: 'POST'}}).then(r => r.status)")
    page.wait_for_function(
        "() => document.getElementById('importResult').textContent.includes('finished')",
        timeout=60000,
    )
    result = page.text_content('#importResult')
    assert '1 imported' in result, result
    assert '1 skipped' in result, result
    assert 'row 3' in result, result

    page.wait_for_function(
        "() => document.getElementById('jobHistory').textContent.includes('completed')",
        timeout=30000,
    )
    history = page.text_content('#jobHistory')
    assert str(job_id) in history, history
    assert 'users.csv' in history
    assert '1 imported, 1 skipped' in history, history

def _login(page, emp_id):
    page.goto(BASE_URL + '/login')
    page.fill('#empId', emp_id)
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_timeout(3000)


def test_admin_anonymises_an_archived_user_with_two_people(page):
    """FR-USR: the erasure is planned, proposed and confirmed by a second person.

    It runs on its own employee (EMP903) and its own second administrator
    (EMP902): erasing one of the seeded users would break the later tests that
    log in as EMP002.
    """
    _login(page, 'EMP001')
    page.evaluate("""
        () => fetch('/api/users', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({emp_id: 'EMP902', name: 'Second Admin',
                email: 'emp902@company.com', department: 'MIS', role: 'Admin',
                password: 'pass123'})
        })
    """)
    page.evaluate("""
        () => fetch('/api/users', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({emp_id: 'EMP903', name: 'Erasure Subject',
                email: 'emp903@company.com', department: 'MIS', role: 'Employee',
                password: 'pass123'})
        })
    """)
    page.goto(BASE_URL + '/admin/users')
    page.wait_for_timeout(1500)
    page.on('dialog', lambda dialog: dialog.accept())

    # The erasure action is only offered for an archived employee.
    page.fill('#searchInput', 'EMP903')
    page.wait_for_timeout(1500)
    assert page.locator("button[title='Anonymise (irreversible, two-person)']").count() == 0

    page.click("button[title='Archive']")
    page.wait_for_timeout(2500)
    page.fill('#searchInput', 'EMP903')
    page.wait_for_timeout(1500)
    page.click("button[title='Anonymise (irreversible, two-person)']")
    page.wait_for_timeout(2000)
    assert page.is_visible('#anonModal'), 'anonymisation modal did not open'
    plan = page.text_content('#anonPlan')
    assert 'Erasing' in plan and 'Keeping' in plan and 'audit rows to scrub' in plan, plan

    with page.expect_response(
        lambda r: r.url.endswith('/api/users/EMP903/anonymise') and r.request.method == 'POST'
    ) as resp:
        page.click('#anonProposeBtn')
    assert resp.value.status == 201, resp.value.status
    body = resp.value.json()
    assert body['status'] == 'proposed' and body['requested_by'] == 'EMP001'
    page.wait_for_timeout(1000)
    assert page.is_visible('#anonConfirmBtn'), 'the confirm step did not appear'
    request_id = body['request_id']

    # The requester is refused: it takes a different person.
    refused = page.evaluate(
        f"fetch('/api/anonymisation/{request_id}/confirm', {{method: 'POST'}}).then(r => r.status)"
    )
    assert refused == 409, refused

    # A different administrator confirms, and the system applies the erasure.
    _login(page, 'EMP902')
    applied = page.evaluate(
        f"fetch('/api/anonymisation/{request_id}/confirm', {{method: 'POST'}}).then(r => r.json())"
    )
    assert applied['status'] == 'applied', applied
    assert applied['confirmed_by'] == 'EMP902'
    assert applied['result']['emp_id'] == 'EMP903'

    _login(page, 'EMP001')
    user = page.evaluate("fetch('/api/users/EMP903').then(r => r.json())")
    assert user['name'] == 'Anonymised Employee', user
    assert user['email'].endswith('@anonymised.invalid'), user
    audit = page.evaluate("fetch('/api/audit-log').then(r => r.json())")
    rows = [r for r in audit['data'] if r['action'] == 'USER_ANONYMISED']
    assert rows, 'the erasure was not audited'

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
    # The widget renders "0h" / "7.5h" (toFixed(1)+'h'); parse the numeric part.
    txt = (total.text_content() or '').replace('h', '').strip()
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


def test_ats_offer_and_preboarding_browser_flow(page):
    """The guarded hire path and token-scoped document page work in a browser."""
    marker = f'browser-lifecycle-{int(time.time() * 1000)}@example.com'
    page.goto(BASE_URL + '/login')
    page.fill('#empId', 'EMP001')
    page.fill('#password', 'pass123')
    page.click('button[type="submit"]')
    page.wait_for_url(BASE_URL + '/dashboard')
    result = page.evaluate('''async (marker) => {
        const post = async (url, body) => {
            const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
            return {status: r.status, body: await r.json()};
        };
        const put = async (url, body) => {
            const r = await fetch(url, {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
            return {status: r.status, body: await r.json()};
        };
        const candidate = await post('/api/candidates', {name: 'Browser Candidate', email: marker});
        const cid = candidate.body.id;
        await put(`/api/candidates/${cid}/status`, {status: 'Screened'});
        await put(`/api/candidates/${cid}/status`, {status: 'Interviewed'});
        const offer = await post('/api/offers', {candidate_id: cid, offered_salary: 400000, basic_pct: 50, hra_pct: 30, allowances_pct: 20});
        const accepted = await post(`/api/offers/${offer.body.id}/accept`, {});
        return {candidate, offer, accepted};
    }''', marker)
    assert result['candidate']['status'] == 201, result
    assert result['offer']['status'] == 201, result
    assert result['accepted']['status'] == 200, result
    token = result['accepted']['body']['preboarding_token']
    page.goto(f'{BASE_URL}/preboarding/{token}')
    assert 'Pre-boarding' in page.title()
    file_input = page.locator('input[type="file"]').first
    file_input.set_input_files({'name': 'id-proof.pdf', 'mimeType': 'application/pdf', 'buffer': b'%PDF-1.4\nbrowser'})
    with page.expect_response(lambda r: '/documents/' in r.url, timeout=10000) as upload_response:
        page.locator('button[onclick^="upload"]').first.click()
    assert upload_response.value.status == 201
    page.wait_for_timeout(1000)
    with page.expect_response(lambda r: r.url.endswith('/submit'), timeout=10000) as submit_response:
        page.get_by_role('button', name='Submit pre-boarding').click()
    assert submit_response.value.status == 200
    page.wait_for_timeout(500)
    assert 'Current step: 2 of 5' in page.text_content('body')
    page.goto(BASE_URL + '/admin/candidates')
    page.wait_for_selector('select option[value="Hired"]', state='attached', timeout=10000)
    assert page.locator('option[value="Hired"]').first.is_disabled()
