"""Phase-2 service-layer cutover adapter (SRS v2.0 §14, Phase 2).

Runs app.py's existing DuckDB-flavoured SQL against PostgreSQL 17 without
touching the application's queries. It is a drop-in for the connection that
``get_db()`` returns, preserving DuckDB semantics:

* ``?`` placeholders  -> psycopg ``%s``  (only when parameters are passed;
  a literal ``%`` in the SQL then also becomes ``%%``, as psycopg requires)
* ``strftime('<f>', col)`` -> ``to_char(col, '<pg-f>')``  (DuckDB-only fn)
* ``PRAGMA ...`` statements -> no-op
* every statement autocommits (parity with ``duckdb.connect``), so errors
  never poison the connection and ``commit()`` is a no-op
* naive ``TIMESTAMP`` columns round-trip naive ``datetime`` objects, and
  DECIMAL/REAL/DATE/INTEGER map to the same Python types DuckDB returns

Schema layout
-------------
The Phase 0-1 work built the v2.0 target schema in ``public`` (from
``db/postgres_schema.sql`` via Alembic baseline + ETL; data lives there).
Phase 2 deliberately serves the *v1.0 data model* from a separate schema
(``legacy``) so the unchanged app code runs on PostgreSQL today, while the
v2.0 target stays intact in ``public`` for side-by-side diffing until the
Phase-3 service-layer rewrite switches ``APP_DB_SCHEMA`` to ``public``.

Run the existing unit suite against PostgreSQL::

    APP_DB=postgres DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/hrms \\
        python -m pytest tests/test_app.py -v

The test harness calls :func:`reset_schema` before importing ``app`` so each
run starts from a fresh ``legacy`` schema.
"""

from __future__ import annotations

import os
import re

import psycopg

DEFAULT_DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:55432/hrms"
)
DEFAULT_SCHEMA = os.getenv("APP_DB_SCHEMA", "legacy")

_SCHEMA_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# DuckDB strftime() specifier -> PostgreSQL to_char() template.
# app.py only uses %Y, %m and the combined %Y-%m; the rest map canonically.
_STRFTIME_SPEC = {
    "Y": "YYYY",  # year (4 digits)
    "y": "YY",    # year (2 digits)
    "m": "MM",    # month number
    "d": "DD",    # day of month
    "H": "HH24",  # hour (00-23)
    "I": "HH12",  # hour (01-12)
    "M": "MI",    # minute
    "S": "SS",    # second
    "p": "AM",    # AM/PM
    "B": "Month", # full month name (DuckDB %B)
}

_STRFTIME_CALL = re.compile(
    r"strftime\(\s*'([^']*)'\s*,\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)\s*\)"
)

# DuckDB's 2-arg date(): date('now' | 'YYYY-MM-DD', '<interval string>').
# Drain 'now' -> current statement timestamp, re-add the interval, cast to date.
_DATE_FN_CALL = re.compile(r"date\(\s*'([^']+)'\s*,\s*'([^']+)'\s*\)")


def database_url() -> str:
    """Return a DSN psycopg understands (strip the ``+psycopg`` driver suffix)."""
    return re.sub(r"^(postgres(?:ql)?)\+[A-Za-z0-9_.]+", r"\1", DEFAULT_DATABASE_URL)


def app_schema() -> str:
    return os.getenv("APP_DB_SCHEMA", DEFAULT_SCHEMA)


def _strftime_to_pg_format(fmt: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(fmt):
        ch = fmt[i]
        if ch != "%":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= len(fmt):
            raise ValueError(f"dangling '%' in strftime format {fmt!r}")
        spec = fmt[i + 1]
        if spec == "%":
            out.append("%")
        else:
            try:
                out.append(_STRFTIME_SPEC[spec])
            except KeyError:
                raise ValueError(
                    f"unsupported strftime specifier '%{spec}' in {fmt!r}; "
                    f"add a mapping in db_backend.py"
                ) from None
        i += 2
    return "".join(out)


def _translate_strftime(sql: str) -> str:
    def _repl(m: re.Match) -> str:
        return f"to_char({m.group(2)}, '{_strftime_to_pg_format(m.group(1))}')"

    return _STRFTIME_CALL.sub(_repl, sql)


def _translate_date_fn(sql: str) -> str:
    def _repl(m: re.Match) -> str:
        base, interval = m.group(1), m.group(2)
        if base == "now":
            base_expr = "CURRENT_TIMESTAMP"
        else:
            base_expr = f"DATE '{base}'"
        return f"({base_expr} + INTERVAL '{interval}')::DATE"

    return _DATE_FN_CALL.sub(_repl, sql)


_BOOLEAN_COL_CACHE: dict[str, frozenset[str]] = {}


def _boolean_columns(schema: str) -> frozenset[str]:
    """Boolean column names in ``schema``, introspected once per process."""
    if schema not in _BOOLEAN_COL_CACHE:
        cols: set[str] = set()
        try:
            with psycopg.connect(database_url(), autocommit=True) as ic:
                rows = ic.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = %s AND data_type = 'boolean'",
                    (schema,),
                ).fetchall()
                cols = {r[0] for r in rows}
        except Exception:
            cols = set()
        _BOOLEAN_COL_CACHE[schema] = frozenset(cols)
    return _BOOLEAN_COL_CACHE[schema]


