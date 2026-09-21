import logging
from io import BytesIO

import pandas as pd
from flask import Blueprint, jsonify, render_template, request, session

from .db import _scalar, get_db
from .decorators import admin_required, hr_or_admin_required, login_required
from .helpers import (
    audit_log,
    check_password,
    gen_id,
    get_user,
    hash_password,
    now_ist,
    parse_date,
    send_email,
)

logger = logging.getLogger('hrms')

users_bp = Blueprint('users', __name__)


@users_bp.route('/admin/users')
@admin_required
def admin_users():
    return render_template('admin_users.html')


@users_bp.route('/api/users', methods=['GET'])
@admin_required
def get_users():
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 50, type=int), 200)
    offset = (page - 1) * per_page
    active_only = request.args.get('active', '').lower() in ('1', 'true', 'yes', 'on')
    search = request.args.get('search', '').strip()
    role_filter = request.args.get('role', '').strip()
    status_filter = request.args.get('status', '').strip()
    dept_filter = request.args.get('department', '').strip()
    conn = get_db()
    conditions = []
    params = []
    if active_only:
        conditions.append("status IN ('Active', 'Onboarding')")
    if search:
        conditions.append("(LOWER(emp_id) LIKE ? OR LOWER(name) LIKE ? OR LOWER(email) LIKE ?)")
        like = f"%{search.lower()}%"
        params.extend([like, like, like])
    if role_filter:
        conditions.append("role = ?")
        params.append(role_filter)
    if status_filter:
        conditions.append("status = ?")
        params.append(status_filter)
    if dept_filter:
        conditions.append("department = ?")
        params.append(dept_filter)
    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
    total = _scalar(f"SELECT COUNT(*) FROM users{where_clause}", params)
    rows = conn.execute(
        f"SELECT emp_id, name, email, role, status, department, first_login, allow_login, allow_breaks, shift_start, shift_end FROM users{where_clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [per_page, offset]
    ).fetchall()
    onboarding_emps = [r[0] for r in rows if r[4] == 'Onboarding']
    wf_map = {}
    if onboarding_emps:
        placeholders = ','.join(['?'] * len(onboarding_emps))
        wf_rows = conn.execute(
            f"SELECT emp_id, current_step, step_1_status, step_2_status, step_3_status, step_4_status, step_5_status FROM onboarding_workflow WHERE emp_id IN ({placeholders})",
            onboarding_emps
        ).fetchall()
        step_labels = ['Document Upload', 'Document Validation', 'System Allocation', 'Desk & ID Card', 'Team Introduction']
        for w in wf_rows:
            statuses = [w[2], w[3], w[4], w[5], w[6]]
            current = w[1]
            pending_steps = []
            for i, s in enumerate(statuses):
                if s == 'Pending':
                    pending_steps.append(step_labels[i])
            if pending_steps:
                wf_map[w[0]] = pending_steps[0]
            elif current >= 5 and all(s == 'Completed' for s in statuses):
                wf_map[w[0]] = 'Completed'
            else:
                wf_map[w[0]] = 'In Progress'
    conn.close()
    return jsonify({
        'total': total, 'page': page, 'per_page': per_page,
        'data': [{
            'emp_id': r[0], 'name': r[1], 'email': r[2], 'role': r[3],
            'status': r[4], 'department': r[5],
            'first_login': r[6].strftime('%I:%M %p') if r[6] else 'N/A',
            'allow_login': int(r[7]) if r[7] else 1,
            'allow_breaks': int(r[8]) if r[8] else 1,
            'shift_start': r[9] or '',
            'shift_end': r[10] or '',
            'onboarding_status': wf_map.get(r[0], '')
        } for r in rows]
    }), 200


