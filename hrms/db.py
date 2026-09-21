import logging
import os
import threading

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
        self._closed = False

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


_local = threading.local()
_conns = set()
_conns_lock = threading.Lock()


def close_db():
    global _conns
    with _conns_lock:
        conns = list(_conns)
        _conns = set()
    for c in conns:
        c._closed = True
        try:
            c._conn.close()
        except Exception:
            pass


def get_db():
    conn = getattr(_local, 'conn', None)
    if conn is None or conn._closed:
        conn = _PersistentConnection(duckdb.connect(DB_FILE))
        _local.conn = conn
        with _conns_lock:
            _conns.add(conn)
    return conn


def health_status():
    return {
        'db_file': DB_FILE,
        'connected': len(_conns) > 0,
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
