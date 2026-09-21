import logging
from io import BytesIO

import pandas as pd
from flask import Blueprint, jsonify, render_template, request, send_file

from .db import get_db
from .decorators import admin_required, hr_or_admin_required
from .helpers import now_ist, parse_date

logger = logging.getLogger('hrms')

reports_bp = Blueprint('reports', __name__)


@reports_bp.route('/admin/reports')
@hr_or_admin_required
def admin_reports():
    return render_template('admin_reports.html')


@reports_bp.route('/api/v1/reports/export')
@reports_bp.route('/api/reports/export')
@admin_required
def export_report():
    start_date = parse_date(request.args.get('start_date'), now_ist().date())
    end_date = parse_date(request.args.get('end_date'), start_date)
    fmt = request.args.get('format', 'xlsx')

    conn = get_db()
    rows = conn.execute("""
        SELECT u.emp_id, u.name, u.department,
               COALESCE((SELECT SUM(total_hours) FROM user_sessions us WHERE us.emp_id = u.emp_id AND us.session_date BETWEEN ? AND ?), 0) AS total_hours,
               COALESCE((SELECT SUM(duration_minutes) FROM breaks b WHERE b.emp_id = u.emp_id AND b.break_date BETWEEN ? AND ? AND b.status = 'Completed'), 0) AS break_minutes,
               COALESCE((SELECT COUNT(*) FROM breaks b WHERE b.emp_id = u.emp_id AND b.break_date BETWEEN ? AND ? AND b.status = 'Completed'), 0) AS break_count,
               (SELECT MIN(login_time) FROM user_sessions us WHERE us.emp_id = u.emp_id AND us.session_date BETWEEN ? AND ?) AS first_login,
               (SELECT MAX(logout_time) FROM user_sessions us WHERE us.emp_id = u.emp_id AND us.session_date BETWEEN ? AND ?) AS last_logout
        FROM users u WHERE u.role = 'Employee' ORDER BY u.name
    """, [start_date, end_date, start_date, end_date, start_date, end_date,
          start_date, end_date, start_date, end_date]).fetchall()
    conn.close()

    def mins_to_hms(minutes):
        total = int(round(float(minutes) * 60))
        h = total // 3600
        m = (total % 3600) // 60
        s = total % 60
        return f'{h:02d}:{m:02d}:{s:02d}'

    def hours_to_hms(hours):
        total = int(round(float(hours) * 3600))
        h = total // 3600
        m = (total % 3600) // 60
        s = total % 60
        return f'{h:02d}:{m:02d}:{s:02d}'

    def fmt_time(val):
        if val is None:
            return '--:--:--'
        try:
            return val.strftime('%H:%M:%S')
        except Exception:
            return '--:--:--'

    data = []
    for r in rows:
        sh = float(r[3] or 0)
        bm = float(r[4] or 0)
        ph = max(0, sh - bm / 60)
        eff = round((ph / sh) * 100, 1) if sh > 0 else 0
        data.append({
            'Employee ID': r[0], 'Name': r[1], 'Department': r[2] or 'N/A',
            'First Login': fmt_time(r[6]),
            'Last Logout': fmt_time(r[7]),
            'Total Hours': hours_to_hms(sh),
            'Break Duration': mins_to_hms(bm),
            'Break Count': int(r[5]),
            'Productive Hours': hours_to_hms(ph),
            'Efficiency %': eff,
        })

    df = pd.DataFrame(data) if data else pd.DataFrame(columns=[
        'Employee ID', 'Name', 'Department', 'First Login', 'Last Logout',
        'Total Hours', 'Break Duration', 'Break Count', 'Productive Hours', 'Efficiency %'])
    df['Period'] = f'{start_date} to {end_date}'

    if fmt == 'xlsx':
        buf = BytesIO()
        with pd.ExcelWriter(buf, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Report')
        buf.seek(0)
        return send_file(
            buf, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f'hrms_report_{start_date}_{end_date}.xlsx'
        )

    csv_buf = BytesIO()
    df.to_csv(csv_buf, index=False)
    csv_buf.seek(0)
    return send_file(
        csv_buf, mimetype='text/csv',
        as_attachment=True,
        download_name=f'hrms_report_{start_date}_{end_date}.csv'
    )


@reports_bp.route('/api/v1/reports/pdf')
@reports_bp.route('/api/reports/pdf')
@admin_required
def export_report_pdf():
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    start_date = parse_date(request.args.get('start_date'), now_ist().date())
    end_date = parse_date(request.args.get('end_date'), start_date)
    if end_date and end_date < start_date:
        start_date, end_date = end_date, start_date

    conn = get_db()
    rows = conn.execute("""
        SELECT u.emp_id, u.name, u.department,
               COALESCE((SELECT SUM(total_hours) FROM user_sessions us WHERE us.emp_id = u.emp_id AND us.session_date BETWEEN ? AND ?), 0) AS total_hours,
               COALESCE((SELECT SUM(duration_minutes) FROM breaks b WHERE b.emp_id = u.emp_id AND b.break_date BETWEEN ? AND ? AND b.status = 'Completed'), 0) AS break_minutes,
               COALESCE((SELECT COUNT(*) FROM user_sessions us WHERE us.emp_id = u.emp_id AND us.session_date BETWEEN ? AND ?), 0) AS session_count
        FROM users u WHERE u.role = 'Employee' ORDER BY u.name
    """, [start_date, end_date, start_date, end_date, start_date, end_date]).fetchall()
    conn.close()

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=30, bottomMargin=30)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('ReportTitle', parent=styles['Title'], fontSize=18, spaceAfter=6, textColor=colors.HexColor('#0F172A'))
    subtitle_style = ParagraphStyle('Subtitle', parent=styles['Normal'], fontSize=11, spaceAfter=20, textColor=colors.HexColor('#64748B'), alignment=1)
    elements = []

    elements.append(Paragraph('HRMS Employee Efficiency Report', title_style))
    elements.append(Paragraph(f'Period: {start_date} to {end_date}', subtitle_style))
    elements.append(Spacer(1, 12))

    header = ['Employee ID', 'Name', 'Department', 'Hours', 'Break Min', 'Sessions', 'Efficiency']
    table_data = [header]
    for r in rows:
        sh = float(r[3] or 0)
        bm = int(r[4] or 0)
        ph = max(0, sh - bm / 60)
        eff = f'{round((ph / sh) * 100, 1) if sh > 0 else 0}%'
        table_data.append([str(r[0]), str(r[1]), str(r[2] or 'N/A'), f'{sh:.2f}', str(int(bm)), str(int(r[5])), eff])

    table = Table(table_data, colWidths=[60, 90, 80, 50, 55, 55, 60])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0F172A')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 8),
        ('FONTSIZE', (0, 1), (-1, -1), 8),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F8FAFC')]),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))
    elements.append(table)

    doc.build(elements)
    buf.seek(0)
    return send_file(
        buf, mimetype='application/pdf',
        as_attachment=True,
        download_name=f'hrms_report_{start_date}_{end_date}.pdf'
    )


