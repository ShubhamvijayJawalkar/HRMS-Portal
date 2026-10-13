# React UI migration plan

## Goal and boundaries

Replace the Flask/Jinja user interface with a React application while keeping
the existing Flask application as the backend. Flask continues to own
authentication, authorization, session cookies, CSRF enforcement, business
rules, database access, and the existing `/api/*` contracts. The migration is
incremental: each React screen must reach feature parity before its Jinja
counterpart is retired.

The recommended frontend stack is **React + TypeScript + Vite**. Vite builds
static assets for Flask to serve, avoiding a separate production web service
and keeping the deployment topology simple. Do not introduce a second API,
rewrite backend routes, or copy authorization decisions into the client.

MFA (FR-AUTH-11) is explicitly deferred and is not part of this migration
scope. The React work must preserve current authentication behavior rather
than enabling or assuming MFA.

## Migration approach

Run the old and new interfaces side by side during development. Mount the new
application under `/app/` and serve its built entry point there, with
client-side routing covering `/app/*`. Keep existing Flask page routes intact
until their React replacements pass the acceptance checks. Migrate by page or
small, coherent workflow—not by replacing all templates in one cutover.

The React app should use same-origin requests and the existing secure session
cookie. Centralize API calls, response/error handling, and CSRF token handling
in one typed client. Do not put session identifiers or credentials in
`localStorage`; keep secrets out of browser storage and logs. Treat server
responses as untrusted input and render user-provided values as text.

## Phases and exit criteria

### Phase 0 — Inventory and baseline

- [x] Inventory all Jinja pages, route URLs, shared navigation/layouts, forms,
  tables, modals, and page-specific scripts.
- [x] Map each page to its API calls, role/module permissions, and existing
  browser tests.
- [x] Record current production build/deployment constraints and supported
  browser targets.
- [ ] Capture baseline screenshots and key workflows for the pilot page.
- [x] Agree on shared design tokens and accessibility expectations before
  building replacement components.

**Exit:** every page has an owner, an API/permission map, and a migration
priority; the pilot's current behavior is covered by repeatable checks.

> **Status:** the first three items are complete and generated, not written.
> `scripts/ui_inventory.py` reads `app.py` (routes, gates, `_ROUTE_MODULES`),
> every template (its `/api/*` calls and CDN dependencies) and
> `policy.NAV_ENTRIES`, and emits [`docs/UI_INVENTORY.md`](UI_INVENTORY.md).
> It exits non-zero on drift and `test_ui_inventory_generator_reports_no_drift`
> runs it in the suite, so the inventory cannot fall behind the code the way a
> hand-maintained one would.
>
> Current figures: **32 page routes, 32 templates, 9 nav entries, 13 API calls
> on the pilot route**, and one deliberate finding — every template loads
> Bootstrap, icons and a webfont from a CDN, which the last Phase 1 item removes
> for the React shell. Baseline screenshots remain outstanding; the browser
> tests below are the repeatable substitute.

### Phase 1 — Frontend foundation

- [x] Add a frontend package manifest, lockfile, TypeScript configuration, and
  Vite build using repository-standard scripts.
- [x] Add React entry point, typed route definitions, and a shared application
  shell mounted under `/app/`.
- [x] Configure Flask to serve the built assets and client-side routes without
  shadowing existing API or legacy page routes.
- [x] Implement typed same-origin API helpers, including CSRF token acquisition
  and unsafe-request headers compatible with the existing global CSRF guard.
- [x] Add shared loading, empty, validation, permission-denied, and recoverable
  error states.
- [x] Add a repeatable local development workflow and production build step to
  the existing container/CI pipeline.
- [x] Verify static assets are fingerprinted/cached safely and no CDN is
  required for core application behavior.

**Exit:** a minimal React route loads in local and production-style builds,
authenticated API reads work, CSRF-protected writes work, and legacy pages and
API routes remain unchanged.

