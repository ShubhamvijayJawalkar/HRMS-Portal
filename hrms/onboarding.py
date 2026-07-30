import os, logging
from datetime import datetime, timedelta
from io import BytesIO
from flask import Blueprint, render_template, request, jsonify, session, send_file, redirect
import pandas as pd
from .db import get_db, _scalar
from .helpers import (
    now_ist, gen_id, parse_date, audit_log, _is_admin,
    add_notification, notify_admins, UPLOAD_FOLDER, ONBOARDING_DOC_TYPES, validate_upload
)
from .decorators import login_required, admin_required

onboarding_bp = Blueprint('onboarding', __name__)


@onboarding_bp.route('/onboarding')
@login_required
def onboarding_page():
    return render_template('onboarding.html', is_admin=_is_admin(session.get('role')), is_hr=session.get('department') == 'HR')


@onboarding_bp.route('/api/v1/onboarding-tasks', methods=['GET', 'POST'])
@onboarding_bp.route('/api/onboarding-tasks', methods=['GET', 'POST'])
@login_required
def onboarding_api():
    is_admin = _is_admin(session.get('role'))

    if request.method == 'GET':
        status_filter = request.args.get('status', '').strip()
        month_filter = request.args.get('month', '').strip()
        conn = get_db()
        conditions = []
        params: list = []
        if not is_admin:
            conditions.append("t.emp_id = ?")
            params.append(session['emp_id'])
        if status_filter:
            conditions.append("t.status = ?")
            params.append(status_filter)
        if month_filter and '-' in month_filter:
            y, m = month_filter.split('-', 1)
            conditions.append("CAST(strftime('%m', t.due_date) AS INTEGER) = ? AND CAST(strftime('%Y', t.due_date) AS INTEGER) = ?")
            params.extend([int(m), int(y)])
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = conn.execute(
            f"SELECT t.task_id, t.emp_id, u.name, t.task_name, t.assigned_to, t.status, t.due_date, t.completed_at "
            f"FROM onboarding_tasks t JOIN users u ON t.emp_id = u.emp_id{where} ORDER BY t.due_date ASC NULLS LAST, t.task_id DESC",
            params
        ).fetchall()
        stats = conn.execute(
            f"SELECT t.status, COUNT(*) FROM onboarding_tasks t{where} GROUP BY t.status",
            params
        ).fetchall()
        conn.close()
        status_counts = {s[0]: s[1] for s in stats}
        tasks = [{
            'id': r[0], 'emp_id': r[1], 'employee': r[2] or r[1], 'task': r[3],
            'assigned_to': r[4], 'status': r[5],
            'due_date': r[6].isoformat() if r[6] else None,
            'completed_at': r[7].isoformat() if r[7] else None
        } for r in rows]
        if is_admin:
            return jsonify({
                'tasks': tasks,
                'counts': {
                    'pending': status_counts.get('Pending', 0),
                    'completed': status_counts.get('Completed', 0),
                    'in_progress': status_counts.get('In Progress', 0),
                    'total': sum(status_counts.values())
                }
            }), 200
        else:
            return jsonify(tasks), 200

    data = request.get_json(silent=True) or {}
    if not data.get('emp_id') or not data.get('task_name'):
        return jsonify({'error': 'emp_id and task_name required'}), 400
    tid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO onboarding_tasks VALUES (?, ?, ?, ?, ?, ?, ?)",
                 [tid, data['emp_id'], data['task_name'], data.get('assigned_to', 'HR'), 'Pending', parse_date(data.get('due_date')), None])
    conn.close()
    return jsonify({'message': 'Task added', 'id': tid}), 201


@onboarding_bp.route('/api/v1/onboarding-tasks/<int:tid>/complete', methods=['POST'])
@onboarding_bp.route('/api/onboarding-tasks/<int:tid>/complete', methods=['POST'])
@admin_required
def complete_onboarding_task(tid):
    conn = get_db()
    task = conn.execute("SELECT emp_id, task_name FROM onboarding_tasks WHERE task_id = ?", [tid]).fetchone()
    conn.execute("UPDATE onboarding_tasks SET status = 'Completed', completed_at = ? WHERE task_id = ?", [now_ist(), tid])
    if task:
        emp_id, task_name = task
        add_notification(emp_id, 'onboarding', f'Your "{task_name}" task has been completed by admin.', '/onboarding')
        audit_log(session['emp_id'], 'ONBOARDING_TASK_COMPLETE', f'Task "{task_name}" completed for {emp_id}')
    conn.close()
    return jsonify({'message': 'Task completed'}), 200


