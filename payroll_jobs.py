"""Payroll run worker (FR-PAY-05, SRS §6.8 — the *corrected* flow).

``POST /api/payroll-runs`` used to compute every employee's item inside the
request: one HTTP call held a web worker for the length of the run, and a
failed attempt left a half-written run behind with no way to tell. The SRS
specifies the shape instead:

1. ``POST /api/payroll-runs`` validates the period, records the run in
   ``Queued`` and a ``payroll_run_jobs`` row, and answers **202** with the
   job id and the poll location;
2. the scheduler (or the admin "process now" endpoint) claims **one** job with
   a conditional ``UPDATE ... WHERE status = 'pending'``, so two gunicorn
   workers cannot both run it;
3. the worker processes employees **in batches**, each batch one transaction,
   publishing ``employees_done`` so progress is pollable while it runs;
4. on completion the run becomes ``Draft`` (the state FR-PAY-06's maker-checker
   starts from) and the job records its outcome.

**Retry is natural, not designed-in complexity.** Each employee's item insert
is idempotent — an existing ``payroll_items`` row for ``(run_id, emp_id)``
skips the employee — so a job that fails mid-way can simply be re-run, and
reimbursement claims are linked inside the same per-batch transaction that
writes the item, making the two atomic. On failure the job is marked
``failed`` with the reason and the run stays ``Queued``; an administrator
re-queues it with ``POST /api/payroll-runs/<rid>/run``, which is also what the
unit and browser suites use to stay deterministic without waiting for the
scheduler tick.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import outbox
import payroll_engine

logger = logging.getLogger(__name__)

PENDING = 'pending'
RUNNING = 'running'
COMPLETED = 'completed'
FAILED = 'failed'
TERMINAL = (COMPLETED, FAILED)

# A job that claimed itself and died (killed worker, redeploy) is retried
# after this long. Safe because the per-employee item insert is idempotent.
STALE_JOB_MINUTES = 15

BATCH_SIZE = 100


def _from_app():
    from app import get_db  # lazy: no circular import at module load

    return get_db


def _table_exists(conn) -> bool:
    try:
        conn.execute('SELECT 1 FROM payroll_run_jobs LIMIT 1').fetchone()
        return True
    except Exception:
        return False


def _job_id(conn):
    from app import _is_public_target_schema, _next_generated_id, gen_id  # lazy

    if _is_public_target_schema():
        return _next_generated_id(conn, 'payroll_run_jobs', 'job_id')
    while True:
        value = gen_id()
        if not conn.execute('SELECT 1 FROM payroll_run_jobs WHERE job_id = ?', [value]).fetchone():
            return value


def enqueue(conn, run_id, created_by, employees_total: int):
    """Record the pending job for ``run_id`` in the caller's transaction."""
    job_id = _job_id(conn)
    conn.execute(
        "INSERT INTO payroll_run_jobs (job_id, run_id, created_by, employees_total) "
        "VALUES (?, ?, ?, ?)",
        [job_id, run_id, created_by, employees_total],
    )
    return job_id


def dispatch_once() -> int | None:
    """Claim and process at most one queued payroll job. Returns its id."""
    get_db = _from_app()
    conn = get_db()
    if not _table_exists(conn):
        conn.close()
        return None
    now = datetime.now()
    try:
        conn.execute(
            "UPDATE payroll_run_jobs SET status = 'pending' "
            "WHERE status = 'running' AND started_at < ?",
            [now - timedelta(minutes=STALE_JOB_MINUTES)],
        )
        pending = conn.execute(
            "SELECT job_id FROM payroll_run_jobs WHERE status = 'pending' "
            "ORDER BY job_id LIMIT 1"
        ).fetchone()
        if not pending:
            return None
        claimed = conn.execute(
            "UPDATE payroll_run_jobs SET status = 'running', started_at = ? "
            "WHERE job_id = ? AND status = 'pending'",
            [now, pending[0]],
        )
        if not claimed.rowcount:
            return None                     # another worker won the race
        job_id = pending[0]
    finally:
        conn.close()
    try:
        _process(job_id)
    except Exception as exc:                 # never kill the scheduler
        logger.warning('payroll job %s failed: %s', job_id, exc)
        _fail(job_id, f'{type(exc).__name__}: {str(exc)[:400]}')
    return job_id


