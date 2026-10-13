import os
import pathlib
import sys
from datetime import datetime, timedelta

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
# FR-PAY-07 / FR-DOC-02: MinIO object storage for payslips and documents.
# Tests run against mocked S3 via moto (no MinIO container needed).
os.environ.setdefault('S3_ENDPOINT_URL', '')
os.environ.setdefault('S3_ACCESS_KEY_ID', 'minioadmin')
os.environ.setdefault('S3_SECRET_ACCESS_KEY', 'minioadmin')
os.environ.setdefault('S3_BUCKET', 'hrms')
os.environ.setdefault('S3_REGION', 'us-east-1')
os.environ.setdefault('S3_PRESIGNED_EXPIRY', '3600')

# Mock S3 for tests using moto
import moto  # noqa: E402

_mock_s3 = moto.mock_aws()
_mock_s3.start()
import boto3  # noqa: E402

_s3 = boto3.client('s3', region_name='us-east-1',
                   aws_access_key_id='minioadmin', aws_secret_access_key='minioadmin')
_s3.create_bucket(Bucket='hrms')

os.environ['APP_DB_SCHEMA'] = 'legacy'

import db_backend  # noqa: E402

db_backend.reset_schema()

import json  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

import document_security  # noqa: E402
from app import app  # noqa: E402

BASE_URL = 'http://localhost:8787'


# Every page in this app loads Bootstrap, bootstrap-icons and a Google webfont
# from CDNs (see docs/UI_INVENTORY.md). Playwright's default `wait_until='load'`
# waits for *every* subresource, so one slow CDN segment in a codespace fails an
# arbitrary test at page load — recorded repeatedly in AGENTS.md as flakes that
# "passed on a rerun", each time a different test. Reruns are not a fix, and the
# CDN is not something this suite should be waiting on to reach a form.
#
# `commit` returns when the navigation response arrives. Playwright's `fill`,
# `click` and `wait_for_*` all auto-wait for the element they need, so the real
# signal is still "the thing I want appeared" — the CDN is simply no longer in
# the critical path. (The React shell removed that dependency entirely; this is
# about the legacy pages that still have it.)
def _await_page_js(page):
    """Wait until the injected CSRF `window.fetch` patcher has installed itself.

    `security.init_csrf` appends a small inline script to every HTML response
    that wraps `window.fetch` so unsafe requests carry `X-CSRF-Token` (CC-06).
    Many tests below call `fetch` straight from `page.evaluate`, which relies on
    that wrapper.

    With `wait_until='commit'` the navigation has responded but the document has
    not been parsed, so the wrapper is not there yet and the request goes out
    un-tokened — which the server correctly refuses with 403. So the signal has
    to be the wrapper itself, not a sleep and not the URL. `__hrmsCsrf` is the
    idempotence flag that script sets on first run.

    The diagnostic on failure names where the page actually was: the failure this
    guards against is invisible otherwise, and the observed symptom (a 403 far
    from here) points nowhere near the cause.
    """
    try:
        page.wait_for_function('() => window.__hrmsCsrf === 1', timeout=15000)
    except Exception:
        state = page.evaluate("""() => ({
            href: location.href,
            readyState: document.readyState,
            csrf: window.__hrmsCsrf,
            hasRoot: Boolean(document.getElementById('root')),
            bodyLength: document.body ? document.body.innerHTML.length : -1,
        })""")
        raise AssertionError(
            'the CSRF fetch patcher never installed. '
            f'Page state: {state}. Either the response was not text/html '
            '(init_csrf injects only into HTML), or the document had not been '
            'parsed yet.'
        ) from None


def _login(page, emp_id='EMP001', password='pass123'):
    """Sign in through the password-only flow."""
    page.goto(BASE_URL + '/login', wait_until='commit')
    _await_page_js(page)
    page.fill('#empId', emp_id)
    page.fill('#password', password)
    page.click('button[type="submit"]')
    page.wait_for_url(BASE_URL + '/dashboard')
    _await_page_js(page)

@pytest.fixture(scope='session', autouse=True)
def server():
    scanner = document_security.clamav_scanner.scan_bytes
    document_security.clamav_scanner.scan_bytes = lambda _content: None
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
    document_security.clamav_scanner.scan_bytes = scanner

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
    page.goto(BASE_URL + '/login', wait_until='commit')
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
    page.goto(BASE_URL + '/admin/users', wait_until='commit')
    page.wait_for_timeout(2000)
    tbody = page.locator('#usersTableBody')
    assert tbody.is_visible()
    # Wait for the rows rather than a fixed sleep: the table is filled by page JS,
    # and this test flaked once on a slow CDN load with an empty #pageInfo.
    tbody.locator('tr').first.wait_for(state='visible', timeout=15000)
    assert page.text_content('#pageInfo').startswith('Page')


def test_shared_navigation_theme_is_responsive(page):
    _login(page, 'EMP002', 'pass123')
    page.wait_for_selector('.app-navbar .navbar-brand', timeout=15000)
    desktop_theme = page.evaluate("""() => ({
        background: getComputedStyle(document.body).backgroundColor,
        nav: getComputedStyle(document.querySelector('.app-navbar')).backgroundColor
    })""")
    assert desktop_theme['background'] == 'rgb(244, 247, 251)', desktop_theme
    assert '19, 31, 56' in desktop_theme['nav'], desktop_theme

    page.set_viewport_size({'width': 390, 'height': 844})
    toggler = page.locator('.app-navbar .navbar-toggler')
    assert toggler.is_visible()
    toggler.click()
    assert page.locator('#mainNav').is_visible()


def test_employee_cannot_access_admin_users(page):
    _login(page, 'EMP002', 'pass123')
    page.wait_for_timeout(2000)
    page.goto(BASE_URL + '/admin/users', wait_until='commit')
    page.wait_for_timeout(3000)
    assert page.url == BASE_URL + '/dashboard', f'Expected redirect to dashboard but got {page.url}'

def test_admin_create_user(page):
    _login(page, 'EMP001', 'pass123')
    page.wait_for_timeout(3000)
    page.goto(BASE_URL + '/admin/users', wait_until='commit')
    # The create button appears only once the directory JS has loaded; clicking
    # before it exists is a no-op, and the fixed-sleep version of this step
    # failed one full run on a slow CDN while passing in isolation. The button
    # appearing is the signal.
    page.locator('.create-user-btn').wait_for(state='visible', timeout=15000)
    page.click('.create-user-btn')
    page.locator('#empId').wait_for(state='visible', timeout=10000)
    page.fill('#empId', 'EMP901')
    page.fill('#name', 'Test User')
    page.fill('#email', 'test901@company.com')
    page.select_option('#department', 'MIS')
    page.select_option('#role', 'Employee')
    with page.expect_response(lambda r: r.url.endswith('/api/users') and r.request.method == 'POST') as resp:
        page.click('#createUserModal .btn-primary')
    assert resp.value.ok, f'Create user failed: {resp.value.status}'
    # The directory re-renders from the post-create reload; wait for the row the
    # same way, rather than a fixed sleep that fails on a slow machine.
    page.locator('#usersTableBody', has_text='EMP901').wait_for(state='visible', timeout=15000)
    body = page.text_content('#usersTableBody')
    assert 'EMP901' in body, f'EMP901 not found in {body}'

