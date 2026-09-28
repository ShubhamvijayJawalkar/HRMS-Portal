"""Background bulk-import jobs (FR-USR-04).

The v1.0 CSV user import ran entirely inside the request: pandas read the whole
file into memory, and every row cost a couple of queries, so a large upload held
a web worker open for the length of the load and left no record of what
happened. This module makes the load asynchronous and inspectable:

* ``POST /api/users/import`` validates the header and the size, stores the
  upload, records a ``pending`` job and returns ``202`` with the job id;
* ``dispatch_once()`` claims **one** job with an atomic status transition and
  processes it, so a second worker (gunicorn runs several) can never take the
  same job;
* ``GET /api/users/import/<job_id>`` reports progress, and the UI polls it.

The per-row contract is unchanged: every row goes through
``app._validate_user_payload`` and the same case-insensitive duplicate checks, so
a bulk load cannot bypass the directory rules. Rows are also *naturally
idempotent* — an existing employee ID or email is skipped, not re-inserted — so a
job that dies half-way can simply be retried, which is what the stale-job
recovery in :func:`dispatch_once` relies on.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

# A directory import is a maintenance task, not a bulk data feed: the caps keep
# one upload from exhausting a worker or the disk.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024      # 5 MB
MAX_ROWS = 5000

REQUIRED_COLUMNS = ('emp_id', 'name', 'email')

# Job lifecycle: pending -> running -> completed | failed | cancelled
PENDING = 'pending'
RUNNING = 'running'
COMPLETED = 'completed'
FAILED = 'failed'
CANCELLED = 'cancelled'
TERMINAL = (COMPLETED, FAILED, CANCELLED)

# A job that claimed itself and then died (killed worker, restart) is retried
# after this long. Retrying is safe because the importer skips rows that already
# exist.
STALE_JOB_MINUTES = 15

# Per-row errors are capped so a systematically bad file cannot fill the row.
MAX_RECORDED_ERRORS = 50


class ImportError_(ValueError):
    """The upload cannot be queued at all (bad header, too big, too many rows)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _from_app():
    from app import get_db, hash_password  # lazy: no circular import at module load

    return get_db, hash_password


def _job_id(conn):
    from app import _is_public_target_schema, _next_generated_id, gen_id  # lazy

    if _is_public_target_schema():
        return _next_generated_id(conn, 'import_jobs', 'job_id')
    while True:
        value = gen_id()
        if not conn.execute('SELECT 1 FROM import_jobs WHERE job_id = ?', [value]).fetchone():
            return value


def _table_exists(conn) -> bool:
    try:
        conn.execute('SELECT 1 FROM import_jobs LIMIT 1').fetchone()
        return True
    except Exception:
        return False


def upload_dir() -> str:
    """Where uploads live (the container mounts ``/app/uploads`` as a volume)."""
    return os.path.join(os.getcwd(), 'uploads', 'imports')


def inspect_upload(storage) -> tuple[str, int]:
    """Validate the upload and return ``(header_csv, row_count)``.

    Only the header and the row count are read here; the payload is streamed to
    disk afterwards and parsed again in the worker.
    """
    filename = getattr(storage, 'filename', '') or ''
    if not filename.lower().endswith('.csv'):
        raise ImportError_('CSV file required', 400)
    stream = storage.stream
    size = 0
    rows = 0
    header = None
    try:
        stream.seek(0)
    except (AttributeError, ValueError):
        pass
    for chunk in _iter_chunks(stream):
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            raise ImportError_(
                f'file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB', 413
            )
        if header is None:
            text = chunk.decode('utf-8-sig', errors='replace')
            first_line = text.splitlines()[0] if text.splitlines() else ''
            header = [column.strip().lower() for column in first_line.split(',')]
            missing = [column for column in REQUIRED_COLUMNS if column not in header]
            if missing:
                raise ImportError_(f'Missing columns: {", ".join(missing)}', 400)
        rows += chunk.count(b'\n')
    try:
        stream.seek(0)          # the caller still has to write the file out
    except (AttributeError, ValueError):
        pass
    if header is None:
        raise ImportError_('CSV file is empty', 400)
    # A trailing newline over-counts by one; a header-only file has none.
    data_rows = max(rows - 1, 0)
    if data_rows == 0:
        raise ImportError_('CSV file has a header but no data rows', 400)
    if data_rows > MAX_ROWS:
        raise ImportError_(f'file has {data_rows} rows; the limit is {MAX_ROWS}', 413)
    return ','.join(header), data_rows


def _iter_chunks(stream, size=64 * 1024):
    while True:
        chunk = stream.read(size)
        if not chunk:
            return
        yield chunk


