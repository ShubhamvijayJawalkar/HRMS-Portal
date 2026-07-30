import os
import logging
import secrets
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from zoneinfo import ZoneInfo

import bcrypt

from flask import request

from .db import get_db, _scalar

logger = logging.getLogger('hrms')

IST = ZoneInfo('Asia/Kolkata')

UPLOAD_FOLDER = os.getenv('UPLOAD_FOLDER', os.path.join(os.path.dirname(os.path.dirname(__file__)), 'uploads'))
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg', 'gif', 'doc', 'docx', 'xls', 'xlsx', 'csv', 'txt'}
MAX_FILE_SIZE = 10 * 1024 * 1024


def validate_upload(file):
    if not file or not file.filename:
        return 'No file selected'
    ext = os.path.splitext(file.filename)[1].lower().lstrip('.')
    if ext not in ALLOWED_EXTENSIONS:
        return f'File type .{ext} not allowed. Allowed: {", ".join(sorted(ALLOWED_EXTENSIONS))}'
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > MAX_FILE_SIZE:
        return f'File too large ({size} bytes). Maximum: {MAX_FILE_SIZE} bytes'
    return None

ADMIN_ROLES = ('Admin', 'Super Admin')


def now_ist():
    return datetime.now(IST).replace(tzinfo=None)


def iso_ist(dt):
    if dt is None:
        return None
    return dt.strftime('%Y-%m-%dT%H:%M:%S+05:30')


def fmt_time_ist(dt):
    if dt is None:
        return None
    return dt.strftime('%H:%M:%S IST')


def _is_admin(role):
    return role in ADMIN_ROLES


def gen_id():
    return secrets.randbelow(2_147_483_647)


def parse_date(date_string, default=None):
    if not date_string:
        return default
    try:
        return datetime.strptime(date_string, '%Y-%m-%d').date()
    except ValueError:
        return default


def hash_password(password):
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def check_password(password, hashed):
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        return False


def get_user(emp_id):
    conn = get_db()
    u = conn.execute(
        "SELECT emp_id, name, email, role, status, department, allow_login, allow_breaks, designation, manager_emp_id, phone, date_of_birth, date_of_joining, address, emergency_contact_name, emergency_contact_phone, shift_start, shift_end FROM users WHERE emp_id = ?",
        [emp_id]
    ).fetchone()
    conn.close()
    return u


def audit_log(emp_id, action, details=None):
    conn = None
    try:
        conn = get_db()
        log_id = secrets.randbelow(2_147_483_647)
        try:
            ip = request.remote_addr
        except RuntimeError:
            ip = '0.0.0.0'
        conn.execute(
            "INSERT INTO audit_log (log_id, emp_id, action, details, ip_address, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            [log_id, emp_id, action, details, ip, now_ist()]
        )
    except Exception as e:
        logger.exception("audit_log failed: %s", e)
    finally:
        if conn:
            conn.close()


def _category_for_type(ntype):
    ntype_lower = ntype.lower()
    if 'leave' in ntype_lower:
        return 'Leaves'
    if 'expense' in ntype_lower:
        return 'Expenses'
    if 'ticket' in ntype_lower:
        return 'Tickets'
    if 'onboard' in ntype_lower or 'offboard' in ntype_lower:
        return 'Onboarding'
    if 'payroll' in ntype_lower or 'salary' in ntype_lower:
        return 'Payroll'
    return 'Leaves'


