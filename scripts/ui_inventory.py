#!/usr/bin/env python
"""Phase 0 — React migration inventory, generated from the code.

The migration plan's Phase 0 exit criterion is that *every* page has an API and
permission map and a migration priority. Written by hand that inventory is stale
the moment a route or an API call changes, and a stale inventory is worse than no
inventory: it is the thing the migration order is planned from.

So it is generated. This script reads three sources of truth that already exist
and cannot drift:

* ``app.py`` — the page routes, their gate decorator, and their SRS module
  (``_ROUTE_MODULES``), which is what the navbar reads to decide visibility.
* ``templates/*.html`` — the API calls each page actually makes, so the React
  screen's API surface is measured, not guessed.
* ``policy.NAV_ENTRIES`` — the navigation the navbar is built from.

Usage::

    python scripts/ui_inventory.py                      # print a summary
    python scripts/ui_inventory.py --markdown docs/UI_INVENTORY.md

Exits non-zero if a page route has no template, which is the drift this is
designed to catch: a template that exists with no route is dead, and a route
with no template is broken.
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP_PY = ROOT / "app.py"
TEMPLATES = ROOT / "templates"

# Decorator name -> gate tag. These are the gate strings `_tag_gate` stamps onto
# the view, which is exactly what `navigation_for()` consults, so the table here
# reproduces the live answer rather than a parallel guess at it.
GATE_DECORATORS = {
    "login_required": "login",
    "admin_required": "admin",
    "hr_or_admin_required": "hr_or_admin",
    "reporting_line_required": "reporting_line",
    "expense_actor_required": "expense_actor",
}

# Pages with no navbar (standalone auth / token pages). They are still in scope
# for the migration, but they do not share the authenticated shell, so recording
# that here keeps Phase 2 from treating them like a dashboard tab.
STANDALONE = {"login", "reset_password_page", "preboarding_page"}

# Routes that are deliberately *not* migrated, with the reason. Recorded rather
# than filtered silently: `/` is a redirect into the dashboard and `/logout` is
# a session action, so neither is a UI screen, and a test asserts every route is
# either phased or listed here. Giving them a phase would imply work that does
# not exist.
NOT_MIGRATED = {
    "/": "redirects to /dashboard (or /login); not a screen",
    "/logout": "session action; closes the user_sessions row and clears the cookie",
}

# Migration order. Phase 2's pilot is the employee dashboard because it exercises
# a broad slice of the read APIs while touching no write contract, so it is the
# cheapest page that can prove the shell, the API client and CSRF end to end.
PHASE_OF_ROUTE = {
    "/dashboard": 2, "/leaves": 3, "/profile": 3, "/regularization": 3,
    "/goals": 3, "/documents": 3, "/expenses": 3, "/tickets": 3,
    "/admin/users": 3, "/admin/permissions": 3, "/admin/import-users": 3,
    "/admin/holidays": 3, "/admin/leaves": 3, "/admin/onboarding": 3,
    "/admin/offboarding": 3, "/admin/audit": 3, "/admin/payroll": 3,
    "/admin/salary-structures": 3, "/admin/reports": 3, "/admin/analytics": 3,
    "/admin/assets": 3, "/admin/documents": 3, "/admin/expenses": 3,
    "/admin/tickets": 3, "/admin/goals": 3, "/admin/reviews": 3,
    "/admin/jobs": 3, "/admin/candidates": 3,
    "/login": 3, "/reset-password": 3, "/onboarding": 3, "/offboarding": 3,
    "/preboarding/<token>": 3,
}

# `fetch('/api/...')`, `apiGet('/api/...')`, template literals, and the older
# `$.get`/`$.post` spellings. Deliberately permissive: an over-count is a
# readable row, an under-count is a screen that quietly stops working.
_API_PATTERNS = (
    re.compile(r"""fetch\(\s*[`'"]([^`'"]*?/api/[^`'"]*)"""),
    re.compile(r"""\bapi(?:Get|Post|Put|Delete|Patch)\(\s*[`'"]([^`'"]*?/api/[^`'"]*)"""),
    re.compile(r"""\$\.(?:get|post|put|delete|ajax)\(\s*[`'"]([^`'"]*?/api/[^`'"]*)"""),
    re.compile(r"""url_for\(\s*[`'"]([^`'"]+)["'`]\s*,\s*endpoint\s*=\s*(?:True|)"""),
)

