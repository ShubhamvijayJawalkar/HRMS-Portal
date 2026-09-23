#!/usr/bin/env python3
"""Generate the living HRMS v2.0 migration TO DO list (PDF).

Edit the STATUS values below (DONE / IN_PROGRESS / PENDING) and the UPDATE
LOG, then re-run to regenerate ``docs/HRMS_ToDo.pdf``. Every completed task
gets its evidence text updated here too.

    python scripts/update_todo_pdf.py
"""

from __future__ import annotations

import datetime
import os
import subprocess

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

DONE = "DONE"
IN_PROGRESS = "IN PROGRESS"
PENDING = "PENDING"

STATUS_COLORS = {
    DONE: colors.HexColor("#1b5e20"),
    IN_PROGRESS: colors.HexColor("#b26a00"),
    PENDING: colors.HexColor("#616161"),
}

# ── Update log (append newest first) ────────────────────────────────────
UPDATE_LOG = [
    ("2026-09-23", "CC-07 idempotent writes done: @idempotent decorator on 9 POST routes, "
     "idempotency_keys DDL + hourly purge, 6 unit tests green (DuckDB 40 / PG 43 / PG+Redis 43), "
     "probe replays on pure v2.0 JSONB -> 86/86 GET + 12/12 write. Next: service-layer rewrite."),
    ("2026-09-23", "TODO list created. Next task slated: CC-07 idempotency."),
]

# ── Task list: (phase, task detail, evidence, status) ───────────────────
TASKS = [
    # ── Phase 0-1 ────────────────────────────────────────────────────────
    ("Phase 0-1", "Freeze the v1.0 DuckDB schema; inventory tables for ETL", "Schema freeze note in docs/MIGRATION.md (08f7e40)", DONE),
    ("Phase 0-1", "Build v2.0 target schema (db/postgres_schema.sql + Alembic baseline)", "49-table public schema, CC-01..CC-16 documented (08f7e40)", DONE),
    ("Phase 0-1", "One-time DuckDB -> PostgreSQL ETL with reconciliation", "hrms DB seeded; counts reconciled (08f7e40)", DONE),
    # ── Phase 2 ──────────────────────────────────────────────────────────
    ("Phase 2", "DuckDB->psycopg adapter (db_backend.py): translate/strftime/autocommit", "App runs on PostgreSQL legacy schema (b9164f2)", DONE),
    ("Phase 2", "Unit + browser suites green on PostgreSQL (legacy schema preserved)", "29/29 unit, 15/15 Playwright (b9164f2)", DONE),
    # ── Phase 3a (CC-06) ─────────────────────────────────────────────────
    ("Phase 3a", "Argon2id password hashing; legacy bcrypt re-hashed on login", "security.py (98563ee)", DONE),
    ("Phase 3a", "Global CSRF enforcement (fetch wrapper + csrf_token field + token API)", "98563ee", DONE),
    ("Phase 3a", "Server-side Redis sessions (REDIS_URL opt-in) + login rate limiting", "98563ee", DONE),
    # ── Phase 3b (CC-01 + public flip) ───────────────────────────────────
    ("Phase 3b", "CC-01 identity rule enforced: scripts/check_cc_rules.py + PG-gated test", "45 identity + 4 natural keys, sequences ahead of data (acb5219)", DONE),
    ("Phase 3b", "Public-flip readiness probe (scripts/probe_public_flip.py)", "GET + write-flow matrix against a throwaway public DB (acb5219)", DONE),
    ("Phase 3b", "Boolean adapter compat: predicates, INSERT params, naive datetime round-trip", "Inert on legacy (zero boolean cols); PG-gated tests (21480fe)", DONE),
    ("Phase 3b", "Write-flow probe + salary_structures seed data fix (CC-05)", "11/11 core write flows green on public (21480fe)", DONE),
    ("Phase 3b", "CC-09 transactional outbox (outbox.py + scheduler + admin endpoints)", "Atomic business-write + event; backoff -> dead-letter (7e52f88)", DONE),
    ("Phase 3b", "CC-07 idempotency: @idempotent decorator + idempotency_keys wired for keyed POST retries", "Replay-without-duplicate, 409 on body reuse, claim released on failure; 6 unit tests green on every stack; probe replays on pure v2.0 JSONB", DONE),
    ("Phase 3b", "Service-layer rewrite onto public: notifications.category, shift_assignments, expanded audit_log", "Second-largest remaining phase; several increments", PENDING),
    ("Phase 3b", "Extend probe write section: forgot-password, payroll bank-file/TDS, ticket/ATS", "Measurable backlog for the service-layer rewrite", PENDING),
    # ── Phase 4 ──────────────────────────────────────────────────────────
    ("Phase 4", "Attendance finalisation job (FR-JOB-01)", "SRS §14 Phase 4", PENDING),
    ("Phase 4", "Maker-checker payroll (FR-PAY-06)", "SRS §14 Phase 4", PENDING),
    ("Phase 4", "Corrected ATS / onboarding / offboarding flows", "SRS §14 Phase 4", PENDING),
    # ── Phase 5-6 ────────────────────────────────────────────────────────
    ("Phase 5", "Final cutover: flip APP_DB_SCHEMA to public, retire legacy", "Out of the ETL blueprint; tracked in SRS", PENDING),
    ("Phase 6", "Decommission DuckDB runtime", "Out of the ETL blueprint; tracked in SRS", PENDING),
]

