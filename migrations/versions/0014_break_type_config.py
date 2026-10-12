"""location-aware break type configuration (FR-ATT-01)

Revision ID: 0014_break_type_config
Revises: 0013_analytics_snapshots

Break types were seeded in code and the Lunch approval rule was hard-coded in
the routes. Add a location scope and an explicit approval flag so authorised
administrators can manage the configuration through the API.
"""

from alembic import op

revision = "0014_break_type_config"
down_revision = "0013_analytics_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE break_types ADD COLUMN IF NOT EXISTS location VARCHAR")
    op.execute(
        "ALTER TABLE break_types ADD COLUMN IF NOT EXISTS requires_approval "
        "BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute(
        "UPDATE break_types SET requires_approval = TRUE "
        "WHERE lower(break_type) = 'lunch'"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_break_types_name_ci "
        "ON break_types (lower(break_type))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_break_types_name_ci")
    op.execute("ALTER TABLE break_types DROP COLUMN IF EXISTS requires_approval")
    op.execute("ALTER TABLE break_types DROP COLUMN IF EXISTS location")
