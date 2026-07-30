import logging
from flask import Blueprint, render_template, request, jsonify

from .db import get_db, _scalar
from .helpers import now_ist, parse_date
from .decorators import admin_required, hr_or_admin_required

logger = logging.getLogger('hrms')

analytics_bp = Blueprint('analytics', __name__)


def _common_filter_params():
    dept = request.args.get('department', '').strip()
    date_from = request.args.get('date_from', '').strip()
    date_to = request.args.get('date_to', '').strip()
    return dept, date_from, date_to


def _date_condition(date_from, date_to, column='start_date'):
    clauses = []
    params = []
    if date_from:
        clauses.append(f"{column} >= ?")
        params.append(parse_date(date_from) or now_ist().date())
    if date_to:
        clauses.append(f"{column} <= ?")
        params.append(parse_date(date_to) or now_ist().date())
    return clauses, params


def _dept_where_clause(dept, alias='u'):
    if dept and dept != 'All':
        return f" AND {alias}.department = ?", [dept]
    return "", []


@analytics_bp.route('/admin/analytics')
@hr_or_admin_required
def admin_analytics():
    return render_template('analytics.html')


@analytics_bp.route('/api/v1/analytics/headcount')
@analytics_bp.route('/api/analytics/headcount')
@admin_required
def analytics_headcount():
    dept, date_from, date_to = _common_filter_params()
    extra_where, extra_params = _dept_where_clause(dept, 'u')
    conn = get_db()
    total = _scalar(
        "SELECT COUNT(*) FROM users u WHERE u.role = 'Employee'" + extra_where,
        extra_params, conn=conn
    ) if extra_params else _scalar("SELECT COUNT(*) FROM users WHERE role = 'Employee'", conn=conn)
    dept_rows = conn.execute(
        "SELECT u.department, COUNT(*) FROM users u WHERE u.role = 'Employee' AND u.department IS NOT NULL{} GROUP BY u.department ORDER BY COUNT(*) DESC".format(extra_where),
        extra_params
    ).fetchall()
    conn.close()
    return jsonify({'total': total, 'by_department': [{'dept': r[0], 'count': r[1]} for r in dept_rows]}), 200


@analytics_bp.route('/api/v1/analytics/leave-trends')
@analytics_bp.route('/api/analytics/leave-trends')
@admin_required
def analytics_leave_trends():
    dept, date_from, date_to = _common_filter_params()
    if date_from or date_to:
        date_clauses, date_params = _date_condition(date_from, date_to, 'lr.start_date')
    else:
        months = request.args.get('months', 6, type=int)
        date_clauses = ["lr.start_date >= CURRENT_DATE - INTERVAL '{} months'".format(months)]
        date_params = []
    dept_join = " JOIN users u ON lr.emp_id = u.emp_id" if dept else ""
    dept_extra, dept_params = _dept_where_clause(dept) if dept else ("", [])
    date_where = " AND " + " AND ".join(date_clauses) if date_clauses else ""
    params = date_params + dept_params
    conn = get_db()
    rows = conn.execute(
        "SELECT strftime('%Y-%m', lr.start_date) as month, lr.leave_type, COUNT(*) as cnt"
        " FROM leave_requests lr{} WHERE lr.status = 'Approved'{}{}"
        " GROUP BY month, lr.leave_type ORDER BY month".format(dept_join, date_where, dept_extra),
        params
    ).fetchall()
    conn.close()
    return jsonify([{'month': r[0], 'type': r[1], 'count': r[2]} for r in rows]), 200


@analytics_bp.route('/api/v1/analytics/attrition-risk')
@analytics_bp.route('/api/analytics/attrition-risk')
@admin_required
def analytics_attrition():
    """
    Attrition-risk heuristic (before Phase 5):
      - leave_count: approved leaves last 3 months, weight ×0.5
      - reg_count: pending regularization (no time limit), weight ×2.0
      - early_break: breaks <5 min last 1 month, NOT in score
      - score = leave_count × 0.5 + reg_count × 2.0

    After Phase 5 refinement:
      - reg_count gets a 3-month lookback window
      - early_break added to score at ×1.0
      - attendance_rate added: (session days last 3mo / weekdays) inverted → (1 - rate) × 3
      - normalised weights: leave ×0.4, reg ×1.5, early_break ×0.8, attendance_deficit ×3.0
    """
    dept, date_from, date_to = _common_filter_params()
    dept_extra, dept_params = _dept_where_clause(dept)

    conn = get_db()
    rows = conn.execute("""
        SELECT u.emp_id, u.name, u.department, u.designation,
            COALESCE(lr.leave_count, 0) as leave_count,
            COALESCE(reg.reg_count, 0) as reg_count,
            COALESCE(eb.early_break, 0) as early_break,
            COALESCE(att.att_days, 0) as att_days,
            COALESCE(pr.rating, 0) as rating
        FROM users u
        LEFT JOIN (SELECT emp_id, COUNT(*) as leave_count FROM leave_requests WHERE status = 'Approved' AND start_date >= CURRENT_DATE - INTERVAL '3 months' GROUP BY emp_id) lr ON u.emp_id = lr.emp_id
        LEFT JOIN (SELECT emp_id, COUNT(*) as reg_count FROM regularization_requests WHERE status = 'Pending' AND created_at >= CURRENT_DATE - INTERVAL '3 months' GROUP BY emp_id) reg ON u.emp_id = reg.emp_id
        LEFT JOIN (SELECT emp_id, COUNT(*) as early_break FROM breaks WHERE break_date >= CURRENT_DATE - INTERVAL '1 months' AND duration_minutes < 5 GROUP BY emp_id) eb ON u.emp_id = eb.emp_id
        LEFT JOIN (SELECT s.emp_id, COUNT(DISTINCT CAST(s.login_time AS DATE)) as att_days FROM user_sessions s WHERE s.login_time >= CURRENT_DATE - INTERVAL '3 months' GROUP BY s.emp_id) att ON u.emp_id = att.emp_id
        LEFT JOIN (SELECT emp_id, AVG(overall_rating) as rating FROM performance_reviews WHERE status = 'Submitted' GROUP BY emp_id) pr ON u.emp_id = pr.emp_id
        WHERE u.role = 'Employee'{0}
        ORDER BY (
            COALESCE(lr.leave_count, 0) * 0.4 +
            COALESCE(reg.reg_count, 0) * 1.5 +
            COALESCE(eb.early_break, 0) * 0.8 +
            CASE WHEN COALESCE(att.att_days, 0) < 45
                 THEN (1.0 - COALESCE(att.att_days, 0) * 1.0 / 45.0) * 3.0
                 ELSE 0 END
        ) DESC LIMIT 20
    """.format(dept_extra), dept_params).fetchall()
    conn.close()

    result = []
    for r in rows:
        leave_count = r[4]
        reg_count = r[5]
        early_break = r[6]
        att_days = r[7]
        rating = r[8] or 0
        attendance_rate = min(att_days / 45.0, 1.0) if 45 > 0 else 1.0
        raw_score = leave_count * 0.4 + reg_count * 1.5 + early_break * 0.8 + max(0, 1.0 - (att_days / 45.0)) * 3.0

        result.append({
            'emp_id': r[0], 'name': r[1], 'department': r[2], 'designation': r[3],
            'leave_count': leave_count, 'reg_count': reg_count, 'early_break': early_break,
            'attendance_rate': round(attendance_rate, 2), 'rating': rating,
            'risk_score': round(raw_score, 1)
        })
    return jsonify(result), 200


