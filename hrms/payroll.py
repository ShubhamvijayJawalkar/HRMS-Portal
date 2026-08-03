import csv
import logging
from io import BytesIO, TextIOWrapper

from flask import Blueprint, jsonify, render_template, request, send_file, session

from .db import _scalar, get_db
from .decorators import admin_required, hr_or_admin_required, login_required
from .helpers import (
    _is_admin,
    audit_log,
    calc_payroll_item,
    calc_payroll_item_from_rates,
    calc_tds,
    calc_tds_from_rates,
    gen_id,
    generate_payslip_pdf,
    now_ist,
    parse_date,
)

logger = logging.getLogger(__name__)
payroll_bp = Blueprint('payroll', __name__)


@payroll_bp.route('/admin/payroll')
@hr_or_admin_required
def admin_payroll():
    return render_template('payroll.html')


@payroll_bp.route('/admin/settings/payroll-rates')
@admin_required
def admin_payroll_rates_page():
    return render_template('admin_payroll_rates.html')


@payroll_bp.route('/admin/salary-structures')
@hr_or_admin_required
def admin_salary():
    return render_template('salary.html')


# ── Salary Structures ────────────────────────────────────────────

@payroll_bp.route('/api/v1/salary-structures', methods=['GET', 'POST'])
@payroll_bp.route('/api/salary-structures', methods=['GET', 'POST'])
@hr_or_admin_required
def salary_api():
    if request.method == 'GET':
        conn = get_db()
        rows = conn.execute("SELECT s.struct_id, s.emp_id, u.name, s.basic, s.hra, s.allowances, s.deductions, s.effective_from FROM salary_structures s JOIN users u ON s.emp_id = u.emp_id ORDER BY s.effective_from DESC").fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'basic': float(r[3]), 'hra': float(r[4]), 'allowances': float(r[5]), 'deductions': float(r[6]), 'effective_from': r[7].isoformat() if r[7] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('emp_id') or not data.get('basic'):
        return jsonify({'error': 'emp_id and basic required'}), 400
    sid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO salary_structures (struct_id, emp_id, basic, hra, allowances, deductions, effective_from) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 [sid, data['emp_id'], float(data['basic']), float(data.get('hra', 0)), float(data.get('allowances', 0)), float(data.get('deductions', 0)),
                  parse_date(data.get('effective_from'), now_ist().date())])
    conn.close()
    audit_log(session['emp_id'], 'SALARY_STRUCTURE_CREATE', f'Salary structure saved for {data["emp_id"]}')
    return jsonify({'message': 'Salary structure saved', 'id': sid}), 201


@payroll_bp.route('/api/salary-structures/preview', methods=['POST'])
@hr_or_admin_required
def preview_payslip():
    data = request.get_json(silent=True) or {}
    basic = float(data.get('basic', 0))
    hra = float(data.get('hra', 0))
    allowances = float(data.get('allowances', 0))
    deductions = float(data.get('deductions', 0))
    gross, total_ded, net, pf, esi, pt = calc_payroll_item_from_rates(None, basic, hra, allowances, deductions)
    annual_gross = gross * 12
    tds = calc_tds_from_rates(annual_gross)
    monthly_tds = round(tds / 12, 2)
    return jsonify({
        'gross': gross,
        'basic': basic,
        'hra': hra,
        'allowances': allowances,
        'deductions': deductions,
        'pf': pf,
        'esi': esi,
        'pt': pt,
        'other_deductions': deductions,
        'total_deductions': total_ded,
        'net': net,
        'annual_gross': annual_gross,
        'annual_tds': tds,
        'monthly_tds': monthly_tds,
    }), 200


# ── Payroll Rates ────────────────────────────────────────────────

