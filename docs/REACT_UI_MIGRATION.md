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

- [ ] Inventory all Jinja pages, route URLs, shared navigation/layouts, forms,
  tables, modals, and page-specific scripts.
- [ ] Map each page to its API calls, role/module permissions, and existing
  browser tests.
- [ ] Record current production build/deployment constraints and supported
  browser targets.
- [ ] Capture baseline screenshots and key workflows for the pilot page.
- [ ] Agree on shared design tokens and accessibility expectations before
  building replacement components.

**Exit:** every page has an owner, an API/permission map, and a migration
priority; the pilot's current behavior is covered by repeatable checks.

### Phase 1 — Frontend foundation

- [ ] Add a frontend package manifest, lockfile, TypeScript configuration, and
  Vite build using repository-standard scripts.
- [ ] Add React entry point, typed route definitions, and a shared application
  shell mounted under `/app/`.
- [ ] Configure Flask to serve the built assets and client-side routes without
  shadowing existing API or legacy page routes.
- [ ] Implement typed same-origin API helpers, including CSRF token acquisition
  and unsafe-request headers compatible with the existing global CSRF guard.
- [ ] Add shared loading, empty, validation, permission-denied, and recoverable
  error states.
- [ ] Add a repeatable local development workflow and production build step to
  the existing container/CI pipeline.
- [ ] Verify static assets are fingerprinted/cached safely and no CDN is
  required for core application behavior.

**Exit:** a minimal React route loads in local and production-style builds,
authenticated API reads work, CSRF-protected writes work, and legacy pages and
API routes remain unchanged.

### Phase 2 — Shared UX and pilot workflow

- [ ] Build the role-aware navigation, page layout, responsive breakpoints,
  accessible controls, and reusable form/table/modal components.
- [ ] Use the existing server-side policy as the source for navigation
  visibility; do not treat hidden React controls as authorization.
- [ ] Migrate one pilot workflow end to end, including loading, error, empty,
  success, validation, and unauthorized states.
- [ ] Recommended pilot: the employee dashboard, followed by the break panel
  and leave balance/request workflow; these exercise a broad set of existing
  APIs while keeping the backend contracts intact.
- [ ] Add browser coverage for representative keyboard, responsive, and
  authenticated flows.

**Exit:** pilot feature parity is demonstrated by API/browser tests and manual
review on desktop and mobile; accessibility issues that block core workflows
are resolved.

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

- [ ] **UI-01 — Inventory the current interface** (Phase 0)
- [ ] **UI-02 — Establish UX, accessibility, and browser baselines** (Phase 0)
- [ ] **UI-03 — Scaffold React, TypeScript, and Vite** (Phase 1)
- [ ] **UI-04 — Integrate `/app/` routing and static asset serving** (Phase 1)
- [ ] **UI-05 — Implement typed API, session, and CSRF integration** (Phase 1)
- [ ] **UI-06 — Add shared shell, navigation, and component foundation** (Phase 2)
- [ ] **UI-07 — Migrate and validate the employee dashboard pilot** (Phase 2)
- [ ] **UI-08 — Migrate employee self-service modules** (Phase 3)
- [ ] **UI-09 — Migrate HR/admin and approval workflows** (Phase 3)
- [ ] **UI-10 — Migrate Finance, reporting, and analytics** (Phase 3)
- [ ] **UI-11 — Migrate recruiting and lifecycle workflows** (Phase 3)
- [ ] **UI-12 — Migrate login, reset, and remaining account flows** (Phase 3)
- [ ] **UI-13 — Cut over routes and retire superseded templates/assets** (Phase 4)
- [ ] **UI-14 — Update CI, deployment, and operator documentation** (Phase 4)

**Not included:** enabling MFA, changing SRS requirements, replacing Flask,
redesigning the database, or rewriting backend APIs as part of the UI migration.
