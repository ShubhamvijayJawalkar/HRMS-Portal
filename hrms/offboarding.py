from flask import Blueprint, jsonify, render_template, request, session

from .db import get_db
from .decorators import admin_required, login_required
from .helpers import _is_admin, add_notification, audit_log, gen_id, notify_admins, now_ist, parse_date

offboarding_bp = Blueprint('offboarding', __name__)


@offboarding_bp.route('/offboarding')
@login_required
def offboarding_page():
    is_admin = _is_admin(session.get('role'))
    is_hr = session.get('department') == 'HR'
    return render_template('offboarding.html', is_admin=is_admin, is_hr=is_hr)


@offboarding_bp.route('/api/v1/offboarding-tasks', methods=['GET', 'POST'])
@offboarding_bp.route('/api/offboarding-tasks', methods=['GET', 'POST'])
@login_required
def offboarding_api():
    if request.method == 'GET':
        conn = get_db()
        if _is_admin(session.get('role')):
            rows = conn.execute("SELECT t.task_id, t.emp_id, u.name, t.task_name, t.assigned_to, t.status, t.due_date, t.completed_at FROM offboarding_tasks t JOIN users u ON t.emp_id = u.emp_id ORDER BY t.task_id DESC").fetchall()
        else:
            rows = conn.execute("SELECT t.task_id, t.emp_id, u.name, t.task_name, t.assigned_to, t.status, t.due_date, t.completed_at FROM offboarding_tasks t JOIN users u ON t.emp_id = u.emp_id WHERE t.emp_id = ? ORDER BY t.task_id DESC", [session['emp_id']]).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'task': r[3], 'assigned_to': r[4], 'status': r[5], 'due_date': r[6].isoformat() if r[6] else None, 'completed_at': r[7].isoformat() + '+05:30' if r[7] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('emp_id') or not data.get('task_name'):
        return jsonify({'error': 'emp_id and task_name required'}), 400
    tid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO offboarding_tasks VALUES (?, ?, ?, ?, ?, ?, ?)",
                 [tid, data['emp_id'], data['task_name'], data.get('assigned_to', 'HR'), 'Pending', parse_date(data.get('due_date')), None])
    conn.close()
    return jsonify({'message': 'Task added', 'id': tid}), 201


@offboarding_bp.route('/api/v1/offboarding-tasks/<int:tid>/complete', methods=['POST'])
@offboarding_bp.route('/api/offboarding-tasks/<int:tid>/complete', methods=['POST'])
@admin_required
def complete_offboarding_task(tid):
    conn = get_db()
    conn.execute("UPDATE offboarding_tasks SET status = 'Completed', completed_at = ? WHERE task_id = ?", [now_ist(), tid])
    audit_log(session['emp_id'], 'OFFBOARDING_TASK_COMPLETE', f'Offboarding task {tid} completed')
    conn.close()
    return jsonify({'message': 'Task completed'}), 200


@offboarding_bp.route('/api/v1/exit-interviews', methods=['GET', 'POST'])
@offboarding_bp.route('/api/exit-interviews', methods=['GET', 'POST'])
@admin_required
def exit_interviews_api():
    if request.method == 'GET':
        conn = get_db()
        rows = conn.execute("SELECT ei.interview_id, ei.emp_id, u.name, ei.reason, ei.feedback, ei.exit_date, ei.created_at FROM exit_interviews ei JOIN users u ON ei.emp_id = u.emp_id ORDER BY ei.created_at DESC").fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'reason': r[3], 'feedback': r[4], 'exit_date': r[5].isoformat() if r[5] else None, 'created_at': r[6].isoformat() + '+05:30' if r[6] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('emp_id') or not data.get('reason') or not data.get('exit_date'):
        return jsonify({'error': 'emp_id, reason, exit_date required'}), 400
    eid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO exit_interviews VALUES (?, ?, ?, ?, ?, ?)",
                 [eid, data['emp_id'], data['reason'], data.get('feedback'), parse_date(data['exit_date']), now_ist()])
    audit_log(session['emp_id'], 'EXIT_INTERVIEW_CREATE', f'Exit interview recorded for {data["emp_id"]}')
    conn.close()
    return jsonify({'message': 'Exit interview recorded'}), 201