@onboarding_bp.route('/api/v1/onboarding-tasks/<int:tid>/progress', methods=['POST'])
@onboarding_bp.route('/api/onboarding-tasks/<int:tid>/progress', methods=['POST'])
@admin_required
def progress_onboarding_task(tid):
    conn = get_db()
    conn.execute("UPDATE onboarding_tasks SET status = 'In Progress' WHERE task_id = ? AND status = 'Pending'", [tid])
    conn.close()
    return jsonify({'message': 'Marked in progress'}), 200


@onboarding_bp.route('/api/onboarding-proceed', methods=['POST'])
@admin_required
def onboarding_proceed():
    data = request.get_json(silent=True) or {}
    emp_id = data.get('emp_id', '').strip()
    if not emp_id:
        return jsonify({'error': 'emp_id required'}), 400
    conn = get_db()
    wf = conn.execute(
        "SELECT current_step, step_1_status, step_2_status, step_3_status, step_4_status, step_5_status FROM onboarding_workflow WHERE emp_id = ?",
        [emp_id]
    ).fetchone()
    if not wf:
        conn.close()
        return jsonify({'error': 'No onboarding workflow found'}), 404
    current_step = wf[0]
    now = now_ist()
    if current_step == 2:
        all_approved = conn.execute(
            "SELECT COUNT(*) FROM onboarding_checklist WHERE emp_id = ? AND status = 'Approved'", [emp_id]
        ).fetchone()[0]
        if all_approved < 5:
            conn.close()
            return jsonify({'error': 'All 5 documents must be approved before proceeding'}), 400
        due = (now + timedelta(days=7)).date()
        tid = gen_id()
        conn.execute(
            "INSERT INTO onboarding_tasks (task_id, emp_id, task_name, assigned_to, status, due_date) VALUES (?, ?, 'System Allocation', 'Admin', 'Pending', ?)",
            [tid, emp_id, due]
        )
        conn.execute(
            "UPDATE onboarding_workflow SET current_step = 3, step_2_status = 'Completed', step_3_status = 'InProgress', updated_at = ? WHERE emp_id = ?",
            [now, emp_id]
        )
        add_notification(emp_id, 'onboarding', 'Admin has initiated system allocation. It is now in progress.', '/onboarding')
        audit_log(session['emp_id'], 'ONBOARDING_PROCEED', f'Step 2→3 (System Allocation) for {emp_id}')
        conn.close()
        return jsonify({'message': 'System Allocation task created', 'next_step': 3}), 200
    elif current_step == 3:
        sys_alloc = conn.execute(
            "SELECT COUNT(*) FROM onboarding_tasks WHERE emp_id = ? AND task_name = 'System Allocation' AND status = 'Completed'", [emp_id]
        ).fetchone()[0]
        if sys_alloc == 0:
            conn.close()
            return jsonify({'error': 'System Allocation task must be completed first'}), 400
        due = (now + timedelta(days=5)).date()
        tid = gen_id()
        conn.execute(
            "INSERT INTO onboarding_tasks (task_id, emp_id, task_name, assigned_to, status, due_date) VALUES (?, ?, 'Desk & ID Card Allocation', 'Admin', 'Pending', ?)",
            [tid, emp_id, due]
        )
        conn.execute(
            "UPDATE onboarding_workflow SET current_step = 4, step_3_status = 'Completed', step_4_status = 'InProgress', updated_at = ? WHERE emp_id = ?",
            [now, emp_id]
        )
        add_notification(emp_id, 'onboarding', 'Admin has initiated desk & ID card allocation. It is now in progress.', '/onboarding')
        audit_log(session['emp_id'], 'ONBOARDING_PROCEED', f'Step 3→4 (Desk & ID) for {emp_id}')
        conn.close()
        return jsonify({'message': 'Desk & ID Card Allocation task created', 'next_step': 4}), 200
    elif current_step == 4:
        desk_alloc = conn.execute(
            "SELECT COUNT(*) FROM onboarding_tasks WHERE emp_id = ? AND task_name = 'Desk & ID Card Allocation' AND status = 'Completed'", [emp_id]
        ).fetchone()[0]
        if desk_alloc == 0:
            conn.close()
            return jsonify({'error': 'Desk & ID Card Allocation task must be completed first'}), 400
        conn.execute(
            "UPDATE onboarding_workflow SET current_step = 5, step_4_status = 'Completed', step_5_status = 'InProgress', updated_at = ? WHERE emp_id = ?",
            [now, emp_id]
        )
        add_notification(emp_id, 'onboarding', 'Admin has proceeded to team introduction. Please complete it to finish onboarding.', '/onboarding')
        audit_log(session['emp_id'], 'ONBOARDING_PROCEED', f'Step 4→5 (Team Intro) for {emp_id}')
        conn.close()
        return jsonify({'message': 'Proceeded to Team Introduction', 'next_step': 5}), 200
    else:
        conn.close()
        return jsonify({'error': 'No next step available'}), 400