@users_bp.route('/api/users', methods=['POST'])
@admin_required
def add_user():
    data = request.get_json(silent=True) or {}
    if not data.get('emp_id') or not data.get('name') or not data.get('email'):
        return jsonify({'error': 'Missing required fields'}), 400
    if '@' not in data.get('email', ''):
        return jsonify({'error': 'Invalid email'}), 400
    conn = get_db()
    if conn.execute("SELECT 1 FROM users WHERE emp_id = ?", [data['emp_id']]).fetchone():
        conn.close()
        return jsonify({'error': 'Employee ID already exists'}), 409
    pwd = data.get('password', 'pass123')
    conn.execute(
        "INSERT INTO users (emp_id, name, email, password, role, department, designation, status, first_login, created_at, allow_login, allow_breaks, shift_start, shift_end) VALUES (?, ?, ?, ?, ?, ?, ?, 'Active', ?, ?, ?, ?, ?, ?)",
        [data['emp_id'], data['name'], data['email'], hash_password(pwd),
         data.get('role', 'Employee'), data.get('department', ''),
         data.get('designation', ''),
         now_ist(), now_ist(),
         int(data.get('allow_login', 1)), int(data.get('allow_breaks', 1)),
         data.get('shift_start', ''), data.get('shift_end', '')]
    )
    conn.close()
    audit_log(session['emp_id'], 'USER_CREATE', f'Created user {data["emp_id"]}')

    admin_name = session.get('name', 'Admin')
    from datetime import timedelta as _td

    from .auth import secrets as _secrets
    reset_token = _secrets.token_urlsafe(32)
    conn = get_db()
    conn.execute(
        "INSERT INTO password_reset_tokens (token_id, emp_id, token, expires_at) VALUES (?, ?, ?, ?)",
        [gen_id(), data['emp_id'], reset_token, now_ist() + _td(hours=24)]
    )
    conn.close()
    reset_link = f"{request.host_url}reset-password?token={reset_token}"
    creds_body = f"""<div style="font-family:Arial,sans-serif;max-width:500px;margin:0 auto;padding:24px;background:#f8fafc;border-radius:12px;border:1px solid #e2e8f0;">
        <h2 style="color:#0f172a;margin:0 0 16px;">Welcome to HRMS</h2>
        <p style="color:#334155;">Hi <strong>{data['name']}</strong>,</p>
        <p style="color:#334155;">Your account has been created by <strong>{admin_name}</strong>. Use the link below to set your password and log in:</p>
        <div style="background:white;border:1px solid #e2e8f0;border-radius:8px;padding:16px;margin:16px 0;">
            <p style="margin:4px 0;"><strong>Employee ID:</strong> {data['emp_id']}</p>
            <p style="margin:4px 0;"><strong>Email:</strong> {data['email']}</p>
            <p style="margin:4px 0;"><strong>Role:</strong> {data.get('role', 'Employee')}</p>
            <p style="margin:4px 0;"><strong>Department:</strong> {data.get('department', 'N/A')}</p>
        </div>
        <p style="text-align:center;margin:24px 0;">
            <a href="{reset_link}" style="background:#2563eb;color:white;padding:12px 24px;border-radius:6px;text-decoration:none;font-weight:bold;">Set Your Password</a>
        </p>
        <p style="color:#64748b;font-size:12px;">Link expires in 24 hours. Do not share this link with anyone.</p>
        <p style="color:#64748b;font-size:12px;">- HRMS Team</p>
    </div>"""
    send_email(data['email'], 'Your HRMS Account: Set Your Password', creds_body)

    return jsonify({'message': 'User added', 'email_sent': True}), 201


@users_bp.route('/api/users/<emp_id>', methods=['GET'])
@admin_required
def get_user_route(emp_id):
    u = get_user(emp_id)
    if not u:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({
        'emp_id': u[0], 'name': u[1], 'email': u[2], 'role': u[3],
        'status': u[4], 'department': u[5],
        'allow_login': int(u[6]) if u[6] else 1,
        'allow_breaks': int(u[7]) if u[7] else 1,
        'shift_start': u[16] or '' if len(u) > 16 else '',
        'shift_end': u[17] or '' if len(u) > 17 else ''
    }), 200


@users_bp.route('/api/users/<emp_id>', methods=['PUT'])
@admin_required
def update_user(emp_id):
    data = request.get_json(silent=True) or {}
    conn = get_db()
    conn.execute(
        "UPDATE users SET name = ?, email = ?, role = ?, department = ?, status = ?, allow_login = ?, allow_breaks = ?, shift_start = ?, shift_end = ? WHERE emp_id = ?",
        [data.get('name'), data.get('email'), data.get('role'), data.get('department', ''),
         data.get('status', 'Active'), int(data.get('allow_login', 1)),
         int(data.get('allow_breaks', 1)), data.get('shift_start', ''), data.get('shift_end', ''), emp_id]
    )
    conn.close()
    audit_log(session['emp_id'], 'USER_UPDATE', f'Updated user {emp_id}')
    return jsonify({'message': 'User updated'}), 200


@users_bp.route('/api/users/<emp_id>/block', methods=['POST'])
@admin_required
def block_user(emp_id):
    conn = get_db()
    conn.execute("UPDATE users SET status = 'Blocked' WHERE emp_id = ?", [emp_id])
    conn.close()
    audit_log(session['emp_id'], 'USER_BLOCK', f'Blocked user {emp_id}')
    return jsonify({'message': 'User blocked'}), 200


