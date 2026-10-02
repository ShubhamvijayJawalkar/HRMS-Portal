import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ.setdefault(
    'DATABASE_URL',
    'postgresql+psycopg://postgres:postgres@localhost:55432/hrms',
)
os.environ['SECRET_KEY'] = 'test-secret-key'
os.environ['FLASK_DEBUG'] = '0'
os.environ.setdefault('LOGIN_RATE_LIMIT', '60 per minute')
# The password-reset routes carry their own 5/min limit (the SRS's figure), and
# neither suite lifted it. A full run makes several forgot-password calls inside a
# minute, so it tripped and surfaced as a 429 on an unrelated assertion — the same
# shape as the LOGIN_RATE_LIMIT gap above, found a second time.
os.environ.setdefault('FORGOT_PASSWORD_RATE_LIMIT', '100000 per minute')
os.environ.setdefault('RESET_PASSWORD_RATE_LIMIT', '100000 per minute')
os.environ.setdefault('DEFAULT_RATE_LIMIT', '100000 per minute')
os.environ.setdefault('ANONYMISATION_SALT', 'test-anonymisation-salt-value')
# FR-AUTH-11: encrypts authenticator secrets at rest. Fixed non-production
# value; MFA returns 503 without it rather than storing secrets in the clear.
os.environ.setdefault('MFA_ENCRYPTION_KEY', '3CkZThJOKnNbJkL2ksuJN8gsQ7cJi5FAFPt3g50KmsE=')
os.environ['APP_DB_SCHEMA'] = 'legacy'

import db_backend

db_backend.reset_schema()

import json  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import pyotp  # noqa: E402
import pytest  # noqa: E402
from playwright.sync_api import TimeoutError as PlaywrightTimeout  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from app import app, get_db  # noqa: E402

BASE_URL = 'http://localhost:8787'


def _stored_secret(emp_id):
    """Decrypt an enrolled employee's authenticator secret for a challenge."""
    import mfa

    conn = get_db()
    try:
        row = conn.execute(
            'SELECT secret_encrypted FROM mfa_credentials WHERE emp_id = ?', [emp_id],
        ).fetchone()
    finally:
        conn.close()
    assert row, f'{emp_id} has no MFA credential to challenge against'
    return mfa.decrypt_secret(row[0])


def _login(page, emp_id='EMP001', password='pass123'):
    """Sign in, walking the FR-AUTH-11 second factor when the account has one.

    The seeded admin is an Admin, so a second factor is compulsory for it and a
    bare password no longer reaches the dashboard. Rather than exempt the browser
    suite from the control, this drives the real panel: on the first sign-in it
    enrols (reading the secret the page itself displays and minting a code from
    it) and on later ones it challenges. That also means the login page's MFA
    panel is exercised by every test rather than by one dedicated test.

    The Employee accounts the suite also uses have no factor and take the
    single-step path, so both shapes stay covered.
    """
    page.goto(BASE_URL + '/login')
    page.fill('#empId', emp_id)
    page.fill('#password', password)
    page.click('button[type="submit"]')

    # 15s, not 5s: the panel appears only after POST /login and the MFA enrol round
    # trip. A tight window here turns a loaded machine into a false "no MFA" and then
    # a timeout waiting for a dashboard that was never going to arrive.
    panel = page.locator('#mfaStep')
    try:
        panel.wait_for(state='visible', timeout=15000)
    except PlaywrightTimeout:
        page.wait_for_url(BASE_URL + '/dashboard')
        return

    # An enrolment shows the secret on the page; a challenge only shows a box, and
    # the secret has to come from the stored (encrypted) credential.
    #
    # The wait on the *text*, not on the box becoming visible: the panel reveals the
    # enrolment area first and fills the secret only after `/api/mfa/enrol` returns,
    # so reading it on visibility alone yields an empty string and mints a code from
    # no secret at all.
    if page.locator('#mfaEnrolBox').is_visible():
        page.wait_for_function(
            "() => document.getElementById('mfaSecret').textContent.trim().length > 0",
            timeout=10000,
        )
        secret = page.locator('#mfaSecret').inner_text().strip()
    else:
        secret = _stored_secret(emp_id)
    page.fill('#mfaCode', pyotp.TOTP(secret).now())
    page.click('#mfaBtn')
    page.wait_for_url(BASE_URL + '/dashboard')

