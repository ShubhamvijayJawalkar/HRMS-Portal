import logging

from flask import Blueprint, jsonify, render_template, request

from .db import _scalar, get_db
from .decorators import admin_required, hr_or_admin_required, login_required
from .helpers import gen_id, now_ist, parse_date

logger = logging.getLogger('hrms')

audit_bp = Blueprint('audit', __name__)


@audit_bp.route('/admin/audit')
@hr_or_admin_required
def audit_page():
    return render_template('admin_audit.html')


@audit_bp.route('/api/v1/audit-log')
@audit_bp.route('/api/audit-log')
@admin_required
def get_audit_log():
    limit = request.args.get('limit', 200, type=int)
    offset = request.args.get('offset', 0, type=int)
    action_filter = request.args.get('action', '').strip().upper()
    module_filter = request.args.get('module', '').strip().upper()
    conn = get_db()
    conditions = []
    params = []
    if action_filter:
        conditions.append("action LIKE ?")
        params.append('%' + action_filter + '%')
    if module_filter:
        conditions.append("action LIKE ?")
        params.append(module_filter + '%')
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    total = _scalar("SELECT COUNT(*) FROM audit_log" + where, params, conn=conn)
    rows = conn.execute(
        "SELECT log_id, emp_id, action, details, ip_address, created_at FROM audit_log{} ORDER BY created_at DESC LIMIT ? OFFSET ?".format(where),
        params + [limit, offset]
    ).fetchall()
    conn.close()
    return jsonify({
        'total': total,
        'data': [{
            'log_id': r[0], 'emp_id': r[1], 'action': r[2],
            'details': r[3], 'ip_address': r[4],
            'created_at': r[5].isoformat() + '+05:30' if r[5] else None
        } for r in rows]
    }), 200


@audit_bp.route('/api/audit-modules')
@admin_required
def audit_modules():
    conn = get_db()
    rows = conn.execute("""
        SELECT DISTINCT action FROM audit_log
        WHERE action LIKE '%\\_%' ESCAPE '\\'
        ORDER BY action
    """).fetchall()
    conn.close()
    modules = sorted(set(r[0].split('_')[0] for r in rows if r[0] and '_' in r[0]))
    if not modules:
        modules = ['USER', 'LEAVE', 'LOGIN', 'LOGOUT', 'CANDIDATE', 'OFFER', 'EXPENSE', 'REGULARIZATION', 'PASSWORD', 'PROFILE']
    return jsonify(modules), 200


@audit_bp.route('/api/v1/holidays', methods=['GET'])
@audit_bp.route('/api/holidays', methods=['GET'])
@login_required
def get_holidays():
    year = request.args.get('year', now_ist().year, type=int)
    conn = get_db()
    try:
        rows = conn.execute("SELECT holiday_id, name, holiday_date, type FROM holidays WHERE year = ? ORDER BY holiday_date", [year]).fetchall()
        return jsonify([{'id': r[0], 'name': r[1], 'date': r[2].isoformat(), 'type': r[3]} for r in rows]), 200
    finally:
        conn.close()


@audit_bp.route('/api/v1/holidays', methods=['POST'])
@audit_bp.route('/api/holidays', methods=['POST'])
@admin_required
def add_holiday():
    data = request.get_json(silent=True) or {}
    if not data.get('name') or not data.get('date'):
        return jsonify({'error': 'name and date required'}), 400
    htype = data.get('type', 'National')
    if htype not in ('National', 'Optional'):
        return jsonify({'error': 'type must be National or Optional'}), 400
    d = parse_date(data['date'])
    if d is None:
        return jsonify({'error': 'Invalid date format'}), 400
    hid = gen_id()
    conn = get_db()
    try:
        dup = conn.execute("SELECT 1 FROM holidays WHERE holiday_date = ? AND name = ?", [d, data['name']]).fetchone()
        if dup:
            return jsonify({'error': 'Holiday with this name and date already exists'}), 409
        conn.execute("INSERT INTO holidays VALUES (?, ?, ?, ?, ?)",
                     [hid, data['name'], d, d.year, htype])
        return jsonify({'message': 'Holiday added', 'id': hid}), 201
    finally:
        conn.close()


@audit_bp.route('/api/v1/holidays/<int:hid>', methods=['DELETE'])
@audit_bp.route('/api/holidays/<int:hid>', methods=['DELETE'])
@admin_required
def delete_holiday(hid):
    conn = get_db()
    try:
        result = conn.execute("DELETE FROM holidays WHERE holiday_id = ?", [hid])
        if result.rowcount == 0:
            return jsonify({'error': 'Holiday not found'}), 404
        return jsonify({'message': 'Deleted'}), 200
    finally:
        conn.close()


@audit_bp.route('/admin/holidays')
@admin_required
def admin_holidays():
    return render_template('holidays.html')