def test_admin_edits_user_permissions(page):
    """FR-USR-09: the permissions modal reads defaults and stores an override."""
    _login(page, 'EMP001', 'pass123')
    page.wait_for_timeout(3000)
    page.goto(BASE_URL + '/admin/users', wait_until='commit')
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
    page.goto(BASE_URL + '/admin/users', wait_until='commit')
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
    page.goto(BASE_URL + '/admin/users', wait_until='commit')
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
    page.goto(BASE_URL + '/logout', wait_until='commit')
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
    page.goto(BASE_URL + '/admin/import-users', wait_until='commit')
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
    page.goto(BASE_URL + '/admin/users', wait_until='commit')
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
    with page.expect_response(
        lambda response: response.url.endswith('/api/user/break-summary')
        and response.request.method == 'GET',
        timeout=15000,
    ) as summary_response:
        page.reload(wait_until='domcontentloaded')
    summary = summary_response.value.json()
    assert {'date', 'break_types', 'breaks'} <= summary.keys()
    page.locator('#breaktab').wait_for(state='visible', timeout=15000)
    page.click('#breaktab')
    btns = page.locator('.break-type-btn')
    # The buttons appear once the tab has rendered; wait for the first one
    # rather than a fixed sleep over the tab switch.
    btns.first.wait_for(state='visible', timeout=15000)
    assert btns.count() >= 1

def test_can_start_and_end_break(page):
    _login(page, 'EMP002', 'pass123')
    page.wait_for_timeout(3000)
    page.locator('#breaktab').wait_for(state='visible', timeout=15000)
    page.click('#breaktab')
    first_btn = page.locator('.break-type-btn').first
    first_btn.wait_for(state='visible', timeout=15000)
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
    # The widget is rendered by dashboard JS from the session-summary fetch; a
    # fixed sleep before reading it failed a full run on a slow page. The text
    # appearing is the signal.
    page.wait_for_function(
        "document.getElementById('totalLoginHours') && "
        "document.getElementById('totalLoginHours').textContent.trim() !== ''",
        timeout=15000)
    # The widget renders "0h" / "7.5h" (toFixed(1)+'h'); parse the numeric part.
    txt = (total.text_content() or '').replace('h', '').strip()
    val = float(txt)
    assert val >= 0, f'Login hours should be >= 0, got {val}'

def test_end_break_self_heal(page):
    _login(page, 'EMP002', 'pass123')
    page.wait_for_timeout(3000)
    # The break tab appears only once the dashboard JS has loaded; a fixed sleep
    # before this click failed one full run on a slow page while passing in
    # isolation. The tab appearing is the signal.
    page.locator('#breaktab').wait_for(state='visible', timeout=15000)
    page.click('#breaktab')
    # Same for the tab content: the click can land while the tab is still
    # switching and `#activeBreakInfo` does not exist yet.
    page.locator('#activeBreakInfo').wait_for(state='visible', timeout=15000)
    page.evaluate('localStorage.removeItem("activeBreakId")')
    page.evaluate('endBreak()')
    # endBreak() re-renders after the server round-trip; the text appearing is
    # the signal, not a clock. (An expression body: `wait_for_function` wraps
    # the string, so a `return` statement is a SyntaxError.)
    page.wait_for_function(
        "document.getElementById('activeBreakInfo') && "
        "(document.getElementById('activeBreakInfo').textContent.includes('No active break')"
        " || document.getElementById('activeBreakInfo').textContent.includes('Break'))",
        timeout=15000)
    txt = page.text_content('#activeBreakInfo')
    assert 'No active break' in txt or 'Break' in txt

def test_break_daily_limit_enforced(page):
    _login(page, 'EMP002', 'pass123')
    page.goto(BASE_URL + '/dashboard', wait_until='commit')
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
    page.goto(BASE_URL + '/dashboard', wait_until='commit')
    page.wait_for_timeout(2000)
    rows = page.locator('#loginSessionsTable tr')
    count = rows.count()
    assert count >= 0

def test_holidays_page_loads_for_admin(page):
    _login(page, 'EMP001', 'pass123')
    page.wait_for_timeout(5000)
    page.goto(BASE_URL + '/admin/holidays', wait_until='commit')
    page.wait_for_timeout(2000)
    body = page.text_content('body')
    assert 'Holiday Calendar' in body
    assert 'Add Holiday' in body


def test_large_leave_export_polls_then_opens_download(page):
    import json

    _login(page, 'EMP001', 'pass123')
    page.goto(BASE_URL + '/admin/holidays', wait_until='commit')
    excel_button = page.locator('button[title="Download Excel"]')
    excel_button.wait_for(state='visible', timeout=15000)
    page.route(
        '**/api/leaves/export?*',
        lambda route: route.fulfill(
            status=202, content_type='application/json',
            body=json.dumps({
                'job_id': 999, 'status': 'pending', 'row_count': 501,
                'status_url': '/api/leaves/exports/999',
            }),
        ),
    )
    page.route(
        '**/api/leaves/exports/999',
        lambda route: route.fulfill(
            content_type='application/json',
            body=json.dumps({
                'job_id': 999, 'status': 'completed',
                'download': '/api/leaves/exports/999/download',
            }),
        ),
    )
    page.route(
        '**/api/leaves/exports/999/download',
        lambda route: route.fulfill(
            content_type='text/html', body='<html>download route reached</html>',
        ),
    )
    excel_button.click()
    page.locator('.toast-msg').filter(
        has_text='Large export queued (501 rows)',
    ).wait_for(state='visible', timeout=5000)
    page.wait_for_url('**/api/leaves/exports/999/download', timeout=10000)


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
    page.goto(BASE_URL + '/leaves', wait_until='commit')
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
    page.goto(f'{BASE_URL}/preboarding/{token}', wait_until='commit')
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
    page.goto(BASE_URL + '/admin/candidates', wait_until='commit')
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

    page.goto(BASE_URL + '/login', wait_until='commit')
    _await_page_js(page)
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
    page.goto(f'{BASE_URL}/reset-password?token={token}', wait_until='commit')
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
    page.goto(f'{BASE_URL}/reset-password?token={token}', wait_until='commit')
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
    page.goto(BASE_URL + '/login', wait_until='commit')
    _login(page, 'EMP002', 'jade-marlin-quilt-77')


