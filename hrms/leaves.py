from datetime import datetime

import pandas as pd
from flask import Blueprint, jsonify, render_template, request, send_file, session

from .db import get_db
from .decorators import admin_required, hr_or_admin_required, login_required
from .helpers import _is_admin, add_notification, audit_log, gen_id, now_ist, parse_date

leaves_bp = Blueprint('leaves', __name__)


@leaves_bp.route('/leaves')
@login_required
def leaves_page():
    conn = get_db()
    mgr = conn.execute("SELECT manager_emp_id FROM users WHERE emp_id = ?", [session['emp_id']]).fetchone()
    conn.close()
    return render_template('leaves.html', is_manager=mgr and mgr[0] is not None)


@leaves_bp.route('/admin/leaves')
@hr_or_admin_required
def admin_leaves_page():
    conn = get_db()
    mgr = conn.execute("SELECT manager_emp_id FROM users WHERE emp_id = ?", [session['emp_id']]).fetchone()
    conn.close()
    return render_template('admin_leaves.html', is_manager=mgr and mgr[0] is not None)


@leaves_bp.route('/api/v1/leaves', methods=['GET', 'POST'])
@leaves_bp.route('/api/leaves', methods=['GET', 'POST'])
@login_required
def leaves_api():
    """Create or list leave requests
    ---
    get:
      tags: [Leaves]
      parameters:
        - in: query
          name: status
          type: string
      responses:
        200:
          description: Leave list
    post:
      tags: [Leaves]
      parameters:
        - in: body
          name: body
          schema:
            type: object
            properties:
              leave_type: {type: string}
              start_date: {type: string, format: date}
              end_date: {type: string, format: date}
              reason: {type: string}
      responses:
        201:
          description: Leave created
    """
    emp_id = session['emp_id']
    conn = get_db()

    if request.method == 'GET':
        status_filter = request.args.get('status')
        month_filter = request.args.get('month', type=int)
        year_filter = request.args.get('year', type=int)
        pending_my_approval = request.args.get('pending_my_approval', '').lower() == 'true'
        is_admin = _is_admin(session.get('role'))
        if pending_my_approval:
            query = """SELECT l.leave_id, l.emp_id, u.name, l.leave_type, l.start_date, l.end_date,
                       l.reason, l.status, l.approved_by, l.created_at,
                       m.name as mgr_name
                       FROM leave_requests l
                       LEFT JOIN users u ON l.emp_id = u.emp_id
                       LEFT JOIN users m ON u.manager_emp_id = m.emp_id
                       WHERE u.manager_emp_id = ? AND l.status = 'Pending'"""
            params = [emp_id]
            if status_filter:
                query += " AND l.status = ?"
                params.append(status_filter)
            query += " ORDER BY l.created_at DESC"
        elif is_admin:
            query = """SELECT l.leave_id, l.emp_id, u.name, l.leave_type, l.start_date, l.end_date,
                       l.reason, l.status, l.approved_by, l.created_at
                       FROM leave_requests l LEFT JOIN users u ON l.emp_id = u.emp_id"""
            params = []
            conditions = []
            if status_filter:
                conditions.append("l.status = ?")
                params.append(status_filter)
            if year_filter:
                conditions.append("l.year = ?")
                params.append(year_filter)
            if month_filter:
                conditions.append("CAST(strftime('%m', l.start_date) AS INTEGER) = ?")
                params.append(month_filter)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY l.created_at DESC"
        else:
            query = """SELECT l.leave_id, l.emp_id, u.name, l.leave_type, l.start_date, l.end_date,
                       l.reason, l.status, l.approved_by, l.created_at
                       FROM leave_requests l LEFT JOIN users u ON l.emp_id = u.emp_id
                       WHERE l.emp_id = ?"""
            params = [emp_id]
            if status_filter:
                query += " AND l.status = ?"
                params.append(status_filter)
            if year_filter:
                query += " AND l.year = ?"
                params.append(year_filter)
            if month_filter:
                query += " AND CAST(strftime('%m', l.start_date) AS INTEGER) = ?"
                params.append(month_filter)
            query += " ORDER BY l.created_at DESC"
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return jsonify([{
            'leave_id': r[0], 'emp_id': r[1], 'emp_name': r[2] or r[1], 'leave_type': r[3],
            'start_date': r[4].isoformat(), 'end_date': r[5].isoformat(),
            'reason': r[6], 'status': r[7], 'approved_by': r[8],
            'created_at': r[9].isoformat() + '+05:30' if r[9] else None
        } for r in rows]), 200

    data = request.get_json(silent=True) or {}
    lt = data.get('leave_type')
    sd = parse_date(data.get('start_date'))
    ed = parse_date(data.get('end_date'), sd)
    if not lt or not sd or not ed:
        conn.close()
        return jsonify({'error': 'leave_type, start_date, end_date required'}), 400
    if ed < sd:
        sd, ed = ed, sd

    balance = conn.execute(
        "SELECT balance_id, total_days, used_days FROM leave_balance WHERE emp_id = ? AND leave_type = ? AND year = ?",
        [emp_id, lt, now_ist().year]
    ).fetchone()
    if balance:
        requested = (ed - sd).days + 1
        remaining = balance[1] - balance[2]
        if requested > remaining:
            conn.close()
            return jsonify({'error': f'Insufficient balance. Remaining: {remaining} days'}), 400

    if conn.execute(
        "SELECT 1 FROM leave_requests WHERE emp_id = ? AND status IN ('Pending','Approved') AND start_date <= ? AND end_date >= ?",
        [emp_id, ed, sd]
    ).fetchone():
        conn.close()
        return jsonify({'error': 'Overlapping leave request already exists for these dates'}), 409

    leave_id = gen_id()
    conn.execute(
        "INSERT INTO leave_requests (leave_id, emp_id, leave_type, start_date, end_date, year, reason, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'Pending')",
        [leave_id, emp_id, lt, sd, ed, sd.year, data.get('reason', '')]
    )
    conn.close()
    audit_log(emp_id, 'LEAVE_APPLY', f'{lt} leave {sd} to {ed}')
    add_notification(session['emp_id'], 'LEAVE_APPLIED', f'Your {lt} leave ({sd} to {ed}) has been submitted.', '/leaves')
    return jsonify({'message': 'Leave application submitted', 'leave_id': leave_id}), 201