@payroll_bp.route('/api/payroll-rates', methods=['GET', 'PUT'])
@admin_required
def payroll_rates_api():
    if request.method == 'GET':
        conn = get_db()
        rows = conn.execute("SELECT rate_id, label, rate_type, value, effective_from, effective_to, description FROM payroll_rates ORDER BY rate_id").fetchall()
        conn.close()
        return jsonify([{
            'id': r[0], 'label': r[1], 'rate_type': r[2], 'value': float(r[3]),
            'effective_from': r[4].isoformat() if r[4] else None,
            'effective_to': r[5].isoformat() if r[5] else None,
            'description': r[6] or ''
        } for r in rows]), 200
    data = request.get_json(silent=True) or {}
    rate_id = data.get('id')
    if not rate_id:
        return jsonify({'error': 'rate_id required'}), 400
    conn = get_db()
    existing = conn.execute("SELECT label, value, effective_from, effective_to, description FROM payroll_rates WHERE rate_id = ?", [rate_id]).fetchone()
    if not existing:
        conn.close()
        return jsonify({'error': 'Rate not found'}), 404
    conn.execute(
        "UPDATE payroll_rates SET label = ?, value = ?, effective_from = ?, effective_to = ?, description = ? WHERE rate_id = ?",
        [data.get('label', existing[0]), float(data.get('value', existing[1])),
         parse_date(data.get('effective_from'), existing[2] or now_ist().date()),
         parse_date(data.get('effective_to'), existing[3]),
         data.get('description', existing[4] or ''), rate_id]
    )
    conn.close()
    audit_log(session['emp_id'], 'PAYROLL_RATE_UPDATE', f'Rate {rate_id} ({existing[0]}) updated to {data.get("value", existing[1])}')
    return jsonify({'message': 'Rate updated'}), 200


# ── Review Cycle Progress ────────────────────────────────────────

@payroll_bp.route('/api/review-cycle-progress')
@login_required
def review_cycle_progress():
    emp_id = session['emp_id']
    conn = get_db()
    current_period = _current_review_period()
    if _is_admin(session.get('role')):
        total = _scalar("SELECT COUNT(*) FROM performance_reviews WHERE review_period = ?", [current_period], conn=conn)
        completed = _scalar("SELECT COUNT(*) FROM performance_reviews WHERE review_period = ? AND status = 'Submitted'", [current_period], conn=conn)
    else:
        total = _scalar("SELECT COUNT(*) FROM performance_reviews WHERE emp_id = ? AND review_period = ?", [emp_id, current_period], conn=conn)
        completed = _scalar("SELECT COUNT(*) FROM performance_reviews WHERE emp_id = ? AND review_period = ? AND status = 'Submitted'", [emp_id, current_period], conn=conn)
    conn.close()
    return jsonify({'period': current_period, 'total': total, 'completed': completed, 'pending': total - completed}), 200


def _current_review_period():
    now = now_ist()
    q = (now.month - 1) // 3 + 1
    return f"Q{q} {now.year}"


def open_review_cycle():
    conn = get_db()
    try:
        period = _current_review_period()
        existing = _scalar("SELECT COUNT(*) FROM performance_reviews WHERE review_period = ?", [period], conn=conn)
        if existing > 0:
            logger.info("Review cycle %s already has %d reviews, skipping", period, existing)
            return
        employees = conn.execute("SELECT emp_id FROM users WHERE role = 'Employee' AND status = 'Active'").fetchall()
        admin = conn.execute("SELECT emp_id FROM users WHERE role IN ('Admin', 'Super Admin') AND status = 'Active' ORDER BY emp_id LIMIT 1").fetchone()
        reviewer = admin[0] if admin else employees[0][0] if employees else None
        if not reviewer:
            logger.warning("No reviewer found for review cycle %s", period)
            return
        count = 0
        for (eid,) in employees:
            conn.execute(
                "INSERT INTO performance_reviews (review_id, emp_id, reviewer_id, review_period, overall_rating, comments, status, created_at, submitted_at) VALUES (?, ?, ?, ?, NULL, NULL, 'Draft', ?, NULL)",
                [gen_id(), eid, reviewer, period, now_ist()]
            )
            count += 1
        conn.commit()
        logger.info("Opened %d reviews for cycle %s", count, period)
    except Exception as e:
        logger.error("Failed to open review cycle: %s", e)
    finally:
        conn.close()


# ── Payroll Runs ─────────────────────────────────────────────────