def test_admin_sets_a_password_from_the_user_management_panel(page):
    """The recovery path when there is no mail server.

    `/api/forgot-password` can only deliver its link by email, so on a deployment
    with no SMTP a forgotten password has no self-service route and the admin panel
    is the only way out. That makes this a load-bearing control rather than a
    convenience, so it is exercised through the real UI — a modal that exists in the
    template but is never reachable would pass every API test.

    Driven as a browser test for a reason a unit test cannot cover: the generated
    password has to be shown *outside* the input it came from, because the field is
    cleared after submit. The same mistake once hid a whole success message inside a
    form that success had just hidden.
    """
    _login(page, 'EMP001', 'pass123')
    page.wait_for_timeout(3000)
    page.goto(BASE_URL + '/admin/users', wait_until='commit')
    page.wait_for_selector("button[title^='Set a new password']", timeout=15000)

    page.fill('#searchInput', 'EMP002')
    # **Scope the click to the row.** The first version of this test used
    # `page.click("button[title='...']")`, which clicks the *first match on the
    # page* — and because the search input debounces, it reset the password of a
    # different employee ("Second Admin (EMP902)") and then failed on the label
    # assertion. It passed in isolation and failed in a full run, which is the
    # signature of test-order interference rather than a product bug: EMP902 only
    # exists because another test creates it.
    #
    # Two changes: wait for the row itself (the signal that the filter applied),
    # then click within that row. Never `page.click` on a selector that matches
    # several rows.
    row = page.locator('tr', has=page.locator('td strong', has_text='EMP002')).first
    row.wait_for(state='visible', timeout=15000)
    row.locator("button[title^='Set a new password']").click()
    # The element appearing is the signal; a fixed sleep for a modal is the
    # sleep-vs-signal pattern this file has now had to fix five times.
    page.locator('#passwordModal').wait_for(state='visible', timeout=15000)
    assert 'EMP002' in page.text_content('#pwTargetLabel')

    # Blank generates a compliant password, which is the whole point on a deployment
    # with no mail server: there is no email to deliver it, so it must be shown.
    with page.expect_response(
        lambda r: '/api/admin/users/EMP002/password' in r.url and r.request.method == 'POST'
    ) as resp:
        page.click('#pwConfirm')
    assert resp.value.ok, f'{resp.value.status}: {resp.value.text()}'
    payload = resp.value.json()
    generated = payload.get('generated_password')
    assert generated, f'no password was generated or returned: {payload}'

    # Rendered outside the (now cleared) input, so it survives the form reset.
    page.locator('#pwResult').wait_for(state='visible', timeout=10000)
    assert generated in page.text_content('#pwResult')
    assert 'EMP902' not in page.text_content('#pwResult'), (
        'the password was set on the wrong employee: the row-scoped click should make '
        'that impossible'
    )
    assert page.input_value('#pwNew') == '', 'the field was not cleared after submit'

    # The warning about signing the employee out is stated, not assumed — it is the
    # surprising part of the action and an admin clicking the wrong button would
    # otherwise sign a colleague out with no idea why.
    modal_text = page.text_content('#passwordModal')
    assert 'signs the employee out' in modal_text.lower(), modal_text[:300]

    # And it really works: sign in as EMP002 with the generated password.
    page.goto(BASE_URL + '/login', wait_until='commit')
    _login(page, 'EMP002', generated)


# ══════════════════════════════════════════════════════════════════════════
#  React UI — Phase 1 / Phase 2 pilot of docs/REACT_UI_MIGRATION.md
#
#  These drive the real SPA against the real session and the real CSRF guard.
#  The unit suite proves the serving contracts; only a browser proves the shell
#  mounts, renders the policy-filtered navigation and can complete a
#  CSRF-protected write — each of which fails silently (a blank page, a missing
#  header, a 403) rather than loudly.
# ══════════════════════════════════════════════════════════════════════════

REACT_BUILT = (pathlib.Path(__file__).resolve().parent.parent / 'static' / 'app'
               / 'index.html').exists()

requires_react_build = pytest.mark.skipif(
    not REACT_BUILT,
    reason='React bundle not built; run `npm --prefix frontend run build`',
)


@requires_react_build
def test_react_shell_mounts_and_renders_the_policy_navigation(page):
    """The shell boots, and its navbar is the policy-filtered one.

    EMP001 is an Admin, so the tree is the full one. The assertion that matters
    is not the labels but that the *Modules* group exists at all: it is dropped
    entirely when no child is permitted, so its presence means the SPA rendered
    what the server decided rather than a hard-coded list.
    """
    _login(page)
    page.goto(BASE_URL + '/app/dashboard', wait_until='commit')

    page.locator('h1.page__title').wait_for(state='visible', timeout=15000)
    assert 'Welcome back' in page.inner_text('h1.page__title')

    labels = page.eval_on_selector_all(
        '.navbar__links > li > a, .navbar__links > li > button',
        'els => els.map(e => e.innerText.trim())')
    assert 'Dashboard' in labels, labels
    assert 'Modules' in labels, labels
    assert 'Profile' in labels, labels

    children = page.eval_on_selector_all(
        '.navbar__dropdown .dropdown-item', 'els => els.map(e => e.innerText.trim())')
    assert children, 'the Modules dropdown is empty'

    # `Users` is a top-level entry, not a dropdown child, so it is asserted
    # against every link. Hrefs are *router* paths, which the `/app` basename
    # has already prefixed — checking for a bare `/admin/users` fails against
    # correct output.
    all_hrefs = page.eval_on_selector_all(
        '.navbar__links a', 'els => els.map(e => e.getAttribute("href"))')
    assert '/app/admin/users' in all_hrefs, all_hrefs
    assert '/app/admin/payroll' in all_hrefs, all_hrefs

    assert page.eval_on_selector_all('[role="separator"]', 'els => els.length') >= 1, (
        'navigation_for() forwards a divider entry; the React navbar must render it '
        'as a separator rather than an item with no label'
    )


@requires_react_build
def test_react_dashboard_panels_load_without_console_errors(page):
    """Every panel resolves, and nothing is left spinning or errored.

    Each panel owns its own request, so one failing endpoint shows as one failed
    panel rather than a blank page — which means the page can look fine while a
    panel is silently stuck. Waiting for the elements is the signal.
    """
    _login(page)
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))

    page.goto(BASE_URL + '/app/dashboard', wait_until='commit')
    page.locator('h1.page__title').wait_for(state='visible', timeout=15000)

    for title in ['Your profile', 'Notifications', 'Upcoming holidays',
                 'Login sessions', 'Breaks', 'Leave']:
        page.locator('.panel__title', has_text=title).first.wait_for(
            state='visible', timeout=15000)

    page.wait_for_function(
        '() => document.querySelectorAll(".state--loading").length === 0', timeout=15000)
    assert page.eval_on_selector_all('.state--error', 'els => els.length') == 0, (
        page.eval_on_selector_all('.state--error', 'els => els.map(e => e.innerText)'))
    assert not errors, f'uncaught JS errors in the SPA: {errors}'