@onboarding_bp.route('/api/v1/onboarding-tasks/<int:tid>', methods=['DELETE'])
@onboarding_bp.route('/api/onboarding-tasks/<int:tid>', methods=['DELETE'])
@admin_required
def delete_onboarding_task(tid):
    conn = get_db()
    conn.execute("DELETE FROM onboarding_tasks WHERE task_id = ?", [tid])
    conn.close()
    return jsonify({'message': 'Task deleted'}), 200


@onboarding_bp.route('/api/v1/onboarding-tasks/export', methods=['GET'])
@onboarding_bp.route('/api/onboarding-tasks/export', methods=['GET'])
@admin_required
def export_onboarding():
    from io import BytesIO
    import pandas as pd
    month_filter = request.args.get('month', '').strip()
    status_filter = request.args.get('status', '').strip()
    conn = get_db()
    try:
        conditions = []
        params2: list = []
        if month_filter and '-' in month_filter:
            y, m = month_filter.split('-', 1)
            conditions.append("CAST(strftime('%m', t.due_date) AS INTEGER) = ? AND CAST(strftime('%Y', t.due_date) AS INTEGER) = ?")
            params2.extend([int(m), int(y)])
        if status_filter:
            conditions.append("t.status = ?")
            params2.append(status_filter)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = conn.execute(
            f"SELECT t.emp_id, u.name, t.task_name, t.assigned_to, t.status, t.due_date, t.completed_at "
            f"FROM onboarding_tasks t LEFT JOIN users u ON t.emp_id = u.emp_id{where} ORDER BY t.due_date ASC",
            params2
        ).fetchall()
    finally:
        conn.close()

    data_rows = [{
        'Employee ID': r[0], 'Employee Name': r[1] or r[0], 'Task': r[2],
        'Assigned To': r[3], 'Status': r[4],
        'Due Date': r[5].isoformat() if r[5] else '',
        'Completed At': r[6].isoformat() if r[6] else ''
    } for r in rows]

    buf = BytesIO()
    df = pd.DataFrame(data_rows) if data_rows else pd.DataFrame(columns=['Employee ID', 'Employee Name', 'Task', 'Assigned To', 'Status', 'Due Date', 'Completed At'])
    with pd.ExcelWriter(buf, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Onboarding')
    buf.seek(0)
    label = month_filter if month_filter else 'all'
    return send_file(buf, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     download_name=f'onboarding_{label}.xlsx', as_attachment=True)


ONBOARDING_DOC_TYPES = ['ID Proof', 'Address Proof', 'Photo', 'Previous Organisation Documents', 'Qualification Documents']

@onboarding_bp.route('/api/v1/onboarding-checklist', methods=['GET', 'POST'])
@onboarding_bp.route('/api/onboarding-checklist', methods=['GET', 'POST'])
def onboarding_checklist_api():
    is_admin = _is_admin(session.get('role'))
    is_hr = session.get('department') == 'HR'

    if request.method == 'POST':
        if not is_admin:
            if 'emp_id' not in session:
                return jsonify({'error': 'Authentication required'}), 401
            return jsonify({'error': 'Forbidden - admin access required to initiate onboarding'}), 403

    emp_id = session['emp_id'] if 'emp_id' in session else None

    if request.method == 'GET':
        if not emp_id:
            if request.is_json:
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('auth.login'))
        target_emp = request.args.get('emp_id', '').strip()
        conn = get_db()
        if target_emp and (is_admin or is_hr):
            rows = conn.execute(
                "SELECT c.checklist_id, c.emp_id, u.name, c.doc_type, c.status, c.file_name, c.uploaded_at, c.reviewed_by, c.reviewed_at, c.notes "
                "FROM onboarding_checklist c JOIN users u ON c.emp_id = u.emp_id WHERE c.emp_id = ? ORDER BY c.doc_type",
                [target_emp]
            ).fetchall()
        elif is_admin or is_hr:
            rows = conn.execute(
                "SELECT c.checklist_id, c.emp_id, u.name, c.doc_type, c.status, c.file_name, c.uploaded_at, c.reviewed_by, c.reviewed_at, c.notes "
                "FROM onboarding_checklist c JOIN users u ON c.emp_id = u.emp_id ORDER BY c.emp_id, c.doc_type"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT c.checklist_id, c.emp_id, u.name, c.doc_type, c.status, c.file_name, c.uploaded_at, c.reviewed_by, c.reviewed_at, c.notes "
                "FROM onboarding_checklist c JOIN users u ON c.emp_id = u.emp_id WHERE c.emp_id = ? ORDER BY c.doc_type",
                [emp_id]
            ).fetchall()

        employees_with_checklist = {}
        for r in rows:
            eid = r[1]
            if eid not in employees_with_checklist:
                employees_with_checklist[eid] = {'emp_id': eid, 'employee': r[2] or eid, 'docs': [], 'progress': {'uploaded': 0, 'approved': 0, 'total': 0}}
            doc = {
                'id': r[0], 'doc_type': r[3], 'status': r[4], 'file_name': r[5],
                'uploaded_at': r[6].isoformat() if r[6] else None,
                'reviewed_by': r[7], 'reviewed_at': r[8].isoformat() if r[8] else None,
                'notes': r[9]
            }
            employees_with_checklist[eid]['docs'].append(doc)
            employees_with_checklist[eid]['progress']['total'] += 1
            if r[4] in ('Uploaded', 'Approved'):
                employees_with_checklist[eid]['progress']['uploaded'] += 1
            if r[4] == 'Approved':
                employees_with_checklist[eid]['progress']['approved'] += 1

        wf_rows = conn.execute("SELECT emp_id, current_step, step_1_status, step_2_status, step_3_status, step_4_status, step_5_status, intro_completed_by FROM onboarding_workflow").fetchall()
        wf_map = {}
        for w in wf_rows:
            wf_map[w[0]] = {
                'current_step': w[1],
                'steps': [
                    {'step': 1, 'label': 'Document Upload', 'status': w[2]},
                    {'step': 2, 'label': 'Document Validated by HR', 'status': w[3]},
                    {'step': 3, 'label': 'System Allocated', 'status': w[4]},
                    {'step': 4, 'label': 'Desk & ID Card Given', 'status': w[5]},
                    {'step': 5, 'label': 'Introduction with Team', 'status': w[6]},
                ],
                'intro_completed_by': w[7]
            }
        conn.close()
        for eid, data in employees_with_checklist.items():
            data['workflow'] = wf_map.get(eid, {'current_step': 0, 'steps': [], 'intro_completed_by': None})
        return jsonify(list(employees_with_checklist.values())), 200

    data = request.get_json(silent=True) or {}
    target_emp = data.get('emp_id', '').strip()
    if not target_emp:
        return jsonify({'error': 'emp_id required'}), 400
    conn = get_db()
    wf_check = conn.execute("SELECT current_step FROM onboarding_workflow WHERE emp_id = ?", [target_emp]).fetchone()
    if wf_check:
        conn.close()
        return jsonify({'error': 'Onboarding already exists for this employee. Cannot re-initiate.'}), 409
    cid = gen_id()
    for i, dt in enumerate(ONBOARDING_DOC_TYPES):
        conn.execute(
            "INSERT INTO onboarding_checklist (checklist_id, emp_id, doc_type, status) VALUES (?, ?, ?, 'Pending')",
            [cid + i, target_emp, dt]
        )
    now = now_ist()
    conn.execute(
        "INSERT INTO onboarding_workflow (emp_id, current_step, step_1_status, created_at, updated_at) VALUES (?, 1, 'InProgress', ?, ?)",
        [target_emp, now, now]
    )
    conn.execute("UPDATE users SET status = 'Onboarding' WHERE emp_id = ?", [target_emp])
    conn.close()
    audit_log(session['emp_id'], 'ONBOARDING_INITIATE', f'Onboarding initiated for {target_emp}')
    return jsonify({'message': 'Onboarding initiated', 'emp_id': target_emp}), 201