def add_notification(emp_id, ntype, message, link=None):
    conn = None
    try:
        conn = get_db()
        category = _category_for_type(ntype)
        pref = conn.execute(
            "SELECT in_app, email FROM notification_preferences WHERE emp_id = ? AND category = ?",
            [emp_id, category]
        ).fetchone()
        pref_in_app = pref[0] if pref else 1
        pref_email = pref[1] if pref else 1

        if pref_in_app:
            conn.execute(
                "INSERT INTO notifications (notification_id, emp_id, type, message, related_link, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                [gen_id(), emp_id, ntype, message, link, now_ist()]
            )

        if pref_email:
            user = conn.execute("SELECT email, name FROM users WHERE emp_id = ?", [emp_id]).fetchone()
            if user and user[0]:
                email_body = f"""<div style="font-family:Arial,sans-serif;max-width:500px;margin:0 auto;padding:24px;background:#f8fafc;border-radius:12px;border:1px solid #e2e8f0;">
                    <h2 style="color:#0f172a;margin:0 0 16px;">HRMS Notification</h2>
                    <p style="color:#334155;">Hi <strong>{user[1] or emp_id}</strong>,</p>
                    <p style="color:#334155;">{message}</p>
                    {f'<p style="color:#334155;"><a href="{link}" style="color:#0369a1;">View Details</a></p>' if link else ''}
                    <p style="color:#64748b;font-size:12px;">- HRMS Team</p>
                </div>"""
                send_email(user[0], f'HRMS: {ntype}', email_body)
    except Exception as e:
        logger.warning("Notification failed: %s", e)
    finally:
        if conn:
            conn.close()


def notify_admins(ntype, message, link=None):
    conn = None
    try:
        conn = get_db()
        admins = conn.execute("SELECT emp_id FROM users WHERE role IN ('Admin', 'Super Admin')").fetchall()
        for a in admins:
            conn.execute(
                "INSERT INTO notifications (notification_id, emp_id, type, message, related_link, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                [gen_id(), a[0], ntype, message, link, now_ist()]
            )
    except Exception as e:
        logger.warning("Admin notification failed: %s", e)
    finally:
        if conn:
            conn.close()


def _get_shift_date_for_dt(emp_id, dt, conn):
    row = conn.execute("SELECT shift_start FROM users WHERE emp_id = ?", [emp_id]).fetchone()
    if row and row[0] and row[0] != '24x7':
        try:
            parts = row[0].split(':')
            h, m = int(parts[0]), int(parts[1])
            shift_start_today = dt.replace(hour=h, minute=m, second=0, microsecond=0)
            if dt >= shift_start_today:
                return dt.date()
            else:
                return (dt - timedelta(days=1)).date()
        except Exception:
            pass
    return dt.date()


def _get_shift_start_dt(emp_id, conn, target_date=None):
    now = now_ist()
    row = conn.execute("SELECT shift_start, shift_end FROM users WHERE emp_id = ?", [emp_id]).fetchone()
    shift_start_str = row[0] if row else None
    shift_end_str = row[1] if row else None
    if shift_start_str and shift_start_str != '24x7':
        try:
            parts = shift_start_str.split(':')
            h, m = int(parts[0]), int(parts[1])
            if target_date:
                dt = datetime(target_date.year, target_date.month, target_date.day, h, m, 0, 0)
            else:
                dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if dt > now:
                    dt -= timedelta(days=1)
            return dt
        except Exception:
            pass
    if target_date:
        return datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0, 0)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _get_shift_end_dt(emp_id, shift_start_dt, conn):
    row = conn.execute("SELECT shift_start, shift_end FROM users WHERE emp_id = ?", [emp_id]).fetchone()
    shift_start_str = row[0] if row else None
    shift_end_str = row[1] if row else None
    if shift_start_str and shift_start_str != '24x7' and shift_end_str:
        try:
            sp = shift_start_str.split(':')
            ep = shift_end_str.split(':')
            sh, sm = int(sp[0]), int(sp[1])
            eh, em = int(ep[0]), int(ep[1])
            end_dt = shift_start_dt.replace(hour=eh, minute=em, second=0, microsecond=0)
            if eh < sh or (eh == sh and em < sm):
                end_dt += timedelta(days=1)
            return end_dt
        except Exception:
            pass
    return shift_start_dt + timedelta(days=1)