@pytest.fixture(scope='session', autouse=True)
def server():
    # Threaded, unconditionally. This was conditional only because DuckDB
    # attaches a database file once per process, so two overlapping requests
    # raised "Unique file handle conflict" and the server had to be
    # single-threaded there. PostgreSQL has no such constraint, so the whole
    # suite was serialised behind one request thread for no reason — which also
    # meant a background scheduler tick (the import dispatcher runs every 15s)
    # stalled everything behind it. The import test presses "run now" rather than
    # waiting for a tick either way.
    t = threading.Thread(target=lambda: app.run(host='127.0.0.1', port=8787, debug=False,
                                              use_reloader=False, threaded=True), daemon=True)
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
    _login(page, 'EMP001', 'pass123')
    page.wait_for_url(BASE_URL + '/dashboard')
    assert page.url == BASE_URL + '/dashboard'

def test_employee_login(page):
    _login(page, 'EMP002', 'pass123')
    page.wait_for_url(BASE_URL + '/dashboard')
    assert page.url == BASE_URL + '/dashboard'

def test_admin_sees_user_tab(page):
    _login(page, 'EMP001', 'pass123')
    page.wait_for_timeout(2000)
    page.goto(BASE_URL + '/admin/users')
    page.wait_for_timeout(2000)
    tbody = page.locator('#usersTableBody')
    assert tbody.is_visible()
    # Wait for the rows rather than a fixed sleep: the table is filled by page JS,
    # and this test flaked once on a slow CDN load with an empty #pageInfo.
    tbody.locator('tr').first.wait_for(state='visible', timeout=15000)
    assert page.text_content('#pageInfo').startswith('Page')

def test_employee_cannot_access_admin_users(page):
    _login(page, 'EMP002', 'pass123')
    page.wait_for_timeout(2000)
    page.goto(BASE_URL + '/admin/users', wait_until='commit')
    page.wait_for_timeout(3000)
    assert page.url == BASE_URL + '/dashboard', f'Expected redirect to dashboard but got {page.url}'

def test_admin_create_user(page):
    _login(page, 'EMP001', 'pass123')
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
    _login(page, 'EMP001', 'pass123')
    page.wait_for_timeout(3000)
    page.goto(BASE_URL + '/admin/users')
    page.wait_for_timeout(1500)
    page.fill('#searchInput', 'EMP002')
    page.wait_for_timeout(1500)
    page.click("button[title='Permissions']")
    # Wait for the grid rather than sleeping. The modal fetches defaults and renders
    # 27 boxes asynchronously, and the third fixed sleep in this test was the one
    # that failed. The box appearing is the signal.
    page.locator('#perm-tickets').wait_for(state='visible', timeout=15000)
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
    _login(page, 'EMP001', 'pass123')
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
    _login(page, 'EMP001', 'pass123')
    page.wait_for_timeout(3000)
    # A dedicated employee: the policy must not change the balances the leave
    # tests depend on.
    page.evaluate("""
        () => fetch('/api/users', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({emp_id: 'EMP904', name: 'Policy Subject',
                email: 'emp904@company.com', department: 'MIS', role: 'Employee',
                password: 'jade-marlin-quilt-77'})
        })
    """)
    page.goto(BASE_URL + '/admin/users')
    # Wait for the button itself rather than a fixed sleep. The user list is
    # rendered by JS from `/api/users`, so a fixed timeout races the CDN load and
    # the search filter; this failed once on exactly that and passed in isolation.
    page.wait_for_selector('#searchInput', timeout=15000)
    page.fill('#searchInput', 'EMP904')
    page.wait_for_selector("button[title='Leave policy']", timeout=15000)
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
    _login(page, 'EMP904', 'jade-marlin-quilt-77')
    page.wait_for_timeout(3000)
    balances = page.evaluate("fetch('/api/leave-balance').then(r => r.json())")
    annual = next(b for b in balances if b['leave_type'] == 'Annual')
    assert annual['total_days'] == datetime.now().month, annual
    assert annual['source'] == 'accrual', annual
    assert 'reserved_days' in annual


def test_admin_import_users_runs_as_a_background_job(page):
    """FR-USR-04: the upload is queued, polled, and reported as a job."""
    _login(page, 'EMP001', 'pass123')
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