@requires_react_build
def test_react_links_carry_the_mount_prefix_exactly_once(page):
    """Every rendered href is `/app/<route>`, never `/app/app/<route>`.

    The router runs with `basename="/app"` and react-router prepends it itself.
    Prefixing the `to`/`path` values as well produces a doubled path that still
    *renders* — the shell matches the stripped path — while every link points
    somewhere that 404s. A test that only asserted "the dashboard rendered"
    passed straight through that bug.
    """
    _login(page)
    page.goto(BASE_URL + '/app/dashboard', wait_until='commit')
    page.locator('h1.page__title').wait_for(state='visible', timeout=15000)

    hrefs = page.eval_on_selector_all(
        '.navbar__brand, .navbar__links a',
        'els => els.map(e => e.getAttribute("href"))')
    assert hrefs, 'no navigation links rendered'
    for href in hrefs:
        assert not href.startswith('/app/app/'), f'doubled mount prefix: {href}'
        assert href.startswith('/app/'), f'link is not inside the SPA mount: {href}'
    assert '/app/dashboard' in hrefs


@requires_react_build
def test_react_client_route_survives_a_hard_refresh(page):
    """A browser refresh on a client-side route must boot the SPA.

    `/app/<route>` has no file behind it; the Flask fallback answers with the
    entry point and the router takes over. Without that, every shared link and
    every refresh is a 404.

    Both outcomes are asserted, because there are now two kinds of route: a
    migrated screen resolves to itself, and an unmigrated one resolves to the
    explicit "not yet migrated" page. What must never happen is the third thing
    — a silent redirect that makes an unbuilt screen look like it worked.
    """
    _login(page)

    # A migrated route stays put across a hard refresh.
    page.goto(BASE_URL + '/app/leaves', wait_until='commit')
    page.locator('h1.page__title').wait_for(state='visible', timeout=15000)
    assert 'Leave' in page.inner_text('h1.page__title')
    assert page.url.endswith('/app/leaves'), page.url

    page.reload(wait_until='commit')
    page.locator('h1.page__title').wait_for(state='visible', timeout=15000)
    assert page.url.endswith('/app/leaves'), page.url
    assert 'Leave' in page.inner_text('h1.page__title')

    # A genuinely unknown route says so rather than pretending to be the
    # dashboard.
    page.goto(BASE_URL + '/app/nope-not-a-page', wait_until='commit')
    page.locator('h1.page__title').wait_for(state='visible', timeout=15000)
    assert 'Not yet migrated' in page.inner_text('h1.page__title')
    assert page.url.endswith('/app/nope-not-a-page'), page.url


@requires_react_build
def test_react_requires_a_session_and_redirects_to_the_login_page(page):
    """No session means the server's login page, not an empty shell.

    The SPA does not render its own sign-in form: that flow is still a Jinja
    template owning the deferred MFA step and the session/attendance
    bookkeeping, and migrating it is Phase 3.
    """
    page.goto(BASE_URL + '/app/dashboard', wait_until='commit')
    page.wait_for_url(BASE_URL + '/login', timeout=15000)
    page.locator('#empId').wait_for(state='visible', timeout=15000)


@requires_react_build
def test_react_completes_a_csrf_protected_write(page):
    """The typed API client can perform an unsafe request.

    This is the Phase 1 exit criterion the legacy templates get for free: they
    rely on `security.init_csrf` injecting a `window.fetch` wrapper into every
    HTML response, which an SPA does not have. So the client acquires the token
    from `/api/csrf-token` and sends the header itself — and the pair of
    requests below asserts the write is genuinely CSRF-checked, not merely
    accepted, by showing the same call refused without the header.
    """
    _login(page)
    page.goto(BASE_URL + '/app/dashboard', wait_until='commit')
    page.locator('h1.page__title').wait_for(state='visible', timeout=15000)

    result = page.evaluate("""async () => {
        const tok = await (await fetch('/api/csrf-token', {credentials: 'same-origin'})).json();
        const accepted = await fetch('/api/notifications/read', {
            method: 'POST', credentials: 'same-origin',
            headers: {'Content-Type': 'application/json', 'X-CSRF-Token': tok.csrf_token},
            body: JSON.stringify({}),
        });
        const refused = await fetch('/api/notifications/read', {
            method: 'POST', credentials: 'same-origin',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({}),
        });
        return {accepted: accepted.status, refused: refused.status};
    }""")
    assert result['accepted'] == 200, result
    assert result['refused'] == 403, result


# ══════════════════════════════════════════════════════════════════════════
#  Pilot workflows at parity — UI-07 (docs/REACT_UI_MIGRATION.md, Phase 2)
#
#  These drive the break and leave workflows through the real React UI. The
#  refusal cases are the reason they exist: a migrated screen that only ever
#  exercises its happy path passes while being unable to show an employee *why*
#  their Lunch break needs approval or *why* their leave was refused, and both of
#  those are the failure an employee actually experiences.
#
#  Every fixture row carries a marker in `reason`, and the cleanup deletes it —
#  unconditionally, in a `finally`, so a failing run does not leave leave
#  requests behind for the next one to collide with.
# ══════════════════════════════════════════════════════════════════════════

_PROBE_MARKER = 'zz-react-parity-probe'


def _next_monday(offset_weeks=0):
    """A Monday `offset_weeks` weeks out.

    Monday-to-Tuesday is guaranteed two working days, which is what several
    assertions need. A naive `today + N` lands on a weekend often enough to be
# refused by FR-LEA-09's working-day check, which would look like a UI failure.
    """
    today = datetime.now().date()
    ahead = (7 - today.weekday()) % 7 or 7
    return today + timedelta(days=ahead + 7 * offset_weeks)


def _next_saturday():
    today = datetime.now().date()
    return today + timedelta(days=(5 - today.weekday()) % 7 or 7)


def _next_friday(offset_weeks=0):
    """A Friday `offset_weeks` weeks out.

    Friday-to-Monday is the range FR-LEA-09 exists for: it spans four calendar
    days but charges two, so it is the only shape where the two figures the API
    returns actually differ.
    """
    today = datetime.now().date()
    ahead = (5 - today.weekday()) % 7 or 7
    return today + timedelta(days=ahead + 7 * offset_weeks)


def _cleanup_probe_leaves():
    """Remove probe leave requests. Safe to call when none exist."""
    from app import get_db
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM leave_requests WHERE reason LIKE ?", [f'%{_PROBE_MARKER}%'])
        conn.commit()
    finally:
        conn.close()