@onboarding_bp.route('/api/v1/onboarding-checklist/<int:cid>/upload', methods=['POST'])
@onboarding_bp.route('/api/onboarding-checklist/<int:cid>/upload', methods=['POST'])
@login_required
def upload_onboarding_doc(cid):
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    f = request.files['file']
    error = validate_upload(f)
    if error:
        return jsonify({'error': error}), 400
    conn = get_db()
    row = conn.execute("SELECT emp_id, status, doc_type FROM onboarding_checklist WHERE checklist_id = ?", [cid]).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Checklist item not found'}), 404
    if row[0] != session['emp_id'] and not _is_admin(session.get('role')) and session.get('role') != 'HR':
        conn.close()
        return jsonify({'error': 'Not authorized'}), 403
    doc_type = row[2] or ''
    ext = os.path.splitext(f.filename)[1] or '.bin'
    filename = f"onboarding_{cid}_{int(now_ist().timestamp())}{ext}"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    f.save(filepath)
    file_size = os.path.getsize(filepath)
    max_bytes = 1 * 1024 * 1024 if doc_type == 'Photo' else 250 * 1024
    if file_size > max_bytes:
        os.remove(filepath)
        label = '1 MB' if doc_type == 'Photo' else '250 KB'
        size_mb = round(file_size / (1024 * 1024), 1)
        conn.close()
        return jsonify({'error': f'{doc_type} must be under {label}. Your file is {size_mb} MB'}), 413
    conn.execute(
        "UPDATE onboarding_checklist SET status = 'Uploaded', file_name = ?, file_path = ?, uploaded_at = ? WHERE checklist_id = ?",
        [f.filename, filename, now_ist(), cid]
    )
    conn.close()
    return jsonify({'message': 'File uploaded', 'file_name': f.filename}), 200


