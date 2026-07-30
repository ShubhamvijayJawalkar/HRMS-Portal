from flask import Blueprint, render_template, request, jsonify, session
from .db import get_db
from .helpers import now_ist, gen_id, _is_admin, audit_log
from .decorators import login_required, admin_required, hr_or_admin_required

tickets_bp = Blueprint('tickets', __name__)


@tickets_bp.route('/admin/tickets')
@hr_or_admin_required
def admin_tickets():
    return render_template('admin_tickets.html')


@tickets_bp.route('/tickets')
@login_required
def tickets_page():
    return render_template('tickets.html')


@tickets_bp.route('/api/v1/tickets', methods=['GET', 'POST'])
@tickets_bp.route('/api/tickets', methods=['GET', 'POST'])
@login_required
def tickets_api():
    if request.method == 'GET':
        conn = get_db()
        if _is_admin(session.get('role')):
            rows = conn.execute("SELECT t.ticket_id, t.emp_id, u.name, t.subject, t.category, t.priority, t.status, t.assigned_to, t.created_at, t.updated_at FROM tickets t JOIN users u ON t.emp_id = u.emp_id ORDER BY t.created_at DESC").fetchall()
        else:
            rows = conn.execute("SELECT t.ticket_id, t.emp_id, u.name, t.subject, t.category, t.priority, t.status, t.assigned_to, t.created_at, t.updated_at FROM tickets t JOIN users u ON t.emp_id = u.emp_id WHERE t.emp_id = ? ORDER BY t.created_at DESC", [session['emp_id']]).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'subject': r[3], 'category': r[4], 'priority': r[5], 'status': r[6], 'assigned_to': r[7], 'created_at': r[8].isoformat() + '+05:30' if r[8] else None, 'updated_at': r[9].isoformat() + '+05:30' if r[9] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('subject'):
        return jsonify({'error': 'subject required'}), 400
    tid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO tickets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 [tid, session['emp_id'], data['subject'], data.get('description'), data.get('category'), data.get('priority', 'Medium'),
                  'Open', None, now_ist(), None, None])
    conn.close()
    audit_log(session['emp_id'], 'TICKET_CREATE', f'Ticket {tid}: {data["subject"][:60]}')
    return jsonify({'message': 'Ticket created', 'id': tid}), 201


@tickets_bp.route('/api/v1/tickets/<int:tid>')
@tickets_bp.route('/api/tickets/<int:tid>')
@login_required
def ticket_detail(tid):
    conn = get_db()
    row = conn.execute("SELECT t.ticket_id, t.emp_id, u.name, t.subject, t.description, t.category, t.priority, t.status, t.assigned_to, t.created_at, t.updated_at, t.resolved_at FROM tickets t JOIN users u ON t.emp_id = u.emp_id WHERE t.ticket_id = ?", [tid]).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Not found'}), 404
    if not _is_admin(session.get('role')) and session['emp_id'] != row[1]:
        conn.close()
        return jsonify({'error': 'Forbidden'}), 403
    comments = conn.execute("SELECT c.comment_id, c.emp_id, u.name, c.comment, c.created_at FROM ticket_comments c JOIN users u ON c.emp_id = u.emp_id WHERE c.ticket_id = ? ORDER BY c.created_at", [tid]).fetchall()
    conn.close()
    return jsonify({
        'id': row[0], 'emp_id': row[1], 'employee': row[2], 'subject': row[3], 'description': row[4],
        'category': row[5], 'priority': row[6], 'status': row[7], 'assigned_to': row[8],
        'created_at': row[9].isoformat() + '+05:30' if row[9] else None,
        'updated_at': row[10].isoformat() + '+05:30' if row[10] else None,
        'resolved_at': row[11].isoformat() + '+05:30' if row[11] else None,
        'comments': [{'id': c[0], 'emp_id': c[1], 'name': c[2], 'comment': c[3], 'created_at': c[4].isoformat() + '+05:30' if c[4] else None} for c in comments]
    }), 200


@tickets_bp.route('/api/v1/tickets/<int:tid>/comment', methods=['POST'])
@tickets_bp.route('/api/tickets/<int:tid>/comment', methods=['POST'])
@login_required
def add_ticket_comment(tid):
    data = request.get_json(silent=True) or {}
    if not data.get('comment'):
        return jsonify({'error': 'comment required'}), 400
    conn = get_db()
    chk = conn.execute("SELECT 1 FROM tickets WHERE ticket_id = ?", [tid]).fetchone()
    if not chk:
        conn.close()
        return jsonify({'error': 'Ticket not found'}), 404
    cid = gen_id()
    conn.execute("INSERT INTO ticket_comments VALUES (?, ?, ?, ?, ?)", [cid, tid, session['emp_id'], data['comment'], now_ist()])
    conn.execute("UPDATE tickets SET updated_at = ? WHERE ticket_id = ?", [now_ist(), tid])
    conn.close()
    return jsonify({'message': 'Comment added', 'id': cid}), 201


@tickets_bp.route('/api/v1/tickets/<int:tid>/status', methods=['PUT'])
@tickets_bp.route('/api/tickets/<int:tid>/status', methods=['PUT'])
@login_required
def update_ticket_status(tid):
    data = request.get_json(silent=True) or {}
    status = data.get('status')
    if status not in ('Open', 'In Progress', 'Resolved', 'Closed'):
        return jsonify({'error': 'Invalid status'}), 400
    conn = get_db()
    now = now_ist()
    resolved_at = now if status == 'Resolved' else None
    conn.execute("UPDATE tickets SET status = ?, updated_at = ?, resolved_at = ? WHERE ticket_id = ?", [status, now, resolved_at, tid])
    audit_log(session['emp_id'], 'TICKET_STATUS_UPDATE', f'Ticket {tid} status → {status}')
    conn.close()
    return jsonify({'message': f'Status set to {status}'}), 200


@tickets_bp.route('/api/v1/tickets/<int:tid>/assign', methods=['PUT'])
@tickets_bp.route('/api/tickets/<int:tid>/assign', methods=['PUT'])
@admin_required
def assign_ticket(tid):
    data = request.get_json(silent=True) or {}
    conn = get_db()
    conn.execute("UPDATE tickets SET assigned_to = ?, updated_at = ? WHERE ticket_id = ?", [data.get('assigned_to'), now_ist(), tid])
    audit_log(session['emp_id'], 'TICKET_ASSIGN', f'Ticket {tid} assigned to {data.get("assigned_to", "unassigned")}')
    conn.close()
    return jsonify({'message': 'Ticket assigned'}), 200
