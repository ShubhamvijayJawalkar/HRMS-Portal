"""compatibility approval delegation table (FR-LEA-08a)

Revision ID: 0012_approval_delegations_compat
Revises: 0011_leave_grants

**This revision is a no-op by design, and that is the interesting part.**

`approval_delegations` has existed in the canonical target since the ``0001`` baseline,
with a ``no_overlapping_delegation`` GiST exclusion constraint:

    EXCLUDE USING gist (delegator_id WITH =, daterange(starts_on, ends_on, '[]') WITH &&)

Nothing read or wrote it. The table documented an intent that no route implemented —
the "schema without routes" shape the traceability pass exists to catch — and the
constraint was a statement about a rule no code enforced.

So this revision deliberately adds **nothing** to ``public``. The gap was the missing
``/api/delegations`` routes and the four consumers that never consulted a delegate, both
of which are application code. Adding DDL the target already has would be the
"migration carries changes its own target does not need" habit this project has avoided
elsewhere (``0001`` through ``0011`` each add only what is genuinely absent).

What the compatibility ``legacy`` schema needs is the table itself, and that is added
by ``init_db`` in ``app.py`` rather than here — the same split every other compat
table uses, so the legacy shape is created in one place.

**The overlap rule cannot be reproduced portably, and that is why it is in the code.**
DuckDB — and the legacy PostgreSQL schema as originally designed — cannot build a
GiST exclusion constraint, so ``delegations.create`` runs the same predicate in the
application and turns a conflict into a 409 naming the overlapping range. The
constraint remains the thing that makes the rule true *under concurrency* on the
canonical target; the application check is what makes it true, and reportable, on a
schema that cannot express it.
"""

# Deliberately no `from alembic import op`: this revision executes nothing, and an
# unused import on a no-op reads like a migration whose body was forgotten rather than
# one that was decided to be empty. Alembic only requires `revision`/`down_revision`.

revision = '0012_approval_delegations_compat'
down_revision = '0011_leave_grants'
branch_labels = None
depends_on = None


def upgrade():
    # No-op: `approval_delegations` is already part of the canonical target since the
    # baseline. `init_db` creates it for the compatibility `legacy` shape.
    pass


def downgrade():
    # No-op, matching `upgrade`. Dropping the table would destroy delegation records on
    # a target that owns them, and a downgrade must not remove data the previous
    # revision did not create.
    pass