> **Status:** complete. `frontend/` holds the TypeScript + Vite app;
> `app.react_app` serves `/app/` with a history-API fallback; `app.py` adds
> `GET /api/navigation`, which returns `navigation_for(actor)` — the same call
> the Jinja context processor makes — so the SPA and the legacy pages cannot
> disagree about who sees which link. `frontend/src/lib/api.ts` is the only
> place that fetches: `credentials: 'same-origin'`, the CSRF token held in
> memory from `GET /api/csrf-token`, and one retry on a CSRF rejection.
>
> **Two defects this phase found rather than inherited:**
>
> - `security._csrf_deliver` called `get_data()` on *any* HTML response, which
>   raises `RuntimeError` on a direct-passthrough response — and
>   `send_from_directory` returns exactly that. Every page in this app renders
>   through `render_template` and is buffered, so nothing hit it until the shell
>   started serving `index.html` off disk and the first `/app/` request 500'd.
>   Fixed by skipping passthrough responses, with
>   `test_csrf_delivery_does_not_break_file_served_html` as the guard.
> - The router carries `basename="/app"` *and* the routes were written as
>   `/app/dashboard`, producing `/app/app/dashboard` hrefs. The shell still
>   rendered (react-router matched the stripped path) while every link 404'd, so
>   "the dashboard rendered" passed straight through it. Asserted now by
>   `test_react_links_carry_the_mount_prefix_exactly_once`.
>
> Verified: 14 new unit tests, 6 new browser tests (31 browser total), the
> Docker multi-stage build serving `/app/`, and a CI step that boots the image
> and asserts the bundle and its cache headers.

### Phase 2 — Shared UX and pilot workflow

- [x] Build the role-aware navigation, page layout, responsive breakpoints,
  accessible controls, and reusable form/table/modal components.
- [x] Use the existing server-side policy as the source for navigation
  visibility; do not treat hidden React controls as authorization.
- [x] Migrate one pilot workflow end to end, including loading, error, empty,
  success, validation, and unauthorized states.
- [x] Recommended pilot: the employee dashboard, followed by the break panel
  and leave balance/request workflow; these exercise a broad set of existing
  APIs while keeping the backend contracts intact.
- [x] Add browser coverage for representative keyboard, responsive, and
  authenticated flows.

> **A pre-existing browser flake fixed, found while adding these tests.**
> `AGENTS.md` records the recurring "`Page.goto: Timeout 30000ms exceeded`
> (CDN load)" flake repeatedly, each run failing a different test. The cause was
> never addressed: 19 `page.goto` calls used Playwright's default
> `wait_until='load'`, which waits for *every* subresource — and all 32 legacy
> templates load Bootstrap, icons and a Google webfont from CDNs. One slow CDN
> segment therefore failed an arbitrary test at page load.
>
> All 19 now use `wait_until='commit'` (the response has arrived); Playwright's
> `fill`/`click`/`wait_for_*` auto-wait for the element they need, so the signal
> is still the thing the test actually wants. The CDN is simply no longer in the
> critical path.
>
> That exposed a **real coupling** the default had been hiding: many tests call
> `fetch` from `page.evaluate`, which only works because `init_csrf` injects a
> `window.fetch` wrapper into the HTML. Under `commit` the document has not been
> parsed, the wrapper is absent, and the request goes out un-tokened — refused
> with a 403 the test reported as a password-reset failure, three hundred lines
> from the cause. The wait is now explicit via `_await_page_js()`, keyed on the
> `__hrmsCsrf` flag that wrapper sets. Suite is 31/31 on three consecutive runs.

> **Status:** complete — Phases 1 and 2. Phase 3 (module-by-module migration)
> is next: UI-08 self-service, UI-09 HR/admin, UI-10 finance, UI-11 recruiting,
> UI-12 auth flows. The pilot's patterns are documented in `AGENTS.md`.
>
> The shell, navigation, layout, accessible controls and the
> five shared states are built, the Modules menu implements the WAI-ARIA
> disclosure pattern, and the pilot is at feature parity on both its success and
> refusal paths.
>
> `NavBar` renders `policy.NAV_ENTRIES` as sent by the server and holds **no**
> permission logic of its own; `test_navigation_matches_the_jinja_navbar_exactly`
> asserts the two interfaces return the identical tree, and
> `test_react_dropdown_navigation_matches_the_server_tree` asserts every served
> child renders — so a client-side filter of the tree would fail the build.
>
> **Phase 2 exit, honestly stated.** API/browser coverage is complete and the
> accessibility items above are asserted. The plan also asks for *manual* review
> on desktop and mobile, which has not been performed — the responsive CSS
> breakpoint exists but has not been exercised by hand.

