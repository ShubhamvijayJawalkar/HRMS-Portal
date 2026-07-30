import logging
from datetime import datetime, timedelta
from io import BytesIO
import pandas as pd
from flask import Blueprint, render_template, request, jsonify, session, send_file
from .db import get_db, _scalar
from .helpers import now_ist, gen_id, parse_date, _is_admin, _get_shift_start_dt, _get_shift_end_dt, _get_shift_date_for_dt, add_notification, audit_log
from .decorators import login_required, admin_required

logger = logging.getLogger('hrms')
attendance_bp = Blueprint('attendance', __name__)

@attendance_bp.route('/api/start-break', methods=['POST'])
@login_required
def start_break():
    data = request.get_json(silent=True) or {}
    break_type = data.get('break_type')
    emp_id = session['emp_id']
    if not break_type:
        return jsonify({'error': 'Break type required'}), 400
    conn = get_db()
    user = conn.execute("SELECT allow_breaks FROM users WHERE emp_id = ?", [emp_id]).fetchone()
    if user and not user[0]:
        conn.close()
        return jsonify({'error': 'Breaks not allowed'}), 403
    shift_start_dt = _get_shift_start_dt(emp_id, conn)
    bt = conn.execute("SELECT daily_limit_minutes FROM break_types WHERE break_type = ?", [break_type]).fetchone()
    if not bt:
        conn.close()
        return jsonify({'error': 'Invalid break type'}), 400
    limit = bt[0]
    if limit:
        today_total = _scalar("SELECT COALESCE(SUM(duration_minutes), 0) FROM breaks WHERE emp_id = ? AND break_type = ? AND start_time >= ? AND status = 'Completed'", [emp_id, break_type, shift_start_dt])
        if today_total >= limit:
            conn.close()
            return jsonify({'error': f'Daily limit of {limit} min reached for {break_type}'}), 400
    if break_type == 'Lunch':
        approved = conn.execute(
            "SELECT 1 FROM break_approvals WHERE emp_id = ? AND break_type = ? AND break_date = ? AND status IN ('Pending', 'Approved')",
            [emp_id, break_type, _get_shift_date_for_dt(emp_id, now_ist(), conn)]
        ).fetchone()
        if not approved:
            conn.close()
            return jsonify({'error': 'Lunch break requires manager approval'}), 403
    active = conn.execute("SELECT break_id FROM breaks WHERE emp_id = ? AND status = 'Active'", [emp_id]).fetchone()
    if active:
        conn.execute("UPDATE breaks SET end_time = ?, status = 'Completed' WHERE break_id = ?", [now_ist(), active[0]])
        conn.commit()
    break_id = gen_id()
    now = now_ist()
    shift_date = _get_shift_date_for_dt(emp_id, now, conn)
    conn.execute("INSERT INTO breaks (break_id, emp_id, break_type, start_time, break_date, status) VALUES (?, ?, ?, ?, ?, 'Active')", [break_id, emp_id, break_type, now, shift_date])
    conn.close()
    audit_log(emp_id, 'BREAK_START', f'{break_type} break started')
    return jsonify({'message': 'Break started', 'break_id': break_id, 'break_type': break_type}), 201

@attendance_bp.route('/api/end-break/<int:break_id>', methods=['POST'])
@login_required
def end_break(break_id):
    emp_id = session['emp_id']
    conn = get_db()
    info = conn.execute("SELECT start_time, break_type FROM breaks WHERE break_id = ? AND emp_id = ?", [break_id, emp_id]).fetchone()
    if not info:
        conn.close()
        return jsonify({'error': 'Break not found'}), 404
    end_time = now_ist()
    duration = int((end_time - info[0]).total_seconds() / 60)
    conn.execute("UPDATE breaks SET end_time = ?, duration_minutes = ?, status = 'Completed' WHERE break_id = ?", [end_time, duration, break_id])
    audit_log(emp_id, 'BREAK_END', f'{info[1]} break ended ({duration} min)')
    conn.close()
    return jsonify({'message': 'Break ended', 'duration_minutes': duration}), 200