def _react_dashboard(page, emp_id='EMP001', password='pass123'):
    """Sign in and land on the React dashboard with every panel settled."""
    _login(page, emp_id, password)
    page.goto(BASE_URL + '/app/dashboard', wait_until='commit')
    page.locator('h1.page__title').wait_for(state='visible', timeout=15000)
    return page


@requires_react_build
def test_react_break_start_and_end_through_the_ui(page):
    """Start a break, see it as active, end it. The pilot's first write path."""
    _react_dashboard(page)
    panel = page.locator('.panel', has_text='Breaks')

    panel.locator('.break-btn', has_text='Personal').click()
    panel.locator('.break-active').wait_for(state='visible', timeout=15000)
    assert 'Personal' in panel.locator('.break-active__name').inner_text()

    # The elapsed timer is part of the panel, not decoration.
    assert 'min' in panel.locator('.break-active__elapsed').inner_text()

    panel.locator('.btn--danger').click()
    page.wait_for_function(
        '() => !document.querySelector(".break-active")', timeout=15000)
    # Ending is reported with the recorded duration.
    assert 'Break ended' in panel.locator('.notice').inner_text()


@requires_react_build
def test_react_break_moves_focus_to_end_on_start(page):
    """Starting a break replaces panel content; focus must follow it.

    The button the employee pressed ceases to exist, so without this their next
    Tab lands somewhere unpredictable and a screen reader is left pointing at a
    removed node. The End button is the only sensible successor.
    """
    _react_dashboard(page)
    panel = page.locator('.panel', has_text='Breaks')
    panel.locator('.break-btn', has_text='Personal').click()
    panel.locator('.break-active').wait_for(state='visible', timeout=15000)
    assert page.evaluate('() => document.activeElement.innerText').strip() == 'End break'

    panel.locator('.btn--danger').click()
    page.wait_for_function(
        '() => !document.querySelector(".break-active")', timeout=15000)


@requires_react_build
def test_react_break_panel_is_operable_from_the_keyboard(page):
    """A break can be started without a mouse.

    The legacy panel's buttons were real `<button>`s too, but the migrated panel
    is new markup and could have used a div — so it is asserted, not assumed.
    """
    _react_dashboard(page)
    panel = page.locator('.panel', has_text='Breaks')
    panel.locator('.break-btn', has_text='Personal').focus()
    assert 'Personal' in page.evaluate('() => document.activeElement.innerText')
    page.keyboard.press('Enter')
    panel.locator('.break-active').wait_for(state='visible', timeout=15000)
    panel.locator('.btn--danger').click()
    page.wait_for_function(
        '() => !document.querySelector(".break-active")', timeout=15000)


@requires_react_build
def test_react_leave_apply_reports_working_days_not_calendar_days(page):
    """FR-LEA-09: leave is charged in working days, and the UI must say so.

    The response carries both `days_requested` and `calendar_days`. Showing only
    the working-day figure would look like a bug to anyone used to paying four
    days for Friday-to-Monday, so both are rendered.
    """
    _cleanup_probe_leaves()
    try:
        _react_dashboard(page)
        panel = page.locator('.panel', has_text='Leave').last
        # Friday to Monday: four calendar days, two working days. Any other range
        # has the two figures equal, so the clause this test is checking would
        # legitimately be absent and the assertion would fail on correct output.
        friday = _next_friday(4)

        panel.locator('#leave-start').fill(friday.isoformat())
        panel.locator('#leave-end').fill((friday + timedelta(days=3)).isoformat())
        panel.locator('#leave-reason').fill(f'{_PROBE_MARKER} apply')

        with page.expect_response(
            lambda r: '/api/leaves' in r.url and r.request.method == 'POST', timeout=20000
        ) as resp:
            panel.locator('button[type="submit"]').click()
        assert resp.value.status == 201, resp.value.text()

        payload = resp.value.json()
        assert payload['calendar_days'] > payload['days_requested'], payload

        panel.locator('.notice--applied').wait_for(state='visible', timeout=15000)
        outcome = panel.locator('.notice--applied').inner_text()
        assert 'working day' in outcome, outcome
        # Both figures, because only one of them explains the charge.
        assert 'calendar days' in outcome, outcome
        assert str(payload['days_requested']) in outcome, outcome
        assert str(payload['calendar_days']) in outcome, outcome

        row = panel.locator('.leave-row', has_text=_PROBE_MARKER)
        row.wait_for(state='visible', timeout=15000)
        assert 'PENDING' in row.first.inner_text().upper()

        # Cancel it and confirm the ledger action is surfaced: releasing a
        # Pending reservation and un-consuming an Approved one are different
        # outcomes and the employee is told which happened.
        with page.expect_response(lambda r: '/cancel' in r.url, timeout=20000) as cresp:
            row.first.locator('button', has_text='Cancel').click()
        assert cresp.value.status == 200, cresp.value.text()
        panel.locator('.notice--cancelled').wait_for(state='visible', timeout=15000)
        cancelled = panel.locator('.notice--cancelled').inner_text().lower()
        assert 'cancelled' in cancelled, cancelled
        assert 'balance' in cancelled or 'usage' in cancelled, cancelled
        row.first.locator('.status--cancelled').wait_for(state='visible', timeout=15000)
    finally:
        _cleanup_probe_leaves()


@requires_react_build
def test_react_leave_refusals_are_explained(page):
    """Both server refusals must reach the employee, announced as alerts.

    A weekend-only range is refused with a `hint`, and an overlap is a 409. These
    are the two an employee actually hits, and a screen that renders neither is
    indistinguishable from one that is broken.
    """
    _cleanup_probe_leaves()
    try:
        _react_dashboard(page)
        panel = page.locator('.panel', has_text='Leave').last

        def submit(start, end, reason):
            panel.locator('#leave-start').fill(start)
            panel.locator('#leave-end').fill(end)
            panel.locator('#leave-reason').fill(reason)
            with page.expect_response(
                lambda r: '/api/leaves' in r.url and r.request.method == 'POST', timeout=20000
            ) as resp:
                panel.locator('button[type="submit"]').click()
            return resp.value

        saturday = _next_saturday()
        refused = submit(saturday.isoformat(), saturday.isoformat(),
                         f'{_PROBE_MARKER} weekend')
        assert refused.status == 400, refused.text()
        panel.locator('.state--validation').wait_for(state='visible', timeout=15000)
        message = panel.locator('.state--validation').inner_text()
        assert 'weekly off' in message or 'holiday' in message, message
        # Announced, and interrupting: a refused request is not a polite update.
        assert panel.locator('.state--validation').get_attribute('role') == 'alert'

        monday = _next_monday(6)
        first = submit(monday.isoformat(), (monday + timedelta(days=1)).isoformat(),
                       f'{_PROBE_MARKER} overlap')
        assert first.status == 201, first.text()
        second = submit(monday.isoformat(), (monday + timedelta(days=1)).isoformat(),
                        f'{_PROBE_MARKER} overlap-again')
        assert second.status == 409, second.text()
        panel.locator('.state--validation').wait_for(state='visible', timeout=15000)
        assert 'Overlapping' in panel.locator('.state--validation').inner_text()
    finally:
        _cleanup_probe_leaves()