# Routes the inventory generator must not treat as pages.
_NON_PAGE = re.compile(r"^/(api|flasgger_static|static|login|logout)")

# The React SPA mount (docs/REACT_UI_MIGRATION.md, Phase 1). It is a Flask route
# but not a Jinja page, so it is excluded from the page inventory and listed
# separately by the drift check -- otherwise the generator reports it as a route
# that renders no template, and the next person "fixes" that by excluding
# something else.
SPA_RULES = frozenset({"/app/", "/app/<path:path>"})

# Static/CDN assets. Phase 1 requires the app to work with no CDN, so a CDN
# dependency in a template is a migration finding, not trivia.
_CDN = re.compile(r"""(?:src|href)=["'](https?:)?//[^"']+""")


def _literal(node: ast.AST):
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


def _walk_source_order(node):
    """Depth-first, source order.

    ``ast.walk`` is breadth-first, which reorders siblings before it returns
    them. That matters here: ``/dashboard`` renders ``admin_dashboard.html``
    from inside a ``try:`` block and falls back to ``user_dashboard.html`` at the
    end of the function, so BFS visits the *fallback* first and a "first match
    wins" scan reports the wrong template for the one route that has two.
    """
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(list(ast.iter_child_nodes(current))))


def parse_page_routes() -> list[dict]:
    """Page routes with their gate and SRS module, from ``app.py``."""
    tree = ast.parse(APP_PY.read_text())

    modules: dict[str, str] = {}
    for node in ast.walk(tree):
        # _ROUTE_MODULES = { 'view_name': 'module', ... }
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "_ROUTE_MODULES":
            for key, value in zip(node.value.keys, node.value.values):
                k, v = _literal(key), _literal(value)
                if k and v:
                    modules[k] = v

    routes: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        rule: str | None = None
        methods: set[str] | None = None
        gate: str | None = None
        templates: list[str] = []
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and getattr(dec.func, "attr", "") == "route":
                rule = _literal(dec.args[0]) if dec.args else None
                for kw in dec.keywords:
                    if kw.arg == "methods":
                        methods = {_literal(e) for e in kw.value.elts}
            elif isinstance(dec, ast.Name) and dec.id in GATE_DECORATORS:
                gate = GATE_DECORATORS[dec.id]
            elif isinstance(dec, ast.Attribute) and dec.attr in GATE_DECORATORS:
                gate = GATE_DECORATORS[dec.attr]

        # `render_template` is imported from flask as a bare name, so the callee
        # is an ast.Name -- matching only on `.attr` finds none of them.
        for sub in _walk_source_order(node):
            if isinstance(sub, ast.Call) and sub.args:
                callee = getattr(sub.func, "id", None) or getattr(sub.func, "attr", None)
                if callee == "render_template":
                    name = _literal(sub.args[0])
                    if name and name not in templates:
                        templates.append(name)

        if not rule or rule.startswith("/api") or rule in SPA_RULES:
            continue
        if rule in ("/static", "/static/<path:filename>"):
            continue
        if methods is not None and "GET" not in methods:
            continue
        routes.append({
            "rule": rule,
            "endpoint": node.name,
            "gate": gate or "none",
            "module": modules.get(node.name, ""),
            # The first is the primary; /dashboard has a role-selected variant
            # first and a fallback second, and both count as reachable.
            "template": templates[0] if templates else "",
            "variants": templates,
        })
    routes.sort(key=lambda r: r["rule"])
    return routes


def template_api_calls(path: pathlib.Path) -> list[str]:
    """Every ``/api/*`` path the template talks to, normalised and sorted."""
    text = path.read_text(errors="replace")
    found: set[str] = set()
    for pattern in _API_PATTERNS:
        for raw in pattern.findall(text):
            # Template literals carry a ${...} interpolation; keep it, because
            # the shape of the call matters more than its concrete id and the
            # React client has to build the same string.
            found.add(re.sub(r"\s+", " ", raw).strip())
    return sorted(found)


