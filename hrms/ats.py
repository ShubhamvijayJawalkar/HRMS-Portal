from datetime import datetime

from flask import Blueprint, jsonify, render_template, request, session

from .db import get_db
from .decorators import admin_required, hr_or_admin_required
from .helpers import audit_log, gen_id, hash_password, now_ist, parse_date

ats_bp = Blueprint('ats', __name__)


@ats_bp.route('/admin/jobs')
@hr_or_admin_required
def admin_jobs():
    return render_template('jobs.html')


@ats_bp.route('/admin/candidates')
@hr_or_admin_required
def admin_candidates():
    return render_template('candidates.html')


@ats_bp.route('/admin/offers')
@hr_or_admin_required
def admin_offers():
    return render_template('offers.html')


# ── Job Postings ──────────────────────────────────────────────────

@ats_bp.route('/api/v1/jobs', methods=['GET', 'POST'])
@ats_bp.route('/api/jobs', methods=['GET', 'POST'])
@admin_required
def jobs_api():
    if request.method == 'GET':
        conn = get_db()
        rows = conn.execute("SELECT job_id, title, department, location, description, requirements, status, created_at FROM job_postings ORDER BY created_at DESC").fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'title': r[1], 'department': r[2], 'location': r[3], 'description': r[4], 'requirements': r[5], 'status': r[6], 'created_at': r[7].isoformat() + '+05:30' if r[7] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('title'):
        return jsonify({'error': 'title required'}), 400
    jid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO job_postings VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 [jid, data['title'], data.get('department'), data.get('location'), data.get('description'), data.get('requirements'), 'Open', now_ist()])
    conn.close()
    return jsonify({'message': 'Job created', 'id': jid}), 201


@ats_bp.route('/api/v1/jobs/<int:jid>/close', methods=['POST'])
@ats_bp.route('/api/jobs/<int:jid>/close', methods=['POST'])
@admin_required
def close_job(jid):
    conn = get_db()
    conn.execute("UPDATE job_postings SET status = 'Closed' WHERE job_id = ?", [jid])
    conn.close()
    return jsonify({'message': 'Job closed'}), 200


# ── Candidates ────────────────────────────────────────────────────

@ats_bp.route('/api/v1/candidates', methods=['GET', 'POST'])
@ats_bp.route('/api/candidates', methods=['GET', 'POST'])
@hr_or_admin_required
def candidates_api():
    if request.method == 'GET':
        conn = get_db()
        job_filter = request.args.get('job_id')
        if job_filter:
            rows = conn.execute("SELECT c.candidate_id, c.job_id, j.title, c.name, c.email, c.phone, c.status, c.applied_at FROM candidates c LEFT JOIN job_postings j ON c.job_id = j.job_id WHERE c.job_id = ? ORDER BY c.applied_at DESC", [job_filter]).fetchall()
        else:
            rows = conn.execute("SELECT c.candidate_id, c.job_id, j.title, c.name, c.email, c.phone, c.status, c.applied_at FROM candidates c LEFT JOIN job_postings j ON c.job_id = j.job_id ORDER BY c.applied_at DESC").fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'job_id': r[1], 'job_title': r[2] or 'N/A', 'name': r[3], 'email': r[4], 'phone': r[5], 'status': r[6], 'applied_at': r[7].isoformat() + '+05:30' if r[7] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('name') or not data.get('email'):
        return jsonify({'error': 'name and email required'}), 400
    cid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO candidates (candidate_id, job_id, name, email, phone, resume_text, status, applied_at) VALUES (?, ?, ?, ?, ?, ?, 'Applied', ?)",
                 [cid, data.get('job_id'), data['name'], data['email'], data.get('phone'), data.get('resume_text', ''), now_ist()])
    conn.close()
    return jsonify({'message': 'Candidate added', 'id': cid}), 201