@requires_react_build
def test_react_leave_list_heading_matches_the_scope_it_receives(page):
    """The list must not claim to be the caller's own when it is not.

    `GET /api/leaves` is scoped by policy: an employee receives their own
    requests, an Admin receives the company's. The heading said "Your requests"
    unconditionally, so an Admin reading their own dashboard saw four
    colleagues' leave filed under a heading that claimed otherwise. The scope is
    observed from the payload rather than guessed from a role the client would
    have to re-derive.
    """
    _react_dashboard(page)
    panel = page.locator('.panel', has_text='Leave').last

    # Wait for the list to populate. The heading is derived from the payload, so
    # reading it mid-load shows the empty-state version and the assertion would
    # pass or fail for the wrong reason.
    panel.locator('.leave-row').first.wait_for(state='visible', timeout=20000)

    # Read who is signed in from the rendered page rather than assuming EMP001,
    # so the test says something about the viewer's own rows.
    actor_id = page.evaluate(
        '() => document.querySelector(".page__subtitle strong").innerText.trim()')

    rows = panel.locator('.leave-row').count()
    heading = panel.locator('.panel__subhead').nth(2).inner_text().strip().lower()

    # The invariant is per-row, not a count comparison: every row that is
    # somebody else's must be attributed. (An earlier version asserted
    # `rows > owners`, which is only true when the caller happens to have at
    # leave of their own — an Admin with none fails it.)
    rows = page.evaluate("""(viewer) => {
        return Array.from(document.querySelectorAll('.leave-row')).map(row => ({
            empId: row.getAttribute('data-emp-id'),
            hasChip: Boolean(row.querySelector('.list__meta--owner')),
        }));
    }""", actor_id)

    assert len(rows) > 0, 'no rows to check'
    # The owner chip is shown for exactly the rows that are somebody else's, and
    # omitted for the viewer's own — which is what makes the "Leave requests"
    # heading honest rather than a claim about every row in the list.
    for row in rows:
        is_foreign = row['empId'] != actor_id
        assert row['hasChip'] == is_foreign, (row, actor_id)

    if any(row['hasChip'] for row in rows):
        assert heading == 'leave requests', heading
    else:
        assert heading == 'your requests', heading


@requires_react_build
def test_react_every_form_control_has_an_associated_label(page):
    """Every leave control is labelled.

    An unlabelled date input reads as "MM/DD/YYYY" with no indication of whether
    it is the start or the end — the two are interchangeable-looking and not
    interchangeable in meaning.
    """
    _react_dashboard(page)
    labels = page.evaluate("""() => {
        const ids = ['leave-type', 'leave-session', 'leave-start', 'leave-end', 'leave-reason'];
        return ids.map(id => {
            const el = document.getElementById(id);
            if (!el) return `${id}: MISSING`;
            const lab = document.querySelector(`label[for="${id}"]`);
            return lab ? null : `${id}: NO LABEL`;
        }).filter(Boolean);
    }""")
    assert labels == [], labels


@requires_react_build
def test_react_notifications_render_the_unread_count(page):
    """Regression: this panel was silently empty while a notification existed.

    `GET /api/notifications` returns `{unread, data}`, not a bare array. It was
    modelled as an array, `Array.isArray(payload)` was false, and the fallback
    rendered "You have no notifications" next to a live unread notification. A
    wrong response shape does not throw here — it renders an empty state, which
    is indistinguishable from a genuine one.
    """
    from app import add_notification, get_db

    conn = get_db()
    try:
        # `add_notification` reports whether the row was actually written, which
        # is false when a notification preference suppresses the category. If it
        # were suppressed the assertion below would fail for a reason that has
        # nothing to do with the panel.
        written = add_notification('EMP001', 'LEAVE_APPLIED',
                                   f'{_PROBE_MARKER} notification probe', '/leaves')
        assert written, 'the probe notification was suppressed, not missing'
    finally:
        conn.close()

    _react_dashboard(page)
    panel = page.locator('.panel', has_text='Notifications')
    panel.locator('.badge-inline').wait_for(state='visible', timeout=15000)
    body = panel.inner_text()
    assert 'unread' in body, body
    assert _PROBE_MARKER in body, (
        'the notification message is missing, so the panel is rendering an empty '
        f'state: {body}'
    )


@requires_react_build
def test_react_modules_dropdown_is_keyboard_operable(page):
    """The Modules menu follows the ARIA disclosure pattern, not just CSS.

    CSS `:hover`/`:focus-within` reveals the menu, but that leaves `aria-expanded`
    describing nothing — a screen reader is told the menu is collapsed while it is
    on screen, and a keyboard user who opened it is trapped with no Escape. All
    three are asserted because all three are the failure mode.
    """
    _react_dashboard(page)
    trigger = page.locator('.navbar__link--group')

    assert trigger.get_attribute('aria-expanded') == 'false'
    assert not page.locator('.navbar__dropdown').is_visible(), 'menu starts closed'

    # ArrowDown opens and moves focus into the first item.
    trigger.focus()
    page.keyboard.press('ArrowDown')
    page.locator('.navbar__dropdown').wait_for(state='visible', timeout=10000)
    assert trigger.get_attribute('aria-expanded') == 'true'
    assert 'Assets' in page.evaluate('() => document.activeElement.innerText')

    # ArrowDown advances.
    page.keyboard.press('ArrowDown')
    assert 'Jobs' in page.evaluate('() => document.activeElement.innerText')

    # Escape closes and returns focus to the trigger, so the keyboard is not
    # left inside a hidden element.
    page.keyboard.press('Escape')
    assert trigger.evaluate('el => el === document.activeElement'), (
        'focus was not returned to the trigger after Escape'
    )
    assert trigger.get_attribute('aria-expanded') == 'false'
    assert not page.locator('.navbar__dropdown').is_visible()