@onboarding_bp.route('/api/v1/onboarding-checklist/<int:cid>/file')
@onboarding_bp.route('/api/onboarding-checklist/<int:cid>/file')
@login_required
def view_onboarding_file(cid):
    conn = get_db()
    row = conn.execute("SELECT file_path, file_name, emp_id FROM onboarding_checklist WHERE checklist_id = ?", [cid]).fetchone()
    conn.close()
    if not row or not row[0]:
        return jsonify({'error': 'File not found'}), 404
    if row[2] != session['emp_id'] and not _is_admin(session.get('role')) and session.get('department') != 'HR':
        return jsonify({'error': 'Not authorized'}), 403
    filepath = os.path.join(UPLOAD_FOLDER, row[0])
    if not os.path.exists(filepath):
        return jsonify({'error': 'File not found on disk'}), 404
    ext = os.path.splitext(row[1] or '')[1].lower()
    mime_map = {'.pdf': 'application/pdf', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.doc': 'application/msword', '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}
    return send_file(filepath, mimetype=mime_map.get(ext, 'application/octet-stream'))


@onboarding_bp.route('/api/v1/onboarding-checklist/<int:cid>/review', methods=['POST'])
@onboarding_bp.route('/api/onboarding-checklist/<int:cid>/review', methods=['POST'])
@admin_required
def review_onboarding_doc(cid):
    data = request.get_json(silent=True) or {}
    action = data.get('action', '').strip()
    notes = data.get('notes', '').strip()
    if action not in ('Approved', 'Rejected'):
        return jsonify({'error': 'action must be Approved or Rejected'}), 400
    if action == 'Rejected' and not notes:
        return jsonify({'error': 'Rejection reason is required'}), 400
    conn = get_db()
    conn.execute(
        "UPDATE onboarding_checklist SET status = ?, reviewed_by = ?, reviewed_at = ?, notes = ? WHERE checklist_id = ?",
        [action, session['emp_id'], now_ist(), notes or None, cid]
    )
    row = conn.execute("SELECT emp_id FROM onboarding_checklist WHERE checklist_id = ?", [cid]).fetchone()
    emp_id = row[0] if row else None
    if emp_id and action == 'Rejected':
        doc_row = conn.execute("SELECT doc_type FROM onboarding_checklist WHERE checklist_id = ?", [cid]).fetchone()
        doc_name = doc_row[0] if doc_row else 'document'
        add_notification(emp_id, 'onboarding', f'Your {doc_name} was rejected. Reason: {notes or "No reason provided"}. Please re-upload.', '/onboarding')
    if emp_id and action == 'Approved':
        pending = conn.execute(
            "SELECT COUNT(*) FROM onboarding_checklist WHERE emp_id = ? AND status != 'Approved'", [emp_id]
        ).fetchone()[0]
        if pending == 0:
            wf = conn.execute("SELECT current_step FROM onboarding_workflow WHERE emp_id = ?", [emp_id]).fetchone()
            if wf and wf[0] == 2:
                add_notification(emp_id, 'onboarding', 'All your onboarding documents have been approved! Waiting for admin to proceed.', '/onboarding')
    audit_log(session['emp_id'], 'ONBOARDING_DOC_' + action.upper(), f'Document {cid} {action.lower()} for {emp_id}')
    conn.close()
    return jsonify({'message': f'Document {action.lower()}'}), 200


