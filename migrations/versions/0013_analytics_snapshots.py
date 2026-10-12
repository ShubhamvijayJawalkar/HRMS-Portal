"""materialised analytics snapshots (FR-ANL-01)

Revision ID: 0013_analytics_snapshots
Revises: 0012_approval_delegations_compat

FR-ANL-01 asks for headcount, leave trends, expense summary and performance
summary "served from materialized views refreshed on a schedule, with a documented
'as of' timestamp on the response". The four figures shipped but were recomputed
live on every request and carried no timestamp.

The materialisation is a **snapshot table** rather than PostgreSQL MATERIALIZED
VIEW objects, and the reasoning lives in ``analytics.py``: the leave-trend metric
takes a ``months`` query window a view cannot parameterise, and the application
runs against two schema shapes, so one refresh path has to serve both. The
observable contract the requirement names — precomputed on a schedule, carrying the
"as of" timestamp — is unchanged.

``payload`` is JSONB because the four metrics have four different shapes. The
compatibility schema gets a TEXT column from ``init_db`` and the module decodes it
on read, so both shapes round-trip. ``metric`` is the natural primary key: there is
exactly one row per named metric, so it is added to the CC-01 natural-key exceptions.
"""

from alembic import op

revision = "0013_analytics_snapshots"
down_revision = "0012_approval_delegations_compat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS analytics_snapshots (
            metric      VARCHAR(64) PRIMARY KEY,
            payload     JSONB NOT NULL,
            computed_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS analytics_snapshots")