@requires_react_build
def test_react_dropdown_navigation_matches_the_server_tree(page):
    """Every Modules link is reachable, and each maps to the route it names.

    A group rendered with fewer children than the server sent means something
    filtered them client-side — which would be a client-side authorization
    decision, the one thing this migration is not allowed to make.
    """
    _react_dashboard(page)
    page.locator('.navbar__link--group').click()
    page.locator('.navbar__dropdown').wait_for(state='visible', timeout=10000)

    hrefs = page.eval_on_selector_all(
        '.navbar__dropdown .dropdown-item',
        'els => els.map(e => e.getAttribute("href"))')
    served = page.evaluate("""async () => {
        const r = await fetch('/api/navigation', {credentials: 'same-origin'});
        const d = await r.json();
        const group = d.navigation.find(e => e.group);
        if (!group) return [];
        return group.children.filter(c => !c.divider).map(c => c.href);
    }""")

    assert served, 'no Modules group was served to this actor'
    assert hrefs == ['/app' + h for h in served], (hrefs, served)


@requires_react_build
def test_react_dropdown_click_is_not_swallowed_by_hover(page):
    """Clicking the trigger must open the menu, not close a hover-opened one.

    A click is always preceded by a mouseenter, so a naive `toggle` opens the
    menu on the way to the button and shuts it on the button itself — the
    trigger looks broken to every mouse user. Found by this very suite: the test
    that clicks the dropdown passed alone and failed in a full run, because a
    preceding test had left the pointer over the navbar.

    Three assertions, because the state has three legitimate outcomes: opened by
    hover and pinned by click, closed by a second click, and re-opened by hover
    after the pointer leaves and returns.
    """
    _react_dashboard(page)
    trigger = page.locator('.navbar__link--group')
    menu = page.locator('.navbar__dropdown')
    away = page.locator('.page__title')

    # `click` moves the pointer onto the trigger, firing mouseenter first.
    trigger.click()
    menu.wait_for(state='visible', timeout=10000)
    assert trigger.get_attribute('aria-expanded') == 'true', (
        'the click closed a menu that hover had just opened'
    )

    # A second click closes it: the first click pinned it, the second toggles.
    trigger.click()
    page.wait_for_function(
        '() => !document.querySelector(".navbar__dropdown").classList'
        '.contains("is-open")', timeout=5000)
    assert not menu.is_visible()

    # And hover alone opens it for a mouse user who never clicks. The pointer
    # must leave the trigger first — `hover()` is a no-op while it is already
    # over the element, so no mouseenter can fire and asserting without the move
    # away would fail on correct behaviour.
    away.hover()
    trigger.hover()
    menu.wait_for(state='visible', timeout=10000)
    assert trigger.get_attribute('aria-expanded') == 'true'

    # Leaving closes a hover-opened menu.
    away.hover()
    page.wait_for_function(
        '() => !document.querySelector(".navbar__dropdown").classList'
        '.contains("is-open")', timeout=5000)
    assert not menu.is_visible()


# ══════════════════════════════════════════════════════════════════════════
#  UI-08 — self-service screens (docs/REACT_UI_MIGRATION.md, Phase 3)
#
#  /profile, /tickets, /regularization, /goals, /leaves.
#
#  These run as EMP002, a plain Employee, on purpose. An Admin sees a different
#  set of nav links and a company-wide version of three of these lists, so a test
#  that only ever signed in as an administrator would never prove that a plain
#  employee can use the self-service UI at all — which is the majority case.
# ══════════════════════════════════════════════════════════════════════════

_UI08_MARK = 'zz-ui08'

# Unique per run. Playwright's `fill()` on a field that already holds the value
# fires no React change event, so a fixed value stops marking the form dirty on
# the second run and a "Save is enabled" assertion fails for a reason that has
# nothing to do with the product.
_UI08_UNIQUE = str(int(datetime.now().timestamp()) % 1000000)
# Day offset for the "free date" fixture, distinct per test so two runs in the
# same session cannot collide on a Pending regularization row.
_UI08_OFFSET = int(datetime.now().timestamp()) % 20


def _cleanup_ui08():
    """Remove every UI-08 fixture row. Safe when there are none."""
    from app import get_db
    conn = get_db()
    try:
        conn.execute('DELETE FROM tickets WHERE subject LIKE ?', [f'%{_UI08_MARK}%'])
        conn.execute(
            'DELETE FROM regularization_requests WHERE reason LIKE ?', [f'%{_UI08_MARK}%'])
        conn.execute('DELETE FROM goals WHERE title LIKE ?', [f'%{_UI08_MARK}%'])
        conn.commit()
    finally:
        conn.close()


def _employee_dashboard(page):
    _login(page, 'EMP002', 'pass123')
    page.goto(BASE_URL + '/app/dashboard', wait_until='commit')
    page.locator('h1.page__title').wait_for(state='visible', timeout=15000)
    return page


@requires_react_build
def test_react_self_service_nav_is_rendered_for_a_plain_employee(page):
    """The navbar an employee receives is the one they can actually use.

    `Tickets` and `Goals` are Modules children gated on their own SRS module, so
    they are absent for an Employee — correctly. A test asserting only that the
    link exists would be asserting the admin's navbar, and would pass while every
    employee saw a broken set of links.
    """
    _employee_dashboard(page)
    labels = page.eval_on_selector_all(
        '.navbar__links > li > a', 'els => els.map(e => e.innerText.trim())')
    for expected in ['Dashboard', 'Leaves', 'Regularization', 'Profile']:
        assert expected in labels, (expected, labels)
    assert 'Tickets' not in labels, (
        'an Employee was offered the Tickets link, which is gated on the '
        f'`tickets` module: {labels}'
    )


@requires_react_build
def test_react_self_service_pages_reachable_by_client_side_navigation(page):
    """Each screen resolves through a real navbar click, not a raw goto.

    A raw `goto` proves the route matches; it does not prove the nav hrefs point
    at it. Clicking does both, and it is the path a person actually takes.
    """
    _employee_dashboard(page)
    for label, want in [('Profile', 'Profile'), ('Regularization', 'Regularization'),
                        ('Leaves', 'Leave')]:
        page.locator(f'.navbar__links a:has-text("{label}")').first.click()
        page.locator('h1.page__title').wait_for(state='visible', timeout=15000)
        assert want in page.inner_text('h1.page__title'), (label, page.url)
        # Still inside the SPA, i.e. no full page load happened.
        assert page.url.startswith(BASE_URL + '/app/'), page.url


@requires_react_build
def test_react_ticket_can_be_raised(page):
    _cleanup_ui08()
    try:
        _employee_dashboard(page)
        page.goto(BASE_URL + '/app/tickets', wait_until='commit')
        page.locator('h1.page__title').wait_for(state='visible', timeout=15000)

        subject = f'{_UI08_MARK} ticket {_UI08_UNIQUE}'
        page.locator('#t-subject').fill(subject)
        page.locator('#t-description').fill('Created by the UI-08 migration tests')
        page.locator('#t-priority').select_option('High')

        with page.expect_response(
            lambda r: '/api/tickets' in r.url and r.request.method == 'POST', timeout=20000
        ) as resp:
            page.locator('button[type="submit"]').click()
        assert resp.value.status == 201, resp.value.text()

        page.locator('.notice--ok').wait_for(state='visible', timeout=15000)
        row = page.locator('.list__item', has_text=subject)
        row.first.wait_for(state='visible', timeout=15000)
        assert 'OPEN' in row.first.inner_text().upper()
        # The form clears after a successful create, so a second submit cannot
        # silently file the same ticket twice.
        assert page.input_value('#t-subject') == ''
    finally:
        _cleanup_ui08()


