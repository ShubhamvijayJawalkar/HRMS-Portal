"""duplicate (name, date) per location, as a constraint (FR-HOL-02)

Revision ID: 0007_holiday_unique_location
Revises: 0006_holiday_optins_unique

The SRS asks for the duplicate rule to be "prevented by a unique constraint". It
was a ``SELECT 1 ... WHERE name = ? AND holiday_date = ?`` in the route handler,
which is a message, not a rule: two concurrent adds of the same holiday both pass
the check and both insert. Nothing in the database refused the second row.

The constraint is on ``(name, holiday_date, COALESCE(location, ''))`` rather than
the obvious ``(name, holiday_date, location)``, and the difference is not
cosmetic. In SQL, NULL is distinct from NULL, so a plain unique index would
accept any number of org-wide (NULL location) holidays with the same name and
date — the case that matters most, because an org-wide duplicate is the mistake
an admin actually makes. The expression index makes the org-wide case collide.

The location is compared case-insensitively in the application
(``holiday_calendar.duplicate_key``) but the index uses the raw column. That is a
deliberate, recorded gap rather than an oversight: a case-insensitive index would
need a second functional index on ``LOWER(location)`` and a functional index on a
nullable expression is not portable to the DuckDB compatibility schema, which
cannot build partial indexes at all. The application check catches the
case-variant, the index catches the exact duplicate, and the two disagree only on
a case-variant arriving concurrently.
"""

from alembic import op

revision = "0007_holiday_unique_location"
down_revision = "0006_holiday_optins_unique"
branch_labels = None
depends_on = None

INDEX = "uq_holiday_name_date_location"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS {INDEX}
            ON holidays (name, holiday_date, COALESCE(location, ''))
        """
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
