import logging
import secrets
from datetime import timedelta

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from .db import get_db
from .extensions import limiter
from .helpers import (
    _get_shift_date_for_dt,
    _is_admin,
    audit_log,
    check_password,
    gen_id,
    hash_password,
    now_ist,
    send_email,
)

logger = logging.getLogger('hrms')

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/')
def index():
    if 'emp_id' in session:
        return redirect(url_for('auth.dashboard'))
    return redirect(url_for('auth.login'))


@auth_bp.route('/api/credentials')
def get_credentials():
    # Inline check since login_required redirects to url_for('auth.login')
    if 'emp_id' not in session:
        return jsonify({'error': 'Authentication required'}), 401
    if not _is_admin(session.get('role')):
        return jsonify({'error': 'Admin access required'}), 403
    conn = get_db()
    rows = conn.execute("SELECT emp_id, name, role, department FROM users ORDER BY emp_id").fetchall()
    conn.close()
    result = [{'emp_id': r[0], 'name': r[1], 'role': r[2], 'department': r[3] or '-'} for r in rows]
    return jsonify(result), 200


@auth_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit("20 per minute")
def login():
    if request.method == 'GET':
        return render_template('login.html')

    data = request.get_json(silent=True) or {}
    emp_id = data.get('emp_id', '').strip().upper()
    password = data.get('password', '')

    if not emp_id or not password:
        return jsonify({'error': 'Missing credentials'}), 400

    conn = get_db()
    row = conn.execute(
        "SELECT emp_id, name, role, password, status, allow_login, department FROM users WHERE emp_id = ?",
        [emp_id]
    ).fetchone()
    conn.close()

    if not row:
        return jsonify({'error': 'Invalid Employee ID'}), 401

    stored_hash = row[3]
    if not check_password(password, stored_hash):
        return jsonify({'error': 'Invalid Password'}), 401

    if not row[5]:
        return jsonify({'error': 'Login is not allowed for this user'}), 403
    if row[4] == 'Blocked':
        return jsonify({'error': 'Account is blocked'}), 403

    session_id = gen_id()
    now = now_ist()
    conn = get_db()
    shift_date = _get_shift_date_for_dt(row[0], now, conn)
    conn.execute(
        "INSERT INTO user_sessions (session_id, emp_id, login_time, session_date) VALUES (?, ?, ?, ?)",
        [session_id, row[0], now, shift_date]
    )
    conn.close()

    session.clear()
    session['emp_id'] = row[0]
    session['name'] = row[1]
    session['role'] = row[2]
    session['department'] = row[6] or ''
    session['session_id'] = session_id

    audit_log(row[0], 'LOGIN', f'User {row[1]} logged in')
    return jsonify({'message': 'Login successful', 'redirect': '/dashboard'}), 200


@auth_bp.route('/logout')
def logout():
    emp_id = session.get('emp_id')
    session_id = session.get('session_id')
    if emp_id:
        conn = get_db()
        if session_id:
            sess = conn.execute(
                "SELECT login_time FROM user_sessions WHERE session_id = ? AND emp_id = ? AND logout_time IS NULL",
                [session_id, emp_id]
            ).fetchone()
        else:
            sess = conn.execute(
                "SELECT session_id, login_time FROM user_sessions WHERE emp_id = ? AND logout_time IS NULL ORDER BY login_time DESC LIMIT 1",
                [emp_id]
            ).fetchone()
        if sess:
            if session_id:
                login_time, curr_sid = sess[0], session_id
            else:
                curr_sid, login_time = sess[0], sess[1]
            logout_time = now_ist()
            hours = round((logout_time - login_time).total_seconds() / 3600, 2)
            conn.execute(
                "UPDATE user_sessions SET logout_time = ?, total_hours = ? WHERE session_id = ?",
                [logout_time, hours, curr_sid]
            )
        conn.close()
        audit_log(emp_id, 'LOGOUT', 'User logged out')
    session.clear()
    return redirect(url_for('auth.login'))


@auth_bp.route('/dashboard')
def dashboard():
    if 'emp_id' not in session:
        return redirect(url_for('auth.login'))
    if _is_admin(session.get('role')) or session.get('department') == 'HR':
        from .attendance import _compute_dashboard_stats
        return render_template('admin_dashboard.html', stats=_compute_dashboard_stats())
    from .attendance import _compute_shift_summary
    return render_template('user_dashboard.html', shift=_compute_shift_summary(session['emp_id']))


@auth_bp.route('/api/forgot-password', methods=['POST'])
@limiter.limit("5 per minute")
def forgot_password():
    data = request.get_json(silent=True) or {}
    emp_id = data.get('emp_id', '').strip().upper()
    email = data.get('email', '')

    conn = get_db()
    row = conn.execute(
        "SELECT email FROM users WHERE emp_id = ? AND email = ?",
        [emp_id, email]
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'No matching user found'}), 404

    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO password_reset_tokens (token_id, emp_id, token, expires_at) VALUES (?, ?, ?, ?)",
        [gen_id(), emp_id, token, now_ist() + timedelta(hours=1)]
    )
    conn.close()

    reset_link = f"{request.host_url}reset-password?token={token}"
    send_email(email, 'HRMS: Password Reset',
        f'<p>Click <a href="{reset_link}">here</a> to reset your password.</p><p>Link expires in 1 hour.</p>')

    return jsonify({
        'message': 'If the account exists, a reset link has been generated.',
    }), 200


@auth_bp.route('/api/reset-password', methods=['POST'])
@limiter.limit("5 per minute")
def reset_password():
    data = request.get_json(silent=True) or {}
    token = data.get('token', '')
    new_pwd = data.get('new_password', '')

    if len(new_pwd) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400

    conn = get_db()
    row = conn.execute(
        "SELECT token_id, emp_id FROM password_reset_tokens WHERE token = ? AND used = 0 AND expires_at > ?",
        [token, now_ist()]
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Invalid or expired token'}), 400

    conn.execute("UPDATE password_reset_tokens SET used = 1 WHERE token_id = ?", [row[0]])
    conn.execute("UPDATE users SET password = ? WHERE emp_id = ?", [hash_password(new_pwd), row[1]])
    conn.close()
    audit_log(row[1], 'PASSWORD_RESET', 'Password reset via token')
    return jsonify({'message': 'Password reset successfully'}), 200


@auth_bp.route('/api/csrf-token', methods=['GET'])
def get_csrf_token():
    if 'emp_id' not in session:
        return jsonify({'error': 'Authentication required'}), 401
    token = secrets.token_hex(32)
    session['csrf_token'] = token
    return jsonify({'csrf_token': token})