@reports_bp.route('/api/reports')
@admin_required
def get_reports():
    start_date = parse_date(request.args.get('start_date'), now_ist().date())
    end_date = parse_date(request.args.get('end_date'), start_date)
    if end_date and end_date < start_date:
        start_date, end_date = end_date, start_date

    department = request.args.get('department', '').strip()
    emp_id_filter = request.args.get('emp_id', '').strip()

    conn = get_db()
    try:
        user_where = "WHERE u.role = 'Employee'"
        user_params = []
        if department:
            user_where += " AND u.department = ?"
            user_params.append(department)
        if emp_id_filter:
            user_where += " AND u.emp_id = ?"
            user_params.append(emp_id_filter)

        summary = conn.execute(f"""
            SELECT u.emp_id, u.name, u.department,
                   (SELECT MIN(login_time) FROM user_sessions us WHERE us.emp_id = u.emp_id AND us.session_date BETWEEN ? AND ?),
                   (SELECT MAX(logout_time) FROM user_sessions us WHERE us.emp_id = u.emp_id AND us.session_date BETWEEN ? AND ?),
                   COALESCE((SELECT SUM(total_hours) FROM user_sessions us WHERE us.emp_id = u.emp_id AND us.session_date BETWEEN ? AND ?), 0),
                   COALESCE((SELECT SUM(duration_minutes) FROM breaks b WHERE b.emp_id = u.emp_id AND b.break_date BETWEEN ? AND ? AND b.status = 'Completed'), 0),
                   COALESCE((SELECT COUNT(*) FROM breaks b WHERE b.emp_id = u.emp_id AND b.break_date BETWEEN ? AND ? AND b.status = 'Completed'), 0),
                   COALESCE((SELECT COUNT(*) FROM user_sessions us WHERE us.emp_id = u.emp_id AND us.session_date BETWEEN ? AND ?), 0)
            FROM users u {user_where} ORDER BY u.name
        """, [start_date, end_date, start_date, end_date, start_date, end_date,
              start_date, end_date, start_date, end_date, start_date, end_date] + user_params).fetchall()

        break_where = "WHERE b.break_date BETWEEN ? AND ?"
        break_params = [start_date, end_date]
        if department:
            break_where += " AND u.department = ?"
            break_params.append(department)
        if emp_id_filter:
            break_where += " AND b.emp_id = ?"
            break_params.append(emp_id_filter)

        break_details = conn.execute(f"""
            SELECT b.break_id, b.emp_id, u.name, u.department, b.break_type, b.start_time, b.end_time, b.duration_minutes, b.break_date, b.status
            FROM breaks b JOIN users u ON b.emp_id = u.emp_id {break_where} ORDER BY b.break_date DESC, b.start_time DESC
        """, break_params).fetchall()

        sess_where = "WHERE us.session_date BETWEEN ? AND ?"
        sess_params = [start_date, end_date]
        if department:
            sess_where += " AND u.department = ?"
            sess_params.append(department)
        if emp_id_filter:
            sess_where += " AND us.emp_id = ?"
            sess_params.append(emp_id_filter)

        session_details = conn.execute(f"""
            SELECT us.session_id, us.emp_id, u.name, u.department, us.login_time, us.logout_time, us.total_hours, us.session_date
            FROM user_sessions us JOIN users u ON us.emp_id = u.emp_id {sess_where} ORDER BY us.session_date DESC, us.login_time DESC
        """, sess_params).fetchall()

        departments = [r[0] for r in conn.execute("SELECT DISTINCT department FROM users WHERE role = 'Employee' AND department IS NOT NULL ORDER BY department").fetchall()]
        employees = conn.execute(f"SELECT emp_id, name FROM users u {user_where} ORDER BY name", user_params).fetchall()
    finally:
        conn.close()

    summary_list = []
    for r in summary:
        sh = float(r[5] or 0)
        bm = int(r[6] or 0)
        bh = bm / 60
        ph = max(0, sh - bh)
        eff = round((ph / sh) * 100, 1) if sh > 0 else 0
        summary_list.append({
            'emp_id': r[0], 'employee_name': r[1], 'department': r[2] or 'N/A',
            'first_login': r[3].strftime('%H:%M:%S') if r[3] else 'N/A',
            'last_logout': r[4].strftime('%H:%M:%S') if r[4] else 'N/A',
            'total_session_hours': sh, 'total_break_minutes': bm,
            'total_breaks': int(r[7] or 0), 'session_count': int(r[8] or 0),
            'efficiency_percent': eff, 'productive_hours': round(ph, 2)
        })

    break_list = [{
        'break_id': r[0], 'emp_id': r[1], 'employee_name': r[2], 'department': r[3] or 'N/A',
        'break_type': r[4], 'start_time': r[5].strftime('%H:%M:%S') if r[5] else 'N/A',
        'end_time': r[6].strftime('%H:%M:%S') if r[6] else 'Ongoing',
        'duration_minutes': int(r[7]) if r[7] else 0,
        'break_date': r[8].isoformat() if r[8] else 'N/A', 'status': r[9]
    } for r in break_details]

    session_list = [{
        'session_id': r[0], 'emp_id': r[1], 'employee_name': r[2], 'department': r[3] or 'N/A',
        'login_time': r[4].strftime('%H:%M:%S') if r[4] else 'N/A',
        'logout_time': r[5].strftime('%H:%M:%S') if r[5] else 'Active',
        'total_hours': float(r[6]) if r[6] else 0,
        'session_date': r[7].isoformat() if r[7] else 'N/A'
    } for r in session_details]

    return jsonify({
        'report_range': {'start_date': start_date.isoformat(), 'end_date': end_date.isoformat()},
        'departments': departments,
        'employees': [{'emp_id': e[0], 'name': e[1]} for e in employees],
        'summary': summary_list, 'break_details': break_list, 'session_details': session_list
    }), 200