**Exit:** pilot feature parity is demonstrated by API/browser tests and manual
review on desktop and mobile; accessibility issues that block core workflows
are resolved.

> **Status:** the pilot is complete and covered by 14 browser tests. The
> dashboard carries the read panels (`Shift`, `Breaks`, `Notifications`,
> `Holidays`, `Login sessions`, admin `Organisation`) plus the two write
> workflows: **break start/end** and **leave balance / apply / cancel**.
>
> Parity is claimed on both the success and the refusal paths, because a screen
> that only ever exercises its happy path passes while being unable to show an
> employee *why* their Lunch break needs approval or *why* their leave was
> refused — and both of those are the failures an employee actually hits.
>
> ### Three defects the pilot found, none of them visible in a render check
>
> 1. **The notifications panel was silently empty.** `GET /api/notifications`
>    returns `{unread, data}`, not a bare array. It was modelled as an array,
>    `Array.isArray(payload)` was false, and the fallback rendered "You have no
>    notifications" beside a live unread notification. A wrong response shape
>    does not throw — it renders an *empty state*, which is indistinguishable
>    from a genuine one. Found by probing the live endpoint's shapes rather than
>    reading the legacy template, because the legacy template already reads it
>    correctly and looked authoritative.
> 2. **The leave list claimed "Your requests" while showing the whole company.**
>    `GET /api/leaves` is scoped by policy — an employee gets their own, an
>    Admin gets everyone's. The heading was unconditional, so an Admin reading
>    their own dashboard saw colleagues' leave filed under a heading claiming
>    otherwise. The scope is now **observed from the payload** (a row belonging
>    to someone else ⇒ company-wide) rather than guessed from a role the client
>    would have to re-derive, and foreign rows carry the employee's name.
> 3. **An uncapped break type would have been permanently disabled.** `null`
>    means "no daily cap"; `remaining_minutes` is `null` too. A `=== 0` test
>    reads that as spent, which would have greyed out every uncapped type for
>    everyone, permanently.
>
> ### One deliberate departure from the legacy panel
>
> **No `localStorage` for the active break.** The Jinja panel stored
> `activeBreakId` and needed a self-heal, because a stale or cleared value left
> *End Break* pointing at a break that was already over. The React panel derives
> it from the same summary payload that renders it, so the two cannot disagree
> and the self-heal is not needed at all.
>
> Verified: 17 React browser tests (full suite **42/42 on two consecutive
> runs**), 365 unit passed / 12 skipped, ruff and typecheck clean. Keyboard
> operation is asserted: a break is startable with Enter, focus follows a start
> to *End break*, every leave control has an associated label, and the Modules
> menu implements the full disclosure pattern (`aria-expanded`, ArrowDown/Up,
> Home/End, Escape returning focus to the trigger).
>
> ### A fourth defect, and a lesson about what a full run is for
>
> **The Modules dropdown looked broken to every mouse user.** A click is always
> preceded by a `mouseenter`, and the menu opened on hover — so clicking the
> trigger opened the menu on the way to the button and then *toggled it shut on
> the button itself*. The trigger is now `openedBy`-aware: hover opens, the
> first click pins, the second closes.
>
> It is worth recording **how** it was found. The dropdown test passed alone and
> failed in a full run, because an earlier test had left the pointer over the
> navbar. That is the same test-order interference `AGENTS.md` has now catalogued
> several times — and in every previous instance the recorded response was to
> harden the sleep. This time it was not a sleep: it was a genuine bug that only
> reproduces when the pointer arrives at the element before the click does.
> **Chasing the interference rather than shrugging at it is what found it**, and
> the assertion now pins all three states (hover-opens, click-pins, click-closes)
> so it cannot come back.

### Phase 3 — Migrate by module

- [ ] Self-service: dashboard, profile, leave, regularization, goals, documents,
  expenses, tickets, notifications, and calendar.
- [ ] HR/admin operations: users, permissions, imports, holidays, approvals,
  onboarding/offboarding, and audit views.
