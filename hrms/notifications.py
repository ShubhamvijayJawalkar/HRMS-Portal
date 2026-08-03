import logging

from flask import Blueprint, jsonify, request, session

from .db import _scalar, get_db
from .decorators import admin_required, login_required
from .helpers import gen_id, send_email

logger = logging.getLogger('hrms')

notifications_bp = Blueprint('notifications', __name__)


@notifications_bp.route('/api/v1/notifications', methods=['GET'])
@notifications_bp.route('/api/notifications', methods=['GET'])
@login_required
def get_notifications():
    conn = get_db()
    rows = conn.execute(
        "SELECT notification_id, type, message, related_link, is_read, created_at FROM notifications WHERE emp_id = ? ORDER BY created_at DESC LIMIT 50",
        [session['emp_id']]
    ).fetchall()
    unread = _scalar("SELECT COUNT(*) FROM notifications WHERE emp_id = ? AND is_read = 0", [session['emp_id']])
    conn.close()
    return jsonify({
        'unread': unread,
        'data': [{'id': r[0], 'type': r[1], 'message': r[2], 'link': r[3], 'is_read': bool(r[4]), 'created_at': r[5].isoformat() + '+05:30' if r[5] else None} for r in rows]
    }), 200


@notifications_bp.route('/api/v1/notifications/read', methods=['POST'])
@notifications_bp.route('/api/notifications/read', methods=['POST'])
@login_required
def mark_notifications_read():
    conn = get_db()
    conn.execute("DELETE FROM notifications WHERE emp_id = ?", [session['emp_id']])
    conn.close()
    return jsonify({'message': 'Cleared'}), 200


@notifications_bp.route('/api/v1/send-notification-email', methods=['POST'])
@notifications_bp.route('/api/send-notification-email', methods=['POST'])
@admin_required
def send_notification_email():
    data = request.get_json(silent=True) or {}
    to = data.get('to')
    subject = data.get('subject', 'HRMS Notification')
    body = data.get('body', '')
    category = data.get('category', 'Leaves')
    if not to:
        return jsonify({'error': 'recipient required'}), 400
    pref = _scalar("SELECT email FROM notification_preferences WHERE emp_id = (SELECT emp_id FROM users WHERE email = ?) AND category = ?",
                   [to, category])
    if pref == 0:
        return jsonify({'message': 'Skipped — recipient opted out of email for this category'}), 200
    ok = send_email(to, subject, body)
    if ok:
        return jsonify({'message': 'Email sent'}), 200
    return jsonify({'warning': 'Email sending failed (SMTP may not be configured)'}), 200


@notifications_bp.route('/api/v1/notification-preferences', methods=['GET'])
@notifications_bp.route('/api/notification-preferences', methods=['GET'])
@login_required
def get_notification_preferences():
    conn = get_db()
    rows = conn.execute(
        "SELECT pref_id, category, in_app, email FROM notification_preferences WHERE emp_id = ? ORDER BY category",
        [session['emp_id']]
    ).fetchall()
    conn.close()
    categories = ['Onboarding', 'Leaves', 'Expenses', 'Tickets', 'Payroll']
    existing = {r[1]: {'pref_id': r[0], 'in_app': bool(r[2]), 'email': bool(r[3])} for r in rows}
    result = []
    for cat in categories:
        if cat in existing:
            result.append({'category': cat, 'in_app': existing[cat]['in_app'], 'email': existing[cat]['email']})
        else:
            result.append({'category': cat, 'in_app': True, 'email': True})
    return jsonify(result), 200


@notifications_bp.route('/api/v1/notification-preferences', methods=['POST'])
@notifications_bp.route('/api/notification-preferences', methods=['POST'])
@login_required
def set_notification_preferences():
    data = request.get_json(silent=True) or []
    if not isinstance(data, list):
        return jsonify({'error': 'Expected a list of preference objects'}), 400
    conn = get_db()
    emp_id = session['emp_id']
    for pref in data:
        category = pref.get('category')
        in_app = int(pref.get('in_app', 1))
        email = int(pref.get('email', 1))
        if not category:
            continue
        existing = conn.execute(
            "SELECT pref_id FROM notification_preferences WHERE emp_id = ? AND category = ?",
            [emp_id, category]
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE notification_preferences SET in_app = ?, email = ? WHERE pref_id = ?",
                [in_app, email, existing[0]]
            )
        else:
            conn.execute(
                "INSERT INTO notification_preferences (pref_id, emp_id, category, in_app, email) VALUES (?, ?, ?, ?, ?)",
                [gen_id(), emp_id, category, in_app, email]
            )
    conn.close()
    return jsonify({'message': 'Preferences updated'}), 200