@leaves_bp.route('/api/v1/leaves/export', methods=['GET'])
@leaves_bp.route('/api/leaves/export', methods=['GET'])
@admin_required
def export_leaves():
    month = request.args.get('month', now_ist().month, type=int)
    year = request.args.get('year', now_ist().year, type=int)
    status_filter = request.args.get('status')
    conn = get_db()
    try:
        query = """SELECT l.emp_id, u.name, l.leave_type, l.start_date, l.end_date,
                   l.reason, l.status, l.approved_by, l.created_at
                   FROM leave_requests l LEFT JOIN users u ON l.emp_id = u.emp_id
                   WHERE l.year = ? AND CAST(strftime('%m', l.start_date) AS INTEGER) = ?"""
        params: list = [year, month]
        if status_filter:
            query += " AND l.status = ?"
            params.append(status_filter)
        query += " ORDER BY l.start_date"
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    import io
    data = [{
        'Employee ID': r[0], 'Employee Name': r[1] or r[0], 'Leave Type': r[2],
        'From': r[3].isoformat(), 'To': r[4].isoformat(), 'Days': (r[4] - r[3]).days + 1,
        'Reason': r[5] or '', 'Status': r[6], 'Approved By': r[7] or ''
    } for r in rows]

    buf = io.BytesIO()
    df = pd.DataFrame(data) if data else pd.DataFrame(columns=['Employee ID','Employee Name','Leave Type','From','To','Days','Reason','Status','Approved By'])
    with pd.ExcelWriter(buf, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Leaves')
    buf.seek(0)
    month_name = datetime(2000, month, 1).strftime('%B')
    return send_file(buf, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     download_name=f'leaves_{month_name}_{year}.xlsx', as_attachment=True)


@leaves_bp.route('/api/v1/leaves/<int:leave_id>/approve', methods=['POST'])
@leaves_bp.route('/api/leaves/<int:leave_id>/approve', methods=['POST'])
@login_required
def approve_leave(leave_id):
    conn = get_db()
    row = conn.execute(
        "SELECT l.emp_id, l.leave_type, l.start_date, l.end_date, l.status, u.manager_emp_id FROM leave_requests l LEFT JOIN users u ON l.emp_id = u.emp_id WHERE l.leave_id = ?",
        [leave_id]
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Leave not found'}), 404
    if row[4] != 'Pending':
        conn.close()
        return jsonify({'error': 'Leave is not pending'}), 400

    manager_emp_id = row[5]
    current_user = session['emp_id']
    is_admin_or_hr = _is_admin(session.get('role')) or session.get('department') == 'HR'

    if manager_emp_id:
        if current_user != manager_emp_id and not is_admin_or_hr:
            conn.close()
            return jsonify({'error': 'Only the assigned manager can approve this request'}), 403
    elif not is_admin_or_hr:
        conn.close()
        return jsonify({'error': 'Only HR/Admin can approve this request'}), 403

    days = (row[3] - row[2]).days + 1
    conn.execute(
        "UPDATE leave_requests SET status = 'Approved', approved_by = ?, updated_at = ? WHERE leave_id = ?",
        [session['emp_id'], now_ist(), leave_id]
    )
    conn.execute(
        "UPDATE leave_balance SET used_days = used_days + ? WHERE emp_id = ? AND leave_type = ? AND year = ?",
        [days, row[0], row[1], row[2].year]
    )
    conn.close()
    audit_log(session['emp_id'], 'LEAVE_APPROVE', f'Leave {leave_id} approved')
    add_notification(row[0], 'LEAVE_APPROVED', f'Your {row[1]} leave ({row[2]} to {row[3]}) has been approved.', '/leaves')
    return jsonify({'message': 'Leave approved'}), 200


@leaves_bp.route('/api/v1/leaves/<int:leave_id>/reject', methods=['POST'])
@leaves_bp.route('/api/leaves/<int:leave_id>/reject', methods=['POST'])
@login_required
def reject_leave(leave_id):
    conn = get_db()
    row = conn.execute(
        "SELECT l.emp_id, l.leave_type, l.start_date, l.end_date, l.status, u.manager_emp_id FROM leave_requests l LEFT JOIN users u ON l.emp_id = u.emp_id WHERE l.leave_id = ?",
        [leave_id]
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Not found'}), 404
    if row[4] != 'Pending':
        conn.close()
        return jsonify({'error': 'Leave is not pending'}), 400

    manager_emp_id = row[5]
    current_user = session['emp_id']
    is_admin_or_hr = _is_admin(session.get('role')) or session.get('department') == 'HR'

    if manager_emp_id:
        if current_user != manager_emp_id and not is_admin_or_hr:
            conn.close()
            return jsonify({'error': 'Only the assigned manager can reject this request'}), 403
    elif not is_admin_or_hr:
        conn.close()
        return jsonify({'error': 'Only HR/Admin can reject this request'}), 403

    conn.execute(
        "UPDATE leave_requests SET status = 'Rejected', approved_by = ?, updated_at = ? WHERE leave_id = ?",
        [session['emp_id'], now_ist(), leave_id]
    )
    conn.close()
    audit_log(session['emp_id'], 'LEAVE_REJECT', f'Leave {leave_id} rejected')
    add_notification(row[0], 'LEAVE_REJECTED', f'Your {row[1]} leave ({row[2]} to {row[3]}) has been rejected.', '/leaves')
    return jsonify({'message': 'Leave rejected'}), 200


@leaves_bp.route('/api/v1/leaves/<int:leave_id>/cancel', methods=['POST'])
@leaves_bp.route('/api/leaves/<int:leave_id>/cancel', methods=['POST'])
@login_required
def cancel_leave(leave_id):
    conn = get_db()
    row = conn.execute(
        "SELECT emp_id, status FROM leave_requests WHERE leave_id = ?",
        [leave_id]
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Leave not found'}), 404
    if row[0] != session['emp_id'] and not _is_admin(session.get('role')):
        conn.close()
        return jsonify({'error': 'Not authorized'}), 403
    if row[1] != 'Pending':
        conn.close()
        return jsonify({'error': 'Only pending leaves can be cancelled'}), 400
    conn.execute(
        "UPDATE leave_requests SET status = 'Cancelled', updated_at = ? WHERE leave_id = ?",
        [now_ist(), leave_id]
    )
    conn.close()
    audit_log(session['emp_id'], 'LEAVE_CANCEL', f'Leave {leave_id} cancelled')
    return jsonify({'message': 'Leave cancelled'}), 200


@leaves_bp.route('/api/v1/leave-balance')
@leaves_bp.route('/api/leave-balance')
@login_required
def leave_balance_api():
    """Get leave balance for current user"""
    emp_id = session['emp_id']
    year = now_ist().year
    conn = get_db()
    rows = conn.execute(
        "SELECT leave_type, total_days, used_days FROM leave_balance WHERE emp_id = ? AND year = ?",
        [emp_id, year]
    ).fetchall()
    conn.close()
    return jsonify([{
        'leave_type': r[0], 'total_days': r[1],
        'used_days': r[2], 'remaining': r[1] - r[2]
    } for r in rows]), 200