@ats_bp.route('/api/v1/candidates/<int:cid>/convert', methods=['POST'])
@ats_bp.route('/api/candidates/<int:cid>/convert', methods=['POST'])
@hr_or_admin_required
def convert_candidate(cid):
    conn = get_db()
    candidate = conn.execute("SELECT name, email, phone, status FROM candidates WHERE candidate_id = ?", [cid]).fetchone()
    if not candidate:
        conn.close()
        return jsonify({'error': 'Candidate not found'}), 404
    if candidate[3] != 'Hired':
        conn.close()
        return jsonify({'error': 'Candidate must have Hired status to convert'}), 400

    data = request.get_json(silent=True) or {}
    dept = data.get('department', '').strip()
    desig = data.get('designation', '').strip()
    doj_str = data.get('date_of_joining', '').strip()
    manager_id = data.get('manager_emp_id', '').strip()

    doj = parse_date(doj_str) or now_ist().date()

    offer = conn.execute(
        "SELECT offer_id, offered_salary FROM offer_letters WHERE candidate_id = ? AND status = 'Accepted' ORDER BY offer_date DESC LIMIT 1",
        [cid]
    ).fetchone()

    emp_id = str(gen_id())
    pwd = hash_password('password123')
    now = now_ist()

    conn.execute(
        "INSERT INTO users (emp_id, name, email, password, role, department, designation, phone, date_of_joining, manager_emp_id, status, allow_login, allow_breaks, first_login, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Pre-hire', 0, 1, ?, ?)",
        [emp_id, candidate[0], candidate[1], pwd, 'Employee', dept or None, desig or None, candidate[2] or None, doj, manager_id or None, now, now]
    )

    if offer:
        conn.execute(
            "UPDATE users SET candidate_id = ?, offer_id = ? WHERE emp_id = ?",
            [cid, offer[0], emp_id]
        )

    starting_salary = float(offer[1]) if offer else 0
    if starting_salary > 0:
        basic = round(starting_salary * 0.5, 2)
        hra = round(starting_salary * 0.2, 2)
        allowances = round(starting_salary * 0.2, 2)
        struct_id = gen_id()
        conn.execute(
            "INSERT INTO salary_structures (struct_id, emp_id, basic, hra, allowances, deductions, effective_from) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [struct_id, emp_id, basic, hra, allowances, 0, doj]
        )

    wf_id = gen_id()
    conn.execute(
        "INSERT INTO onboarding_workflow (emp_id, current_step, step_1_status, created_at, updated_at) VALUES (?, 1, 'InProgress', ?, ?)",
        [emp_id, now, now]
    )

    conn.execute(
        "INSERT INTO onboarding_checklist (checklist_id, emp_id, doc_type, status) VALUES (?, ?, 'ID Proof', 'Pending'), (?, ?, 'Address Proof', 'Pending'), (?, ?, 'Photo', 'Pending'), (?, ?, 'Previous Organisation Documents', 'Pending'), (?, ?, 'Qualification Documents', 'Pending')",
        [wf_id, emp_id, wf_id+1, emp_id, wf_id+2, emp_id, wf_id+3, emp_id, wf_id+4, emp_id]
    )

    conn.close()
    audit_log(session['emp_id'], 'CANDIDATE_CONVERTED', f'Candidate {cid} converted to employee {emp_id}')
    return jsonify({'message': 'Employee created', 'emp_id': emp_id}), 201


@ats_bp.route('/api/v1/candidates/<int:cid>/status', methods=['PUT'])
@ats_bp.route('/api/candidates/<int:cid>/status', methods=['PUT'])
@hr_or_admin_required
def update_candidate_status(cid):
    data = request.get_json(silent=True) or {}
    status = data.get('status')
    if status not in ('Applied', 'Screened', 'Interviewed', 'Offered', 'Hired', 'Rejected'):
        return jsonify({'error': 'Invalid status'}), 400
    conn = get_db()
    old_status = conn.execute("SELECT status FROM candidates WHERE candidate_id = ?", [cid]).fetchone()
    conn.execute("UPDATE candidates SET status = ? WHERE candidate_id = ?", [status, cid])
    conn.close()
    audit_log(session['emp_id'], 'CANDIDATE_STATUS_CHANGE', f'Candidate {cid} status: {old_status[0] if old_status else "?"} → {status}')
    return jsonify({'message': f'Status updated to {status}'}), 200


# ── Interviews ────────────────────────────────────────────────────

@ats_bp.route('/api/v1/interviews', methods=['GET', 'POST'])
@ats_bp.route('/api/interviews', methods=['GET', 'POST'])
@hr_or_admin_required
def interviews_api():
    if request.method == 'GET':
        conn = get_db()
        cid = request.args.get('candidate_id')
        if cid:
            rows = conn.execute("SELECT i.interview_id, i.candidate_id, c.name, i.scheduled_at, i.interviewer, i.mode, i.feedback, i.status FROM interviews i JOIN candidates c ON i.candidate_id = c.candidate_id WHERE i.candidate_id = ? ORDER BY i.scheduled_at DESC", [cid]).fetchall()
        else:
            rows = conn.execute("SELECT i.interview_id, i.candidate_id, c.name, i.scheduled_at, i.interviewer, i.mode, i.feedback, i.status FROM interviews i JOIN candidates c ON i.candidate_id = c.candidate_id ORDER BY i.scheduled_at DESC").fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'candidate_id': r[1], 'candidate_name': r[2], 'scheduled_at': r[3].isoformat() + '+05:30' if r[3] else None, 'interviewer': r[4], 'mode': r[5], 'feedback': r[6], 'status': r[7]} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('candidate_id') or not data.get('scheduled_at'):
        return jsonify({'error': 'candidate_id and scheduled_at required'}), 400
    iid = gen_id()
    conn = get_db()
    scheduled_at = None
    sat = data.get('scheduled_at', '')
    if sat:
        for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
            try:
                scheduled_at = datetime.strptime(sat, fmt)
                break
            except ValueError:
                continue
    conn.execute("INSERT INTO interviews VALUES (?, ?, ?, ?, ?, ?, ?)",
                 [iid, data['candidate_id'], scheduled_at, data.get('interviewer'), data.get('mode', 'In-person'), data.get('feedback'), 'Scheduled'])
    conn.close()
    return jsonify({'message': 'Interview scheduled', 'id': iid}), 201