@onboarding_bp.route('/api/v1/onboarding-checklist/<int:cid>', methods=['DELETE'])
@onboarding_bp.route('/api/onboarding-checklist/<int:cid>', methods=['DELETE'])
@admin_required
def delete_onboarding_checklist(cid):
    conn = get_db()
    conn.execute("DELETE FROM onboarding_checklist WHERE checklist_id = ?", [cid])
    conn.close()
    return jsonify({'message': 'Deleted'}), 200


@onboarding_bp.route('/api/v1/onboarding-initiate-setup', methods=['POST'])
@onboarding_bp.route('/api/onboarding-initiate-setup', methods=['POST'])
@admin_required
def initiate_setup_tasks():
    data = request.get_json(silent=True) or {}
    emp_id = data.get('emp_id', '').strip()
    if not emp_id:
        return jsonify({'error': 'emp_id required'}), 400
    conn = get_db()
    pending = conn.execute(
        "SELECT COUNT(*) FROM onboarding_checklist WHERE emp_id = ? AND status != 'Approved'", [emp_id]
    ).fetchone()[0]
    if pending > 0:
        conn.close()
        return jsonify({'error': f'{pending} document(s) not yet approved'}), 400
    existing = conn.execute(
        "SELECT COUNT(*) FROM onboarding_tasks WHERE emp_id = ? AND task_name IN ('IT System Setup', 'ID Card & Desk Allocation')", [emp_id]
    ).fetchone()[0]
    if existing > 0:
        conn.close()
        return jsonify({'error': 'Setup tasks already exist for this employee'}), 409
    tid = gen_id()
    due = (now_ist() + timedelta(days=7)).date()
    conn.execute(
        "INSERT INTO onboarding_tasks (task_id, emp_id, task_name, assigned_to, status, due_date) VALUES (?, ?, 'IT System Setup', 'Admin', 'Pending', ?)",
        [tid, emp_id, due]
    )
    conn.execute(
        "INSERT INTO onboarding_tasks (task_id, emp_id, task_name, assigned_to, status, due_date) VALUES (?, ?, 'ID Card & Desk Allocation', 'Admin', 'Pending', ?)",
        [tid + 1, emp_id, due]
    )
    conn.close()
    return jsonify({'message': 'IT & Admin setup tasks created'}), 201


@onboarding_bp.route('/api/onboarding-checklist/submit', methods=['POST'])
@login_required
def submit_docs_for_approval():
    emp_id = session['emp_id']
    conn = get_db()
    wf = conn.execute("SELECT current_step FROM onboarding_workflow WHERE emp_id = ?", [emp_id]).fetchone()
    if not wf or wf[0] != 1:
        conn.close()
        return jsonify({'error': 'Not at step 1 or no onboarding in progress'}), 400
    docs = conn.execute(
        "SELECT checklist_id, status FROM onboarding_checklist WHERE emp_id = ?", [emp_id]
    ).fetchall()
    if not docs:
        conn.close()
        return jsonify({'error': 'No documents found'}), 400
    uploaded = sum(1 for d in docs if d[1] in ('Uploaded', 'Approved'))
    if uploaded == 0:
        conn.close()
        return jsonify({'error': 'Upload at least one document before submitting'}), 400
    now = now_ist()
    conn.execute(
        "UPDATE onboarding_workflow SET current_step = 2, step_1_status = 'Completed', step_2_status = 'InProgress', updated_at = ? WHERE emp_id = ?",
        [now, emp_id]
    )
    notify_admins('onboarding', f'{session.get("name") or emp_id} has submitted onboarding documents for HR review.', '/onboarding')
    conn.close()
    return jsonify({'message': 'Documents submitted for HR approval'}), 200