@attendance_bp.route('/api/user-breaks')
@login_required
def get_user_breaks():
    emp_id = session['emp_id']
    conn = get_db()
    shift_start_dt = _get_shift_start_dt(emp_id, conn)
    breaks = conn.execute("SELECT break_id, break_type, start_time, end_time, duration_minutes, status FROM breaks WHERE emp_id = ? AND (status = 'Active' OR start_time >= ?) ORDER BY start_time DESC", [emp_id, shift_start_dt]).fetchall()
    conn.close()
    return jsonify([{'break_id': b[0], 'break_type': b[1], 'start_time': b[2].isoformat() + '+05:30' if b[2] else None, 'end_time': b[3].isoformat() + '+05:30' if b[3] else None, 'duration_minutes': b[4] or 0, 'status': b[5]} for b in breaks]), 200

@attendance_bp.route('/api/break-approvals', methods=['GET', 'POST'])
@login_required
def break_approvals_api():
    emp_id = session['emp_id']
    if request.method == 'GET':
        conn = get_db()
        if _is_admin(session.get('role')):
            rows = conn.execute("SELECT a.approval_id, a.emp_id, u.name, a.break_type, a.break_date, a.reason, a.status, a.approved_by, a.created_at FROM break_approvals a JOIN users u ON a.emp_id = u.emp_id ORDER BY a.created_at DESC").fetchall()
        else:
            rows = conn.execute("SELECT a.approval_id, a.emp_id, u.name, a.break_type, a.break_date, a.reason, a.status, a.approved_by, a.created_at FROM break_approvals a JOIN users u ON a.emp_id = u.emp_id WHERE a.emp_id = ? ORDER BY a.created_at DESC", [emp_id]).fetchall()
        conn.close()
        return jsonify([{'approval_id': r[0], 'emp_id': r[1], 'emp_name': r[2], 'break_type': r[3], 'break_date': r[4].isoformat() + '+05:30', 'reason': r[5], 'status': r[6], 'approved_by': r[7], 'created_at': r[8].isoformat() + '+05:30' if r[8] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    bt = data.get('break_type')
    if bt != 'Lunch':
        return jsonify({'error': 'Only Lunch breaks require approval'}), 400
    conn = get_db()
    if conn.execute("SELECT 1 FROM break_approvals WHERE emp_id = ? AND break_type = ? AND break_date = ? AND status = 'Pending'", [emp_id, bt, _get_shift_date_for_dt(emp_id, now_ist(), conn)]).fetchone():
        conn.close()
        return jsonify({'error': 'Pending approval already exists for today'}), 409
    aid = gen_id()
    shift_date = _get_shift_date_for_dt(emp_id, now_ist(), conn)
    conn.execute("INSERT INTO break_approvals (approval_id, emp_id, break_type, break_date, reason, status) VALUES (?, ?, ?, ?, ?, 'Pending')", [aid, emp_id, bt, shift_date, data.get('reason', '')])
    conn.close()
    return jsonify({'message': 'Lunch break approval requested', 'approval_id': aid}), 201

@attendance_bp.route('/api/break-approvals/<int:aid>/approve', methods=['POST'])
@login_required
def approve_break(aid):
    conn = get_db()
    row = conn.execute(
        "SELECT a.emp_id, a.break_type, a.break_date, u.manager_emp_id FROM break_approvals a LEFT JOIN users u ON a.emp_id = u.emp_id WHERE a.approval_id = ? AND a.status = 'Pending'",
        [aid]
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Approval request not found or already processed'}), 404
    manager_emp_id = row[3]
    current_user = session['emp_id']
    is_admin_or_hr = _is_admin(session.get('role')) or session.get('department') == 'HR'
    if manager_emp_id:
        if current_user != manager_emp_id and not is_admin_or_hr:
            conn.close()
            return jsonify({'error': 'Only the assigned manager can approve this break request'}), 403
    elif not is_admin_or_hr:
        conn.close()
        return jsonify({'error': 'Only HR/Admin can approve this break request'}), 403
    conn.execute("UPDATE break_approvals SET status = 'Approved', approved_by = ? WHERE approval_id = ?", [current_user, aid])
    audit_log(current_user, 'BREAK_APPROVE', f'Break approval {aid} for {row[0]} ({row[1]})')
    conn.close()
    add_notification(row[0], 'BREAK_APPROVED', f'Your {row[1]} break request has been approved.', '/regularization')
    return jsonify({'message': 'Break approved'}), 200

@attendance_bp.route('/api/break-approvals/<int:aid>/reject', methods=['POST'])
@login_required
def reject_break(aid):
    conn = get_db()
    row = conn.execute(
        "SELECT a.emp_id, a.break_type, u.manager_emp_id FROM break_approvals a LEFT JOIN users u ON a.emp_id = u.emp_id WHERE a.approval_id = ? AND a.status = 'Pending'",
        [aid]
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Approval request not found'}), 404
    manager_emp_id = row[2]
    current_user = session['emp_id']
    is_admin_or_hr = _is_admin(session.get('role')) or session.get('department') == 'HR'
    if manager_emp_id:
        if current_user != manager_emp_id and not is_admin_or_hr:
            conn.close()
            return jsonify({'error': 'Only the assigned manager can reject this break request'}), 403
    elif not is_admin_or_hr:
        conn.close()
        return jsonify({'error': 'Only HR/Admin can reject this break request'}), 403
    conn.execute("UPDATE break_approvals SET status = 'Rejected', approved_by = ? WHERE approval_id = ? AND status = 'Pending'", [current_user, aid])
    audit_log(current_user, 'BREAK_REJECT', f'Break approval {aid} for {row[0]} ({row[1]})')
    conn.close()
    add_notification(row[0], 'BREAK_REJECTED', f'Your {row[1]} break request has been rejected.', '/regularization')
    return jsonify({'message': 'Break rejected'}), 200

@attendance_bp.route('/api/break-types')
@login_required
def get_break_types():
    conn = get_db()
    types = conn.execute("SELECT break_type, daily_limit_minutes, description FROM break_types").fetchall()
    conn.close()
    return jsonify([{'break_type': t[0], 'daily_limit_minutes': t[1], 'description': t[2]} for t in types]), 200

@attendance_bp.route('/api/login-hours')
@login_required
def get_login_hours():
    emp_id = session['emp_id']
    conn = get_db()
    date_str = request.args.get('date', '')
    if date_str:
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            target_date = now_ist().date()
    else:
        target_date = now_ist().date()
    sessions = conn.execute("SELECT login_time, logout_time, total_hours, session_date FROM user_sessions WHERE emp_id = ? AND session_date = ? ORDER BY login_time ASC", [emp_id, target_date]).fetchall()
    conn.close()
    return jsonify([{'login_time': s[0].strftime('%H:%M:%S') if s[0] else 'N/A', 'logout_time': s[1].strftime('%H:%M:%S') if s[1] else 'Active', 'total_hours': float(s[2]) if s[2] else 0, 'session_date': s[3].isoformat() + '+05:30' if s[3] else None} for s in sessions]), 200

@attendance_bp.route('/api/user/shift-summary')
@login_required
def get_shift_summary():
    emp_id = session['emp_id']
    conn = get_db()
    date_str = request.args.get('date', '')
    if date_str:
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            target_date = now_ist().date()
    else:
        target_date = now_ist().date()
    shift_start_dt = _get_shift_start_dt(emp_id, conn, target_date)
    shift_end_dt = _get_shift_end_dt(emp_id, shift_start_dt, conn)
    sessions = conn.execute("SELECT login_time, logout_time, total_hours, session_date FROM user_sessions WHERE emp_id = ? AND session_date = ? ORDER BY login_time ASC", [emp_id, target_date]).fetchall()
    breaks = conn.execute("SELECT break_type, start_time, end_time, duration_minutes, status FROM breaks WHERE emp_id = ? AND break_date = ? ORDER BY start_time ASC", [emp_id, target_date]).fetchall()
    conn.close()
    total_session_hours = sum(float(s[2]) for s in sessions if s[2])
    total_break_minutes = sum(int(b[3]) for b in breaks if b[3])
    session_count = len(sessions)
    first_login = sessions[0][0] if sessions else None
    last_logout = None
    for s in reversed(sessions):
        if s[1]:
            last_logout = s[1]
            break
    shift_hours = 0
    if first_login and last_logout:
        shift_hours = round((last_logout - first_login).total_seconds() / 3600, 2)
    elif first_login and not last_logout:
        shift_hours = round((now_ist() - first_login).total_seconds() / 3600, 2)
    productive_hours = max(0, shift_hours - total_break_minutes / 60)
    efficiency = round((productive_hours / shift_hours) * 100, 1) if shift_hours > 0 else 0
    return jsonify({'date': target_date.isoformat() + '+05:30', 'shift_start': shift_start_dt.strftime('%H:%M'), 'shift_end': shift_end_dt.strftime('%H:%M'), 'first_login': first_login.strftime('%H:%M:%S') if first_login else None, 'last_logout': last_logout.strftime('%H:%M:%S') if last_logout else None, 'shift_hours': shift_hours, 'total_session_hours': total_session_hours, 'total_break_minutes': total_break_minutes, 'productive_hours': round(productive_hours, 2), 'efficiency': efficiency, 'session_count': session_count, 'break_count': len(breaks)}), 200

@attendance_bp.route('/api/user/calendar')
@login_required
def get_user_calendar():
    emp_id = session['emp_id']
    month = int(request.args.get('month', now_ist().month))
    year = int(request.args.get('year', now_ist().year))
    start_date = datetime(year, month, 1).date()
    if month == 12:
        end_date = datetime(year + 1, 1, 1).date() - timedelta(days=1)
    else:
        end_date = datetime(year, month + 1, 1).date() - timedelta(days=1)
    conn = get_db()
    sessions = conn.execute("SELECT session_date, login_time, logout_time, total_hours FROM user_sessions WHERE emp_id = ? AND session_date BETWEEN ? AND ? ORDER BY session_date, login_time", [emp_id, start_date, end_date]).fetchall()
    breaks = conn.execute("SELECT break_date, break_type, start_time, end_time, duration_minutes, status FROM breaks WHERE emp_id = ? AND break_date BETWEEN ? AND ? ORDER BY break_date, start_time", [emp_id, start_date, end_date]).fetchall()
    leaves = conn.execute("SELECT start_date, end_date, leave_type, status FROM leave_requests WHERE emp_id = ? AND start_date <= ? AND end_date >= ? ORDER BY start_date", [emp_id, end_date, start_date]).fetchall()
    holidays = conn.execute("SELECT holiday_date, name FROM holidays WHERE holiday_date BETWEEN ? AND ? ORDER BY holiday_date", [start_date, end_date]).fetchall()
    user_row = conn.execute("SELECT shift_start, shift_end FROM users WHERE emp_id = ?", [emp_id]).fetchone()
    conn.close()
    sess_map = {}
    for s in sessions:
        d = s[0].isoformat() + '+05:30' if s[0] else None
        if not d: continue
        if d not in sess_map: sess_map[d] = []
        sess_map[d].append({'login': s[1].strftime('%H:%M') if s[1] else None, 'logout': s[2].strftime('%H:%M') if s[2] else 'Active', 'hours': float(s[3]) if s[3] else 0})
    brk_map = {}
    for b in breaks:
        d = b[0].isoformat() + '+05:30' if b[0] else None
        if not d: continue
        if d not in brk_map: brk_map[d] = []
        brk_map[d].append({'type': b[1], 'start': b[2].strftime('%H:%M') if b[2] else None, 'end': b[3].strftime('%H:%M') if b[3] else None, 'minutes': int(b[4]) if b[4] else 0, 'status': b[5]})
    leave_map = {}
    for l in leaves:
        ld_start, ld_end, leave_type, leave_status = l[0], l[1], l[2], l[3]
        current = ld_start
        while current <= ld_end:
            leave_map[current.isoformat() + '+05:30'] = {'type': leave_type, 'status': leave_status}
            current += timedelta(days=1)
    holiday_map = {}
    for h in holidays:
        d = h[0].isoformat() + '+05:30' if h[0] else None
        if d: holiday_map[d] = h[1]
    shift_start = user_row[0] if user_row and user_row[0] else None
    shift_end = user_row[1] if user_row and user_row[1] else None
    return jsonify({'sessions': sess_map, 'breaks': brk_map, 'leaves': leave_map, 'holidays': holiday_map, 'shift_start': shift_start, 'shift_end': shift_end, 'month': month, 'year': year}), 200

@attendance_bp.route('/api/live-monitoring')
@admin_required
def live_monitoring():
    conn = get_db()
    now = now_ist()
    all_employees = conn.execute("SELECT emp_id FROM users WHERE role = 'Employee'").fetchall()
    shift_starts = [_get_shift_start_dt(eid, conn) for (eid,) in all_employees]
    earliest = min(shift_starts) if shift_starts else now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = conn.execute("SELECT u.emp_id, u.name, u.department, b.break_type, b.start_time, b.status FROM breaks b JOIN users u ON b.emp_id = u.emp_id WHERE b.status = 'Active' AND b.start_time >= ? ORDER BY b.start_time DESC", [earliest]).fetchall()
    conn.close()
    return jsonify([{'emp_id': r[0], 'employee_name': r[1], 'department': r[2], 'break_type': r[3], 'start_time': r[4].strftime('%H:%M:%S'), 'status': r[5]} for r in rows]), 200

@attendance_bp.route('/api/break-summary')
@admin_required
def get_break_summary():
    conn = get_db()
    now = now_ist()
    all_employees = conn.execute("SELECT emp_id FROM users WHERE role = 'Employee'").fetchall()
    shift_starts = [_get_shift_start_dt(eid, conn) for (eid,) in all_employees]
    earliest = min(shift_starts) if shift_starts else now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = conn.execute("SELECT u.emp_id, u.name, u.department, COUNT(CASE WHEN b.status = 'Active' THEN 1 END), COUNT(CASE WHEN b.status = 'Completed' THEN 1 END), SUM(CASE WHEN b.status = 'Completed' THEN b.duration_minutes ELSE 0 END) FROM users u LEFT JOIN breaks b ON u.emp_id = b.emp_id AND b.start_time >= ? WHERE u.role = 'Employee' GROUP BY u.emp_id, u.name, u.department ORDER BY u.name", [earliest]).fetchall()
    conn.close()
    return jsonify([{'emp_id': r[0], 'employee_name': r[1], 'department': r[2], 'active_breaks': int(r[3] or 0), 'completed_breaks': int(r[4] or 0), 'total_break_minutes': int(r[5] or 0)} for r in rows]), 200

@attendance_bp.route('/api/disposed-breaks')
@admin_required
def get_disposed_breaks():
    one_hour_ago = now_ist() - timedelta(hours=1)
    conn = get_db()
    all_employees = conn.execute("SELECT emp_id FROM users WHERE role = 'Employee'").fetchall()
    shift_starts = [_get_shift_start_dt(eid, conn) for (eid,) in all_employees]
    earliest_shift = min(shift_starts) if shift_starts else now_ist().replace(hour=0, minute=0, second=0, microsecond=0)
    current_shift_date = earliest_shift.date()
    rows = conn.execute("SELECT u.emp_id, u.name, u.department, b.break_type, b.start_time, b.end_time, b.duration_minutes, b.status FROM breaks b JOIN users u ON b.emp_id = u.emp_id WHERE b.status = 'Completed' AND b.end_time >= ? AND b.break_date = ? ORDER BY b.end_time DESC", [one_hour_ago, current_shift_date]).fetchall()
    conn.close()
    return jsonify([{'emp_id': r[0], 'employee_name': r[1], 'department': r[2], 'break_type': r[3], 'start_time': r[4].strftime('%H:%M:%S'), 'end_time': r[5].strftime('%H:%M:%S'), 'duration_minutes': r[6] or 0, 'status': r[7]} for r in rows]), 200

@attendance_bp.route('/api/dashboard-stats')
@admin_required
def get_dashboard_stats():
    conn = get_db()
    now = now_ist()
    total = _scalar("SELECT COUNT(*) FROM users WHERE role = 'Employee'")
    all_employees = conn.execute("SELECT emp_id FROM users WHERE role = 'Employee'").fetchall()
    shift_starts = [_get_shift_start_dt(eid, conn) for (eid,) in all_employees]
    shift_date = min(shift_starts).date() if shift_starts else now.date()
    logged_in_today = _scalar("SELECT COUNT(DISTINCT emp_id) FROM user_sessions WHERE session_date = ?", [shift_date])
    on_break = _scalar("SELECT COUNT(DISTINCT emp_id) FROM breaks WHERE status = 'Active' AND break_date = ?", [shift_date])
    blocked = _scalar("SELECT COUNT(*) FROM users WHERE status = 'Blocked'")
    pending_leaves = _scalar("SELECT COUNT(*) FROM leave_requests WHERE status = 'Pending'")
    conn.close()
    return jsonify({'total_employees': total, 'logged_in_today': logged_in_today, 'on_break': on_break, 'blocked_users': blocked, 'pending_leaves': pending_leaves}), 200

@attendance_bp.route('/api/admin/breaks')
@admin_required
def admin_breaks():
    conn = get_db()
    all_employees = conn.execute("SELECT emp_id FROM users WHERE role = 'Employee'").fetchall()
    shift_starts = [_get_shift_start_dt(eid, conn) for (eid,) in all_employees]
    shift_date = min(shift_starts).date() if shift_starts else now_ist().date()
    active = conn.execute("SELECT b.break_id, u.name, b.break_type, b.start_time FROM breaks b JOIN users u ON b.emp_id = u.emp_id WHERE b.status = 'Active' AND b.break_date = ? ORDER BY b.start_time DESC", [shift_date]).fetchall()
    disposed = conn.execute("SELECT u.name, b.break_type, b.duration_minutes, b.end_time FROM breaks b JOIN users u ON b.emp_id = u.emp_id WHERE b.status = 'Completed' AND b.break_date = ? ORDER BY b.end_time DESC LIMIT 20", [shift_date]).fetchall()
    summary = conn.execute("SELECT break_type, COUNT(*), AVG(duration_minutes), MAX(duration_minutes), COUNT(DISTINCT emp_id) FROM breaks WHERE break_date = ? AND status = 'Completed' GROUP BY break_type", [shift_date]).fetchall()
    conn.close()
    return jsonify({'active_breaks': [{'break_id': r[0], 'emp_name': r[1], 'break_type': r[2], 'duration': int((now_ist() - r[3]).total_seconds() / 60) if r[3] else 0} for r in active], 'disposed_breaks': [{'emp_name': r[0], 'break_type': r[1], 'duration': r[2] or 0, 'end_time': r[3].strftime('%H:%M') if r[3] else ''} for r in disposed], 'break_summary': [{'break_type': r[0], 'count': r[1], 'avg_duration': float(r[2] or 0), 'max_duration': float(r[3] or 0), 'employees': r[4]} for r in summary]}), 200

@attendance_bp.route('/api/admin/dispose-break/<int:break_id>', methods=['POST'])
@admin_required
def admin_dispose_break(break_id):
    conn = get_db()
    info = conn.execute("SELECT start_time FROM breaks WHERE break_id = ? AND status = 'Active'", [break_id]).fetchone()
    if not info:
        conn.close()
        return jsonify({'error': 'Break not found or already ended'}), 404
    end_time = now_ist()
    duration = int((end_time - info[0]).total_seconds() / 60)
    conn.execute("UPDATE breaks SET end_time = ?, duration_minutes = ?, status = 'Completed' WHERE break_id = ?", [end_time, duration, break_id])
    audit_log(session['emp_id'], 'BREAK_DISPOSE', f'Admin disposed break {break_id} ({duration} min)')
    conn.close()
    return jsonify({'message': 'Break ended by admin', 'duration_minutes': duration}), 200

@attendance_bp.route('/api/v1/regularization', methods=['GET', 'POST'])
@attendance_bp.route('/api/regularization', methods=['GET', 'POST'])
@login_required
def regularization_api():
    emp_id = session['emp_id']
    is_admin = _is_admin(session.get('role'))
    if request.method == 'GET':
        status_filter = request.args.get('status', '').strip()
        month_filter = request.args.get('month', '', type=str).strip()
        pending_my_approval = request.args.get('pending_my_approval', '').lower() == 'true'
        conn = get_db()
        conditions = []
        params = []
        if pending_my_approval:
            rows = conn.execute("""SELECT r.request_id, r.emp_id, u.name, r.request_date, r.reason, r.status, r.approved_by, r.created_at
                FROM regularization_requests r JOIN users u ON r.emp_id = u.emp_id
                WHERE u.manager_emp_id = ? AND r.status = 'Pending'
                ORDER BY r.request_date DESC, r.created_at DESC""", [emp_id]).fetchall()
            conn.close()
            return jsonify([{'id': r[0], 'emp_id': r[1], 'name': r[2] or r[1], 'date': r[3].isoformat() if r[3] else None, 'reason': r[4], 'status': r[5], 'approved_by': r[6], 'created_at': r[7].isoformat() if r[7] else None} for r in rows]), 200
        elif is_admin:
            if status_filter:
                conditions.append("r.status = ?")
                params.append(status_filter)
            if month_filter and '-' in month_filter:
                y, m = month_filter.split('-', 1)
                conditions.append("CAST(strftime('%m', r.request_date) AS INTEGER) = ? AND CAST(strftime('%Y', r.request_date) AS INTEGER) = ?")
                params.extend([int(m), int(y)])
            where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
            rows = conn.execute(f"SELECT r.request_id, r.emp_id, u.name, r.request_date, r.reason, r.status, r.approved_by, r.created_at FROM regularization_requests r LEFT JOIN users u ON r.emp_id = u.emp_id{where} ORDER BY r.request_date DESC, r.created_at DESC", params).fetchall()
            stats = conn.execute(f"SELECT r.status, COUNT(*) FROM regularization_requests r{where} GROUP BY r.status", params).fetchall()
            conn.close()
            status_counts = {s[0]: s[1] for s in stats}
            return jsonify({'requests': [{'id': r[0], 'emp_id': r[1], 'name': r[2] or r[1], 'date': r[3].isoformat() if r[3] else None, 'reason': r[4], 'status': r[5], 'approved_by': r[6], 'created_at': r[7].isoformat() if r[7] else None} for r in rows], 'counts': {'pending': status_counts.get('Pending', 0), 'approved': status_counts.get('Approved', 0), 'rejected': status_counts.get('Rejected', 0), 'cancelled': status_counts.get('Cancelled', 0), 'total': sum(status_counts.values())}}), 200
        else:
            emp_conditions = ["emp_id = ?"]
            emp_params = [emp_id]
            if month_filter and '-' in month_filter:
                y, m = month_filter.split('-', 1)
                emp_conditions.append("CAST(strftime('%m', request_date) AS INTEGER) = ? AND CAST(strftime('%Y', request_date) AS INTEGER) = ?")
                emp_params.extend([int(m), int(y)])
            emp_where = " WHERE " + " AND ".join(emp_conditions)
            rows = conn.execute(f"SELECT request_id, emp_id, request_date, reason, status, approved_by, created_at FROM regularization_requests{emp_where} ORDER BY request_date DESC, created_at DESC", emp_params).fetchall()
            conn.close()
            return jsonify([{'id': r[0], 'emp_id': r[1], 'date': r[2].isoformat() if r[2] else None, 'reason': r[3], 'status': r[4], 'approved_by': r[5], 'created_at': r[6].isoformat() if r[6] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    d = parse_date(data.get('date'))
    if not d or not data.get('reason'):
        return jsonify({'error': 'date and reason required'}), 400
    from datetime import date as _date
    if d > _date.today():
        return jsonify({'error': 'Cannot regularize a future date'}), 400
    conn = get_db()
    if conn.execute("SELECT 1 FROM regularization_requests WHERE emp_id = ? AND request_date = ? AND status = 'Pending'", [emp_id, d]).fetchone():
        conn.close()
        return jsonify({'error': 'A pending request already exists for this date'}), 409
    rid = gen_id()
    conn.execute("INSERT INTO regularization_requests (request_id, emp_id, request_date, reason, status) VALUES (?, ?, ?, ?, 'Pending')", [rid, emp_id, d, data['reason']])
    conn.close()
    return jsonify({'message': 'Request submitted', 'id': rid}), 201
@attendance_bp.route('/api/v1/regularization/<int:rid>/approve', methods=['POST'])
@attendance_bp.route('/api/regularization/<int:rid>/approve', methods=['POST'])
@login_required
def approve_regularization(rid):
    conn = get_db()
    row = conn.execute(
        "SELECT r.emp_id, u.manager_emp_id FROM regularization_requests r LEFT JOIN users u ON r.emp_id = u.emp_id WHERE r.request_id = ? AND r.status = 'Pending'",
        [rid]
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Request not found or already processed'}), 404
    manager_emp_id = row[1]
    current_user = session['emp_id']
    is_admin_or_hr = _is_admin(session.get('role')) or session.get('department') == 'HR'
    if manager_emp_id:
        if current_user != manager_emp_id and not is_admin_or_hr:
            conn.close()
            return jsonify({'error': 'Only the assigned manager can approve this request'}), 403
    elif not is_admin_or_hr:
        conn.close()
        return jsonify({'error': 'Only HR/Admin can approve this request'}), 403
    conn.execute("UPDATE regularization_requests SET status = 'Approved', approved_by = ?, updated_at = ? WHERE request_id = ?", [current_user, now_ist(), rid])
    conn.close()
    audit_log(current_user, 'REGULARIZATION_APPROVE', f'Regularization {rid} approved')
    add_notification(row[0], 'REGULARIZATION_APPROVED', 'Your regularization request has been approved.', '/regularization')
    return jsonify({'message': 'Approved'}), 200


@attendance_bp.route('/api/v1/regularization/<int:rid>/reject', methods=['POST'])
@attendance_bp.route('/api/regularization/<int:rid>/reject', methods=['POST'])
@login_required
def reject_regularization(rid):
    conn = get_db()
    row = conn.execute(
        "SELECT r.emp_id, u.manager_emp_id FROM regularization_requests r LEFT JOIN users u ON r.emp_id = u.emp_id WHERE r.request_id = ? AND r.status = 'Pending'",
        [rid]
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Request not found or already processed'}), 404
    manager_emp_id = row[1]
    current_user = session['emp_id']
    is_admin_or_hr = _is_admin(session.get('role')) or session.get('department') == 'HR'
    if manager_emp_id:
        if current_user != manager_emp_id and not is_admin_or_hr:
            conn.close()
            return jsonify({'error': 'Only the assigned manager can reject this request'}), 403
    elif not is_admin_or_hr:
        conn.close()
        return jsonify({'error': 'Only HR/Admin can reject this request'}), 403
    conn.execute("UPDATE regularization_requests SET status = 'Rejected', approved_by = ?, updated_at = ? WHERE request_id = ?", [current_user, now_ist(), rid])
    conn.close()
    audit_log(current_user, 'REGULARIZATION_REJECT', f'Regularization {rid} rejected')
    add_notification(row[0], 'REGULARIZATION_REJECTED', 'Your regularization request has been rejected.', '/regularization')
    return jsonify({'message': 'Rejected'}), 200

@attendance_bp.route('/api/v1/regularization/<int:rid>/cancel', methods=['POST'])
@attendance_bp.route('/api/regularization/<int:rid>/cancel', methods=['POST'])
@login_required
def cancel_regularization(rid):
    emp_id = session['emp_id']
    is_admin = _is_admin(session.get('role'))
    conn = get_db()
    row = conn.execute("SELECT emp_id, status FROM regularization_requests WHERE request_id = ?", [rid]).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Request not found'}), 404
    if row[0] != emp_id and not is_admin:
        conn.close()
        return jsonify({'error': 'Not authorized'}), 403
    if row[1] != 'Pending':
        conn.close()
        return jsonify({'error': 'Only pending requests can be cancelled'}), 400
    conn.execute("UPDATE regularization_requests SET status = 'Cancelled', updated_at = ? WHERE request_id = ?", [now_ist(), rid])
    conn.close()
    return jsonify({'message': 'Cancelled'}), 200

@attendance_bp.route('/api/v1/regularization/export', methods=['GET'])
@attendance_bp.route('/api/regularization/export', methods=['GET'])
@admin_required
def export_regularization():
    month_filter = request.args.get('month', '', type=str).strip()
    status_filter = request.args.get('status', '').strip()
    conn = get_db()
    try:
        conditions = []
        params = []
        if month_filter and '-' in month_filter:
            y, m = month_filter.split('-', 1)
            conditions.append("CAST(strftime('%m', r.request_date) AS INTEGER) = ? AND CAST(strftime('%Y', r.request_date) AS INTEGER) = ?")
            params.extend([int(m), int(y)])
        if status_filter:
            conditions.append("r.status = ?")
            params.append(status_filter)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = conn.execute(f"SELECT r.emp_id, u.name, r.request_date, r.reason, r.status, r.approved_by, r.created_at, r.updated_at FROM regularization_requests r LEFT JOIN users u ON r.emp_id = u.emp_id{where} ORDER BY r.request_date DESC", params).fetchall()
    finally:
        conn.close()
    data = [{'Employee ID': r[0], 'Employee Name': r[1] or r[0], 'Date': r[2].isoformat() if r[2] else '', 'Reason': r[3] or '', 'Status': r[4], 'Approved By': r[5] or '', 'Created At': r[6].isoformat() if r[6] else '', 'Updated At': r[7].isoformat() if r[7] else ''} for r in rows]
    buf = BytesIO()
    df = pd.DataFrame(data) if data else pd.DataFrame(columns=['Employee ID', 'Employee Name', 'Date', 'Reason', 'Status', 'Approved By', 'Created At', 'Updated At'])
    with pd.ExcelWriter(buf, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Regularization')
    buf.seek(0)
    label = month_filter if month_filter else 'all'
    return send_file(buf, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', download_name=f'regularization_{label}.xlsx', as_attachment=True)

@attendance_bp.route('/regularization')
@login_required
def regularization_page():
    conn = get_db()
    mgr = conn.execute("SELECT manager_emp_id FROM users WHERE emp_id = ?", [session['emp_id']]).fetchone()
    conn.close()
    return render_template('regularization.html', is_admin=_is_admin(session.get('role')), is_manager=mgr and mgr[0] is not None)