def create_job(storage, created_by) -> dict:
    """Store the upload, record a ``pending`` job and return it."""
    get_db, _ = _from_app()
    header, data_rows = inspect_upload(storage)
    directory = upload_dir()
    os.makedirs(directory, exist_ok=True)
    conn = get_db()
    job_id = _job_id(conn)
    path = os.path.join(directory, f'users-{job_id}.csv')
    try:
        if hasattr(storage, 'save'):
            storage.save(path)
        else:
            with open(path, 'wb') as handle:
                for chunk in _iter_chunks(storage.stream):
                    handle.write(chunk)
        conn.execute(
            "INSERT INTO import_jobs (job_id, job_type, status, filename, stored_path, "
            "total_rows, created_by, created_at) VALUES (?, 'users', 'pending', ?, ?, ?, ?, ?)",
            [job_id, os.path.basename(getattr(storage, 'filename', '') or 'users.csv'),
             path, data_rows, created_by, datetime.now()],
        )
    finally:
        conn.close()
    return _read_job(get_db(), job_id)


def _read_job(conn, job_id) -> dict | None:
    if not _table_exists(conn):
        return None
    row = conn.execute(
        "SELECT job_id, job_type, status, filename, total_rows, processed_rows, imported, "
        "skipped, error_summary, created_by, created_at, started_at, finished_at, failure_reason "
        "FROM import_jobs WHERE job_id = ?",
        [job_id],
    ).fetchone()
    return _as_job(row) if row else None


def _as_job(row) -> dict:
    errors = row[8]
    if isinstance(errors, str):
        try:
            errors = json.loads(errors)
        except (TypeError, ValueError):
            errors = []
    return {
        'job_id': row[0],
        'job_type': row[1],
        'status': row[2],
        'filename': row[3],
        'total_rows': row[4],
        'processed_rows': row[5],
        'imported': row[6],
        'skipped': row[7],
        'errors': errors or [],
        'created_by': row[9],
        'created_at': _iso(row[10]),
        'started_at': _iso(row[11]),
        'finished_at': _iso(row[12]),
        'failure_reason': row[13],
    }


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else (value or None)


def get_job(job_id) -> dict | None:
    get_db, _ = _from_app()
    conn = get_db()
    try:
        return _read_job(conn, job_id)
    finally:
        conn.close()


def list_jobs(limit=20) -> list[dict]:
    get_db, _ = _from_app()
    conn = get_db()
    try:
        if not _table_exists(conn):
            return []
        rows = conn.execute(
            "SELECT job_id, job_type, status, filename, total_rows, processed_rows, imported, "
            "skipped, error_summary, created_by, created_at, started_at, finished_at, failure_reason "
            "FROM import_jobs ORDER BY job_id DESC LIMIT ?",
            [max(1, min(int(limit), 100))],
        ).fetchall()
        return [_as_job(row) for row in rows]
    finally:
        conn.close()


def cancel_job(job_id, actor_emp_id) -> dict | None:
    """Cancel a job that has not started yet."""
    get_db, _ = _from_app()
    conn = get_db()
    try:
        # Read the state first: DuckDB does not report a reliable rowcount for a
        # no-op UPDATE, and "was it already cancelled?" is the whole question.
        row = conn.execute(
            'SELECT status, stored_path FROM import_jobs WHERE job_id = ?', [job_id]
        ).fetchone()
        if not row or row[0] != PENDING:
            return None
        conn.execute(
            "UPDATE import_jobs SET status = 'cancelled', finished_at = ?, failure_reason = ? "
            "WHERE job_id = ?",
            [datetime.now(), f'cancelled by {actor_emp_id}', job_id],
        )
        job = _read_job(conn, job_id)
    finally:
        conn.close()
    _discard_file(row[1])
    return job


def dispatch_job(job_id) -> dict | None:
    """Claim and process one *specific* job now (the "run it" button).

    The same conditional claim as :func:`dispatch_once`, so it is safe when the
    scheduler tick races this call: whoever wins the transition does the work
    and the other one gets the finished job back.
    """
    get_db, _ = _from_app()
    conn = get_db()
    if not _table_exists(conn):
        conn.close()
        return None
    now = datetime.now()
    try:
        row = conn.execute(
            "SELECT job_id, stored_path, status FROM import_jobs WHERE job_id = ?", [job_id]
        ).fetchone()
        if not row:
            return None
        if row[2] in TERMINAL:
            return _read_job(conn, job_id)          # already finished
        claimed = conn.execute(
            "UPDATE import_jobs SET status = 'running', started_at = ? "
            "WHERE job_id = ? AND status = 'pending'",
            [now, job_id],
        )
        if not claimed.rowcount:
            return _read_job(conn, job_id)          # the scheduler won the race
        path = row[1]
    finally:
        conn.close()
    try:
        _process(job_id, path)
    except Exception as exc:
        _fail(job_id, f'{type(exc).__name__}: {str(exc)[:400]}')
        _discard_file(path)
    conn = get_db()
    try:
        return _read_job(conn, job_id)
    finally:
        conn.close()