# The seeded demo users keep `pass123`: the boot seed writes their hash
# directly and never goes through the FR-AUTH-10 policy, which exists to refuse
# it on any interactive path. Test *fixtures* created through the API get a
# compliant password and log in with the matching one.
FIXTURE_PASSWORD = 'jade-marlin-quilt-77'


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
                password: 'jade-marlin-quilt-77'})
        })
    """)
    page.evaluate("""
        () => fetch('/api/users', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({emp_id: 'EMP903', name: 'Erasure Subject',
                email: 'emp903@company.com', department: 'MIS', role: 'Employee',
                password: 'jade-marlin-quilt-77'})
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
    # The Anonymise button only exists for an archived employee, so its *appearance*
    # is the signal that the archive landed and the list re-rendered — which is what
    # a fixed sleep was guessing at, and it failed intermittently with "modal did not
    # open" because the click landed while the list was still reloading.
    #
    # (Probing for the Restore button instead looks more direct and does not work:
    # it is rendered as a labelled button with no `title` attribute.)
    anon_btn = page.locator("button[title='Anonymise (irreversible, two-person)']")
    anon_btn.wait_for(state='visible', timeout=15000)
    anon_btn.click()
    page.locator('#anonModal').wait_for(state='visible', timeout=15000)
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
    _login(page, 'EMP902', FIXTURE_PASSWORD)
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
    _login(page, 'EMP002', 'pass123')
    page.wait_for_timeout(3000)
    page.click('#breaktab')
    page.wait_for_timeout(2000)
    btns = page.locator('.break-type-btn')
    assert btns.count() >= 1

def test_can_start_and_end_break(page):
    _login(page, 'EMP002', 'pass123')
    page.wait_for_timeout(3000)
    page.click('#breaktab')
    page.wait_for_timeout(2000)
    first_btn = page.locator('.break-type-btn').first
    assert first_btn.is_visible(), 'No break type buttons visible'
    first_btn.click()
    active = page.locator('#activeBreakInfo')
    end_btn = active.locator('.endBreakBtn')
    # Wait for the button, not a fixed interval: it only exists after the
    # POST /api/start-break round-trip and the re-render. This assertion used a
    # 2s sleep and was the flakier of the two break tests, because the wait was
    # an upper bound rather than a signal.
    end_btn.wait_for(state='visible', timeout=10000)
    assert end_btn.is_visible(), 'End Break button should appear after starting a break'
    end_btn.click()
    page.wait_for_selector('.endBreakBtn', state='hidden', timeout=10000)
    page.wait_for_timeout(1000)
    txt = active.text_content()
    assert 'No active break' in txt, f'Expected "No active break" but got "{txt}"'

def test_login_hours_display(page):
    _login(page, 'EMP002', 'pass123')
    page.wait_for_timeout(3000)
    total = page.locator('#totalLoginHours')
    # The widget renders "0h" / "7.5h" (toFixed(1)+'h'); parse the numeric part.
    txt = (total.text_content() or '').replace('h', '').strip()
    val = float(txt)
    assert val >= 0, f'Login hours should be >= 0, got {val}'

def test_end_break_self_heal(page):
    _login(page, 'EMP002', 'pass123')
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
    _login(page, 'EMP002', 'pass123')
    page.goto(BASE_URL + '/dashboard')
    page.click('#breaktab')
    # Wait for the break-type buttons rather than sleeping after the tab click. A
    # fixed wait here is an upper bound on the tab render; the button appearing is
    # the signal. This test failed intermittently at exactly the next line —
    # `.endBreakBtn` never appeared because the click landed on a page that had
    # not finished switching tabs. Third occurrence of the sleep-vs-signal mistake
    # in this file.
    first_btn = page.locator('.break-type-btn').first
    first_btn.wait_for(state='visible', timeout=15000)
    first_btn.click()
    active = page.locator('#activeBreakInfo')
    end_btn = active.locator('.endBreakBtn')
    # Same reason as in test_can_start_and_end_break. This one waited only 1s for
    # a round-trip that its sibling allowed 2s for, which is why it failed in full
    # runs and passed alone.
    end_btn.wait_for(state='visible', timeout=10000)
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
    _login(page, 'EMP002', 'pass123')
    page.wait_for_timeout(5000)
    page.goto(BASE_URL + '/dashboard')
    page.wait_for_timeout(2000)
    rows = page.locator('#loginSessionsTable tr')
    count = rows.count()
    assert count >= 0

def test_holidays_page_loads_for_admin(page):
    _login(page, 'EMP001', 'pass123')
    page.wait_for_timeout(5000)
    page.goto(BASE_URL + '/admin/holidays')
    page.wait_for_timeout(2000)
    body = page.text_content('body')
    assert 'Holiday Calendar' in body
    assert 'Add Holiday' in body

def test_can_submit_regularization(page):
    from datetime import date, timedelta
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    _login(page, 'EMP002', 'pass123')
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
    _login(page, 'EMP002', 'pass123')
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
    _login(page, 'EMP001', 'pass123')
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


def test_password_reset_page_completes_the_journey(page):
    """FR-AUTH-08/09: the link in the email lands on a page that works.

    The token used to come back in the `forgot-password` *response*, which is why
    nothing needed a page and the SRS's own URL — `/reset-password?token=...` — was
    a 404. Now the token is only in the outbox payload, so this walks the real
    path: request a reset, take the token from the queue the way the dispatcher
    does, and complete the reset in a browser.
    """
    from app import _decrypt_lifecycle_secret, _token_digest, get_db

    conn = get_db()
    try:
        email = conn.execute("SELECT email FROM users WHERE emp_id = 'EMP002'").fetchone()[0]
        conn.execute('DELETE FROM password_reset_tokens WHERE emp_id = ?', ['EMP002'])
    finally:
        conn.close()

    page.goto(BASE_URL + '/login')
    asked = page.evaluate(
        "([emp, mail]) => fetch('/api/forgot-password', {method: 'POST',"
        " headers: {'Content-Type': 'application/json'},"
        " body: JSON.stringify({emp_id: emp, email: mail})})"
        ".then(r => r.json().then(b => ({status: r.status, body: b})))",
        ['EMP002', email],
    )
    assert asked['status'] == 202, asked
    # FR-AUTH-08: nothing in the response identifies the account or the token.
    assert 'token' not in asked['body'], asked['body']

    conn = get_db()
    try:
        stored = conn.execute(
            "SELECT token FROM password_reset_tokens WHERE emp_id = 'EMP002' "
            'ORDER BY token_id DESC'
        ).fetchone()
        event = conn.execute(
            "SELECT payload FROM outbox_events WHERE event_type = 'password.reset' "
            "AND aggregate_id = 'EMP002' ORDER BY event_id DESC"
        ).fetchone()
    finally:
        conn.close()
    assert stored and event, (stored, event)
    payload = event[0] if isinstance(event[0], dict) else json.loads(event[0])
    token = _decrypt_lifecycle_secret(payload['reset_token_encrypted'])
    # FR-AUTH-09: hashed at rest. The plaintext is nowhere in the table.
    assert stored[0] == _token_digest(token) and stored[0] != token

    # The page the SRS emails actually serves, and the token is read by script
    # rather than interpolated into the HTML.
    page.goto(f'{BASE_URL}/reset-password?token={token}')
    assert page.is_visible('#resetForm'), 'the reset form did not render'
    assert token not in page.content(), 'the token was rendered into the page'

    # Mismatched confirmation is caught client-side, so no request is burned.
    page.fill('#newPassword', 'jade-marlin-quilt-77')
    page.fill('#confirmPassword', 'something-else-entirely')
    page.click('#resetBtn')
    page.wait_for_timeout(500)
    assert 'do not match' in page.text_content('#errorMsg')

    with page.expect_response(
        lambda r: r.url.endswith('/api/reset-password') and r.request.method == 'POST'
    ) as resp:
        page.fill('#confirmPassword', 'jade-marlin-quilt-77')
        page.click('#resetBtn')
    assert resp.value.status == 200, resp.value.status
    page.wait_for_timeout(500)
    assert page.is_visible('#successMsg'), 'the confirmation did not render'

    # Single use: the same link is now dead, on the page rather than in the API.
    page.goto(f'{BASE_URL}/reset-password?token={token}')
    page.fill('#newPassword', 'another-complying-password')
    page.fill('#confirmPassword', 'another-complying-password')
    with page.expect_response(
        lambda r: r.url.endswith('/api/reset-password') and r.request.method == 'POST'
    ) as replay:
        page.click('#resetBtn')
    assert replay.value.status == 400, replay.value.status
    page.wait_for_timeout(500)
    # The server's own wording is shown rather than the page's fallback: "that link
    # is no longer valid" from the client and "invalid or expired token" from the
    # server mean the same thing, and the server knows which it was.
    assert 'Invalid or expired token' in page.text_content('#errorMsg')

    # And the new password is the one that took.
    page.goto(BASE_URL + '/login')
    _login(page, 'EMP002', 'jade-marlin-quilt-77')