def _rewrite_boolean_literals(sql: str, schema: str | None) -> str:
    """Coerce legacy smallint (0/1) predicates for schema BOOLEAN columns.

    The v2.0 target schema normalises flag columns to ``BOOLEAN`` (SRS CC-02)
    while the v1.0 app layer still compares/assigns ``0``/``1``. PostgreSQL
    rejects ``boolcol = 0`` outright, so for columns that are *actually*
    ``boolean`` in the connected schema we rewrite:

    * ``col = 0``      -> ``col = false``   (also ``col=1`` -> ``true``)
    * ``col = ?``      -> ``col = ?::boolean``  (int params 0/1 cast fine)

    This is a strict no-op everywhere else -- the ``legacy`` schema has no
    boolean columns, so Phase-2 behaviour is byte-identical.
    """
    if not schema:
        return sql
    bool_cols = _boolean_columns(schema)
    if not bool_cols:
        return sql
    for col in bool_cols:
        pattern = re.compile(
            rf"(?<!\w)({re.escape(col)})(?!\w)(\s*=\s*)([01](?!\d)|\?)", re.I
        )

        def _repl(m: re.Match) -> str:
            name, op, rhs = m.group(1), m.group(2), m.group(3)
            if rhs == "?":
                return f"{name}{op}?::boolean"
            return f"{name}{op}" + ("true" if rhs == "1" else "false")

        sql = pattern.sub(_repl, sql)
    return sql


def translate(sql: str, params, schema: str | None = None) -> str:
    """Rewrite DuckDB SQL for psycopg. ``params is None`` -> no parameter
    processing, matching psycopg's behaviour (raw passthrough)."""
    sql = _rewrite_boolean_literals(sql, schema)
    sql = _translate_date_fn(_translate_strftime(sql))
    if params is None:
        return sql
    sql = sql.replace("%", "%%")
    return sql.replace("?", "%s")


class DuckDBCompatConnection:
    """DuckDB-drop-in wrapper around a psycopg connection (autocommit=True)."""

    def __init__(self, pg_conn: "psycopg.Connection"):
        self._conn = pg_conn

    def execute(self, sql, params=None):
        conn = self._conn
        q = translate(sql, params, app_schema())
        if params is None:
            return conn.execute(q)
        return conn.execute(q, params)

    def executemany(self, sql, seq_of_params):
        with self._conn.cursor() as cur:
            return cur.executemany(translate(sql, True, app_schema()), seq_of_params)

    def commit(self):
        """Statements autocommit (DuckDB parity); commit() is a no-op."""
        try:
            self._conn.commit()
        except Exception:
            pass

    def rollback(self):
        try:
            self._conn.rollback()
        except Exception:
            pass

    def close(self):
        self._conn.close()


def connect():
    """Open a new autocommit connection pointed at the app schema.

    Mirrors ``get_db()`` semantics: a fresh connection per call, schema
    ensured to exist.
    """
    schema = app_schema()
    if not _SCHEMA_RE.fullmatch(schema):
        raise ValueError(f"invalid APP_DB_SCHEMA {schema!r}")
    conn = psycopg.connect(database_url(), autocommit=True)
    try:
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        conn.execute(f"SET search_path TO {schema}")
    except Exception:
        conn.close()
        raise
    return DuckDBCompatConnection(conn)


def reset_schema(schema: str | None = None) -> None:
    """Drop and recreate the app schema in the target database.

    Used by the test harness *before* importing ``app`` so each run starts
    from a clean ``legacy`` schema. ``public`` (the v2.0 target schema) is
    never touched.
    """
    schema = schema or app_schema()
    if not _SCHEMA_RE.fullmatch(schema):
        raise ValueError(f"invalid schema name {schema!r}")
    conn = psycopg.connect(database_url(), autocommit=True)
    try:
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.execute(f"CREATE SCHEMA {schema}")
    finally:
        conn.close()