def dispatch_once() -> str | None:
    """Claim and process at most one job. Returns the processed job id.

    The claim is a single conditional UPDATE, so concurrent dispatchers (one per
    gunicorn worker) cannot pick up the same job.
    """
    get_db, _ = _from_app()
    conn = get_db()
    if not _table_exists(conn):
        conn.close()
        return None
    now = datetime.now()
    stale_before = now - timedelta(minutes=STALE_JOB_MINUTES)
    try:
        conn.execute(
            "UPDATE import_jobs SET status = 'pending' WHERE status = 'running' AND started_at < ?",
            [stale_before],
        )
        pending = conn.execute(
            "SELECT job_id, stored_path FROM import_jobs WHERE status = 'pending' "
            "ORDER BY job_id LIMIT 1"
        ).fetchone()
        if not pending:
            return None
        claimed = conn.execute(
            "UPDATE import_jobs SET status = 'running', started_at = ? "
            "WHERE job_id = ? AND status = 'pending'",
            [now, pending[0]],
        )
        if not claimed.rowcount:
            return None            # another worker won the race
        job_id, path = pending[0], pending[1]
    finally:
        conn.close()
    try:
        _process(job_id, path)
    except Exception as exc:                       # never kill the scheduler
        _fail(job_id, f'{type(exc).__name__}: {str(exc)[:400]}')
        _discard_file(path)
    return job_id


def _progress(job_id, processed, imported, skipped):
    """Publish progress so a long job is observable while it runs."""
    get_db, _ = _from_app()
    conn = get_db()
    try:
        conn.execute(
            "UPDATE import_jobs SET processed_rows = ?, imported = ?, skipped = ? WHERE job_id = ?",
            [processed, imported, skipped, job_id],
        )
    finally:
        conn.close()


def _fail(job_id, reason):
    get_db, _ = _from_app()
    conn = get_db()
    try:
        conn.execute(
            "UPDATE import_jobs SET status = 'failed', failure_reason = ?, finished_at = ? "
            "WHERE job_id = ?",
            [reason, datetime.now(), job_id],
        )
    finally:
        conn.close()


def _discard_file(path):
    """Delete the stored upload: it holds every employee id and address."""
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def _process(job_id, path):
    """Import one row at a time, recording progress as it goes."""
    import pandas as pd

    from app import UserValidationError, _csv_value, _validate_user_payload, app, audit_log, get_db, hash_password

    conn = get_db()
    password = hash_password('pass123')
    imported = skipped = 0
    errors = []
    processed = 0
    try:
        if not path or not os.path.isfile(path):
            raise ImportError_('the stored upload is missing; re-upload the file', 409)
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        for index, row in frame.iterrows():
            label = f'row {int(index) + 2}'          # +1 header, +1 to 1-base
            payload = {
                'emp_id': _csv_value(row, 'emp_id'),
                'name': _csv_value(row, 'name'),
                'email': _csv_value(row, 'email'),
                'role': _csv_value(row, 'role', 'Employee') or 'Employee',
                'department': _csv_value(row, 'department'),
            }
            try:
                normalized = _validate_user_payload(payload, creating=True)
            except UserValidationError as exc:
                skipped += 1
                if len(errors) < MAX_RECORDED_ERRORS:
                    errors.append(f'{label}: {exc}')
                continue
            if conn.execute('SELECT 1 FROM users WHERE UPPER(emp_id) = ?',
                            [normalized['emp_id']]).fetchone():
                skipped += 1
                if len(errors) < MAX_RECORDED_ERRORS:
                    errors.append(f"{label}: {normalized['emp_id']} already exists")
                continue
            if conn.execute('SELECT 1 FROM users WHERE LOWER(email) = ?',
                            [normalized['email']]).fetchone():
                skipped += 1
                if len(errors) < MAX_RECORDED_ERRORS:
                    errors.append(f"{label}: {normalized['email']} already exists")
                continue
            conn.execute(
                "INSERT INTO users (emp_id, name, email, password, role, department, status, "
                "first_login, created_at, allow_login, allow_breaks) "
                "VALUES (?, ?, ?, ?, ?, ?, 'Active', ?, ?, 1, 1)",
                [normalized['emp_id'], normalized['name'], normalized['email'], password,
                 normalized['role'], normalized['department'], datetime.now(), datetime.now()],
            )
            imported += 1
            processed += 1
            if processed % 25 == 0:
                _progress(job_id, processed, imported, skipped)
        conn.execute(
            "UPDATE import_jobs SET status = 'completed', processed_rows = ?, imported = ?, "
            "skipped = ?, error_summary = ?, finished_at = ? WHERE job_id = ?",
            [processed, imported, skipped, json.dumps(errors), datetime.now(), job_id],
        )
        job = _read_job(conn, job_id)
    finally:
        conn.close()
    # The upload held every employee id and address: do not leave it on disk.
    _discard_file(path)
    summary = job or {}
    with app.app_context():
        audit_log(
            summary.get('created_by') or 'SYSTEM',
            'USER_IMPORT_COMPLETED',
            f"CSV import {job_id}: {imported} imported, {skipped} skipped",
            actor=f"SYSTEM:import-job#{job_id}",
            entity='import_jobs',
            entity_id=job_id,
            after={
                'imported': imported,
                'skipped': skipped,
                'total_rows': summary.get('total_rows'),
            },
        )