@payroll_bp.route('/api/v1/payroll-runs', methods=['GET', 'POST'])
@payroll_bp.route('/api/payroll-runs', methods=['GET', 'POST'])
@hr_or_admin_required
def payroll_runs_api():
    if request.method == 'GET':
        conn = get_db()
        rows = conn.execute("SELECT run_id, month, year, processed_at, status FROM payroll_runs ORDER BY year DESC, month DESC").fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'month': r[1], 'year': r[2], 'processed_at': r[3].isoformat() + '+05:30' if r[3] else None, 'status': r[4]} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    month, year = data.get('month'), data.get('year')
    if not month or not year:
        return jsonify({'error': 'month and year required'}), 400
    conn = get_db()
    conn.execute("BEGIN TRANSACTION")
    try:
        if conn.execute("SELECT 1 FROM payroll_runs WHERE month = ? AND year = ?", [month, year]).fetchone():
            conn.execute("ROLLBACK")
            conn.close()
            return jsonify({'error': 'Payroll already processed for this period'}), 409
        rid = gen_id()
        conn.execute("INSERT INTO payroll_runs VALUES (?, ?, ?, ?, ?)", [rid, month, year, now_ist(), 'Draft'])
        employees = conn.execute("SELECT u.emp_id, COALESCE(s.basic,0), COALESCE(s.hra,0), COALESCE(s.allowances,0), COALESCE(s.deductions,0) FROM users u LEFT JOIN salary_structures s ON u.emp_id = s.emp_id AND s.effective_from <= ? WHERE u.role = 'Employee'", [now_ist().date()]).fetchall()
        for e in employees:
            gross, total_ded, net, pf, esi, pt = calc_payroll_item(e[0], float(e[1]), float(e[2]), float(e[3]), float(e[4]))
            conn.execute("INSERT INTO payroll_items (item_id, run_id, emp_id, gross_salary, deductions_total, net_salary, pf, esi, pt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         [gen_id(), rid, e[0], gross, total_ded, net, pf, esi, pt])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        conn.close()
        raise
    audit_log(session['emp_id'], 'PAYROLL_RUN_CREATE', f'Payroll run {rid} created for {month}/{year}')
    conn.close()
    return jsonify({'message': f'Payroll run created for {month}/{year}'}), 201


@payroll_bp.route('/api/v1/payroll-runs/<int:rid>/finalize', methods=['POST'])
@payroll_bp.route('/api/payroll-runs/<int:rid>/finalize', methods=['POST'])
@hr_or_admin_required
def finalize_payroll(rid):
    conn = get_db()
    row = conn.execute("SELECT status FROM payroll_runs WHERE run_id = ?", [rid]).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Payroll run not found'}), 404
    if row[0] == 'Finalized':
        conn.close()
        return jsonify({'error': 'Payroll run is already finalized'}), 400
    conn.execute("UPDATE payroll_runs SET status = 'Finalized' WHERE run_id = ?", [rid])
    conn.close()
    audit_log(session['emp_id'], 'PAYROLL_RUN_FINALIZE', f'Payroll run {rid} finalized')
    return jsonify({'message': 'Payroll finalized'}), 200


@payroll_bp.route('/api/v1/payroll-runs/<int:rid>/items')
@payroll_bp.route('/api/payroll-runs/<int:rid>/items')
@hr_or_admin_required
def payroll_items(rid):
    conn = get_db()
    rows = conn.execute(
        "SELECT p.item_id, p.emp_id, u.name, p.gross_salary, p.deductions_total, p.net_salary, p.pf, p.esi, p.pt, p.payslip_generated FROM payroll_items p JOIN users u ON p.emp_id = u.emp_id WHERE p.run_id = ? ORDER BY u.name",
        [rid]
    ).fetchall()
    conn.close()
    return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'gross': float(r[3]), 'deductions': float(r[4]), 'net': float(r[5]), 'pf': float(r[6]), 'esi': float(r[7]), 'pt': float(r[8]), 'payslip_generated': bool(r[9])} for r in rows]), 200


