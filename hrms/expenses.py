from flask import Blueprint, jsonify, render_template, request, session

from .db import get_db
from .decorators import hr_or_admin_required, login_required
from .helpers import _is_admin, add_notification, audit_log, gen_id, now_ist

expenses_bp = Blueprint('expenses', __name__)


@expenses_bp.route('/admin/expenses')
@hr_or_admin_required
def admin_expenses():
    conn = get_db()
    mgr = conn.execute("SELECT manager_emp_id FROM users WHERE emp_id = ?", [session['emp_id']]).fetchone()
    conn.close()
    return render_template('admin_expenses.html', is_manager=mgr and mgr[0] is not None)


@expenses_bp.route('/expenses')
@login_required
def expenses_page():
    conn = get_db()
    mgr = conn.execute("SELECT manager_emp_id FROM users WHERE emp_id = ?", [session['emp_id']]).fetchone()
    conn.close()
    return render_template('expenses.html', is_manager=mgr and mgr[0] is not None)


@expenses_bp.route('/api/v1/expense-categories')
@expenses_bp.route('/api/expense-categories')
@login_required
def expense_categories():
    conn = get_db()
    rows = conn.execute("SELECT cat_id, name, description FROM expense_categories").fetchall()
    conn.close()
    return jsonify([{'id': r[0], 'name': r[1], 'description': r[2]} for r in rows]), 200


@expenses_bp.route('/api/v1/expenses', methods=['GET', 'POST'])
@expenses_bp.route('/api/expenses', methods=['GET', 'POST'])
@login_required
def expenses_api():
    if request.method == 'GET':
        pending_my_approval = request.args.get('pending_my_approval', '').lower() == 'true'
        conn = get_db()
        if pending_my_approval:
            rows = conn.execute("""SELECT c.claim_id, c.emp_id, u.name, c.cat_id, e.name, c.amount, c.description, c.status, c.created_at
                FROM expense_claims c JOIN users u ON c.emp_id = u.emp_id
                JOIN expense_categories e ON c.cat_id = e.cat_id
                WHERE u.manager_emp_id = ? AND c.status = 'Pending'
                ORDER BY c.created_at DESC""", [session['emp_id']]).fetchall()
        elif _is_admin(session.get('role')):
            rows = conn.execute("SELECT c.claim_id, c.emp_id, u.name, c.cat_id, e.name, c.amount, c.description, c.status, c.created_at FROM expense_claims c JOIN users u ON c.emp_id = u.emp_id JOIN expense_categories e ON c.cat_id = e.cat_id ORDER BY c.created_at DESC").fetchall()
        else:
            rows = conn.execute("SELECT c.claim_id, c.emp_id, u.name, c.cat_id, e.name, c.amount, c.description, c.status, c.created_at FROM expense_claims c JOIN users u ON c.emp_id = u.emp_id JOIN expense_categories e ON c.cat_id = e.cat_id WHERE c.emp_id = ? ORDER BY c.created_at DESC", [session['emp_id']]).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'cat_id': r[3], 'category': r[4], 'amount': float(r[5]), 'description': r[6], 'status': r[7], 'created_at': r[8].isoformat() + '+05:30' if r[8] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('cat_id') or not data.get('amount'):
        return jsonify({'error': 'cat_id and amount required'}), 400
    cid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO expense_claims VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 [cid, data.get('emp_id', session['emp_id']), data['cat_id'], float(data['amount']), data.get('description'), data.get('receipt_path'), 'Pending', None, now_ist()])
    conn.close()
    return jsonify({'message': 'Expense claimed', 'id': cid}), 201


@expenses_bp.route('/api/v1/expenses/<int:eid>/status', methods=['PUT'])
@expenses_bp.route('/api/expenses/<int:eid>/status', methods=['PUT'])
@login_required
def update_expense_status(eid):
    data = request.get_json(silent=True) or {}
    status = data.get('status')
    if status not in ('Pending', 'Approved', 'Rejected', 'Paid'):
        return jsonify({'error': 'Invalid status'}), 400
    conn = get_db()
    row = conn.execute(
        "SELECT c.emp_id, u.manager_emp_id FROM expense_claims c LEFT JOIN users u ON c.emp_id = u.emp_id WHERE c.claim_id = ?",
        [eid]
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Expense not found'}), 404
    manager_emp_id = row[1]
    current_user = session['emp_id']
    is_admin_or_hr = _is_admin(session.get('role')) or session.get('department') == 'HR'

    if status in ('Approved', 'Rejected'):
        if manager_emp_id:
            if current_user != manager_emp_id and not is_admin_or_hr:
                conn.close()
                return jsonify({'error': 'Only the assigned manager can approve/reject this expense'}), 403
        elif not is_admin_or_hr:
            conn.close()
            return jsonify({'error': 'Only HR/Admin can approve/reject this expense'}), 403

    conn.execute("UPDATE expense_claims SET status = ?, approved_by = ? WHERE claim_id = ?", [status, session['emp_id'], eid])
    conn.close()
    audit_log(session['emp_id'], 'EXPENSE_' + status.upper(), f'Expense {eid} {status}')
    add_notification(row[0], 'EXPENSE_' + status.upper(), f'Your expense claim has been {status}.', '/expenses')
    return jsonify({'message': f'Expense {status.lower()}'}), 200
