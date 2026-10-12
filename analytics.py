"""FR-ANL-01 — materialised analytics snapshots.

The SRS asks for headcount, leave trends, expense summary and performance summary
"as v1.0, **served from materialized views refreshed on a schedule, with a
documented 'as of' timestamp on the response**". All four figures shipped, but they
were recomputed live on every request and carried no timestamp.

This module owns the materialisation. :func:`refresh` computes all four metrics
and stores them in ``analytics_snapshots``; a scheduler job calls it every 30
minutes; each route calls :func:`serve`, which returns the stored payload together
with the ``computed_at`` it was derived at. A read whose snapshot is missing or
older than :data:`MAX_AGE` recomputes live and **stores what it computed**, so a
fresh process warms itself and a missed tick degrades to a slightly more expensive
request rather than stale figures.

**A snapshot table, not PostgreSQL MATERIALIZED VIEWs — and deliberately.** The
observable contract the requirement asks for is unchanged: figures precomputed on a
schedule, carrying the "as of" timestamp they were derived at, rather than derived
per request. Two properties of this application make a table the right mechanism:

* the leave-trend metric takes a ``months`` query window, which a materialized view
  cannot parameterise — the snapshot stores the full monthly series and the route
  filters it;
* the app runs against two shapes (the compatibility schema and the v2.0 ``public``
  target), and a materialized view would have to be created and refreshed
  identically in ``init_db`` and in an Alembic revision, with a unique index added
  first if ``CONCURRENTLY`` were ever wanted.

The deviation is recorded in ``traceability.py`` rather than hidden, the same way
FR-AUTH-03's Redis-counter deviation is.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

#: How long a stored snapshot is trusted before a read recomputes it. The refresh
#: job runs more often than this, so a normally-running deployment always reads a
#: snapshot; the age check is the safety net for a missed tick and for a freshly
#: seeded database that has never been refreshed.
MAX_AGE = timedelta(minutes=45)

#: Leave trends are stored for a wider window than the default so the ``months``
#: query parameter can ask for more without forcing a recompute.
TREND_MONTHS = 24

METRICS = ('headcount', 'leave-trends', 'expense-summary', 'performance-summary')


def _compute(metric, conn):
    """The live figure for one metric. This is the v1.0 query, unchanged."""
    if metric == 'headcount':
        total = conn.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'Employee'"
        ).fetchone()[0]
        dept = conn.execute(
            "SELECT department, COUNT(*) FROM users WHERE role = 'Employee' "
            "AND department IS NOT NULL GROUP BY department ORDER BY COUNT(*) DESC"
        ).fetchall()
        return {'total': total,
                'by_department': [{'dept': r[0], 'count': r[1]} for r in dept]}

    if metric == 'leave-trends':
        rows = conn.execute(f"""
            SELECT strftime('%Y-%m', start_date) AS month, leave_type, COUNT(*) AS cnt
            FROM leave_requests WHERE status = 'Approved'
            AND start_date >= date('now', '-{TREND_MONTHS} months')
            GROUP BY month, leave_type ORDER BY month
        """).fetchall()
        return {'trends': [{'month': r[0], 'type': r[1], 'count': r[2]}
                           for r in rows]}

    if metric == 'expense-summary':
        total = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM expense_claims "
            "WHERE status IN ('Approved','Paid')"
        ).fetchone()[0]
        by_cat = conn.execute(
            "SELECT e.name, COALESCE(SUM(c.amount),0) FROM expense_claims c "
            "JOIN expense_categories e ON c.cat_id = e.cat_id "
            "WHERE c.status IN ('Approved','Paid') GROUP BY e.name "
            "ORDER BY SUM(c.amount) DESC"
        ).fetchall()
        pending = conn.execute(
            "SELECT COUNT(*) FROM expense_claims WHERE status = 'Pending'"
        ).fetchone()[0]
        return {'total': float(total),
                'by_category': [{'cat': r[0], 'amount': float(r[1])} for r in by_cat],
                'pending_claims': pending}

    if metric == 'performance-summary':
        avg = conn.execute(
            "SELECT COALESCE(AVG(overall_rating),0) FROM performance_reviews "
            "WHERE status = 'Submitted'"
        ).fetchone()[0]
        by_dept = conn.execute("""
            SELECT u.department, COALESCE(AVG(r.overall_rating),0)
            FROM performance_reviews r JOIN users u ON r.emp_id = u.emp_id
            WHERE r.status = 'Submitted' AND u.department IS NOT NULL
            GROUP BY u.department ORDER BY AVG(r.overall_rating) DESC
        """).fetchall()
        return {'avg_rating': round(float(avg), 2),
                'by_department': [{'dept': r[0], 'avg': round(float(r[1]), 2)}
                                  for r in by_dept]}

    raise KeyError(f'unknown analytics metric: {metric!r}')


def _read(conn, metric):
    """Return ``(payload, computed_at)`` for a stored snapshot, or ``None``."""
    row = conn.execute(
        "SELECT payload, computed_at FROM analytics_snapshots WHERE metric = ?",
        [metric],
    ).fetchone()
    if row is None:
        return None
    payload = row[0]
    if isinstance(payload, (str, bytes)):
        payload = json.loads(payload)
    return payload, row[1]


def _write(conn, metric, payload, when):
    """Upsert one snapshot. The payload is dumped so both shapes round-trip."""
    encoded = json.dumps(payload)
    conn.execute(
        "INSERT INTO analytics_snapshots (metric, payload, computed_at) "
        "VALUES (?, ?, ?) ON CONFLICT (metric) DO UPDATE SET "
        "payload = EXCLUDED.payload, computed_at = EXCLUDED.computed_at",
        [metric, encoded, when],
    )
    return when


def _months_ago(months):
    """The ``YYYY-MM`` string ``months`` calendar months before today.

    The trend series is monthly, so the window is month-granular: a request for
    ``months=6`` keeps the six calendar months up to and including the current one.
    """
    today = datetime.now()
    year, month = today.year, today.month - months
    while month <= 0:
        month += 12
        year -= 1
    return f'{year:04d}-{month:02d}'


def _window(metric, payload, months):
    """Apply the per-request window that a stored snapshot cannot itself carry."""
    if metric != 'leave-trends':
        return payload
    cutoff = _months_ago(months)
    return {'trends': [t for t in payload.get('trends', []) if t['month'] >= cutoff]}


def serve(metric, conn, months=6):
    """Return ``(payload, as_of, source)`` for one metric.

    ``source`` is ``'snapshot'`` when the stored figure was fresh enough to serve
    and ``'live'`` when it had to be (re)computed — which also warmed the snapshot.
    """
    stored = _read(conn, metric)
    now = datetime.now()
    if stored is not None:
        payload, computed_at = stored
        if computed_at is not None and (now - computed_at) <= MAX_AGE:
            return _window(metric, payload, months), computed_at, 'snapshot'
    payload = _compute(metric, conn)
    computed_at = _write(conn, metric, payload, now)
    return _window(metric, payload, months), computed_at, 'live'


def refresh(conn):
    """Recompute every metric and store it. Returns ``{metric: computed_at}``."""
    now = datetime.now()
    return {metric: _write(conn, metric, _compute(metric, conn), now)
            for metric in METRICS}
