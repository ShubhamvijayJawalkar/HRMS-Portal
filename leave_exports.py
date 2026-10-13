"""Asynchronous large leave exports (FR-LEA-03).

Small exports stay on the existing download path. Large results are queued,
built by the scheduler and stored privately until the requester downloads them.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta

import outbox
import working_days

logger = logging.getLogger(__name__)

ASYNC_EXPORT_THRESHOLD = 500
STALE_JOB_MINUTES = 15
EXPORT_RETENTION_HOURS = 24
PENDING = 'pending'
RUNNING = 'running'
COMPLETED = 'completed'
FAILED = 'failed'


def _app_helpers():
    from app import _is_public_target_schema, _next_generated_id, gen_id, get_db

    return get_db, _is_public_target_schema, _next_generated_id, gen_id


def _where(month: int, year: int, status: str | None) -> tuple[str, list]:
    clause = (
        "l.year = ? AND CAST(strftime('%m', l.start_date) AS INTEGER) = ?"
    )
    params = [year, month]
    if status:
        clause += ' AND l.status = ?'
        params.append(status)
    return clause, params


def count_rows(month: int, year: int, status: str | None = None) -> int:
    get_db, *_ = _app_helpers()
    conn = get_db()
    try:
        where, params = _where(month, year, status)
        return int(conn.execute(
            f'SELECT COUNT(*) FROM leave_requests l WHERE {where}', params
        ).fetchone()[0])
    finally:
        conn.close()


def fetch_rows(conn, month: int, year: int, status: str | None = None):
    where, params = _where(month, year, status)
    rows = conn.execute(
        "SELECT l.emp_id, u.name, l.leave_type, l.start_date, l.end_date, "
        "l.reason, l.status, l.approved_by, l.days "
        "FROM leave_requests l LEFT JOIN users u ON l.emp_id = u.emp_id "
        f"WHERE {where} ORDER BY l.start_date",
        params,
    ).fetchall()
    return [
        (row, row[8] if row[8] is not None else
         working_days.working_days(conn, row[0], row[3], row[4]))
        for row in rows
    ]


def workbook_bytes(computed_rows) -> bytes:
    import pandas as pd

    data = [{
        'Employee ID': row[0], 'Employee Name': row[1] or row[0],
        'Leave Type': row[2], 'From': row[3].isoformat(), 'To': row[4].isoformat(),
        'Working Days': days, 'Calendar Days': (row[4] - row[3]).days + 1,
        'Reason': row[5] or '', 'Status': row[6], 'Approved By': row[7] or '',
    } for row, days in computed_rows]
    columns = [
        'Employee ID', 'Employee Name', 'Leave Type', 'From', 'To', 'Working Days',
        'Calendar Days', 'Reason', 'Status', 'Approved By',
    ]
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
        pd.DataFrame(data, columns=columns).to_excel(
            writer, index=False, sheet_name='Leaves'
        )
    return buffer.getvalue()


def _job_id(conn):
    _, is_public, next_id, gen_id = _app_helpers()
    if is_public():
        return next_id(conn, 'leave_export_jobs', 'job_id')
    while True:
        candidate = gen_id()
        if not conn.execute(
            'SELECT 1 FROM leave_export_jobs WHERE job_id = ?', [candidate]
        ).fetchone():
            return candidate


def create_job(month: int, year: int, status: str | None, created_by: str,
               row_count: int) -> dict:
    from app import audit_log

    with outbox.transaction() as conn:
        job_id = _job_id(conn)
        conn.execute(
            "INSERT INTO leave_export_jobs "
            "(job_id, created_by, month, year, status_filter, status, row_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            [job_id, created_by, month, year, status, row_count, datetime.now()],
        )
        audit_log(
            created_by, 'LEAVE_EXPORT_QUEUED',
            f'Queued leave export job {job_id}',
            entity='leave_export_jobs', entity_id=job_id,
            after={'month': month, 'year': year, 'row_count': row_count},
        )
    return get_job(job_id)


def get_job(job_id: int) -> dict | None:
    get_db, *_ = _app_helpers()
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT job_id, created_by, month, year, status_filter, status, row_count, "
            "file_key, created_at, started_at, finished_at, expires_at, failure_reason "
            "FROM leave_export_jobs WHERE job_id = ?",
            [job_id],
        ).fetchone()
        if not row:
            return None
        expired = row[5] == COMPLETED and row[11] is not None and row[11] <= datetime.now()
        return {
            'job_id': row[0], 'created_by': row[1], 'month': row[2], 'year': row[3],
            'status_filter': row[4],
            'status': 'expired' if expired else row[5], 'row_count': row[6],
            'download': f'/api/leaves/exports/{row[0]}/download'
            if row[5] == COMPLETED and not expired else None,
            'created_at': row[8].isoformat() if row[8] else None,
            'started_at': row[9].isoformat() if row[9] else None,
            'finished_at': row[10].isoformat() if row[10] else None,
            'expires_at': row[11].isoformat() if row[11] else None,
            'failure_reason': row[12],
        }
    finally:
        conn.close()


def file_key_for_download(job_id: int) -> str | None:
    get_db, *_ = _app_helpers()
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT file_key FROM leave_export_jobs WHERE job_id = ? "
            "AND status = 'completed' AND expires_at > ?",
            [job_id, datetime.now()],
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def dispatch_once() -> int | None:
    """Claim and process at most one queued export, safe across workers."""
    import object_storage
    from app import app, audit_log, get_db

    now = datetime.now()
    cleanup_expired()
    conn = get_db()
    try:
        conn.execute(
            "UPDATE leave_export_jobs SET status = 'pending' "
            "WHERE status = 'running' AND started_at < ?",
            [now - timedelta(minutes=STALE_JOB_MINUTES)],
        )
        pending = conn.execute(
            "SELECT job_id, created_by, month, year, status_filter "
            "FROM leave_export_jobs WHERE status = 'pending' ORDER BY job_id LIMIT 1"
        ).fetchone()
        if not pending:
            return None
        job_id, created_by, month, year, status = pending
        claimed = conn.execute(
            "UPDATE leave_export_jobs SET status = 'running', started_at = ? "
            "WHERE job_id = ? AND status = 'pending'",
            [now, job_id],
        )
        if not claimed.rowcount:
            return None
    finally:
        conn.close()

    object_key = None
    try:
        conn = get_db()
        try:
            computed_rows = fetch_rows(conn, month, year, status)
            contents = workbook_bytes(computed_rows)
        finally:
            conn.close()
        object_key = object_storage.upload_leave_export(job_id, contents)
        with app.app_context():
            with outbox.transaction() as completion_conn:
                completion_conn.execute(
                    "UPDATE leave_export_jobs SET status = 'completed', file_key = ?, "
                    "row_count = ?, finished_at = ?, expires_at = ? "
                    "WHERE job_id = ? AND status = 'running'",
                    [
                        object_key, len(computed_rows), datetime.now(),
                        datetime.now() + timedelta(hours=EXPORT_RETENTION_HOURS), job_id,
                    ],
                )
                audit_log(
                    created_by, 'LEAVE_EXPORT_COMPLETED',
                    f'Leave export job {job_id} completed',
                    actor=f'SYSTEM:leave-export-job#{job_id}',
                    entity='leave_export_jobs', entity_id=job_id,
                    after={'row_count': len(computed_rows)},
                )
    except Exception:
        if object_key:
            try:
                object_storage.delete_leave_export(object_key)
            except object_storage.ObjectStorageError:
                logger.exception('Could not remove failed leave export %s', object_key)
        logger.exception('Leave export job %s failed', job_id)
        with app.app_context():
            with outbox.transaction() as failure_conn:
                failed = failure_conn.execute(
                    "UPDATE leave_export_jobs SET status = 'failed', failure_reason = ?, "
                    "finished_at = ? WHERE job_id = ? AND status = 'running'",
                    ['Export generation failed. Please retry or contact an administrator.',
                     datetime.now(), job_id],
                )
                if failed.rowcount:
                    audit_log(
                        created_by, 'LEAVE_EXPORT_FAILED',
                        f'Leave export job {job_id} failed',
                        actor=f'SYSTEM:leave-export-job#{job_id}',
                        entity='leave_export_jobs', entity_id=job_id,
                        before={'status': RUNNING}, after={'status': FAILED},
                    )
    return job_id


def cleanup_expired() -> int:
    """Delete retained export objects after their 24-hour download window."""
    import object_storage
    from app import app, audit_log, get_db

    conn = get_db()
    try:
        expired = conn.execute(
            "SELECT job_id, file_key, created_by FROM leave_export_jobs "
            "WHERE status = 'completed' AND expires_at <= ?",
            [datetime.now()],
        ).fetchall()
    finally:
        conn.close()
    cleaned = 0
    for job_id, object_key, created_by in expired:
        try:
            if object_key:
                object_storage.delete_leave_export(object_key)
        except object_storage.ObjectStorageError:
            logger.exception('Could not expire leave export job %s', job_id)
            continue
        with app.app_context():
            with outbox.transaction() as expiry_conn:
                expired_update = expiry_conn.execute(
                    "UPDATE leave_export_jobs SET status = 'expired', file_key = NULL "
                    "WHERE job_id = ? AND status = 'completed'",
                    [job_id],
                )
                if expired_update.rowcount:
                    audit_log(
                        created_by, 'LEAVE_EXPORT_EXPIRED',
                        f'Leave export job {job_id} expired',
                        actor=f'SYSTEM:leave-export-job#{job_id}',
                        entity='leave_export_jobs', entity_id=job_id,
                        before={'status': COMPLETED}, after={'status': 'expired'},
                    )
                    cleaned += 1
    return cleaned
