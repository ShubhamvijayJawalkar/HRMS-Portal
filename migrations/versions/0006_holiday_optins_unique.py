"""one *active* opt-in per employee per holiday (FR-HOL-03)

Revision ID: 0006_holiday_optins_unique
Revises: 0005_anonymisation_requests

The SRS asks for "one active opt-in per employee per holiday (unique
constraint)". The baseline had a plain ``UNIQUE (emp_id, holiday_id)``, which
looks like it satisfies that and does not: it is a constraint on *one request
ever*, not on *one active request*. An employee who withdrew an opt-in, or whose
request HR rejected, could never ask again for that holiday — which is precisely
what "opt-in/**opt-out**" is supposed to allow. It also contradicted the
constraint four lines above it: ``uq_active_offer_candidate`` is partial, for the
same reason.

So this revision replaces the unconditional constraint with the partial one the
requirement actually describes, and the two files stay in step: a database
created by ``0001`` from the corrected ``db/postgres_schema.sql`` ends in the same
shape, and a database already carrying ``uq_optin`` has it dropped here. The
migration is written to be safe from either starting point.

"Active" is defined once, as ``Pending | Approved``. ``Rejected`` is deliberately
*not* active — a declined employee has to be able to ask again once the
circumstances change — and neither is ``Cancelled``.

``init_db`` cannot create a partial index: DuckDB does not support one. On the
compatibility schema the rule is therefore the conditional
``INSERT ... WHERE NOT EXISTS`` in the route, using the identical status set, so
both backends answer the same way and the index here is what makes the canonical
target refuse the duplicate the application would otherwise have to catch.

Note on the revision id: ``alembic_version.version_num`` is ``VARCHAR(32)``, so a
longer id fails at the *version stamp* rather than at the migration. The DDL
runs, then ``UPDATE alembic_version`` raises ``StringDataRightTruncation`` and the
whole upgrade rolls back with nothing pointing at the name. The first draft of
this revision was 33 characters and failed exactly that way. The id is 24
characters for that reason, not for taste.
"""

from alembic import op

revision = "0006_holiday_optins_unique"
down_revision = "0005_anonymisation_requests"
branch_labels = None
depends_on = None

INDEX = "uq_holiday_optins_active"
LEGACY_CONSTRAINT = "uq_optin"


def upgrade() -> None:
    # The unconditional constraint is what made an opt-out irreversible. Dropping
    # it before adding the partial one also means a pre-existing duplicate pair
    # cannot make the CREATE fail — there cannot be one, because the constraint
    # was there to prevent it, but the ordering makes that explicit rather than
    # assumed.
    op.execute(f"ALTER TABLE holiday_optins DROP CONSTRAINT IF EXISTS {LEGACY_CONSTRAINT}")
    op.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS {INDEX}
            ON holiday_optins (emp_id, holiday_id)
            WHERE status IN ('Pending', 'Approved')
        """
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    # Restoring the unconditional constraint can fail if an employee has both a
    # withdrawn and a new request for the same holiday — which is exactly the state
    # this revision made representable. That is the correct behaviour for a
    # downgrade of a relaxation, so it is left to raise rather than papered over.
    op.execute(
        f"ALTER TABLE holiday_optins ADD CONSTRAINT {LEGACY_CONSTRAINT} UNIQUE (emp_id, holiday_id)"
    )