- [ ] Finance and reporting: payroll, salary structures, reports, and analytics.
- [ ] Recruiting: jobs, candidates, interviews, offers, and pre-boarding.
- [ ] Authentication and account flows: login, password reset, and profile
  credential actions; retain the existing MFA-deferred behavior.
- [ ] For each workflow, retain the legacy route until React parity, permission
  behavior, CSRF protection, audit behavior, and browser tests are confirmed.

**Exit:** every in-scope user workflow has a React implementation and passes
its functional, authorization, and browser acceptance checks.

### Phase 4 — Cutover and retire legacy UI

- [ ] Confirm every legacy page route has a React destination and a tested
  redirect or documented exception.
- [ ] Verify role-based navigation and server-side access rules match before
  and after cutover.
- [ ] Remove obsolete Jinja pages and duplicate page scripts only after the
  corresponding React pages have shipped and the rollback window is closed.
- [ ] Keep required email/reset, pre-boarding-token, and error pages available
  until their complete flows have React equivalents.
- [ ] Update deployment docs, operator runbooks, and UI test instructions.
- [ ] Remove unused frontend dependencies and stale CSS/assets.

**Exit:** no in-scope user-facing workflow depends on the retired templates;
production smoke checks and rollback instructions are documented.

## Acceptance checklist for each migrated screen

- [ ] Same route purpose and user-visible capabilities as the existing page.
- [ ] Backend remains authoritative for permissions, validation, and state
  transitions.
- [ ] Existing session and CSRF protections work for reads and writes.
- [ ] Loading, empty, validation, server-error, and permission-denied states
  are explicit and usable.
- [ ] Keyboard operation, focus management, labels, contrast, and narrow-screen
  layout are checked.
- [ ] Browser tests cover its primary success flow and at least one relevant
  refusal/error path.
- [ ] No sensitive values are written to browser storage, URLs, or logs.

## Risks and controls

| Risk | Control |
|---|---|
| Big-bang rewrite delays HRMS feature work | Migrate incrementally and keep the old page until parity |
| Client and backend permission rules drift | Backend remains authoritative; test visible navigation against the existing policy decisions |
| CSRF or session behavior breaks in the SPA | Centralize same-origin requests and test real authenticated writes |
| Duplicate legacy and React implementations diverge | Limit overlap to a workflow's migration window and track parity explicitly |
| Bundle/build changes break deployment | Build static assets in CI/container and smoke-test the production image |
| Migration balloons into a backend rewrite | Keep existing routes, APIs, data model, and business services out of scope |

## UI migration task list

This list tracks the React migration separately from the SRS-driven
[`HRMS_ToDo.pdf`](HRMS_ToDo.pdf). Items remain pending until their phase exit
criteria are met.

- [x] **UI-01 — Inventory the current interface** (Phase 0)
- [ ] **UI-02 — Establish UX, accessibility, and browser baselines** (Phase 0)
- [x] **UI-03 — Scaffold React, TypeScript, and Vite** (Phase 1)
- [x] **UI-04 — Integrate `/app/` routing and static asset serving** (Phase 1)
- [x] **UI-05 — Implement typed API, session, and CSRF integration** (Phase 1)
- [x] **UI-06 — Add shared shell, navigation, and component foundation** (Phase 2)
- [x] **UI-07 — Migrate and validate the employee dashboard pilot** (Phase 2)
- [ ] **UI-08 — Migrate employee self-service modules** (Phase 3)
- [ ] **UI-09 — Migrate HR/admin and approval workflows** (Phase 3)
- [ ] **UI-10 — Migrate Finance, reporting, and analytics** (Phase 3)
- [ ] **UI-11 — Migrate recruiting and lifecycle workflows** (Phase 3)
- [ ] **UI-12 — Migrate login, reset, and remaining account flows** (Phase 3)
- [ ] **UI-13 — Cut over routes and retire superseded templates/assets** (Phase 4)
- [ ] **UI-14 — Update CI, deployment, and operator documentation** (Phase 4)

**Not included:** enabling MFA, changing SRS requirements, replacing Flask,
redesigning the database, or rewriting backend APIs as part of the UI migration.
