"""account lockout state (FR-AUTH-03)

Revision ID: 0009_account_lockout
Revises: 0008_notification_preferences

The SRS asks for "10 consecutive failures within 15 minutes locks the account
for 15 minutes and notifies the user by email", and its flow diagram puts the
counter in Redis. Three columns on ``users`` instead — see ``lockout.py`` for
why, which is the same reasoning as the password-policy check: a control that
silently stops existing when Redis is unreachable has failed open, and this app
treats Redis as optional.

**The columns are additive and nullable-safe.** ``failed_attempts`` defaults to 0
and ``last_failed_login``/``locked_until`` to NULL, so every existing row is
already in the correct state and the migration needs no backfill and no data
movement. ``ADD COLUMN ... NOT NULL DEFAULT 0`` is metadata-only on PostgreSQL
12+, which matters because this is a zero-downtime expand/contract project: a
rewrite of ``users`` for the sake of an integer would take an ``ACCESS EXCLUSIVE``
lock on the table every login reads.

**No index.** Nothing queries by ``locked_until``; the admin directory reads it
row by row alongside the rest of the employee record. An index would be paid for
on every write and used by nothing.

**Lockout is not a status.** It is deliberately *not* an enum value on
``users.status``. A lockout is a temporary consequence of failed sign-ins, while
``Blocked``/``Archived`` are deliberate account states; collapsing them would make
a fifteen-minute nuisance indistinguishable from a sanctioned decision in the
database, in the admin UI and in the audit trail.
"""

from alembic import op

revision = "0009_account_lockout"
down_revision = "0008_notification_preferences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS failed_attempts INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_failed_login TIMESTAMPTZ")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_until TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS locked_until")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS last_failed_login")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS failed_attempts")