def run_now(run_id) -> dict:
    """Process the queued job for ``run_id`` synchronously (admin retry path).

    Used by the "process now" endpoint: if the job is pending or failed it is
    reset and processed; a completed run is a 409; a job another worker is
    running is a 409. Returns ``(status_code, payload)`` shaped like the
    dispatcher's outcome, so the route can answer 200/409/404.
    """
    get_db = _from_app()
    conn = get_db()
    try:
        run = conn.execute(
            "SELECT r.status, j.job_id, j.status FROM payroll_runs r "
            "JOIN payroll_run_jobs j ON j.run_id = r.run_id "
            "WHERE r.run_id = ? ORDER BY j.job_id DESC LIMIT 1",
            [run_id],
        ).fetchone()
        if not run:
            return {'status_code': 404, 'error': 'Payroll run not found'}
        run_status, job_id, job_status = run
        if run_status == 'Draft':
            return {'status_code': 409, 'error': 'Payroll run has already been processed'}
        if job_status == RUNNING:
            return {'status_code': 409, 'error': 'Payroll job is already running'}
        reset = conn.execute(
            "UPDATE payroll_run_jobs "
            "SET status = 'running', started_at = ?, employees_done = ("
            "    SELECT COUNT(*) FROM payroll_items WHERE run_id = ?"
            ") WHERE job_id = ? AND status IN ('pending', 'failed')",
            [datetime.now(), run_id, job_id],
        )
        if not reset.rowcount:
            return {'status_code': 409, 'error': 'Payroll job is not retryable'}
    finally:
        conn.close()
    try:
        _process(job_id)
    except Exception as exc:
        logger.warning('payroll job %s failed (on-demand): %s', job_id, exc)
        _fail(job_id, f'{type(exc).__name__}: {str(exc)[:400]}')
        return {'status_code': 500, 'error': 'Payroll job failed', 'job_id': job_id}
    status, reason = _job_outcome(job_id)
    if status == COMPLETED:
        return {'status_code': 200, 'run_id': run_id, 'job_id': job_id, 'status': 'Draft'}
    return {'status_code': 500, 'error': reason or 'Payroll job failed', 'job_id': job_id}


def _job_outcome(job_id):
    conn = _from_app()()
    try:
        row = conn.execute(
            "SELECT status, failure_reason FROM payroll_run_jobs WHERE job_id = ?",
            [job_id],
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)
    finally:
        conn.close()


def _fail(job_id, reason):
    conn = _from_app()()
    try:
        conn.execute(
            "UPDATE payroll_run_jobs SET status = 'failed', failure_reason = ?, "
            "finished_at = ? WHERE job_id = ?",
            [reason[:500], datetime.now(), job_id],
        )
    finally:
        conn.close()