def _fix_seed_shift_dates(conn, now):
    sessions = conn.execute("SELECT session_id, emp_id, login_time FROM user_sessions").fetchall()
    for sid, eid, lt in sessions:
        if lt:
            correct_date = _get_shift_date_for_dt(eid, lt, conn)
            conn.execute("UPDATE user_sessions SET session_date = ? WHERE session_id = ?", [correct_date, sid])
    breaks = conn.execute("SELECT break_id, emp_id, start_time FROM breaks").fetchall()
    for bid, eid, st in breaks:
        if st:
            correct_date = _get_shift_date_for_dt(eid, st, conn)
            conn.execute("UPDATE breaks SET break_date = ? WHERE break_id = ?", [correct_date, bid])
    conn.commit()


def send_email(to, subject, body):
    smtp_host = os.getenv('SMTP_HOST', '')
    if not smtp_host:
        logger.info("Email disabled (SMTP_HOST not set) -- would send to %s: %s", to, subject)
        return True
    try:
        smtp_port = int(os.getenv('SMTP_PORT', '587'))
        smtp_user = os.getenv('SMTP_USER', '')
        smtp_pass = os.getenv('SMTP_PASS', '')
        email_from = os.getenv('EMAIL_FROM', 'noreply@hrms.com')
        msg = MIMEMultipart()
        msg['From'] = email_from
        msg['To'] = to
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'html'))
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        logger.info("Email sent to %s: %s", to, subject)
        return True
    except Exception as e:
        logger.warning("Email failed to %s: %s", to, e)
        return False


def calc_payroll_item(emp_id, basic, hra, allowances, deductions):
    gross = basic + hra + allowances
    pf = min(gross * 0.12, 1800)
    esi = gross * 0.0075 if gross <= 21000 else 0
    pt = 200 if gross > 10000 else 0
    total_ded = deductions + pf + esi + pt
    net = gross - total_ded
    return gross, round(total_ded, 2), round(net, 2), round(pf, 2), round(esi, 2), pt


def _get_rates(conn, target_date=None):
    if target_date is None:
        target_date = now_ist().date()
    rows = conn.execute(
        "SELECT rate_type, value FROM payroll_rates WHERE effective_from <= ? AND (effective_to IS NULL OR effective_to >= ?)",
        [target_date, target_date]
    ).fetchall()
    return {r[0]: float(r[1]) for r in rows}


def calc_payroll_item_from_rates(emp_id, basic, hra, allowances, deductions, conn=None):
    own_conn = conn is None
    if own_conn:
        conn = get_db()
    try:
        rates = _get_rates(conn)
        gross = basic + hra + allowances
        pf_rate = rates.get('pf_rate', 12.0) / 100.0
        pf_max = rates.get('pf_max', 1800.0)
        pf = min(gross * pf_rate, pf_max)
        esi_rate = rates.get('esi_rate', 0.75) / 100.0
        esi_max_gross = rates.get('esi_max_gross', 21000.0)
        esi = gross * esi_rate if gross <= esi_max_gross else 0
        pt_threshold = rates.get('pt_threshold', 10000.0)
        pt_amount = rates.get('pt_amount', 200.0)
        pt = pt_amount if gross > pt_threshold else 0
        total_ded = deductions + pf + esi + pt
        net = gross - total_ded
        return gross, round(total_ded, 2), round(net, 2), round(pf, 2), round(esi, 2), pt
    finally:
        if own_conn:
            conn.close()


def calc_tds(annual_gross):
    if annual_gross <= 300000:
        return 0
    elif annual_gross <= 600000:
        return (annual_gross - 300000) * 0.05
    elif annual_gross <= 900000:
        return 15000 + (annual_gross - 600000) * 0.1
    elif annual_gross <= 1200000:
        return 45000 + (annual_gross - 900000) * 0.15
    elif annual_gross <= 1500000:
        return 90000 + (annual_gross - 1200000) * 0.2
    else:
        return 150000 + (annual_gross - 1500000) * 0.3


