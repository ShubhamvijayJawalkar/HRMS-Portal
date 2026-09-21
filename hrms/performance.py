from flask import Blueprint, jsonify, render_template, request, session

from .db import get_db
from .decorators import admin_required, hr_or_admin_required, login_required
from .helpers import _is_admin, gen_id, now_ist, parse_date

performance_bp = Blueprint('performance', __name__)


@performance_bp.route('/admin/goals')
@hr_or_admin_required
def admin_goals():
    return render_template('goals.html')


@performance_bp.route('/admin/reviews')
@hr_or_admin_required
def admin_reviews():
    return render_template('reviews.html')


@performance_bp.route('/goals')
@login_required
def goals_page():
    return render_template('my_goals.html')


# ── Goals ──────────────────────────────────────────────────────────

@performance_bp.route('/api/v1/goals', methods=['GET', 'POST'])
@performance_bp.route('/api/goals', methods=['GET', 'POST'])
@login_required
def goals_api():
    if request.method == 'GET':
        conn = get_db()
        if _is_admin(session.get('role')):
            rows = conn.execute("SELECT g.goal_id, g.emp_id, u.name, g.title, g.description, g.target_date, g.weight, g.rating, g.status, g.created_at FROM goals g JOIN users u ON g.emp_id = u.emp_id ORDER BY g.created_at DESC").fetchall()
        else:
            rows = conn.execute("SELECT g.goal_id, g.emp_id, u.name, g.title, g.description, g.target_date, g.weight, g.rating, g.status, g.created_at FROM goals g JOIN users u ON g.emp_id = u.emp_id WHERE g.emp_id = ? ORDER BY g.created_at DESC", [session['emp_id']]).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'title': r[3], 'description': r[4], 'target_date': r[5].isoformat() if r[5] else None, 'weight': r[6], 'rating': r[7], 'status': r[8], 'created_at': r[9].isoformat() + '+05:30' if r[9] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('title'):
        return jsonify({'error': 'title required'}), 400
    gid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO goals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 [gid, data.get('emp_id', session['emp_id']), data['title'], data.get('description'),
                  parse_date(data.get('target_date')), data.get('weight', 1), None, 'Active', now_ist()])
    conn.close()
    return jsonify({'message': 'Goal created', 'id': gid}), 201


@performance_bp.route('/api/v1/goals/<int:gid>/rate', methods=['PUT'])
@performance_bp.route('/api/goals/<int:gid>/rate', methods=['PUT'])
@admin_required
def rate_goal(gid):
    data = request.get_json(silent=True) or {}
    rating = data.get('rating')
    if not rating or rating < 1 or rating > 5:
        return jsonify({'error': 'rating must be 1-5'}), 400
    conn = get_db()
    conn.execute("UPDATE goals SET rating = ?, status = 'Completed' WHERE goal_id = ?", [rating, gid])
    conn.close()
    return jsonify({'message': 'Goal rated'}), 200


@performance_bp.route('/api/v1/goals/<int:gid>', methods=['PUT'])
@performance_bp.route('/api/goals/<int:gid>', methods=['PUT'])
@login_required
def update_goal(gid):
    data = request.get_json(silent=True) or {}
    conn = get_db()
    for field in ('title', 'description', 'target_date', 'weight', 'status'):
        if field in data:
            conn.execute(f"UPDATE goals SET {field} = ? WHERE goal_id = ?", [data[field], gid])
    conn.close()
    return jsonify({'message': 'Goal updated'}), 200


# ── Performance Reviews ───────────────────────────────────────────

@performance_bp.route('/api/v1/performance-reviews', methods=['GET', 'POST'])
@performance_bp.route('/api/performance-reviews', methods=['GET', 'POST'])
@hr_or_admin_required
def reviews_api():
    if request.method == 'GET':
        conn = get_db()
        rows = conn.execute(
            "SELECT r.review_id, r.emp_id, u.name, r.reviewer_id, rev.name, r.review_period, r.overall_rating, r.comments, r.status, r.submitted_at FROM performance_reviews r JOIN users u ON r.emp_id = u.emp_id JOIN users rev ON r.reviewer_id = rev.emp_id ORDER BY r.created_at DESC"
        ).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'reviewer_id': r[3], 'reviewer': r[4], 'period': r[5], 'rating': float(r[6]) if r[6] else None, 'comments': r[7], 'status': r[8], 'submitted_at': r[9].isoformat() + '+05:30' if r[9] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('emp_id') or not data.get('reviewer_id') or not data.get('review_period'):
        return jsonify({'error': 'emp_id, reviewer_id, review_period required'}), 400
    rid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO performance_reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 [rid, data['emp_id'], data['reviewer_id'], data['review_period'], None, None, 'Draft', now_ist(), None])
    conn.close()
    return jsonify({'message': 'Review created', 'id': rid}), 201


@performance_bp.route('/api/v1/performance-reviews/<int:rid>/submit', methods=['PUT'])
@performance_bp.route('/api/performance-reviews/<int:rid>/submit', methods=['PUT'])
@login_required
def submit_review(rid):
    data = request.get_json(silent=True) or {}
    conn = get_db()
    conn.execute("UPDATE performance_reviews SET overall_rating = ?, comments = ?, status = 'Submitted', submitted_at = ? WHERE review_id = ?",
                 [data.get('rating'), data.get('comments'), now_ist(), rid])
    conn.close()
    return jsonify({'message': 'Review submitted'}), 200


# ── 360 Feedback ──────────────────────────────────────────────────

@performance_bp.route('/api/v1/feedback-360', methods=['GET', 'POST'])
@performance_bp.route('/api/feedback-360', methods=['GET', 'POST'])
@login_required
def feedback_api():
    if request.method == 'GET':
        conn = get_db()
        if _is_admin(session.get('role')):
            rows = conn.execute("SELECT f.feedback_id, f.emp_id, u.name, f.reviewer_id, rev.name, f.category, f.rating, f.comment, f.submitted_at FROM feedback_360 f JOIN users u ON f.emp_id = u.emp_id JOIN users rev ON f.reviewer_id = rev.emp_id ORDER BY f.submitted_at DESC").fetchall()
        else:
            rows = conn.execute("SELECT f.feedback_id, f.emp_id, u.name, f.reviewer_id, rev.name, f.category, f.rating, f.comment, f.submitted_at FROM feedback_360 f JOIN users u ON f.emp_id = u.emp_id JOIN users rev ON f.reviewer_id = rev.emp_id WHERE f.emp_id = ? ORDER BY f.submitted_at DESC", [session['emp_id']]).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'reviewer_id': r[3], 'reviewer': r[4], 'category': r[5], 'rating': r[6], 'comment': r[7], 'submitted_at': r[8].isoformat() + '+05:30' if r[8] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('emp_id') or not data.get('rating'):
        return jsonify({'error': 'emp_id and rating required'}), 400
    fid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO feedback_360 VALUES (?, ?, ?, ?, ?, ?, ?)",
                 [fid, data['emp_id'], session['emp_id'], data.get('category'), data['rating'], data.get('comment'), now_ist()])
    conn.close()
    return jsonify({'message': 'Feedback submitted'}), 201