@payroll_bp.route('/api/v1/payslip/<int:run_id>/<emp_id>')
@payroll_bp.route('/api/payslip/<int:run_id>/<emp_id>')
@login_required
def get_payslip(run_id, emp_id):
    if not _is_admin(session.get('role')) and session['emp_id'] != emp_id:
        return jsonify({'error': 'Forbidden'}), 403
    conn = get_db()
    row = conn.execute(
        "SELECT p.item_id, r.month, r.year, p.emp_id, u.name, u.department, u.designation, p.gross_salary, p.deductions_total, p.net_salary, p.pf, p.esi, p.pt FROM payroll_items p JOIN payroll_runs r ON p.run_id = r.run_id JOIN users u ON p.emp_id = u.emp_id WHERE p.run_id = ? AND p.emp_id = ?",
        [run_id, emp_id]
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({
        'item_id': row[0], 'month': row[1], 'year': row[2], 'emp_id': row[3], 'employee': row[4],
        'department': row[5], 'designation': row[6], 'gross': float(row[7]), 'deductions': float(row[8]),
        'net': float(row[9]), 'pf': float(row[10]), 'esi': float(row[11]), 'pt': float(row[12])
    }), 200


@payroll_bp.route('/api/v1/my-payslips')
@payroll_bp.route('/api/my-payslips')
@login_required
def my_payslips():
    conn = get_db()
    rows = conn.execute(
        "SELECT r.run_id, r.month, r.year, p.net_salary, r.status FROM payroll_items p JOIN payroll_runs r ON p.run_id = r.run_id WHERE p.emp_id = ? ORDER BY r.year DESC, r.month DESC",
        [session['emp_id']]
    ).fetchall()
    conn.close()
    return jsonify([{'run_id': r[0], 'month': r[1], 'year': r[2], 'net': float(r[3]), 'status': r[4]} for r in rows]), 200


# ── Full Payroll (Payslip PDF, Bank File, TDS) ──────────────────

@payroll_bp.route('/api/v1/payroll-runs/<int:rid>/payslip-pdf/<emp_id>')
@payroll_bp.route('/api/payroll-runs/<int:rid>/payslip-pdf/<emp_id>')
@login_required
def payslip_pdf(rid, emp_id):
    if not _is_admin(session.get('role')) and session['emp_id'] != emp_id:
        return jsonify({'error': 'Forbidden'}), 403
    pdf = generate_payslip_pdf(rid, emp_id)
    if not pdf:
        return jsonify({'error': 'Not found'}), 404
    return send_file(pdf, mimetype='application/pdf', as_attachment=True, download_name=f'payslip_{emp_id}_{rid}.pdf')


@payroll_bp.route('/api/v1/payroll-runs/<int:rid>/bank-file')
@payroll_bp.route('/api/payroll-runs/<int:rid>/bank-file')
@hr_or_admin_required
def bank_file_export(rid):
    conn = get_db()
    rows = conn.execute(
        "SELECT p.emp_id, u.name, p.net_salary FROM payroll_items p JOIN users u ON p.emp_id = u.emp_id WHERE p.run_id = ? ORDER BY u.name",
        [rid]
    ).fetchall()
    conn.close()
    if not rows:
        return jsonify({'error': 'No items'}), 404
    buf = BytesIO()
    text_buf = TextIOWrapper(buf, encoding='utf-8', newline='')
    writer = csv.writer(text_buf)
    writer.writerow(['Employee ID', 'Name', 'Net Salary', 'Account Number', 'IFSC'])
    for r in rows:
        writer.writerow([r[0], r[1], f"{float(r[2]):.2f}", '', ''])
    text_buf.flush()
    buf.seek(0)
    return send_file(buf, mimetype='text/csv', as_attachment=True, download_name=f'payroll_{rid}.csv')


@payroll_bp.route('/api/v1/payroll-runs/<int:rid>/tds-report')
@payroll_bp.route('/api/payroll-runs/<int:rid>/tds-report')
@hr_or_admin_required
def tds_report(rid):
    conn = get_db()
    run = conn.execute("SELECT month, year FROM payroll_runs WHERE run_id = ?", [rid]).fetchone()
    if not run:
        conn.close()
        return jsonify({'error': 'Run not found'}), 404
    rows = conn.execute(
        "SELECT p.emp_id, u.name, p.gross_salary FROM payroll_items p JOIN users u ON p.emp_id = u.emp_id WHERE p.run_id = ?",
        [rid]
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        monthly_gross = float(r[2])
        annual_gross = monthly_gross * 12
        tds = round(calc_tds(annual_gross) / 12, 2)
        result.append({'emp_id': r[0], 'name': r[1], 'monthly_gross': monthly_gross, 'annual_gross': annual_gross, 'tds': tds})
    return jsonify(result), 200