def calc_tds_from_rates(annual_gross, conn=None):
    own_conn = conn is None
    if own_conn:
        conn = get_db()
    try:
        rates = _get_rates(conn)
        slabs = [
            (rates.get('tds_slab_0_max', 300000.0), 0.0),
            (rates.get('tds_slab_1_max', 600000.0), rates.get('tds_rate_1', 5.0) / 100.0),
            (rates.get('tds_slab_2_max', 900000.0), rates.get('tds_rate_2', 10.0) / 100.0),
            (rates.get('tds_slab_3_max', 1200000.0), rates.get('tds_rate_3', 15.0) / 100.0),
            (rates.get('tds_slab_4_max', 1500000.0), rates.get('tds_rate_4', 20.0) / 100.0),
        ]
        tds_rate_5 = rates.get('tds_rate_5', 30.0) / 100.0
        prev_max = 0.0
        tax = 0.0
        for slab_max, rate in slabs:
            if annual_gross > slab_max:
                tax += (slab_max - prev_max) * rate
                prev_max = slab_max
            else:
                tax += (annual_gross - prev_max) * rate
                return round(tax, 2)
        tax += (annual_gross - prev_max) * tds_rate_5
        return round(tax, 2)
    finally:
        if own_conn:
            conn.close()


ONBOARDING_DOC_TYPES = ['ID Proof', 'Address Proof', 'Photo', 'Previous Organisation Documents', 'Qualification Documents']


def generate_payslip_pdf(run_id, emp_id):
    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib import colors

    conn = get_db()
    row = conn.execute(
        "SELECT p.item_id, r.month, r.year, p.emp_id, u.name, u.department, u.designation, p.gross_salary, p.deductions_total, p.net_salary, p.pf, p.esi, p.pt FROM payroll_items p JOIN payroll_runs r ON p.run_id = r.run_id JOIN users u ON p.emp_id = u.emp_id WHERE p.run_id = ? AND p.emp_id = ?",
        [run_id, emp_id]
    ).fetchone()
    conn.close()
    if not row:
        return None

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=30, bottomMargin=30)
    styles = getSampleStyleSheet()
    elements = []

    elements.append(Paragraph(f"PAYSLIP - {row[1]}/{row[2]}", styles['Title']))
    elements.append(Spacer(1, 12))

    data = [
        ['Employee ID', row[3]],
        ['Name', row[4]],
        ['Department', row[5] or '-'],
        ['Designation', row[6] or '-'],
        ['Gross Salary', f"\u20b9{float(row[7]):,.2f}"],
        ['PF', f"\u20b9{float(row[10]):,.2f}"],
        ['ESI', f"\u20b9{float(row[11]):,.2f}"],
        ['Professional Tax', f"\u20b9{float(row[12]):,.2f}"],
        ['Other Deductions', f"\u20b9{max(0, float(row[8]) - float(row[10]) - float(row[11]) - float(row[12])):,.2f}"],
        ['Total Deductions', f"\u20b9{float(row[8]):,.2f}"],
        ['NET SALARY', f"\u20b9{float(row[9]):,.2f}"],
    ]
    t = Table(data, colWidths=[200, 300])
    t.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 11),
        ('BACKGROUND', (0, 0), (0, -1), colors.Color(0.95, 0.95, 0.95)),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.grey),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('SPAN', (0, -1), (1, -1)),
        ('BACKGROUND', (0, -1), (-1, -1), colors.Color(0.12, 0.16, 0.23)),
        ('TEXTCOLOR', (0, -1), (-1, -1), colors.white),
        ('FONTSIZE', (0, -1), (-1, -1), 14),
    ]))
    elements.append(t)

    doc.build(elements)
    buf.seek(0)

    conn = get_db()
    conn.execute("UPDATE payroll_items SET payslip_generated = 1 WHERE run_id = ? AND emp_id = ?", [run_id, emp_id])
    conn.close()

    return buf
