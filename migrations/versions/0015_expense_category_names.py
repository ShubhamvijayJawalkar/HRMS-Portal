"""case-insensitive expense category names (FR-EXP-01)

Revision ID: 0015_expense_category_names
Revises: 0014_break_type_config

Expense categories are now administered through the application. Keep their
names unique regardless of case or surrounding whitespace, including for
concurrent writes, without changing the existing category primary keys.
"""

from alembic import op

revision = "0015_expense_category_names"
down_revision = "0014_break_type_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_expense_categories_name_ci "
        "ON expense_categories (lower(trim(name)))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_expense_categories_name_ci")