@analytics_bp.route('/api/v1/analytics/expense-summary')
@analytics_bp.route('/api/analytics/expense-summary')
@admin_required
def analytics_expense_summary():
    dept, date_from, date_to = _common_filter_params()
    date_clauses, date_params = _date_condition(date_from, date_to, 'c.created_at')
    dept_join = " JOIN users u ON c.emp_id = u.emp_id" if dept else ""
    dept_extra, dept_params = _dept_where_clause(dept) if dept else ("", [])
    date_where = " AND " + " AND ".join(date_clauses) if date_clauses else ""
    params = date_params + dept_params
    conn = get_db()
    total = _scalar(
        "SELECT COALESCE(SUM(amount),0) FROM expense_claims c{} WHERE c.status IN ('Approved','Paid'){}{}".format(dept_join, date_where, dept_extra),
        params, conn=conn
    ) if params else _scalar("SELECT COALESCE(SUM(amount),0) FROM expense_claims WHERE status IN ('Approved','Paid')", conn=conn)
    by_cat = conn.execute(
        "SELECT e.name, COALESCE(SUM(c.amount),0) FROM expense_claims c"
        " JOIN expense_categories e ON c.cat_id = e.cat_id{}"
        " WHERE c.status IN ('Approved','Paid'){}{}"
        " GROUP BY e.name ORDER BY SUM(c.amount) DESC".format(dept_join, date_where, dept_extra),
        params
    ).fetchall()
    pending = _scalar(
        "SELECT COUNT(*) FROM expense_claims c{} WHERE c.status = 'Pending'{}{}".format(dept_join, date_where, dept_extra),
        params, conn=conn
    ) if params else _scalar("SELECT COUNT(*) FROM expense_claims WHERE status = 'Pending'", conn=conn)
    conn.close()
    return jsonify({'total': float(total), 'by_category': [{'cat': r[0], 'amount': float(r[1])} for r in by_cat], 'pending_claims': pending}), 200


@analytics_bp.route('/api/v1/analytics/performance-summary')
@analytics_bp.route('/api/analytics/performance-summary')
@hr_or_admin_required
def analytics_performance():
    dept, date_from, date_to = _common_filter_params()
    date_clauses, date_params = _date_condition(date_from, date_to, 'r.submitted_at')
    dept_extra, dept_params = _dept_where_clause(dept)
    date_where = " AND " + " AND ".join(date_clauses) if date_clauses else ""
    params = date_params + dept_params
    conn = get_db()
    avg_rating = _scalar(
        "SELECT COALESCE(AVG(r.overall_rating),0) FROM performance_reviews r"
        " JOIN users u ON r.emp_id = u.emp_id"
        " WHERE r.status = 'Submitted'{}{}".format(date_where, dept_extra),
        params, conn=conn
    ) if params else _scalar("SELECT COALESCE(AVG(overall_rating),0) FROM performance_reviews WHERE status = 'Submitted'", conn=conn)
    by_dept = conn.execute("""
        SELECT u.department, COALESCE(AVG(r.overall_rating),0)
        FROM performance_reviews r JOIN users u ON r.emp_id = u.emp_id
        WHERE r.status = 'Submitted' AND u.department IS NOT NULL{}{}
        GROUP BY u.department ORDER BY AVG(r.overall_rating) DESC
    """.format(date_where, dept_extra), params).fetchall()
    conn.close()
    return jsonify({'avg_rating': round(float(avg_rating), 2), 'by_department': [{'dept': r[0], 'avg': round(float(r[1]), 2)} for r in by_dept]}), 200
