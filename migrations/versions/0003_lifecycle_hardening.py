"""harden lifecycle constraints after the initial workflow migration

Revision ID: 0003_lifecycle_hardening
Revises: 0002_lifecycle_workflows
"""

from alembic import op

revision = "0003_lifecycle_hardening"
down_revision = "0002_lifecycle_workflows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Link exit interviews to the corrected offboarding workflow.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'fk_exit_interviews_offboard'
            ) THEN
                ALTER TABLE exit_interviews
                    ADD CONSTRAINT fk_exit_interviews_offboard
                    FOREIGN KEY (offboard_id) REFERENCES offboarding_workflow(offboard_id);
            END IF;
        END $$
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_exit_interviews_offboard "
        "ON exit_interviews (offboard_id)"
    )

    # Existing v1 rows may have no split values. Backfill deterministically
    # before enforcing the v2.0 precision and nullability rules.
    op.execute(
        "UPDATE offer_letters SET basic_pct = 50, hra_pct = 30, allowances_pct = 20 "
        "WHERE basic_pct IS NULL OR hra_pct IS NULL OR allowances_pct IS NULL"
    )
    op.execute("ALTER TABLE offer_letters ALTER COLUMN basic_pct SET NOT NULL")
    op.execute("ALTER TABLE offer_letters ALTER COLUMN hra_pct SET NOT NULL")
    op.execute("ALTER TABLE offer_letters ALTER COLUMN allowances_pct SET NOT NULL")
    op.execute("ALTER TABLE offer_letters DROP CONSTRAINT IF EXISTS split_sums_100")
    op.execute(
        """
        ALTER TABLE offer_letters ADD CONSTRAINT split_sums_100
        CHECK (
            basic_pct IS NOT NULL AND hra_pct IS NOT NULL AND allowances_pct IS NOT NULL
            AND basic_pct + hra_pct + allowances_pct = 100
        )
        """
    )

    # Resolve any pre-existing active-offer duplicate before restoring the
    # database-level invariant. Accepted offers win; the lowest ID breaks ties.
    op.execute("DROP INDEX IF EXISTS uq_active_offer_candidate")
    op.execute(
        """
        WITH ranked AS (
            SELECT offer_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY candidate_id
                       ORDER BY CASE WHEN status = 'Accepted' THEN 0 ELSE 1 END, offer_id
                   ) AS rn
            FROM offer_letters
            WHERE status IN ('Pending', 'Accepted')
        )
        DELETE FROM offer_letters o
        USING ranked
        WHERE o.offer_id = ranked.offer_id AND ranked.rn > 1
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_active_offer_candidate "
        "ON offer_letters (candidate_id) WHERE status IN ('Pending', 'Accepted')"
    )

    # Revoked is a terminal state and must permit a later resignation.
    op.execute("DROP INDEX IF EXISTS uq_active_resignation")
    op.execute(
        "CREATE UNIQUE INDEX uq_active_resignation "
        "ON resignations (emp_id) WHERE status NOT IN ('Cancelled', 'Revoked')"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_active_resignation")
    op.execute(
        "CREATE UNIQUE INDEX uq_active_resignation "
        "ON resignations (emp_id) WHERE status <> 'Cancelled'"
    )
    op.execute("DROP INDEX IF EXISTS ix_exit_interviews_offboard")
    op.execute("ALTER TABLE exit_interviews DROP CONSTRAINT IF EXISTS fk_exit_interviews_offboard")
    op.execute("ALTER TABLE offer_letters DROP CONSTRAINT IF EXISTS split_sums_100")
    op.execute(
        "ALTER TABLE offer_letters ADD CONSTRAINT split_sums_100 "
        "CHECK (basic_pct + hra_pct + allowances_pct = 100)"
    )
    op.execute("ALTER TABLE offer_letters ALTER COLUMN basic_pct DROP NOT NULL")
    op.execute("ALTER TABLE offer_letters ALTER COLUMN hra_pct DROP NOT NULL")
    op.execute("ALTER TABLE offer_letters ALTER COLUMN allowances_pct DROP NOT NULL")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_active_offer_candidate "
        "ON offer_letters (candidate_id) WHERE status IN ('Pending', 'Accepted')"
    )
