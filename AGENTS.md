# HRMS Project Guide

## Testing

### Running all tests
```bash
python -m pytest tests/test_app.py -v && python -m pytest tests/test_playwright.py -v
```
**Note:** Run test suites separately — they share an `app` module and separate DB env vars, causing 403 login failures if combined.

### Running specific test files
```bash
python -m pytest tests/test_app.py -v   # Unit tests (fast, 26 tests)
python -m pytest tests/test_playwright.py -v  # Browser tests (~2 min, 14 tests)
```

### Test infrastructure
- `tests/test_app.py` — Flask unit tests (26 tests)
- `tests/test_playwright.py` — Playwright browser tests (14 tests)
- Playwright tests spin up a dev server in a thread per session, each test gets a fresh browser context

### Test patterns
- Each browser test logs in fresh, waits 3-5s for session to stabilize
- Use `wait_until='commit'` for `goto` when page redirects are expected
- Use `page.evaluate()` for direct API calls when page JS doesn't load properly (e.g., leaves page JS with CDN dependency issues)
- Use `with page.expect_response(...)` as context manager for tracking API calls

## Key Fixes Made

### Break System
- `admin_required` decorator: non-admin page requests now redirect to dashboard (not JSON 403)
- `checkActiveBreak()`: always shows "No active break" when no active break exists (removed stale button guard)
- `endBreak()`: fetches active break from server if `localStorage.activeBreakId` is missing (self-heal)
- `POST /api/start-break`: auto-ends any existing active break instead of returning 409; enforces `daily_limit_minutes` per break type; requires manager approval for Lunch
- `GET /api/user-breaks`: uses UNION to always return active breaks from any date plus shift-based history

### Break Types
- Seed: Tea (15 min), Lunch (60 min, requires approval), Personal (30 min)
- `POST /api/start-break` rejects start if daily limit exhausted or Lunch without approval

### Break Approvals
- `break_approvals` table for Lunch break approval workflow
- `GET/POST /api/break-approvals` — employees request, admin approves/rejects
- `POST /api/break-approvals/<id>/approve` and `/reject` — admin endpoints

### Shift Timing
- `shift_start`/`shift_end` columns in user CRUD (create/edit forms, table display)
- Fixed Time / 24×7 type selector

### Navbar
- Role-gated: Users/Org Chart/Holidays = admin only; Modules = admin or HR; Salary/Import = admin only

### Login Hours
- Changed from sum of sessions to `last logout - first login` calculation

### Admin Dashboard
- Fixed JSON key mismatches (`online_count` → `online`)
- Added `/api/admin/breaks` and `/api/admin/dispose-break/<id>` endpoints

## Database
- DuckDB file in temp dir for tests (env var `DB_FILE`)
- Seed data includes 10 users, break types (Tea, Coffee, Lunch), sample sessions/breaks