# ── Test / readiness gates (current green state) ────────────────────────
GATES = [
    ("Unit suite (tests/test_app.py)", "DuckDB", "40 passed, 3 skipped (PG-gated CC-01/boolean tests)"),
    ("Unit suite (tests/test_app.py)", "PostgreSQL", "43 passed"),
    ("Unit suite (tests/test_app.py)", "PostgreSQL + Redis", "43 passed"),
    ("Browser suite (tests/test_playwright.py)", "PostgreSQL", "15 passed"),
    ("CC-01 rule checker (scripts/check_cc_rules.py)", "hrms (public)", "OK - 45 identity + 4 natural keys"),
    ("Public-flip probe (scripts/probe_public_flip.py)", "hrms_probe (public)", "86/86 GET + 12/12 write flows (incl. CC-07 replay), 0 seed rejections"),
]

DONE_BY_PHASE = {p: sum(1 for t in TASKS if t[0] == p and t[3] == DONE) for p in sorted({t[0] for t in TASKS})}
TOTAL_BY_PHASE = {p: sum(1 for t in TASKS if t[0] == p) for p in sorted({t[0] for t in TASKS})}


def _current_branch() -> str:
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=os.path.dirname(os.path.dirname(__file__)),
            text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
        return f"main @ {head}"
    except Exception:
        return "main (unknown HEAD)"


def build_pdf(path: str) -> None:
    doc = SimpleDocTemplate(
        path,
        pagesize=A4,
        leftMargin=0.7 * inch,
        rightMargin=0.7 * inch,
        topMargin=0.6 * inch,
        bottomMargin=0.6 * inch,
        title="HRMS v2.0 Migration - TO DO List",
        author="HRMS migration runbook",
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Title"], fontSize=18, spaceAfter=2, textColor=colors.HexColor("#0f172a"))
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=12, spaceBefore=10, spaceAfter=4, textColor=colors.HexColor("#1e293b"))
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=9, leading=12)
    small = ParagraphStyle("small", parent=styles["BodyText"], fontSize=8, leading=10, textColor=colors.HexColor("#475569"))

    def status_para(text: str):
        return Paragraph(
            f'<font color="{STATUS_COLORS[text]}">{text}</font>',
            ParagraphStyle("st", parent=body, alignment=1, spaceBefore=0),
        )

    story = []
    story.append(Paragraph("HRMS v2.0 Migration - TO DO List", h1))
    story.append(Paragraph("Living document - updated as tasks complete. Source of truth for the migration's remaining work.", small))
    story.append(Spacer(1, 6))

    # Milestone summary
    story.append(Paragraph("Milestone status", h2))
    done_total = sum(1 for t in TASKS if t[3] == DONE)
    summary_rows = [
        ["Completed tasks", f"{done_total} / {len(TASKS)}"],
        ["Current branch", _current_branch()],
        ["Next task", "Service-layer rewrite onto public - notifications.category, shift_assignments, expanded audit_log"],
    ]
    for phase in sorted(TOTAL_BY_PHASE):
        summary_rows.append([f"{phase} progress", f"{DONE_BY_PHASE[phase]} / {TOTAL_BY_PHASE[phase]} done"])
    s_table = Table(
        [[Paragraph(f"<b>{r[0]}</b>", small), Paragraph(r[1], small)] for r in summary_rows],
        colWidths=[2.2 * inch, 4.3 * inch],
    )
    s_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f1f5f9")),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(s_table)

    # Task list grouped by phase
    story.append(Paragraph("TO DO / status by phase", h2))
    current_phase = None
    for phase, task, evidence, status in TASKS:
        if phase != current_phase:
            current_phase = phase
            story.append(Paragraph(
                f"{phase}  <font size=8 color='#64748b'>({DONE_BY_PHASE[phase]}/{TOTAL_BY_PHASE[phase]} done)</font>",
                h2,
            ))
        stat_p = status_para(status)
        row = [
            stat_p,
            Paragraph(f"<b>{task}</b><br/><font size=7 color='#64748b'>{evidence}</font>", small),
        ]
        t = Table([row], colWidths=[1.0 * inch, 5.5 * inch])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#f8fafc")),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e2e8f0")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(KeepTogether(t))
        story.append(Spacer(1, 3))

    # Test gates
    story.append(PageBreak())
    story.append(Paragraph("Test & readiness gates", h2))
    g_head = [Paragraph("<b>Gate</b>", small), Paragraph("<b>Stack</b>", small), Paragraph("<b>Current result</b>", small)]
    g_rows = [g_head] + [[Paragraph(a, small) for a in r] for r in GATES]
    g_table = Table(g_rows, colWidths=[2.6 * inch, 1.5 * inch, 2.4 * inch], repeatRows=1)
    g_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(g_table)

    # Update log
    story.append(Paragraph("Update log (newest first)", h2))
    log_rows = [[Paragraph(f"<b>{d}</b>", small), Paragraph(n, small)] for d, n in UPDATE_LOG]
    l_table = Table(log_rows, colWidths=[1.2 * inch, 4.8 * inch])
    l_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e2e8f0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(l_table)
    story.append(Spacer(1, 8))
    story.append(Paragraph(f"Generated {datetime.date.today().isoformat()} by scripts/update_todo_pdf.py", small))

    doc.build(story)
    print(f"wrote {path}")


if __name__ == "__main__":
    build_pdf(os.path.join(os.path.dirname(__file__), "..", "docs", "HRMS_ToDo.pdf"))