@requires_react_build
def test_react_regularization_requires_a_reason_and_reports_refusals(page):
    """Client guard for the reason, and the server's own 409 rendered verbatim.

    The server refuses a second Pending request for the same date (409) — seeded
    data makes that the *default* outcome for today, so it is not an edge case
    and must not vanish into a failed request.
    """
    _cleanup_ui08()
    try:
        _employee_dashboard(page)
        page.goto(BASE_URL + '/app/regularization', wait_until='commit')
        page.locator('h1.page__title').wait_for(state='visible', timeout=15000)

        # Client-side: no reason.
        page.locator('button[type="submit"]').click()
        page.locator('.state--validation').wait_for(state='visible', timeout=15000)
        assert 'reason' in page.inner_text('.state--validation').lower()

        # Server-side: today's date already has a Pending request.
        page.locator('#r-reason').fill(f'{_UI08_MARK} duplicate')
        with page.expect_response(
            lambda r: '/api/regularization' in r.url and r.request.method == 'POST',
            timeout=20000,
        ) as dup:
            page.locator('button[type="submit"]').click()
        if dup.value.status == 409:
            page.locator('.state--validation').wait_for(state='visible', timeout=15000)
            assert 'already exists' in page.inner_text('.state--validation')
            assert page.locator('.state--validation').get_attribute('role') == 'alert'

        # A free date succeeds.
        free = (datetime.now().date() + timedelta(days=40 + _UI08_OFFSET)).isoformat()
        page.locator('#r-date').fill(free)
        page.locator('#r-reason').fill(f'{_UI08_MARK} forgot to punch in')
        with page.expect_response(
            lambda r: '/api/regularization' in r.url and r.request.method == 'POST',
            timeout=20000,
        ) as ok:
            page.locator('button[type="submit"]').click()
        assert ok.value.status == 201, ok.value.text()
        page.locator('.list__item', has_text=_UI08_MARK).first.wait_for(
            state='visible', timeout=15000)
    finally:
        _cleanup_ui08()


@requires_react_build
def test_react_goal_creation_has_no_employee_picker_and_no_self_rating(page):
    """Two controls are deliberately absent, and both absences are the point.

    * The legacy page fetched `/api/users?per_page=200` to populate an employee
      picker. That endpoint is `admin_required`, so an Employee got a **403** and
      a form that silently could not be submitted. `POST /api/goals` takes
      `emp_id` from the session (CC-10), so there is nothing to pick.
    * `goals.check_rating` refuses the goal's **owner** (FR-PERF-01). A "Rate"
      button on your own goal would offer an action the server permanently
      rejects.
    """
    _cleanup_ui08()
    try:
        _employee_dashboard(page)
        page.goto(BASE_URL + '/app/goals', wait_until='commit')
        page.locator('h1.page__title').wait_for(state='visible', timeout=15000)

        # No dropdown of employees anywhere on the page.
        assert page.locator('select').count() == 0, (
            'a select appeared on the goals form; /api/users is admin-only, so an '
            'employee picker cannot work'
        )
        assert page.locator('button:has-text("Rate")').count() == 0

        title = f'{_UI08_MARK} goal {_UI08_UNIQUE}'
        page.locator('#g-title').fill(title)
        page.locator('#g-weight').fill('7')
        with page.expect_response(
            lambda r: '/api/goals' in r.url and r.request.method == 'POST', timeout=20000
        ) as resp:
            page.locator('button[type="submit"]').click()
        assert resp.value.status == 201, resp.value.text()

        row = page.locator('.list__item', has_text=title)
        row.first.wait_for(state='visible', timeout=15000)
        assert 'weight 7' in row.first.inner_text()
        # An unrated goal says so, rather than showing a blank.
        assert 'not yet rated' in row.first.inner_text()
    finally:
        _cleanup_ui08()


@requires_react_build
def test_react_profile_edit_and_password_guards(page):
    """Profile save is gated on an actual change, and the password form guards."""
    _employee_dashboard(page)
    page.goto(BASE_URL + '/app/profile', wait_until='commit')
    page.locator('h1.page__title').wait_for(state='visible', timeout=15000)

    save = page.locator('button[type="submit"]:has-text("Save changes")')
    # Pristine: nothing to save, so the button is disabled. Offering a save that
    # silently writes nothing is how "my change did not stick" happens.
    assert save.is_disabled()

    # Unique value — see the note on _UI08_UNIQUE.
    phone = f'9{_UI08_UNIQUE}'
    original = page.input_value('#p-phone')
    page.locator('#p-phone').fill(phone)
    assert not save.is_disabled()
    with page.expect_response(
        lambda r: r.url.endswith('/api/profile') and r.request.method == 'PUT', timeout=20000
    ) as resp:
        save.click()
    assert resp.value.status == 200, resp.value.text()
    page.locator('.notice--ok').wait_for(state='visible', timeout=15000)

    # Mismatch is caught before a request is made.
    page.locator('#pw-current').fill('pass123')
    page.locator('#pw-new').fill('abcdefghijk')
    page.locator('#pw-confirm').fill('something-else')
    page.locator('button[type="submit"]:has-text("Change password")').click()
    page.wait_for_selector('.field__error', timeout=10000)
    assert 'do not match' in page.inner_text('.field__error')

    # Restore, so this test does not leave the shared fixture employee changed.
    page.locator('#p-phone').fill(original)
    with page.expect_response(
        lambda r: r.url.endswith('/api/profile') and r.request.method == 'PUT', timeout=20000
    ):
        save.click()


@requires_react_build
def test_react_unmigrated_route_says_so_and_links_to_the_legacy_page(page):
    """A route with no React screen must not silently redirect to the dashboard.

    Redirecting makes a nav link look present and do nothing, which is how a
    half-migrated app becomes indistinguishable from a broken one. The page
    should name the gap and offer the legacy URL, which still works.
    """
    _employee_dashboard(page)
    page.goto(BASE_URL + '/app/documents', wait_until='commit')
    page.locator('h1.page__title').wait_for(state='visible', timeout=15000)
    assert 'Not yet migrated' in page.inner_text('h1.page__title')

    link = page.locator('a:has-text("Open the current page")')
    link.wait_for(state='visible', timeout=10000)
    # A full navigation, not a router push: there is no React bundle at /documents.
    assert link.get_attribute('href') == '/documents', link.get_attribute('href')