@users_bp.route('/api/users/<emp_id>/unblock', methods=['POST'])
@admin_required
def unblock_user(emp_id):
    conn = get_db()
    conn.execute("UPDATE users SET status = 'Active' WHERE emp_id = ?", [emp_id])
    conn.close()
    audit_log(session['emp_id'], 'USER_UNBLOCK', f'Unblocked user {emp_id}')
    return jsonify({'message': 'User unblocked'}), 200


@users_bp.route('/api/users/<emp_id>', methods=['DELETE'])
@admin_required
def delete_user(emp_id):
    if emp_id == session.get('emp_id'):
        return jsonify({'error': 'Cannot delete your own account'}), 400
    conn = get_db()
    user = conn.execute("SELECT name FROM users WHERE emp_id = ?", [emp_id]).fetchone()
    if not user:
        conn.close()
        return jsonify({'error': 'User not found'}), 404
    try:
        tables = [
            ('user_sessions', 'emp_id'), ('breaks', 'emp_id'), ('leave_requests', 'emp_id'),
            ('leave_balance', 'emp_id'), ('break_approvals', 'emp_id'), ('audit_log', 'emp_id'),
            ('notifications', 'emp_id'), ('password_reset_tokens', 'emp_id'),
            ('employee_documents', 'emp_id'),
            ('regularization_requests', 'emp_id'), ('onboarding_tasks', 'emp_id'),
            ('onboarding_checklist', 'emp_id'), ('onboarding_workflow', 'emp_id'),
            ('offboarding_tasks', 'emp_id'), ('offboarding_workflow', 'emp_id'), ('exit_interviews', 'emp_id'),
            ('salary_structures', 'emp_id'), ('payroll_items', 'emp_id'),
            ('goals', 'emp_id'), ('performance_reviews', 'emp_id'),
            ('performance_reviews', 'reviewer_id'),
            ('feedback_360', 'emp_id'), ('feedback_360', 'reviewer_id'),
            ('expense_claims', 'emp_id'),
            ('assets', 'emp_id'), ('documents', 'emp_id'), ('dependents', 'emp_id'),
            ('interviews', 'interviewer'),
            ('notification_preferences', 'emp_id'),
        ]
        for table, col in tables:
            conn.execute(f"DELETE FROM {table} WHERE {col} = ?", [emp_id])
        conn.execute("DELETE FROM ticket_comments WHERE emp_id = ?", [emp_id])
        conn.execute("DELETE FROM ticket_comments WHERE ticket_id IN (SELECT ticket_id FROM tickets WHERE emp_id = ?)", [emp_id])
        conn.execute("DELETE FROM tickets WHERE emp_id = ?", [emp_id])
        conn.execute("DELETE FROM users WHERE emp_id = ?", [emp_id])
    except Exception as e:
        conn.close()
        logger.error("delete_user failed for %s: %s", emp_id, e)
        return jsonify({'error': 'Failed to delete user'}), 500
    conn.close()
    audit_log(session['emp_id'], 'USER_DELETE', f'Deleted user {emp_id} ({user[0]})')
    return jsonify({'message': f'User {emp_id} deleted permanently'}), 200


@users_bp.route('/api/v1/users/import', methods=['POST'])
@users_bp.route('/api/users/import', methods=['POST'])
@admin_required
def import_users_csv():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    f = request.files['file']
    if not f.filename or not f.filename.endswith('.csv'):
        return jsonify({'error': 'CSV file required'}), 400
    conn = None
    try:
        df = pd.read_csv(BytesIO(f.read()))
        required = ['emp_id', 'name', 'email']
        missing = [c for c in required if c not in df.columns]
        if missing:
            return jsonify({'error': f'Missing columns: {missing}'}), 400
        conn = get_db()
        pwd = hash_password('pass123')
        count = 0
        for _, row in df.iterrows():
            eid = str(row['emp_id']).strip().upper()
            if conn.execute("SELECT 1 FROM users WHERE emp_id = ?", [eid]).fetchone():
                continue
            conn.execute(
                "INSERT INTO users (emp_id, name, email, password, role, department, status, first_login, created_at, allow_login, allow_breaks) VALUES (?, ?, ?, ?, ?, ?, 'Active', ?, ?, 1, 1)",
                [eid, str(row.get('name', '')), str(row.get('email', '')), pwd,
                 str(row.get('role', 'Employee')), str(row.get('department', '')),
                 now_ist(), now_ist()]
            )
            count += 1
        return jsonify({'message': f'{count} users imported'}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    finally:
        if conn:
            conn.close()


@users_bp.route('/admin/import-users')
@hr_or_admin_required
def import_users_page():
    return render_template('import_users.html')


@users_bp.route('/api/v1/dependents', methods=['GET', 'POST'])
@users_bp.route('/api/dependents', methods=['GET', 'POST'])
@login_required
def dependents_api():
    emp_id = session['emp_id']
    if request.method == 'GET':
        conn = get_db()
        rows = conn.execute("SELECT dependent_id, name, relationship, date_of_birth FROM dependents WHERE emp_id = ?", [emp_id]).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'name': r[1], 'relationship': r[2], 'date_of_birth': r[3].isoformat() if r[3] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('name') or not data.get('relationship'):
        return jsonify({'error': 'name and relationship required'}), 400
    did = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO dependents VALUES (?, ?, ?, ?, ?)",
                 [did, emp_id, data['name'], data['relationship'], parse_date(data.get('date_of_birth'))])
    conn.close()
    return jsonify({'message': 'Dependent added', 'id': did}), 201