@reports_bp.route('/api/reports/department-summary')
@admin_required
def get_department_summary():
    start_date = parse_date(request.args.get('start_date'), now_ist().date())
    end_date = parse_date(request.args.get('end_date'), start_date)
    if end_date and end_date < start_date:
        start_date, end_date = end_date, start_date

    conn = get_db()
    rows = conn.execute("""
        SELECT u.department,
               COUNT(DISTINCT u.emp_id) AS employee_count,
               COALESCE(SUM(us.total_hours), 0) AS total_hours,
               COALESCE(SUM(b.duration_minutes), 0) AS total_break_minutes,
               COALESCE(SUM(CASE WHEN b.status = 'Completed' THEN 1 ELSE 0 END), 0) AS total_breaks
        FROM users u
        LEFT JOIN user_sessions us ON us.emp_id = u.emp_id AND us.session_date BETWEEN ? AND ?
        LEFT JOIN breaks b ON b.emp_id = u.emp_id AND b.break_date BETWEEN ? AND ?
        WHERE u.role = 'Employee' AND u.department IS NOT NULL
        GROUP BY u.department ORDER BY u.department
    """, [start_date, end_date, start_date, end_date]).fetchall()
    conn.close()

    result = []
    for r in rows:
        sh = float(r[2] or 0)
        bm = int(r[3] or 0)
        ph = max(0, sh - bm / 60)
        eff = round((ph / sh) * 100, 1) if sh > 0 else 0
        result.append({
            'department': r[0], 'employee_count': int(r[1]),
            'total_hours': round(sh, 2), 'total_break_minutes': bm,
            'total_breaks': int(r[4]), 'productive_hours': round(ph, 2),
            'efficiency_percent': eff
        })

    return jsonify({'departments': result}), 200