def template_cdn_assets(path: pathlib.Path) -> list[str]:
    return sorted(set(_CDN.findall(path.read_text(errors="replace"))))


def nav_entries() -> list[dict]:
    """The navbar tree from ``policy.NAV_ENTRIES``, read statically.

    Parsed as an AST node rather than by bracket-matching the source text: the
    tuple contains no parentheses of its own today, but a bracket scanner over
    a source file is the kind of thing that silently mis-counts the day someone
    adds one.
    """
    tree = ast.parse((ROOT / "policy.py").read_text())
    node = None
    for candidate in ast.walk(tree):
        if isinstance(candidate, ast.AnnAssign) and getattr(candidate.target, "id", "") == "NAV_ENTRIES":
            node = candidate.value
            break
    if node is None:
        raise SystemExit("NAV_ENTRIES not found in policy.py")
    entries = ast.literal_eval(node)
    return [
        {"href": e["href"], "label": e["label"], "icon": e["icon"],
         "module": e.get("module", ""), "always": bool(e.get("always")),
         "group": e.get("group", ""),
         "children": [{"href": c["href"], "label": c["label"], "icon": c["icon"],
                       "module": c.get("module", "")}
                      for c in e.get("children", ()) if not c.get("divider")]}
        for e in entries
    ]


def playwright_coverage() -> dict[str, list[str]]:
    """Which browser tests reference each page URL."""
    path = ROOT / "tests" / "test_playwright.py"
    if not path.exists():
        return {}
    tree = ast.parse(path.read_text())
    hits: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        for sub in ast.walk(node):
            url = None
            if isinstance(sub, ast.Call) and getattr(sub.func, "attr", "") == "goto" and sub.args:
                url = _literal(sub.args[0])
            elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                if re.fullmatch(r"/[a-zA-Z0-9/_<>-]+", sub.value):
                    url = sub.value
            if url and not url.startswith(("/api", "/static")):
                hits.setdefault(url, []).append(node.name)
    return {k: sorted(set(v)) for k, v in sorted(hits.items())}


def build() -> dict:
    routes = parse_page_routes()
    templates: dict[str, dict] = {}
    for tpl in sorted(TEMPLATES.glob("*.html")):
        templates[tpl.name] = {
            "api": template_api_calls(tpl),
            "cdn": template_cdn_assets(tpl),
            "lines": len(tpl.read_text(errors="replace").splitlines()),
        }
    return {
        "routes": routes,
        "templates": templates,
        "nav": nav_entries(),
        "tests": playwright_coverage(),
    }


def route_apis(data: dict, route: dict) -> list[str]:
    """The API calls a route's screen will need — the union across variants.

    ``/dashboard`` serves two templates chosen by policy, so its React screen
    needs both sets. Counting only the primary template understates the pilot
    page's surface by half, which is exactly the number Phase 1 gets wrong.
    """
    apis: set[str] = set()
    for name in route["variants"] or [route["template"]]:
        apis.update(data["templates"].get(name, {}).get("api", []))
    return sorted(apis)


def check_drift(data: dict) -> list[str]:
    """Problems that mean the inventory is wrong rather than merely incomplete."""
    problems = []
    # A route may render more than one template (/dashboard picks by policy), so
    # every variant counts as reachable, not just the primary.
    rendered = {t for r in data["routes"] for t in r["variants"]}
    for name in data["templates"]:
        if name in rendered or name == "_navbar.html":
            continue
        problems.append(f"template {name} is rendered by no page route (dead template)")
    for route in data["routes"]:
        if not route["variants"] and route["endpoint"] not in ("index", "logout"):
            problems.append(f"route {route['rule']} renders no template")
        if route["gate"] not in ("none", "login") and not route["module"]:
            problems.append(
                f"route {route['rule']} is gated on '{route['gate']}' but has no SRS module"
            )
    return problems