@offboarding_bp.route('/api/offboarding-workflow', methods=['GET'])
@login_required
def offboarding_workflow_api():
    is_admin = _is_admin(session.get('role'))
    is_hr = session.get('department') == 'HR'
    conn = get_db()
    if is_admin or is_hr:
        rows = conn.execute(
            "SELECT w.workflow_id, w.emp_id, u.name, u.department, w.step1_done, w.step2_done, w.step3_done, w.step4_done, w.step5_done, w.completed, w.created_at, w.updated_at "
            "FROM offboarding_workflow w JOIN users u ON w.emp_id = u.emp_id ORDER BY u.name ASC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT w.workflow_id, w.emp_id, u.name, u.department, w.step1_done, w.step2_done, w.step3_done, w.step4_done, w.step5_done, w.completed, w.created_at, w.updated_at "
            "FROM offboarding_workflow w JOIN users u ON w.emp_id = u.emp_id WHERE w.emp_id = ?",
            [session['emp_id']]
        ).fetchall()
    conn.close()
    result = []
    steps_labels = [
        'Resignation/Termination Recorded',
        'Manager Clearance',
        'Asset Return',
        'Final Settlement',
        'Exit Interview & Access Revocation'
    ]
    for r in rows:
        steps = []
        done_flags = [r[4], r[5], r[6], r[7], r[8]]
        for i in range(5):
            steps.append({'step': i + 1, 'label': steps_labels[i], 'done': bool(done_flags[i])})
        result.append({
            'workflow_id': r[0],
            'emp_id': r[1],
            'employee': r[2] or r[1],
            'department': r[3] or 'N/A',
            'steps': steps,
            'completed': bool(r[9]),
            'created_at': r[10].isoformat() if r[10] else None,
            'updated_at': r[11].isoformat() if r[11] else None
        })
    return jsonify(result), 200


@offboarding_bp.route('/api/offboarding-proceed', methods=['POST'])
@admin_required
def offboarding_proceed():
    data = request.get_json(silent=True) or {}
    emp_id = data.get('emp_id', '').strip()
    if not emp_id:
        return jsonify({'error': 'emp_id required'}), 400
    conn = get_db()
    wf = conn.execute(
        "SELECT workflow_id, step1_done, step2_done, step3_done, step4_done, step5_done, completed FROM offboarding_workflow WHERE emp_id = ?",
        [emp_id]
    ).fetchone()
    if not wf:
        conn.close()
        return jsonify({'error': 'No offboarding workflow found. Please create one first.'}), 404
    if wf[6]:
        conn.close()
        return jsonify({'error': 'Offboarding already completed'}), 400
    now = now_ist()
    steps_map = [
        ('step1_done', 1),
        ('step2_done', 2),
        ('step3_done', 3),
        ('step4_done', 4),
        ('step5_done', 5),
    ]
    next_step = None
    for col, step_num in steps_map:
        if not wf[step_num]:
            next_step = (col, step_num)
            break
    if not next_step:
        conn.close()
        return jsonify({'error': 'All steps already completed'}), 400
    col, step_num = next_step
    if step_num > 1:
        if not wf[step_num - 1]:
            conn.close()
            return jsonify({'error': 'Previous step must be completed first'}), 400
    conn.execute(f"UPDATE offboarding_workflow SET {col} = 1, updated_at = ? WHERE emp_id = ?", [now, emp_id])
    step_labels = ['Resignation/Termination Recorded', 'Manager Clearance', 'Asset Return', 'Final Settlement', 'Exit Interview & Access Revocation']
    add_notification(emp_id, 'offboarding', f'Offboarding step "{step_labels[step_num - 1]}" completed by admin.', '/offboarding')
    audit_log(session['emp_id'], 'OFFBOARDING_PROCEED', f'Step {step_num} ({step_labels[step_num - 1]}) completed for {emp_id}')
    if step_num == 5:
        conn.execute("UPDATE offboarding_workflow SET completed = 1, updated_at = ? WHERE emp_id = ?", [now, emp_id])
        conn.execute("UPDATE users SET status = 'Inactive' WHERE emp_id = ?", [emp_id])
        emp_name = conn.execute("SELECT name FROM users WHERE emp_id = ?", [emp_id]).fetchone()
        notify_admins('offboarding', f'{emp_name[0] if emp_name else emp_id} has been fully offboarded and status set to Inactive.', '/offboarding')
    conn.close()
    return jsonify({'message': f'Step {step_num} completed: {step_labels[step_num - 1]}', 'next_step': step_num + 1 if step_num < 5 else None, 'completed': step_num == 5}), 200
