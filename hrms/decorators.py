from functools import wraps

from flask import session, request, redirect, url_for, jsonify

from .helpers import _is_admin


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'emp_id' not in session:
            if request.is_json:
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'emp_id' not in session:
            if request.is_json:
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('auth.login'))
        if not _is_admin(session.get('role')):
            if request.is_json:
                return jsonify({'error': 'Forbidden'}), 403
            return redirect(url_for('auth.dashboard'))
        return f(*args, **kwargs)
    return decorated


def hr_or_admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'emp_id' not in session:
            if request.is_json:
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('auth.login'))
        if _is_admin(session.get('role')) or session.get('department') == 'HR':
            return f(*args, **kwargs)
        if request.is_json:
            return jsonify({'error': 'Forbidden - HR access required'}), 403
        return redirect(url_for('auth.dashboard'))
    return decorated


def department_required(*depts):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if 'emp_id' not in session:
                if request.is_json:
                    return jsonify({'error': 'Authentication required'}), 401
                return redirect(url_for('auth.login'))
            if _is_admin(session.get('role')) or session.get('department') in depts:
                return f(*args, **kwargs)
            if request.is_json:
                return jsonify({'error': 'Forbidden - insufficient department access'}), 403
            return redirect(url_for('auth.dashboard'))
        return decorated
    return decorator


def manager_or_admin_required(f):
    """Require Admin role OR the user must be the manager of the target employee.

    Resolves the target employee from (in order of priority):
      1. 'emp_id' route parameter
      2. 'emp_id' in JSON body
      3. Current session user (self-service)

    If the target employee's ``manager_emp_id`` matches the current user,
    access is granted.  Admin users always pass.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'emp_id' not in session:
            if request.is_json:
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('auth.login'))

        current_emp_id = session['emp_id']

        target_emp_id = kwargs.get('emp_id')
        if not target_emp_id:
            data = request.get_json(silent=True) or {}
            target_emp_id = data.get('emp_id')
        if not target_emp_id:
            target_emp_id = current_emp_id

        if _is_admin(session.get('role')):
            return f(*args, **kwargs)

        conn = get_db()
        try:
            target_user = conn.execute(
                "SELECT manager_emp_id FROM users WHERE emp_id = ?", [target_emp_id]
            ).fetchone()
            if target_user and target_user[0] == current_emp_id:
                return f(*args, **kwargs)

            if request.is_json:
                return jsonify({'error': 'Forbidden - manager or admin access required'}), 403
            return redirect(url_for('auth.dashboard'))
        finally:
            conn.close()
    return decorated
