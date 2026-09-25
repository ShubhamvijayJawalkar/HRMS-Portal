#!/usr/bin/env python3
"""Phase 0 — generate docs/data_dictionary.md from the migrated PostgreSQL target.

The SRS v2.0 (§15) requires traceability artifacts that are *generated*, not
hand-maintained, so this script introspects the live target DB (the source of
truth) and annotates each table with its v2.0 retention class (§7.4) and the
PII columns that sit behind the `pii_reveal` permission (Appendix B).

Run after the Phase-1 ETL, e.g.:
  python scripts/generate_data_dictionary.py [--database-url ...] [--out docs/data_dictionary.md]

Environment: DATABASE_URL (default: postgresql+psycopg://postgres:postgres@localhost:55432/hrms)
"""
from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

REPO_ROOT = Path(__file__).resolve().parents[1]

# Retention classes per SRS v2.0 §7.4. Everything not listed here is `transactional`.
STATUTORY_7Y = {
    "payroll_runs", "payroll_items", "payroll_approvals", "salary_structures",
    "expense_claims", "leave_requests", "leave_balance", "attendance_days",
    "breaks", "regularization_requests",
}
AUDIT_INDEFINITE = {"audit_log", "outbox_events"}

# PII-bearing columns — these sit behind the `pii_reveal` permission (Appendix B).
PII_COLUMNS = {
    ("users", "phone"), ("users", "address"), ("users", "emergency_contact_name"),
    ("users", "emergency_contact_phone"), ("users", "date_of_birth"),
    ("dependents", "name"), ("dependents", "relationship"), ("dependents", "date_of_birth"),
    ("employee_documents", "file_name"), ("documents", "name"),
    ("candidates", "name"), ("candidates", "email"), ("candidates", "phone"),
    ("candidates", "resume_text"),
}


def retention_for(table: str) -> str:
    if table in AUDIT_INDEFINITE:
        return "audit-indefinite-with-review"
    if table in STATUTORY_7Y:
        return "statutory-7y"
    return "transactional"


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate docs/data_dictionary.md from the target DB")
    ap.add_argument("--database-url", default=os.getenv(
        "DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:55432/hrms"))
    ap.add_argument("--out", default=REPO_ROOT / "docs" / "data_dictionary.md")
    args = ap.parse_args()

    engine = create_engine(args.database_url)
    insp = inspect(engine)
    tables = sorted(insp.get_table_names(schema="public"))

    pk_map = {t: insp.get_pk_constraint(t)["constrained_columns"] for t in tables}
    fk_map = {
        t: [
            (f["constrained_columns"][0], f["referred_table"], f["referred_columns"][0])
            for f in insp.get_foreign_keys(t)
        ]
        for t in tables
    }
    idx_map = {t: insp.get_indexes(t) for t in tables}
    ranges = {t: text(f"SELECT COUNT(*) FROM {t}") for t in tables}

    lines = [
        "# HRMS v2.0 — Data Dictionary",
        "",
        f"_Generated {datetime.now(UTC).isoformat()} by `scripts/generate_data_dictionary.py` "
        f"from `{args.database_url.split('@')[-1]}`. **Do not hand-edit** — regenerate (SRS §15)._",
        "",
        "Corresponds to SRS v2.0 §7 (Data model & database constraints). ",
        "Retention classes per §7.4: `transactional`, `statutory-7y` (7 years per §11.4), "
        "`audit-indefinite-with-review`. PII columns are permission-gated behind `pii_reveal`.",
        "",
        "## Tables overview",
        "",
        "| Table | Rows | Retention | PII cols |",
        "|-------|-----:|-----------|----------|",
    ]
    with engine.connect() as conn:
        for t in tables:
            n = conn.execute(ranges[t]).scalar()
            pii = sorted(c for c, _ in PII_COLUMNS if (c, t) in PII_COLUMNS)
            lines.append(f"| `{t}` | {n} | {retention_for(t)} | {', '.join(pii) if pii else '—'} |")
        lines.append("")
        lines.append("---")
        lines.append("")

        for t in tables:
            lines.append(f"## `{t}`")
            lines.append("")
            lines.append(f"- Retention class: **{retention_for(t)}**")
            if pk_map[t]:
                lines.append(f"- Primary key: `{', '.join(pk_map[t])}`")
            if fk_map[t]:
                fks = "; ".join(f"`{c}` → `{ref}.{rc}`" for c, ref, rc in fk_map[t])
                lines.append(f"- Foreign keys: {fks}")
            uniq_idx = [i for i in idx_map[t] if i.get("unique")]
            excl_idx = [i for i in idx_map[t] if i.get("duplicates_constraint")]
            if uniq_idx:
                def _cols(i):
                    return ", ".join(c if c else "(expression)" for c in (i.get("column_names") or []))

                lines.append("- Unique indexes: " + "; ".join(
                    f"`{i['name']}` ({_cols(i)})"
                    + (f" WHERE {i['dialect_options'].get('postgresql_where', '')}" if i.get("dialect_options", {}).get("postgresql_where") else "")
                    for i in uniq_idx))
            else:
                lines.append("- Unique indexes: none")
            if excl_idx:
                def _expr(i):
                    return ", ".join(e for e in (i.get("expressions") or []))
                lines.append("- Exclusion constraints: " + "; ".join(
                    f"`{i['name']}` ({_expr(i)})"
                    + (f" WHERE {i['dialect_options'].get('postgresql_where', '')}" if i.get("dialect_options", {}).get("postgresql_where") else "")
                    for i in excl_idx))
            lines.append("")
            lines.append("| Column | Type | Null | Default | PII |")
            lines.append("|--------|------|:----:|---------|:---:|")
            rows = conn.execute(text(
                "SELECT column_name, data_type, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t "
                "ORDER BY ordinal_position"), {"t": t}).fetchall()
            for name, dtype, nullable, default in rows:
                pii = "⚠" if (t, name) in PII_COLUMNS else ""
                lines.append(f"| `{name}` | {dtype} | {'Y' if nullable == 'YES' else 'N'} | `{default or ''}` | {pii} |")
            lines.append("")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out} ({len(tables)} tables)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