@users_bp.route('/api/v1/dependents/<int:did>', methods=['DELETE'])
@users_bp.route('/api/dependents/<int:did>', methods=['DELETE'])
@login_required
def delete_dependent(did):
    conn = get_db()
    conn.execute("DELETE FROM dependents WHERE dependent_id = ? AND emp_id = ?", [did, session['emp_id']])
    conn.close()
    return jsonify({'message': 'Deleted'}), 200


@users_bp.route('/api/v1/employee-documents', methods=['POST'])
@users_bp.route('/api/employee-documents', methods=['POST'])
@login_required
def documents_api():
    emp_id = session['emp_id']
    data = request.get_json(silent=True) or {}
    if not data.get('doc_type'):
        return jsonify({'error': 'doc_type required'}), 400
    did = gen_id()
    conn = get_db()
    conn.execute(
        "INSERT INTO documents (doc_id, emp_id, name, category, file_path, file_size, uploaded_at, context, doc_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [did, emp_id, data.get('file_name', data['doc_type']), data['doc_type'], None, None, now_ist(), 'Personal', data['doc_type']]
    )
    conn.close()
    return jsonify({'message': 'Document recorded', 'id': did}), 201


@users_bp.route('/profile')
@login_required
def profile_page():
    return render_template('profile.html')


@users_bp.route('/api/profile', methods=['GET', 'PUT'])
@login_required
def profile_api():
    emp_id = session['emp_id']
    if request.method == 'GET':
        u = get_user(emp_id)
        if not u:
            return jsonify({'error': 'Not found'}), 404
        return jsonify({
            'emp_id': u[0], 'name': u[1], 'email': u[2],
            'role': u[3], 'department': u[5],
            'allow_login': u[6], 'allow_breaks': u[7],
            'designation': u[8], 'manager_emp_id': u[9],
            'phone': u[10],
            'date_of_birth': u[11].isoformat() if u[11] else None,
            'date_of_joining': u[12].isoformat() if u[12] else None,
            'address': u[13], 'emergency_contact_name': u[14],
            'emergency_contact_phone': u[15]
        }), 200

    data = request.get_json(silent=True) or {}
    conn = get_db()
    conn.execute(
        "UPDATE users SET name = ?, email = ?, department = ?, phone = ?, address = ?, emergency_contact_name = ?, emergency_contact_phone = ? WHERE emp_id = ?",
        [data.get('name'), data.get('email'), data.get('department', ''),
         data.get('phone'), data.get('address'), data.get('emergency_contact_name'),
         data.get('emergency_contact_phone'), emp_id]
    )
    conn.close()
    session['name'] = data.get('name')
    audit_log(emp_id, 'PROFILE_UPDATE', 'Profile updated')
    return jsonify({'message': 'Profile updated'}), 200


@users_bp.route('/api/change-password', methods=['POST'])
@login_required
def change_password():
    data = request.get_json(silent=True) or {}
    emp_id = session['emp_id']
    current = data.get('current_password', '')
    new_pwd = data.get('new_password', '')

    if len(new_pwd) < 6:
        return jsonify({'error': 'New password must be at least 6 characters'}), 400

    conn = get_db()
    row = conn.execute("SELECT password FROM users WHERE emp_id = ?", [emp_id]).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'User not found'}), 404

    stored = row[0]
    if not check_password(current, stored):
        conn.close()
        return jsonify({'error': 'Current password is incorrect'}), 400

    conn.execute("UPDATE users SET password = ? WHERE emp_id = ?", [hash_password(new_pwd), emp_id])
    conn.close()
    audit_log(emp_id, 'PASSWORD_CHANGE', 'Password changed')
    return jsonify({'message': 'Password changed successfully'}), 200