def _process(job_id):
    get_db = _from_app()
    from app import _payroll_period_bounds  # lazy

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT j.run_id, r.month, r.year, r.adjustment_of_run_id, j.created_by "
            "FROM payroll_run_jobs j JOIN payroll_runs r ON r.run_id = j.run_id "
            "WHERE j.job_id = ?",
            [job_id],
        ).fetchone()
        if not row:
            raise LookupError(f'payroll job {job_id} does not reference a run')
        run_id, month, year, _adjustment, created_by = row
        # A resumed job keeps whatever batches already committed; the counter
        # is rebuilt from the items table rather than trusted.
        conn.execute(
            "UPDATE payroll_run_jobs SET employees_done = ("
            "  SELECT COUNT(*) FROM payroll_items WHERE run_id = ?"
            ") WHERE job_id = ?",
            [run_id, job_id],
        )
        period_start, period_end = _payroll_period_bounds(month, year)
        try:
            rates = payroll_engine.rates_on(conn, period_end)
        except payroll_engine.PayrollRatesError as exc:
            raise RuntimeError(str(exc)) from exc
        total = conn.execute(
            "SELECT COUNT(*) FROM users u JOIN salary_structures s ON s.struct_id = ("
            "  SELECT s2.struct_id FROM salary_structures s2"
            "  WHERE s2.emp_id = u.emp_id AND s2.effective_from <= ?"
            "    AND (s2.effective_to IS NULL OR s2.effective_to >= ?)"
            "  ORDER BY s2.effective_from DESC LIMIT 1"
            ") WHERE u.status IN ('Active', 'Onboarding')",
            [period_end, period_start],
        ).fetchone()[0]
        conn.execute(
            "UPDATE payroll_run_jobs SET employees_total = ? WHERE job_id = ?",
            [int(total), job_id],
        )
    finally:
        conn.close()

    while True:
        with outbox.transaction() as tx:
            batch = tx.execute(
                "SELECT u.emp_id, s.basic, s.hra, s.allowances, s.deductions "
                "FROM users u JOIN salary_structures s ON s.struct_id = ("
                "  SELECT s2.struct_id FROM salary_structures s2"
                "  WHERE s2.emp_id = u.emp_id AND s2.effective_from <= ?"
                "    AND (s2.effective_to IS NULL OR s2.effective_to >= ?)"
                "  ORDER BY s2.effective_from DESC LIMIT 1"
                ") WHERE u.status IN ('Active', 'Onboarding') AND NOT EXISTS ("
                "  SELECT 1 FROM payroll_items i WHERE i.run_id = ? AND i.emp_id = u.emp_id"
                ") ORDER BY u.emp_id LIMIT ?",
                [period_end, period_start, run_id, BATCH_SIZE],
            ).fetchall()
            if not batch:
                break
            for emp_id, basic, hra, allowances, deductions in batch:
                claims = payroll_engine.claim_reimbursements(tx, emp_id, run_id)
                item = payroll_engine.calc_item(
                    tx, emp_id=emp_id, basic=float(basic), hra=float(hra),
                    allowances=float(allowances), deductions=float(deductions),
                    period_start=period_start, period_end=period_end,
                    rates=rates, claims=claims,
                )
                from app import _is_public_target_schema, _next_generated_id  # lazy
                item_id = None if _is_public_target_schema() else _next_generated_id(
                    tx, 'payroll_items', 'item_id'
                )
                columns = ["item_id", "run_id", "emp_id", "gross_salary", "deductions_total",
                           "net_salary", "pf", "esi", "pt", "tds", "lop_amount", "reimbursements"]
                values = [item_id, run_id, emp_id, item['gross'], item['deductions_total'],
                          item['net'], item['pf'], item['esi'], item['pt'], item['tds'],
                          item['lop_amount'], item['reimbursements']]
                if item_id is None:
                    columns = [c for c in columns if c != 'item_id']
                    values = values[1:]
                tx.execute(
                    f"INSERT INTO payroll_items ({', '.join(columns)}) "
                    f"VALUES ({', '.join(['?'] * len(values))})",
                    values,
                )
            tx.execute(
                "UPDATE payroll_run_jobs SET employees_done = employees_done + ? "
                "WHERE job_id = ?",
                [len(batch), job_id],
            )

    with outbox.transaction() as tx:
        tx.execute(
            "UPDATE payroll_runs SET status = 'Draft' WHERE run_id = ? AND status = 'Queued'",
            [run_id],
        )
        tx.execute(
            "UPDATE payroll_run_jobs SET status = 'completed', finished_at = ? "
            "WHERE job_id = ?",
            [datetime.now(), job_id],
        )
        from app import audit_log  # lazy

        audit_log(
            created_by, 'PAYROLL_RUN_EXECUTE',
            f'Payroll run {run_id} processed by job {job_id}',
            actor=f'SYSTEM:payroll-job#{job_id}',
            entity='payroll_runs', entity_id=run_id,
            after={'run_id': run_id, 'job_id': job_id,
                   'month': month, 'year': year, 'status': 'Draft'},
        )


def job_status(job_id) -> dict | None:
    get_db = _from_app()
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT job_id, run_id, status, employees_total, employees_done, "
            "failure_reason, created_at, started_at, finished_at "
            "FROM payroll_run_jobs WHERE job_id = ?",
            [job_id],
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None

    def iso(value):
        return value.isoformat() if hasattr(value, 'isoformat') else value

    return {
        'job_id': row[0], 'run_id': row[1], 'status': row[2],
        'employees_total': row[3], 'employees_done': row[4],
        'failure_reason': row[5],
        'created_at': iso(row[6]), 'started_at': iso(row[7]),
        'finished_at': iso(row[8]),
    }
