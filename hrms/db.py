import os
import logging

import duckdb

logger = logging.getLogger('hrms')

_env_db = os.getenv('DB_FILE', '')
if _env_db:
    DB_FILE = _env_db
else:
    DB_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'hrms.duckdb')


class _PersistentConnection:
    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


_global_conn = None


def close_db():
    global _global_conn
    if _global_conn is not None:
        _global_conn._conn.close()
        _global_conn = None


def get_db():
    global _global_conn
    if _global_conn is None:
        _global_conn = _PersistentConnection(duckdb.connect(DB_FILE))
    return _global_conn


def health_status():
    return {
        'db_file': DB_FILE,
        'connected': _global_conn is not None,
        'exists': os.path.exists(DB_FILE),
    }


def _scalar(sql, params=None, conn=None):
    own_conn = conn is None
    if own_conn:
        conn = get_db()
    try:
        row = conn.execute(sql, params or []).fetchone()
        return row[0] if row else 0
    finally:
        if own_conn:
            conn.close()