@ats_bp.route('/api/v1/interviews/<int:iid>/feedback', methods=['PUT'])
@ats_bp.route('/api/interviews/<int:iid>/feedback', methods=['PUT'])
@hr_or_admin_required
def interview_feedback(iid):
    data = request.get_json(silent=True) or {}
    conn = get_db()
    conn.execute("UPDATE interviews SET feedback = ?, status = 'Completed' WHERE interview_id = ?", [data.get('feedback', ''), iid])
    conn.close()
    return jsonify({'message': 'Feedback saved'}), 200


# ── Offer Letters ─────────────────────────────────────────────────

@ats_bp.route('/api/v1/offers', methods=['GET', 'POST'])
@ats_bp.route('/api/offers', methods=['GET', 'POST'])
@hr_or_admin_required
def offers_api():
    if request.method == 'GET':
        conn = get_db()
        rows = conn.execute("SELECT o.offer_id, o.candidate_id, c.name, c.email, o.offered_salary, o.offer_date, o.status, o.accepted_at, o.notes FROM offer_letters o JOIN candidates c ON o.candidate_id = c.candidate_id ORDER BY o.offer_date DESC").fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'candidate_id': r[1], 'candidate_name': r[2], 'email': r[3], 'salary': float(r[4]) if r[4] else 0, 'offer_date': r[5].isoformat() if r[5] else None, 'status': r[6], 'accepted_at': r[7].isoformat() + '+05:30' if r[7] else None, 'notes': r[8]} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('candidate_id') or not data.get('offered_salary'):
        return jsonify({'error': 'candidate_id and offered_salary required'}), 400
    oid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO offer_letters VALUES (?, ?, ?, ?, ?, ?, ?)",
                 [oid, data['candidate_id'], float(data['offered_salary']), now_ist().date(), 'Pending', None, data.get('notes')])
    conn.execute("UPDATE candidates SET status = 'Offered' WHERE candidate_id = ?", [data['candidate_id']])
    conn.close()
    audit_log(session['emp_id'], 'OFFER_SENT', f'Offer {oid} sent to candidate {data["candidate_id"]}')
    return jsonify({'message': 'Offer sent', 'id': oid}), 201


@ats_bp.route('/api/v1/offers/<int:oid>/reject', methods=['POST'])
@ats_bp.route('/api/offers/<int:oid>/reject', methods=['POST'])
@hr_or_admin_required
def reject_offer(oid):
    conn = get_db()
    row = conn.execute("SELECT candidate_id FROM offer_letters WHERE offer_id = ?", [oid]).fetchone()
    conn.execute("UPDATE offer_letters SET status = 'Rejected' WHERE offer_id = ?", [oid])
    if row:
        conn.execute("UPDATE candidates SET status = 'Rejected' WHERE candidate_id = ?", [row[0]])
    conn.close()
    audit_log(session['emp_id'], 'OFFER_REJECTED', f'Offer {oid} rejected' + (f' for candidate {row[0]}' if row else ''))
    return jsonify({'message': 'Offer rejected'}), 200


@ats_bp.route('/api/v1/offers/<int:oid>/accept', methods=['POST'])
@ats_bp.route('/api/offers/<int:oid>/accept', methods=['POST'])
@hr_or_admin_required
def accept_offer(oid):
    conn = get_db()
    row = conn.execute("SELECT candidate_id FROM offer_letters WHERE offer_id = ?", [oid]).fetchone()
    conn.execute("UPDATE offer_letters SET status = 'Accepted', accepted_at = ? WHERE offer_id = ?", [now_ist(), oid])
    if row:
        conn.execute("UPDATE candidates SET status = 'Hired' WHERE candidate_id = ?", [row[0]])
    conn.close()
    audit_log(session['emp_id'], 'OFFER_ACCEPTED', f'Offer {oid} accepted' + (f' for candidate {row[0]}' if row else ''))
    return jsonify({'message': 'Offer accepted'}), 200
