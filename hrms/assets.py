import logging
from flask import Blueprint, render_template, request, jsonify, session

from .db import get_db
from .helpers import now_ist, gen_id, parse_date
from .decorators import admin_required, hr_or_admin_required, login_required

logger = logging.getLogger('hrms')

assets_bp = Blueprint('assets', __name__)


@assets_bp.route('/admin/assets')
@hr_or_admin_required
def admin_assets():
    return render_template('assets.html')


@assets_bp.route('/api/v1/my-assets')
@assets_bp.route('/api/my-assets')
@login_required
def my_assets():
    conn = get_db()
    rows = conn.execute("SELECT asset_id, asset_type, asset_tag, brand, model, serial_number, issued_date, return_date, status, notes FROM assets WHERE emp_id = ? ORDER BY issued_date DESC", [session['emp_id']]).fetchall()
    conn.close()
    return jsonify([{'id': r[0], 'type': r[1], 'tag': r[2], 'brand': r[3], 'model': r[4], 'serial': r[5], 'issued': r[6].isoformat() + '+05:30' if r[6] else None, 'returned': r[7].isoformat() + '+05:30' if r[7] else None, 'status': r[8], 'notes': r[9]} for r in rows]), 200


@assets_bp.route('/api/v1/assets', methods=['GET', 'POST'])
@assets_bp.route('/api/assets', methods=['GET', 'POST'])
@admin_required
def assets_api():
    if request.method == 'GET':
        emp = request.args.get('emp_id')
        conn = get_db()
        if emp:
            rows = conn.execute("SELECT a.asset_id, a.emp_id, u.name, a.asset_type, a.asset_tag, a.brand, a.model, a.serial_number, a.issued_date, a.return_date, a.status, a.notes FROM assets a JOIN users u ON a.emp_id = u.emp_id WHERE a.emp_id = ? ORDER BY a.issued_date DESC", [emp]).fetchall()
        else:
            rows = conn.execute("SELECT a.asset_id, a.emp_id, u.name, a.asset_type, a.asset_tag, a.brand, a.model, a.serial_number, a.issued_date, a.return_date, a.status, a.notes FROM assets a JOIN users u ON a.emp_id = u.emp_id ORDER BY a.issued_date DESC").fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'type': r[3], 'tag': r[4], 'brand': r[5], 'model': r[6], 'serial': r[7], 'issued': r[8].isoformat() + '+05:30' if r[8] else None, 'returned': r[9].isoformat() + '+05:30' if r[9] else None, 'status': r[10], 'notes': r[11]} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('emp_id') or not data.get('asset_type'):
        return jsonify({'error': 'emp_id and asset_type required'}), 400
    aid = gen_id()
    conn = get_db()
    employee = conn.execute("SELECT 1 FROM users WHERE emp_id = ? AND status = 'Active'", [data['emp_id']]).fetchone()
    if not employee:
        conn.close()
        return jsonify({'error': 'Employee not found or inactive'}), 400
    conn.execute("INSERT INTO assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 [aid, data['emp_id'], data['asset_type'], data.get('asset_tag'), data.get('brand'), data.get('model'), data.get('serial_number'),
                  parse_date(data.get('issued_date'), now_ist().date()), None, 'Issued', data.get('notes')])
    conn.close()
    return jsonify({'message': 'Asset issued', 'id': aid}), 201


@assets_bp.route('/api/v1/assets/<int:aid>/return', methods=['POST'])
@assets_bp.route('/api/assets/<int:aid>/return', methods=['POST'])
@admin_required
def return_asset(aid):
    conn = get_db()
    conn.execute("UPDATE assets SET return_date = ?, status = 'Returned' WHERE asset_id = ?", [now_ist().date(), aid])
    conn.close()
    return jsonify({'message': 'Asset returned'}), 200
