"""stored leave day count (FR-LEA-09)

Revision ID: 0010_leave_days_stored
Revises: 0009_account_lockout

FR-LEA-09 requires that leave days, payroll loss-of-pay and the reports' "working
days" figure all come from **one** function, and the application now has that
function in ``working_days.py``. This revision adds the one column that requirement
needs in order to hold across a request's lifetime.

**Why ``days`` is stored rather than recomputed at each step.** ``leave_requests``
has no day-count column, so approve and cancel both recomputed ``(end - start).days
+ 1``. With the shared function that becomes *a* correct recomputation — and still
not a safe one. Between applying and approving, a holiday can be added or the
employee's weekly-off pattern can change, at which point approve would move a
different number of days than apply reserved and the balance would drift by the
difference. Nothing would show it: every step succeeds and every audit row is
honest. That is the ledger defect FR-LEA-06 was written to close, reappearing one
layer down, so the figure apply computed is the figure approve and cancel use.

**Nullable, with no backfill.** ``days`` is NULL for every existing row, because a
request approved before this migration was charged under the old calendar-day rule
and there is no honest value to reconstruct — the holiday calendar of the day it was
approved is not recoverable. The application treats NULL as "compute on demand and
persist", so old requests keep working and become correct the first time they are
next touched. Backfilling with the current calendar would silently restate history,
which is worse than leaving it.

**``session`` is not added here** — the baseline already has it
(``Full|First-half|Second-half``, FR-LEA-02). Only the compatibility ``legacy``
shape lacked it, and ``init_db`` adds it there. This revision is therefore one
column, deliberately: no migration should carry DDL that its own target already has.

**Metadata-only.** ``ADD COLUMN`` with no default is a catalogue change, so no
``ACCESS EXCLUSIVE`` rewrite of a table every leave page reads.
"""

from alembic import op

revision = '0010_leave_days_stored'
down_revision = '0009_account_lockout'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS days INTEGER')


def downgrade():
    # Additive and nullable, so the reverse is metadata-only too. The column is not
    # used by anything on the way down, so dropping it cannot lose balance data —
    # `leave_balance` is the ledger of record, and this column is a cache of what one
    # request consumed.
    op.execute('ALTER TABLE leave_requests DROP COLUMN IF EXISTS days')