def to_markdown(data: dict) -> str:
    routes, templates, nav, tests = (
        data["routes"], data["templates"], data["nav"], data["tests"]
    )
    lines: list[str] = []
    add = lines.append

    add("# UI inventory (generated)")
    add("")
    add("Generated by `scripts/ui_inventory.py`. Do not hand-edit: every column is")
    add("read out of `app.py`, the templates or `policy.py`, so this file cannot")
    add("drift away from the code the way a written inventory would.")
    add("")
    add(f"- Page routes: **{len(routes)}**")
    add(f"- Templates: **{len(templates)}**")
    add(f"- Browser tests: **{sum(len(v) for v in tests.values())}** across "
        f"**{len(tests)}** URLs")
    add(f"- `/api/*` calls from templates: **{sum(len(t['api']) for t in templates.values())}**")
    add("")

    add("## Page inventory")
    add("")
    add("`Gate` is the decorator the route enforces today; `Module` is the SRS")
    add("capability the policy matrix filters on. Both are what the navbar reads, so")
    add("they are what a React nav link has to reproduce — as a *visibility* hint")
    add("only. The server stays authoritative (FR-USR-15).")
    add("")
    add("| Route | Template | Gate | Module | Phase | APIs | Tested |")
    add("|---|---|---|---|---|---|---|")
    for r in routes:
        apis = route_apis(data, r)
        covered = tests.get(r["rule"], [])
        tested = f"{len(covered)}" if covered else "—"
        variants = " + ".join(f"`{v}`" for v in r["variants"]) or "—"
        add(f"| `{r['rule']}` | {variants} | `{r['gate']}` | "
            f"{'`' + r['module'] + '`' if r['module'] else '—'} | "
            f"{PHASE_OF_ROUTE.get(r['rule'], 4)} | {len(apis)} | {tested} |")
    add("")

    add("## API surface per template")
    add("")
    for name in sorted(templates):
        info = templates[name]
        add(f"### `{name}` ({info['lines']} lines, {len(info['api'])} APIs)")
        add("")
        if info["api"]:
            for api in info["api"]:
                add(f"- `{api}`")
        else:
            add("- _no `/api` calls_")
        if info["cdn"]:
            add("")
            add("CDN dependencies: " + ", ".join(f"`{c}`" for c in info["cdn"]))
        add("")

    add("## Navigation model")
    add("")
    add("`policy.NAV_ENTRIES` is the single source of the navbar. The React shell")
    add("renders it from the same shape, filtered by the server.")
    add("")
    add("| Entry | Module | Kind |")
    add("|---|---|---|")
    for e in nav:
        kind = "group" if e["group"] else ("always" if e["always"] else "module")
        add(f"| `{e['href']}` {e['label']} | "
            f"{'`' + e['module'] + '`' if e['module'] else '—'} | {kind} |")
        for child in e["children"]:
            add(f"| &nbsp;&nbsp;`{child['href']}` {child['label']} | "
                f"`{child['module']}` | child |")
    add("")

    problems = check_drift(data)
    add("## Drift check")
    add("")
    if problems:
        add("Problems found:")
        add("")
        for problem in problems:
            add(f"- {problem}")
    else:
        add("No drift: every page route renders a template, every gated route")
        add("declares an SRS module, and every template is reachable.")
    add("")
    return "\n".join(lines)


def summary(data: dict) -> str:
    out = []
    out.append(f"Page routes: {len(data['routes'])}")
    out.append(f"Templates:   {len(data['templates'])}")
    out.append(f"Nav entries: {len(data['nav'])}")
    out.append("")
    out.append("phase  page                          gate            module        apis  tests")
    for r in sorted(data["routes"], key=lambda r: PHASE_OF_ROUTE.get(r["rule"], 9)):
        apis = route_apis(data, r)
        covered = len(data["tests"].get(r["rule"], []))
        out.append(
            f"  P{PHASE_OF_ROUTE.get(r['rule'], 4)}  "
            f"{r['rule']:<28} {r['gate']:<15} "
            f"{(r['module'] or '-'):<13} {len(apis):>4}  {covered:>5}"
        )
    problems = check_drift(data)
    if problems:
        out.append("")
        out.append("DRIFT:")
        out.extend(f"  - {p}" for p in problems)
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markdown", metavar="PATH",
                        help="write the full Markdown inventory here")
    args = parser.parse_args()

    data = build()
    problems = check_drift(data)

    if args.markdown:
        target = pathlib.Path(args.markdown)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(to_markdown(data))
        print(f"wrote {target}")
    else:
        print(summary(data))

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