@onboarding_bp.route('/api/onboarding/complete-intro', methods=['POST'])
@login_required
def complete_intro():
    emp_id = session['emp_id']
    conn = get_db()
    wf = conn.execute("SELECT current_step FROM onboarding_workflow WHERE emp_id = ?", [emp_id]).fetchone()
    if not wf or wf[0] != 5:
        conn.close()
        return jsonify({'error': 'Not at step 5 or no onboarding in progress'}), 400
    now = now_ist()
    conn.execute(
        "UPDATE onboarding_workflow SET step_5_status = 'Completed', intro_completed_by = ?, updated_at = ? WHERE emp_id = ?",
        [emp_id, now, emp_id]
    )
    conn.execute("UPDATE users SET status = 'Active' WHERE emp_id = ? AND status = 'Onboarding'", [emp_id])
    add_notification(emp_id, 'onboarding', 'Your onboarding is complete! Welcome to the team. Your account is now fully active.', '/dashboard')
    notify_admins('onboarding', f'{session.get("name") or emp_id} has completed their onboarding and is now active.', '/onboarding')
    conn.close()
    return jsonify({'message': 'Introduction completed'}), 200


@onboarding_bp.route('/api/onboarding-status', methods=['GET'])
@login_required
def onboarding_status_api():
    is_admin = _is_admin(session.get('role'))
    is_hr = session.get('department') == 'HR'
    if not is_admin and not is_hr:
        emp_id = session['emp_id']
        conn = get_db()
        wf = conn.execute(
            "SELECT current_step, step_1_status, step_2_status, step_3_status, step_4_status, step_5_status FROM onboarding_workflow WHERE emp_id = ?",
            [emp_id]
        ).fetchone()
        conn.close()
        if not wf:
            return jsonify({'status': 'none', 'current_step': 0, 'steps': []}), 200
        steps = [
            {'step': 1, 'label': 'Document Upload', 'status': wf[1]},
            {'step': 2, 'label': 'Document Validation', 'status': wf[2]},
            {'step': 3, 'label': 'System Allocation', 'status': wf[3]},
            {'step': 4, 'label': 'Desk & ID Card', 'status': wf[4]},
            {'step': 5, 'label': 'Team Introduction', 'status': wf[5]},
        ]
        completed = all(s['status'] == 'Completed' for s in steps)
        return jsonify({'status': 'completed' if completed else 'in_progress', 'current_step': wf[0], 'steps': steps}), 200
    conn = get_db()
    active_onboarding = []
    rows = conn.execute(
        "SELECT u.emp_id, u.name, u.department, u.status, "
        "w.current_step, w.step_1_status, w.step_2_status, w.step_3_status, w.step_4_status, w.step_5_status "
        "FROM users u JOIN onboarding_workflow w ON u.emp_id = w.emp_id "
        "WHERE u.status = 'Onboarding' "
        "ORDER BY u.name ASC"
    ).fetchall()
    for r in rows:
        emp = {
            'emp_id': r[0], 'name': r[1] or r[0], 'department': r[2] or 'N/A',
            'status': r[3], 'current_step': r[4] or 0,
            'steps': [
                {'step': 1, 'label': 'Document Upload', 'status': r[5] or 'Pending'},
                {'step': 2, 'label': 'Document Validation', 'status': r[6] or 'Pending'},
                {'step': 3, 'label': 'System Allocation', 'status': r[7] or 'Pending'},
                {'step': 4, 'label': 'Desk & ID Card', 'status': r[8] or 'Pending'},
                {'step': 5, 'label': 'Team Introduction', 'status': r[9] or 'Pending'},
            ]
        }
        completed_steps = sum(1 for s in emp['steps'] if s['status'] == 'Completed')
        emp['progress'] = completed_steps
        pending_labels = [s['label'] for s in emp['steps'] if s['status'] != 'Completed']
        emp['current_status'] = pending_labels[0] if pending_labels else 'In Progress'
        active_onboarding.append(emp)
    conn.close()
    return jsonify({'active': active_onboarding}), 200
