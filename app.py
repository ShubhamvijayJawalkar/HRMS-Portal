import os
import logging
import secrets
import json
from datetime import datetime, timedelta
from functools import wraps
from io import BytesIO

import duckdb
import bcrypt
import pandas as pd
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_file
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flasgger import Swagger, swag_from
from apscheduler.schedulers.background import BackgroundScheduler

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────
log_level = getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper(), logging.INFO)
logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    level=log_level,
)
logger = logging.getLogger('hrms')

# ── Flask App ──────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', secrets.token_hex(32))
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

DB_FILE = os.getenv('DB_FILE', 'hrms.duckdb')

# ── Rate Limiter ──────────────────────────────────────────────────────
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=['200 per day', '50 per hour'],
    storage_uri='memory://',
)

# ── Swagger ───────────────────────────────────────────────────────────
swagger_config = {
    'headers': [],
    'specs': [
        {
            'endpoint': 'apispec',
            'route': '/apispec.json',
            'rule_filter': lambda rule: rule.rule.startswith('/api/'),
            'model_filter': lambda tag: True,
        }
    ],
    'static_url_path': '/flasgger_static',
    'swagger_ui': True,
    'specs_route': '/docs/',
}
swagger = Swagger(app, config=swagger_config, template={
    'info': {
        'title': 'HRMS API',
        'description': 'Human Resource Management System',
        'version': '1.0.0',
    },
    'securityDefinitions': {
        'sessionAuth': {
            'type': 'apiKey',
            'name': 'Cookie',
            'in': 'header',
        }
    }
})

# ── Scheduler ──────────────────────────────────────────────────────────
scheduler = BackgroundScheduler()
STARTED = False


# ══════════════════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════════════════

def get_db():
    conn = duckdb.connect(DB_FILE)
    conn.execute("SET TimeZone = 'UTC'")
    return conn


def init_db():
    conn = get_db()

    # ── Users ──────────────────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            emp_id VARCHAR PRIMARY KEY,
            name VARCHAR NOT NULL,
            email VARCHAR NOT NULL,
            password VARCHAR NOT NULL,
            role VARCHAR DEFAULT 'Employee',
            department VARCHAR,
            designation VARCHAR,
            manager_emp_id VARCHAR,
            phone VARCHAR,
            date_of_birth DATE,
            date_of_joining DATE,
            address VARCHAR,
            emergency_contact_name VARCHAR,
            emergency_contact_phone VARCHAR,
            status VARCHAR DEFAULT 'Active',
            allow_login INTEGER DEFAULT 1,
            allow_breaks INTEGER DEFAULT 1,
            first_login TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # ── User Sessions ──────────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS user_sessions (
            session_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            login_time TIMESTAMP NOT NULL,
            logout_time TIMESTAMP,
            total_hours DECIMAL(10,2),
            session_date DATE,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Break Types ────────────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS break_types (
            break_type VARCHAR PRIMARY KEY,
            daily_limit_minutes INTEGER,
            description VARCHAR
        )
    ''')

    # ── Breaks ─────────────────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS breaks (
            break_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            break_type VARCHAR NOT NULL,
            start_time TIMESTAMP NOT NULL,
            end_time TIMESTAMP,
            duration_minutes INTEGER,
            break_date DATE,
            status VARCHAR DEFAULT 'Active',
            FOREIGN KEY (emp_id) REFERENCES users(emp_id),
            FOREIGN KEY (break_type) REFERENCES break_types(break_type)
        )
    ''')

    # ── Audit Log (new) ────────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS audit_log (
            log_id INTEGER PRIMARY KEY,
            emp_id VARCHAR,
            action VARCHAR NOT NULL,
            details VARCHAR,
            ip_address VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # ── Leave Requests (new) ───────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS leave_requests (
            leave_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            leave_type VARCHAR NOT NULL,
            start_date DATE NOT NULL,
            end_date DATE NOT NULL,
            reason VARCHAR,
            status VARCHAR DEFAULT 'Pending',
            approved_by VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Leave Balance (new) ────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS leave_balance (
            balance_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            leave_type VARCHAR NOT NULL,
            total_days INTEGER DEFAULT 0,
            used_days INTEGER DEFAULT 0,
            year INTEGER NOT NULL,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Password Reset Tokens (new) ────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            token_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            token VARCHAR NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            used INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Employee Documents ─────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS employee_documents (
            doc_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            doc_type VARCHAR NOT NULL,
            file_name VARCHAR,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Dependents ─────────────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS dependents (
            dependent_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            relationship VARCHAR NOT NULL,
            date_of_birth DATE,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Holidays ───────────────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS holidays (
            holiday_id INTEGER PRIMARY KEY,
            name VARCHAR NOT NULL,
            holiday_date DATE NOT NULL,
            year INTEGER NOT NULL,
            type VARCHAR DEFAULT 'National'
        )
    ''')

    # ── Notifications ──────────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS notifications (
            notification_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            type VARCHAR NOT NULL,
            message VARCHAR NOT NULL,
            related_link VARCHAR,
            is_read INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Regularization Requests ────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS regularization_requests (
            request_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            request_date DATE NOT NULL,
            reason VARCHAR NOT NULL,
            status VARCHAR DEFAULT 'Pending',
            approved_by VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Assets (Phase 2) ────────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS assets (
            asset_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            asset_type VARCHAR NOT NULL,
            asset_tag VARCHAR,
            brand VARCHAR,
            model VARCHAR,
            serial_number VARCHAR,
            issued_date DATE NOT NULL,
            return_date DATE,
            status VARCHAR DEFAULT 'Issued',
            notes VARCHAR,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Job Postings (Phase 2) ──────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS job_postings (
            job_id INTEGER PRIMARY KEY,
            title VARCHAR NOT NULL,
            department VARCHAR,
            location VARCHAR,
            description VARCHAR,
            requirements VARCHAR,
            status VARCHAR DEFAULT 'Open',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # ── Candidates (Phase 2) ────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS candidates (
            candidate_id INTEGER PRIMARY KEY,
            job_id INTEGER,
            name VARCHAR NOT NULL,
            email VARCHAR NOT NULL,
            phone VARCHAR,
            resume_text VARCHAR,
            status VARCHAR DEFAULT 'Applied',
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (job_id) REFERENCES job_postings(job_id)
        )
    ''')

    # ── Interviews (Phase 2) ────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS interviews (
            interview_id INTEGER PRIMARY KEY,
            candidate_id INTEGER NOT NULL,
            scheduled_at TIMESTAMP NOT NULL,
            interviewer VARCHAR,
            mode VARCHAR DEFAULT 'In-person',
            feedback VARCHAR,
            status VARCHAR DEFAULT 'Scheduled',
            FOREIGN KEY (candidate_id) REFERENCES candidates(candidate_id)
        )
    ''')

    # ── Offer Letters (Phase 2) ─────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS offer_letters (
            offer_id INTEGER PRIMARY KEY,
            candidate_id INTEGER NOT NULL,
            offered_salary DECIMAL(12,2),
            offer_date DATE NOT NULL,
            status VARCHAR DEFAULT 'Pending',
            accepted_at TIMESTAMP,
            notes VARCHAR,
            FOREIGN KEY (candidate_id) REFERENCES candidates(candidate_id)
        )
    ''')

    # ── Onboarding Tasks (Phase 2) ──────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS onboarding_tasks (
            task_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            task_name VARCHAR NOT NULL,
            assigned_to VARCHAR NOT NULL,
            status VARCHAR DEFAULT 'Pending',
            due_date DATE,
            completed_at TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Offboarding Tasks (Phase 2) ─────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS offboarding_tasks (
            task_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            task_name VARCHAR NOT NULL,
            assigned_to VARCHAR NOT NULL,
            status VARCHAR DEFAULT 'Pending',
            due_date DATE,
            completed_at TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Exit Interviews (Phase 2) ───────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS exit_interviews (
            interview_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            reason VARCHAR NOT NULL,
            feedback VARCHAR,
            exit_date DATE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Salary Structures (Phase 2) ─────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS salary_structures (
            struct_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            basic DECIMAL(12,2) DEFAULT 0,
            hra DECIMAL(12,2) DEFAULT 0,
            allowances DECIMAL(12,2) DEFAULT 0,
            deductions DECIMAL(12,2) DEFAULT 0,
            effective_from DATE NOT NULL,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Payroll Runs (Phase 2) ──────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS payroll_runs (
            run_id INTEGER PRIMARY KEY,
            month INTEGER NOT NULL,
            year INTEGER NOT NULL,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status VARCHAR DEFAULT 'Draft'
        )
    ''')

    # ── Payroll Items (Phase 2) ─────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS payroll_items (
            item_id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            emp_id VARCHAR NOT NULL,
            gross_salary DECIMAL(12,2) DEFAULT 0,
            deductions_total DECIMAL(12,2) DEFAULT 0,
            net_salary DECIMAL(12,2) DEFAULT 0,
            pf DECIMAL(12,2) DEFAULT 0,
            esi DECIMAL(12,2) DEFAULT 0,
            pt DECIMAL(12,2) DEFAULT 0,
            payslip_generated INTEGER DEFAULT 0,
            FOREIGN KEY (run_id) REFERENCES payroll_runs(run_id),
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Performance Goals (Phase 3) ─────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS goals (
            goal_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            title VARCHAR NOT NULL,
            description VARCHAR,
            target_date DATE,
            weight INTEGER DEFAULT 1,
            rating INTEGER,
            status VARCHAR DEFAULT 'Active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Performance Reviews (Phase 3) ───────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS performance_reviews (
            review_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            reviewer_id VARCHAR NOT NULL,
            review_period VARCHAR NOT NULL,
            overall_rating REAL,
            comments VARCHAR,
            status VARCHAR DEFAULT 'Draft',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            submitted_at TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id),
            FOREIGN KEY (reviewer_id) REFERENCES users(emp_id)
        )
    ''')

    # ── 360 Feedback (Phase 3) ──────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS feedback_360 (
            feedback_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            reviewer_id VARCHAR NOT NULL,
            category VARCHAR,
            rating INTEGER,
            comment VARCHAR,
            submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id),
            FOREIGN KEY (reviewer_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Expense Categories (Phase 3) ─────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS expense_categories (
            cat_id INTEGER PRIMARY KEY,
            name VARCHAR NOT NULL,
            description VARCHAR
        )
    ''')

    # ── Expense Claims (Phase 3) ─────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS expense_claims (
            claim_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            cat_id INTEGER NOT NULL,
            amount DECIMAL(12,2) NOT NULL,
            description VARCHAR,
            receipt_path VARCHAR,
            status VARCHAR DEFAULT 'Pending',
            approved_by VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id),
            FOREIGN KEY (cat_id) REFERENCES expense_categories(cat_id)
        )
    ''')

    # ── Help Desk Tickets (Phase 3) ──────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS tickets (
            ticket_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            subject VARCHAR NOT NULL,
            description VARCHAR,
            category VARCHAR,
            priority VARCHAR DEFAULT 'Medium',
            status VARCHAR DEFAULT 'Open',
            assigned_to VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP,
            resolved_at TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Ticket Comments (Phase 3) ────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS ticket_comments (
            comment_id INTEGER PRIMARY KEY,
            ticket_id INTEGER NOT NULL,
            emp_id VARCHAR NOT NULL,
            comment VARCHAR NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (ticket_id) REFERENCES tickets(ticket_id),
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Document Uploads metadata (Phase 3) ─────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS documents (
            doc_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            category VARCHAR DEFAULT 'Other',
            file_path VARCHAR,
            file_size INTEGER,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Seed Data ──────────────────────────────────────────────────
    pwd_hash = bcrypt.hashpw(b'pass123', bcrypt.gensalt()).decode()
    result = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if result == 0:
        conn.execute(
            "INSERT INTO users (emp_id, name, email, password, role, department, designation, phone, date_of_joining, status, allow_login, allow_breaks, first_login, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ['EMP001', 'Shubham Jawalkar', 'shubham@company.com', pwd_hash,
             'Admin', 'MIS', 'Tech Lead', '9876543210', datetime.now().date(),
             'Active', 1, 1, datetime.now(), datetime.now()]
        )
        conn.execute(
            "INSERT INTO users (emp_id, name, email, password, role, department, designation, phone, date_of_joining, manager_emp_id, status, allow_login, allow_breaks, first_login, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ['EMP002', 'Sachin Bhakte', 'sachinbhakte@gmail.com', pwd_hash,
             'Employee', 'Operations', 'Jr Developer', '9876543211', datetime.now().date(),
             'EMP001', 'Active', 1, 1, datetime.now(), datetime.now()]
        )
        conn.execute(
            "INSERT INTO users (emp_id, name, email, password, role, department, designation, phone, date_of_joining, manager_emp_id, status, allow_login, allow_breaks, first_login, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ['EMP003', 'Mangesh', 'Mangesh@abc.com', pwd_hash,
             'Employee', 'Support', 'Support Engineer', '9876543212', datetime.now().date(),
             'EMP001', 'Active', 1, 1, datetime.now(), datetime.now()]
        )
        conn.execute(
            "INSERT INTO users (emp_id, name, email, password, role, department, designation, phone, date_of_joining, manager_emp_id, status, allow_login, allow_breaks, first_login, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ['EMP004', 'Priya Sharma', 'priya@company.com', pwd_hash,
             'Employee', 'Engineering', 'Sr Software Engineer', '9876543213', datetime.now().date(),
             'EMP001', 'Active', 1, 1, datetime.now(), datetime.now()]
        )
        conn.execute(
            "INSERT INTO users (emp_id, name, email, password, role, department, designation, phone, date_of_joining, manager_emp_id, status, allow_login, allow_breaks, first_login, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ['EMP005', 'Rahul Verma', 'rahul@company.com', pwd_hash,
             'Employee', 'Engineering', 'Software Engineer', '9876543214', datetime.now().date(),
             'EMP004', 'Active', 1, 1, datetime.now(), datetime.now()]
        )
        conn.execute(
            "INSERT INTO users (emp_id, name, email, password, role, department, designation, phone, date_of_joining, manager_emp_id, status, allow_login, allow_breaks, first_login, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ['EMP006', 'Anita Desai', 'anita@company.com', pwd_hash,
             'Employee', 'HR', 'HR Manager', '9876543215', datetime.now().date(),
             'EMP001', 'Active', 1, 1, datetime.now(), datetime.now()]
        )
        conn.execute(
            "INSERT INTO users (emp_id, name, email, password, role, department, designation, phone, date_of_joining, manager_emp_id, status, allow_login, allow_breaks, first_login, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ['EMP007', 'Vikram Singh', 'vikram@company.com', pwd_hash,
             'Employee', 'Finance', 'Finance Manager', '9876543216', datetime.now().date(),
             'EMP001', 'Active', 1, 1, datetime.now(), datetime.now()]
        )
        conn.execute(
            "INSERT INTO users (emp_id, name, email, password, role, department, designation, phone, date_of_joining, manager_emp_id, status, allow_login, allow_breaks, first_login, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ['EMP008', 'Neha Kapoor', 'neha@company.com', pwd_hash,
             'Employee', 'Marketing', 'Marketing Lead', '9876543217', datetime.now().date(),
             'EMP001', 'Active', 1, 1, datetime.now(), datetime.now()]
        )
        conn.execute(
            "INSERT INTO users (emp_id, name, email, password, role, department, designation, phone, date_of_joining, manager_emp_id, status, allow_login, allow_breaks, first_login, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ['EMP009', 'Amit Patel', 'amit@company.com', pwd_hash,
             'Employee', 'Sales', 'Sales Executive', '9876543218', datetime.now().date(),
             'EMP001', 'Active', 1, 1, datetime.now(), datetime.now()]
        )
        conn.execute(
            "INSERT INTO users (emp_id, name, email, password, role, department, designation, phone, date_of_joining, manager_emp_id, status, allow_login, allow_breaks, first_login, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ['EMP010', 'Sonali Joshi', 'sonali@company.com', pwd_hash,
             'Employee', 'Design', 'UI/UX Designer', '9876543219', datetime.now().date(),
             'EMP001', 'Active', 1, 1, datetime.now(), datetime.now()]
        )

        # Seed some holidays
        year = datetime.now().year
        base = int(datetime.now().timestamp() * 1000) % 1000000
        holidays_data = [
            [base + 1, 'New Year', f'{year}-01-01', year, 'National'],
            [base + 2, 'Republic Day', f'{year}-01-26', year, 'National'],
            [base + 3, 'Independence Day', f'{year}-08-15', year, 'National'],
            [base + 4, 'Diwali', f'{year}-11-01', year, 'Optional'],
            [base + 5, 'Christmas', f'{year}-12-25', year, 'Optional'],
        ]
        conn.executemany("INSERT INTO holidays VALUES (?, ?, ?, ?, ?)", holidays_data)

    # ── Seed Expense Categories ───────────────────────────────────
    result = conn.execute("SELECT COUNT(*) FROM expense_categories").fetchone()[0]
    if result == 0:
        conn.executemany(
            "INSERT INTO expense_categories VALUES (?, ?, ?)",
            [[1, 'Travel', 'Travel expenses including flights, trains, cabs'],
             [2, 'Food', 'Meals and refreshments'],
             [3, 'Office Supplies', 'Stationery and office consumables'],
             [4, 'Equipment', 'Hardware and equipment purchases'],
             [5, 'Utilities', 'Phone, internet, electricity bills'],
             [6, 'Other', 'Miscellaneous expenses']]
        )

    # ── Normalize all passwords to bcrypt ─────────────────────────
    conn.execute(
        "UPDATE users SET password = ? WHERE password NOT LIKE '$2%'",
        [pwd_hash]
    )

    # ── Migrate: add new columns if missing ───────────────────────
    for col in ['designation', 'manager_emp_id', 'phone', 'date_of_birth', 'date_of_joining', 'address', 'emergency_contact_name', 'emergency_contact_phone']:
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} VARCHAR")
        except Exception:
            pass

    result = conn.execute("SELECT COUNT(*) FROM break_types").fetchone()[0]
    if result == 0:
        conn.executemany(
            "INSERT INTO break_types VALUES (?, ?, ?)",
            [('Tea', 15, 'Tea Break - 15 minutes'),
             ('Lunch', 60, 'Lunch Break - 1 hour'),
             ('Personal', 30, 'Personal Break - 30 minutes')]
        )

    # ── Seed Leave Balance ─────────────────────────────────────────
    result = conn.execute("SELECT COUNT(*) FROM leave_balance").fetchone()[0]
    if result == 0:
        year = datetime.now().year
        bid = int(datetime.now().timestamp() * 1000) % 1000000
        for emp in conn.execute("SELECT emp_id FROM users").fetchall():
            bid += 1
            conn.execute("INSERT INTO leave_balance VALUES (?, ?, ?, ?, ?, ?)", [bid, emp[0], 'Casual', 12, 0, year])
            bid += 1
            conn.execute("INSERT INTO leave_balance VALUES (?, ?, ?, ?, ?, ?)", [bid, emp[0], 'Sick', 10, 0, year])
            bid += 1
            conn.execute("INSERT INTO leave_balance VALUES (?, ?, ?, ?, ?, ?)", [bid, emp[0], 'Annual', 20, 0, year])

    # ── Seed sample rows for all major modules ────────────────────
    now = datetime.now()
    base_id = int(now.timestamp() * 1000) % 1000000

    if conn.execute("SELECT COUNT(*) FROM user_sessions").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO user_sessions (session_id, emp_id, login_time, logout_time, total_hours, session_date) VALUES (?, ?, ?, ?, ?, ?)",
            [base_id + 1, 'EMP001', now - timedelta(hours=8), now, 8.0, now.date()]
        )
        conn.execute(
            "INSERT INTO user_sessions (session_id, emp_id, login_time, logout_time, total_hours, session_date) VALUES (?, ?, ?, ?, ?, ?)",
            [base_id + 2, 'EMP002', now - timedelta(hours=6), now - timedelta(hours=1), 5.0, now.date()]
        )

    if conn.execute("SELECT COUNT(*) FROM breaks").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO breaks (break_id, emp_id, break_type, start_time, end_time, duration_minutes, break_date, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 3, 'EMP001', 'Tea', now - timedelta(minutes=30), now - timedelta(minutes=15), 15, now.date(), 'Completed']
        )
        conn.execute(
            "INSERT INTO breaks (break_id, emp_id, break_type, start_time, end_time, duration_minutes, break_date, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 4, 'EMP002', 'Lunch', now - timedelta(hours=1), now - timedelta(minutes=30), 30, now.date(), 'Completed']
        )

    if conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO audit_log (log_id, emp_id, action, details, ip_address, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            [base_id + 5, 'EMP001', 'LOGIN', 'User signed in', '127.0.0.1', now]
        )
        conn.execute(
            "INSERT INTO audit_log (log_id, emp_id, action, details, ip_address, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            [base_id + 6, 'EMP002', 'PROFILE_UPDATE', 'Updated profile', '127.0.0.1', now - timedelta(hours=1)]
        )

    if conn.execute("SELECT COUNT(*) FROM leave_requests").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO leave_requests (leave_id, emp_id, leave_type, start_date, end_date, reason, status, approved_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 7, 'EMP002', 'Casual', (now + timedelta(days=3)).date(), (now + timedelta(days=4)).date(), 'Personal work', 'Pending', None, now, now]
        )
        conn.execute(
            "INSERT INTO leave_requests (leave_id, emp_id, leave_type, start_date, end_date, reason, status, approved_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 8, 'EMP003', 'Sick', (now + timedelta(days=10)).date(), (now + timedelta(days=12)).date(), 'Medical appointment', 'Approved', 'EMP001', now, now]
        )

    if conn.execute("SELECT COUNT(*) FROM password_reset_tokens").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO password_reset_tokens (token_id, emp_id, token, expires_at, used, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            [base_id + 9, 'EMP002', 'reset-token-001', now + timedelta(hours=2), 0, now]
        )
        conn.execute(
            "INSERT INTO password_reset_tokens (token_id, emp_id, token, expires_at, used, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            [base_id + 10, 'EMP003', 'reset-token-002', now + timedelta(hours=4), 0, now]
        )

    if conn.execute("SELECT COUNT(*) FROM employee_documents").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO employee_documents (doc_id, emp_id, doc_type, file_name, uploaded_at) VALUES (?, ?, ?, ?, ?)",
            [base_id + 11, 'EMP001', 'Offer Letter', 'offer-letter.pdf', now]
        )
        conn.execute(
            "INSERT INTO employee_documents (doc_id, emp_id, doc_type, file_name, uploaded_at) VALUES (?, ?, ?, ?, ?)",
            [base_id + 12, 'EMP002', 'ID Proof', 'aadhaar.pdf', now - timedelta(days=1)]
        )

    if conn.execute("SELECT COUNT(*) FROM dependents").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO dependents (dependent_id, emp_id, name, relationship, date_of_birth) VALUES (?, ?, ?, ?, ?)",
            [base_id + 13, 'EMP001', 'Ananya', 'Spouse', (now - timedelta(days=365*30)).date()]
        )
        conn.execute(
            "INSERT INTO dependents (dependent_id, emp_id, name, relationship, date_of_birth) VALUES (?, ?, ?, ?, ?)",
            [base_id + 14, 'EMP002', 'Riya', 'Child', (now - timedelta(days=365*7)).date()]
        )

    if conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO notifications (notification_id, emp_id, type, message, related_link, is_read, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [base_id + 15, 'EMP001', 'Leave', 'Your leave request is pending', '/leaves', 0, now]
        )
        conn.execute(
            "INSERT INTO notifications (notification_id, emp_id, type, message, related_link, is_read, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [base_id + 16, 'EMP002', 'Profile', 'Please update your profile', '/profile', 0, now - timedelta(hours=2)]
        )

    if conn.execute("SELECT COUNT(*) FROM regularization_requests").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO regularization_requests (request_id, emp_id, request_date, reason, status, approved_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 17, 'EMP002', now.date(), 'Late arrival', 'Pending', None, now, now]
        )
        conn.execute(
            "INSERT INTO regularization_requests (request_id, emp_id, request_date, reason, status, approved_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 18, 'EMP003', (now - timedelta(days=1)).date(), 'Forgot punch', 'Approved', 'EMP001', now - timedelta(days=1), now]
        )

    if conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO assets (asset_id, emp_id, asset_type, asset_tag, brand, model, serial_number, issued_date, return_date, status, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 19, 'EMP002', 'Laptop', 'LAP-001', 'Dell', 'Latitude 5430', 'SN-1001', (now - timedelta(days=30)).date(), None, 'Issued', 'Primary workstation']
        )
        conn.execute(
            "INSERT INTO assets (asset_id, emp_id, asset_type, asset_tag, brand, model, serial_number, issued_date, return_date, status, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 20, 'EMP003', 'Phone', 'PH-001', 'Samsung', 'Galaxy S24', 'SN-1002', (now - timedelta(days=10)).date(), None, 'Issued', 'Company phone']
        )

    if conn.execute("SELECT COUNT(*) FROM job_postings").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO job_postings (job_id, title, department, location, description, requirements, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 21, 'Software Engineer', 'Engineering', 'Pune', 'Build scalable apps', 'Python, Flask', 'Open', now]
        )
        conn.execute(
            "INSERT INTO job_postings (job_id, title, department, location, description, requirements, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 22, 'HR Specialist', 'HR', 'Remote', 'Support employee lifecycle', 'People operations', 'Open', now]
        )

    if conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO candidates (candidate_id, job_id, name, email, phone, resume_text, status, applied_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 23, base_id + 21, 'Kavya Rao', 'kavya@example.com', '9999999001', 'Experienced backend engineer', 'Applied', now]
        )
        conn.execute(
            "INSERT INTO candidates (candidate_id, job_id, name, email, phone, resume_text, status, applied_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 24, base_id + 22, 'Mihir Shah', 'mihir@example.com', '9999999002', 'HR operations background', 'Screening', now - timedelta(days=1)]
        )

    if conn.execute("SELECT COUNT(*) FROM interviews").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO interviews (interview_id, candidate_id, scheduled_at, interviewer, mode, feedback, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [base_id + 25, base_id + 23, now + timedelta(days=2), 'EMP001', 'Virtual', 'Strong technical skills', 'Scheduled']
        )
        conn.execute(
            "INSERT INTO interviews (interview_id, candidate_id, scheduled_at, interviewer, mode, feedback, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [base_id + 26, base_id + 24, now + timedelta(days=3), 'EMP006', 'In-person', 'Good fit', 'Scheduled']
        )

    if conn.execute("SELECT COUNT(*) FROM offer_letters").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO offer_letters (offer_id, candidate_id, offered_salary, offer_date, status, accepted_at, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [base_id + 27, base_id + 23, 1800000.00, now.date(), 'Pending', None, 'Standard package']
        )
        conn.execute(
            "INSERT INTO offer_letters (offer_id, candidate_id, offered_salary, offer_date, status, accepted_at, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [base_id + 28, base_id + 24, 1200000.00, (now - timedelta(days=1)).date(), 'Accepted', now, 'Offer accepted']
        )

    if conn.execute("SELECT COUNT(*) FROM onboarding_tasks").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO onboarding_tasks (task_id, emp_id, task_name, assigned_to, status, due_date, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [base_id + 29, 'EMP002', 'Laptop setup', 'EMP001', 'Pending', (now + timedelta(days=2)).date(), None]
        )
        conn.execute(
            "INSERT INTO onboarding_tasks (task_id, emp_id, task_name, assigned_to, status, due_date, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [base_id + 30, 'EMP003', 'HR paperwork', 'EMP006', 'Completed', (now - timedelta(days=1)).date(), now - timedelta(hours=3)]
        )

    if conn.execute("SELECT COUNT(*) FROM offboarding_tasks").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO offboarding_tasks (task_id, emp_id, task_name, assigned_to, status, due_date, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [base_id + 31, 'EMP004', 'Collect company assets', 'EMP001', 'Pending', (now + timedelta(days=5)).date(), None]
        )
        conn.execute(
            "INSERT INTO offboarding_tasks (task_id, emp_id, task_name, assigned_to, status, due_date, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [base_id + 32, 'EMP005', 'Revoke access', 'EMP001', 'Completed', (now - timedelta(days=1)).date(), now - timedelta(days=1)]
        )

    if conn.execute("SELECT COUNT(*) FROM exit_interviews").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO exit_interviews (interview_id, emp_id, reason, feedback, exit_date, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            [base_id + 33, 'EMP004', 'Career change', 'Positive experience', (now - timedelta(days=2)).date(), now - timedelta(days=2)]
        )
        conn.execute(
            "INSERT INTO exit_interviews (interview_id, emp_id, reason, feedback, exit_date, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            [base_id + 34, 'EMP005', 'Relocation', 'Clear onboarding', (now - timedelta(days=5)).date(), now - timedelta(days=5)]
        )

    if conn.execute("SELECT COUNT(*) FROM salary_structures").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO salary_structures (struct_id, emp_id, basic, hra, allowances, deductions, effective_from) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [base_id + 35, 'EMP002', 30000.00, 9000.00, 4000.00, 1500.00, (now - timedelta(days=30)).date()]
        )
        conn.execute(
            "INSERT INTO salary_structures (struct_id, emp_id, basic, hra, allowances, deductions, effective_from) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [base_id + 36, 'EMP003', 28000.00, 8400.00, 3200.00, 1200.00, (now - timedelta(days=60)).date()]
        )

    if conn.execute("SELECT COUNT(*) FROM payroll_runs").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO payroll_runs (run_id, month, year, processed_at, status) VALUES (?, ?, ?, ?, ?)",
            [base_id + 37, now.month, now.year, now, 'Draft']
        )
        conn.execute(
            "INSERT INTO payroll_runs (run_id, month, year, processed_at, status) VALUES (?, ?, ?, ?, ?)",
            [base_id + 38, now.month - 1 if now.month > 1 else 12, now.year if now.month > 1 else now.year - 1, now - timedelta(days=30), 'Finalized']
        )

    if conn.execute("SELECT COUNT(*) FROM payroll_items").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO payroll_items (item_id, run_id, emp_id, gross_salary, deductions_total, net_salary, pf, esi, pt, payslip_generated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 39, base_id + 37, 'EMP002', 50000.00, 5000.00, 45000.00, 2500.00, 1500.00, 200.00, 0]
        )
        conn.execute(
            "INSERT INTO payroll_items (item_id, run_id, emp_id, gross_salary, deductions_total, net_salary, pf, esi, pt, payslip_generated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 40, base_id + 38, 'EMP003', 48000.00, 4800.00, 43200.00, 2400.00, 1400.00, 200.00, 1]
        )

    if conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO goals (goal_id, emp_id, title, description, target_date, weight, rating, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 41, 'EMP002', 'Improve delivery', 'Ship one feature per sprint', (now + timedelta(days=30)).date(), 5, 4, 'Active', now]
        )
        conn.execute(
            "INSERT INTO goals (goal_id, emp_id, title, description, target_date, weight, rating, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 42, 'EMP003', 'Customer support excellence', 'Maintain SLA', (now + timedelta(days=45)).date(), 4, 5, 'Active', now]
        )

    if conn.execute("SELECT COUNT(*) FROM performance_reviews").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO performance_reviews (review_id, emp_id, reviewer_id, review_period, overall_rating, comments, status, created_at, submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 43, 'EMP002', 'EMP001', 'Q2 2026', 4.2, 'Strong execution', 'Submitted', now - timedelta(days=5), now - timedelta(days=3)]
        )
        conn.execute(
            "INSERT INTO performance_reviews (review_id, emp_id, reviewer_id, review_period, overall_rating, comments, status, created_at, submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 44, 'EMP003', 'EMP001', 'Q2 2026', 4.6, 'Excellent ownership', 'Draft', now - timedelta(days=2), None]
        )

    if conn.execute("SELECT COUNT(*) FROM feedback_360").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO feedback_360 (feedback_id, emp_id, reviewer_id, category, rating, comment, submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [base_id + 45, 'EMP002', 'EMP006', 'Collaboration', 5, 'Great teammate', now - timedelta(days=1)]
        )
        conn.execute(
            "INSERT INTO feedback_360 (feedback_id, emp_id, reviewer_id, category, rating, comment, submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [base_id + 46, 'EMP003', 'EMP004', 'Communication', 4, 'Clear updates', now - timedelta(days=2)]
        )

    if conn.execute("SELECT COUNT(*) FROM expense_claims").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO expense_claims (claim_id, emp_id, cat_id, amount, description, receipt_path, status, approved_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 47, 'EMP002', 1, 1250.00, 'Mumbai travel', 'travel.pdf', 'Pending', None, now]
        )
        conn.execute(
            "INSERT INTO expense_claims (claim_id, emp_id, cat_id, amount, description, receipt_path, status, approved_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 48, 'EMP003', 2, 850.00, 'Client lunch', 'food.pdf', 'Approved', 'EMP001', now - timedelta(days=2)]
        )

    if conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO tickets (ticket_id, emp_id, subject, description, category, priority, status, assigned_to, created_at, updated_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 49, 'EMP002', 'VPN access issue', 'Unable to connect to VPN', 'IT', 'High', 'Open', 'EMP001', now, now, None]
        )
        conn.execute(
            "INSERT INTO tickets (ticket_id, emp_id, subject, description, category, priority, status, assigned_to, created_at, updated_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 50, 'EMP003', 'Payroll question', 'Need pay slip clarification', 'HR', 'Medium', 'Resolved', 'EMP006', now - timedelta(days=1), now - timedelta(hours=2), now - timedelta(hours=1)]
        )

    if conn.execute("SELECT COUNT(*) FROM ticket_comments").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO ticket_comments (comment_id, ticket_id, emp_id, comment, created_at) VALUES (?, ?, ?, ?, ?)",
            [base_id + 51, base_id + 49, 'EMP001', 'We are looking into it', now]
        )
        conn.execute(
            "INSERT INTO ticket_comments (comment_id, ticket_id, emp_id, comment, created_at) VALUES (?, ?, ?, ?, ?)",
            [base_id + 52, base_id + 50, 'EMP006', 'Shared the payslip details', now - timedelta(hours=1)]
        )

    if conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] < 2:
        conn.execute(
            "INSERT INTO documents (doc_id, emp_id, name, category, file_path, file_size, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [base_id + 53, 'EMP001', 'Offer Letter', 'Offer Letter', '/uploads/offer.pdf', 204800, now]
        )
        conn.execute(
            "INSERT INTO documents (doc_id, emp_id, name, category, file_path, file_size, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [base_id + 54, 'EMP002', 'ID Proof', 'ID Proof', '/uploads/id.pdf', 153600, now - timedelta(days=1)]
        )

    conn.close()
    logger.info("Database initialized")


init_db()


# ══════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════

def audit_log(emp_id, action, details=None):
    try:
        conn = get_db()
        log_id = int(datetime.now().timestamp() * 1_000_000) % 2_147_483_647
        conn.execute(
            "INSERT INTO audit_log (log_id, emp_id, action, details, ip_address, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            [log_id, emp_id, action, details, request.remote_addr, datetime.now()]
        )
        conn.close()
    except Exception as e:
        logger.warning("audit_log failed: %s", e)


def parse_date(date_string, default=None):
    if not date_string:
        return default
    try:
        return datetime.strptime(date_string, '%Y-%m-%d').date()
    except ValueError:
        return default


def gen_id():
    return int(datetime.now().timestamp() * 1_000_000) % 2_147_483_647


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
        "SELECT emp_id, name, email, role, status, department, allow_login, allow_breaks, designation, manager_emp_id, phone, date_of_birth, date_of_joining, address, emergency_contact_name, emergency_contact_phone FROM users WHERE emp_id = ?",
        [emp_id]
    ).fetchone()
    conn.close()
    return u


# ══════════════════════════════════════════════════════════════════════
#  DECORATORS
# ══════════════════════════════════════════════════════════════════════

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'emp_id' not in session:
            if request.is_json:
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'emp_id' not in session:
            if request.is_json:
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('login'))
        emp_id = session['emp_id']
        conn = get_db()
        row = conn.execute("SELECT role FROM users WHERE emp_id = ?", [emp_id]).fetchone()
        conn.close()
        if not row or row[0] != 'Admin':
            return jsonify({'error': 'Forbidden'}), 403
        return f(*args, **kwargs)
    return decorated


def hr_or_admin_required(f):
    """Require Admin role OR HR department"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'emp_id' not in session:
            if request.is_json:
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('login'))
        emp_id = session['emp_id']
        conn = get_db()
        row = conn.execute("SELECT role, department FROM users WHERE emp_id = ?", [emp_id]).fetchone()
        conn.close()
        if not row:
            return jsonify({'error': 'Forbidden'}), 403
        if row[0] == 'Admin' or row[1] == 'HR':
            return f(*args, **kwargs)
        return jsonify({'error': 'Forbidden - HR access required'}), 403
    return decorated


def department_required(*depts):
    """Require specific department(s) or Admin role"""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if 'emp_id' not in session:
                if request.is_json:
                    return jsonify({'error': 'Authentication required'}), 401
                return redirect(url_for('login'))
            emp_id = session['emp_id']
            conn = get_db()
            row = conn.execute("SELECT role, department FROM users WHERE emp_id = ?", [emp_id]).fetchone()
            conn.close()
            if not row:
                return jsonify({'error': 'Forbidden'}), 403
            if row[0] == 'Admin' or row[1] in depts:
                return f(*args, **kwargs)
            return jsonify({'error': 'Forbidden - insufficient department access'}), 403
        return decorated
    return decorator


# ══════════════════════════════════════════════════════════════════════
#  AUTH ROUTES
# ══════════════════════════════════════════════════════════════════════

@app.route('/api/credentials')
def get_credentials():
    """Return known user credentials for demo purposes"""
    conn = get_db()
    rows = conn.execute("SELECT emp_id, name, role, department FROM users ORDER BY emp_id").fetchall()
    conn.close()
    result = [{'emp_id': r[0], 'name': r[1], 'role': r[2], 'department': r[3] or '-', 'password': 'pass123'} for r in rows]
    return jsonify(result), 200


@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def login():
    """User login
    ---
    post:
      tags: [Auth]
      parameters:
        - in: body
          name: body
          schema:
            type: object
            properties:
              emp_id: {type: string}
              password: {type: string}
      responses:
        200: {description: Login success}
        401: {description: Invalid credentials}
    """
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
        if stored_hash.startswith('$2'):
            return jsonify({'error': 'Invalid Password'}), 401
        try:
            import hashlib
            if hashlib.md5(password.encode()).hexdigest() != stored_hash:
                return jsonify({'error': 'Invalid Password'}), 401
            conn = get_db()
            new_hash = hash_password(password)
            conn.execute("UPDATE users SET password = ? WHERE emp_id = ?", [new_hash, emp_id])
            conn.close()
        except Exception:
            return jsonify({'error': 'Invalid Password'}), 401

    if not row[5]:
        return jsonify({'error': 'Login is not allowed for this user'}), 403
    if row[4] == 'Blocked':
        return jsonify({'error': 'Account is blocked'}), 403

    session_id = gen_id()
    session['emp_id'] = row[0]
    session['name'] = row[1]
    session['role'] = row[2]
    session['department'] = row[6] or ''
    session['session_id'] = session_id

    now = datetime.now()
    conn = get_db()
    conn.execute(
        "INSERT INTO user_sessions (session_id, emp_id, login_time, session_date) VALUES (?, ?, ?, ?)",
        [session_id, row[0], now, now.date()]
    )
    conn.close()

    audit_log(row[0], 'LOGIN', f'User {row[1]} logged in')
    return jsonify({'message': 'Login successful', 'redirect': '/dashboard'}), 200


@app.route('/logout')
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
            logout_time = datetime.now()
            hours = round((logout_time - login_time).total_seconds() / 3600, 2)
            conn.execute(
                "UPDATE user_sessions SET logout_time = ?, total_hours = ? WHERE session_id = ?",
                [logout_time, hours, curr_sid]
            )
        conn.close()
        audit_log(emp_id, 'LOGOUT', 'User logged out')
    session.clear()
    return redirect(url_for('login'))


@app.route('/dashboard')
@login_required
def dashboard():
    if session.get('role') == 'Admin' or session.get('department') == 'HR':
        return render_template('admin_dashboard.html')
    return render_template('user_dashboard.html')


@app.route('/profile')
@login_required
def profile_page():
    return render_template('profile.html')


@app.route('/api/profile', methods=['GET', 'PUT'])
@login_required
def profile_api():
    """Employee self-service: view / update profile
    ---
    get:
      tags: [Profile]
      responses:
        200:
          description: Profile data
    put:
      tags: [Profile]
      parameters:
        - in: body
          name: body
          schema:
            type: object
            properties:
              name: {type: string}
              email: {type: string}
              department: {type: string}
      responses:
        200:
          description: Updated
    """
    emp_id = session['emp_id']
    if request.method == 'GET':
        u = get_user(emp_id)
        if not u:
            return jsonify({'error': 'Not found'}), 404
        return jsonify({
            'emp_id': u[0], 'name': u[1], 'email': u[2],
            'role': u[3], 'department': u[5],
            'allow_login': u[6], 'allow_breaks': u[7],
            'designation': u[8], 'manager_emp_id': u[9],
            'phone': u[10],
            'date_of_birth': u[11].isoformat() if u[11] else None,
            'date_of_joining': u[12].isoformat() if u[12] else None,
            'address': u[13], 'emergency_contact_name': u[14],
            'emergency_contact_phone': u[15]
        }), 200

    data = request.get_json(silent=True) or {}
    conn = get_db()
    conn.execute(
        "UPDATE users SET name = ?, email = ?, department = ?, phone = ?, address = ?, emergency_contact_name = ?, emergency_contact_phone = ? WHERE emp_id = ?",
        [data.get('name'), data.get('email'), data.get('department', ''),
         data.get('phone'), data.get('address'), data.get('emergency_contact_name'),
         data.get('emergency_contact_phone'), emp_id]
    )
    conn.close()
    session['name'] = data.get('name')
    audit_log(emp_id, 'PROFILE_UPDATE', 'Profile updated')
    return jsonify({'message': 'Profile updated'}), 200


@app.route('/api/change-password', methods=['POST'])
@login_required
def change_password():
    """Change own password
    ---
    post:
      tags: [Profile]
      parameters:
        - in: body
          name: body
          schema:
            type: object
            properties:
              current_password: {type: string}
              new_password: {type: string}
      responses:
        200: {description: Password changed}
        400: {description: Validation error}
    """
    data = request.get_json(silent=True) or {}
    emp_id = session['emp_id']
    current = data.get('current_password', '')
    new_pwd = data.get('new_password', '')

    if len(new_pwd) < 6:
        return jsonify({'error': 'New password must be at least 6 characters'}), 400

    conn = get_db()
    row = conn.execute("SELECT password FROM users WHERE emp_id = ?", [emp_id]).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'User not found'}), 404

    stored = row[0]
    valid = check_password(current, stored)
    if not valid and not stored.startswith('$2'):
        import hashlib
        valid = hashlib.md5(current.encode()).hexdigest() == stored
    if not valid:
        conn.close()
        return jsonify({'error': 'Current password is incorrect'}), 400

    conn.execute("UPDATE users SET password = ? WHERE emp_id = ?", [hash_password(new_pwd), emp_id])
    conn.close()
    audit_log(emp_id, 'PASSWORD_CHANGE', 'Password changed')
    return jsonify({'message': 'Password changed successfully'}), 200


@app.route('/api/forgot-password', methods=['POST'])
def forgot_password():
    """Request password reset (generates token)
    ---
    post:
      tags: [Auth]
      parameters:
        - in: body
          name: body
          schema:
            type: object
            properties:
              emp_id: {type: string}
              email: {type: string}
      responses:
        200:
          description: Token generated (shown in dev)
    """
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
        [gen_id(), emp_id, token, datetime.now() + timedelta(hours=1)]
    )
    conn.close()

    logger.info("Password reset token for %s: %s", emp_id, token)
    return jsonify({
        'message': 'If the account exists, a reset link has been generated.',
        'token': token,
    }), 200


@app.route('/api/reset-password', methods=['POST'])
def reset_password():
    """Reset password using token
    ---
    post:
      tags: [Auth]
      parameters:
        - in: body
          name: body
          schema:
            type: object
            properties:
              token: {type: string}
              new_password: {type: string}
      responses:
        200:
          description: Password reset
    """
    data = request.get_json(silent=True) or {}
    token = data.get('token', '')
    new_pwd = data.get('new_password', '')

    if len(new_pwd) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400

    conn = get_db()
    row = conn.execute(
        "SELECT token_id, emp_id FROM password_reset_tokens WHERE token = ? AND used = 0 AND expires_at > ?",
        [token, datetime.now()]
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Invalid or expired token'}), 400

    conn.execute("UPDATE password_reset_tokens SET used = 1 WHERE token_id = ?", [row[0]])
    conn.execute("UPDATE users SET password = ? WHERE emp_id = ?", [hash_password(new_pwd), row[1]])
    conn.close()
    audit_log(row[1], 'PASSWORD_RESET', 'Password reset via token')
    return jsonify({'message': 'Password reset successfully'}), 200


# ══════════════════════════════════════════════════════════════════════
#  EMPLOYEE MASTER EXTENSIONS
# ══════════════════════════════════════════════════════════════════════

@app.route('/api/v1/dependents', methods=['GET', 'POST'])
@app.route('/api/dependents', methods=['GET', 'POST'])
@login_required
def dependents_api():
    emp_id = session['emp_id']
    if request.method == 'GET':
        conn = get_db()
        rows = conn.execute("SELECT dependent_id, name, relationship, date_of_birth FROM dependents WHERE emp_id = ?", [emp_id]).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'name': r[1], 'relationship': r[2], 'date_of_birth': r[3].isoformat() if r[3] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('name') or not data.get('relationship'):
        return jsonify({'error': 'name and relationship required'}), 400
    did = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO dependents VALUES (?, ?, ?, ?, ?)",
                 [did, emp_id, data['name'], data['relationship'], parse_date(data.get('date_of_birth'))])
    conn.close()
    return jsonify({'message': 'Dependent added', 'id': did}), 201


@app.route('/api/v1/dependents/<int:did>', methods=['DELETE'])
@app.route('/api/dependents/<int:did>', methods=['DELETE'])
@login_required
def delete_dependent(did):
    conn = get_db()
    conn.execute("DELETE FROM dependents WHERE dependent_id = ? AND emp_id = ?", [did, session['emp_id']])
    conn.close()
    return jsonify({'message': 'Deleted'}), 200


@app.route('/api/v1/documents', methods=['GET', 'POST'])
@app.route('/api/documents', methods=['GET', 'POST'])
@login_required
def documents_api():
    emp_id = session['emp_id']
    if request.method == 'GET':
        conn = get_db()
        rows = conn.execute("SELECT doc_id, doc_type, file_name, uploaded_at FROM employee_documents WHERE emp_id = ?", [emp_id]).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'doc_type': r[1], 'file_name': r[2], 'uploaded_at': r[3].isoformat() if r[3] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('doc_type'):
        return jsonify({'error': 'doc_type required'}), 400
    did = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO employee_documents VALUES (?, ?, ?, ?, ?)",
                 [did, emp_id, data['doc_type'], data.get('file_name', ''), datetime.now()])
    conn.close()
    return jsonify({'message': 'Document recorded', 'id': did}), 201


# ══════════════════════════════════════════════════════════════════════
#  HOLIDAY CALENDAR
# ══════════════════════════════════════════════════════════════════════

@app.route('/api/v1/holidays', methods=['GET'])
@app.route('/api/holidays', methods=['GET'])
@login_required
def get_holidays():
    year = request.args.get('year', datetime.now().year, type=int)
    conn = get_db()
    rows = conn.execute("SELECT holiday_id, name, holiday_date, type FROM holidays WHERE year = ? ORDER BY holiday_date", [year]).fetchall()
    conn.close()
    return jsonify([{'id': r[0], 'name': r[1], 'date': r[2].isoformat(), 'type': r[3]} for r in rows]), 200


@app.route('/api/v1/holidays', methods=['POST'])
@app.route('/api/holidays', methods=['POST'])
@admin_required
def add_holiday():
    data = request.get_json(silent=True) or {}
    if not data.get('name') or not data.get('date'):
        return jsonify({'error': 'name and date required'}), 400
    d = parse_date(data['date'])
    hid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO holidays VALUES (?, ?, ?, ?, ?)",
                 [hid, data['name'], d, d.year, data.get('type', 'National')])
    conn.close()
    return jsonify({'message': 'Holiday added', 'id': hid}), 201


@app.route('/api/v1/holidays/<int:hid>', methods=['DELETE'])
@app.route('/api/holidays/<int:hid>', methods=['DELETE'])
@admin_required
def delete_holiday(hid):
    conn = get_db()
    conn.execute("DELETE FROM holidays WHERE holiday_id = ?", [hid])
    conn.close()
    return jsonify({'message': 'Deleted'}), 200


# ══════════════════════════════════════════════════════════════════════
#  ORG CHART
# ══════════════════════════════════════════════════════════════════════

@app.route('/api/v1/org-chart')
@app.route('/api/org-chart')
@admin_required
def org_chart():
    conn = get_db()
    rows = conn.execute(
        "SELECT emp_id, name, designation, department, manager_emp_id, status FROM users WHERE status = 'Active' ORDER BY name"
    ).fetchall()
    conn.close()
    employees = [{
        'id': r[0],
        'name': r[1],
        'designation': r[2] or '',
        'department': r[3] or '',
        'manager_id': r[4] or '',
        'status': r[5] or 'Active'
    } for r in rows]
    return jsonify(employees), 200


# ══════════════════════════════════════════════════════════════════════
#  NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════

def add_notification(emp_id, ntype, message, link=None):
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO notifications (notification_id, emp_id, type, message, related_link, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            [gen_id(), emp_id, ntype, message, link, datetime.now()]
        )
        conn.close()
    except Exception as e:
        logger.warning("Notification failed: %s", e)


@app.route('/api/v1/notifications', methods=['GET'])
@app.route('/api/notifications', methods=['GET'])
@login_required
def get_notifications():
    conn = get_db()
    rows = conn.execute(
        "SELECT notification_id, type, message, related_link, is_read, created_at FROM notifications WHERE emp_id = ? ORDER BY created_at DESC LIMIT 50",
        [session['emp_id']]
    ).fetchall()
    unread = conn.execute("SELECT COUNT(*) FROM notifications WHERE emp_id = ? AND is_read = 0", [session['emp_id']]).fetchone()[0]
    conn.close()
    return jsonify({
        'unread': unread,
        'data': [{'id': r[0], 'type': r[1], 'message': r[2], 'link': r[3], 'is_read': bool(r[4]), 'created_at': r[5].isoformat() if r[5] else None} for r in rows]
    }), 200


@app.route('/api/v1/notifications/read', methods=['POST'])
@app.route('/api/notifications/read', methods=['POST'])
@login_required
def mark_notifications_read():
    conn = get_db()
    conn.execute("UPDATE notifications SET is_read = 1 WHERE emp_id = ?", [session['emp_id']])
    conn.close()
    return jsonify({'message': 'Marked read'}), 200


# ══════════════════════════════════════════════════════════════════════
#  REGULARIZATION
# ══════════════════════════════════════════════════════════════════════

@app.route('/api/v1/regularization', methods=['GET', 'POST'])
@app.route('/api/regularization', methods=['GET', 'POST'])
@login_required
def regularization_api():
    emp_id = session['emp_id']
    if request.method == 'GET':
        conn = get_db()
        if session.get('role') == 'Admin':
            rows = conn.execute(
                "SELECT request_id, emp_id, request_date, reason, status, approved_by, created_at FROM regularization_requests ORDER BY created_at DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT request_id, emp_id, request_date, reason, status, approved_by, created_at FROM regularization_requests WHERE emp_id = ? ORDER BY created_at DESC",
                [emp_id]
            ).fetchall()
        conn.close()
        return jsonify([{
            'id': r[0], 'emp_id': r[1], 'date': r[2].isoformat(),
            'reason': r[3], 'status': r[4], 'approved_by': r[5],
            'created_at': r[6].isoformat() if r[6] else None
        } for r in rows]), 200

    data = request.get_json(silent=True) or {}
    d = parse_date(data.get('date'))
    if not d or not data.get('reason'):
        return jsonify({'error': 'date and reason required'}), 400
    rid = gen_id()
    conn = get_db()
    conn.execute(
        "INSERT INTO regularization_requests (request_id, emp_id, request_date, reason, status) VALUES (?, ?, ?, ?, 'Pending')",
        [rid, emp_id, d, data['reason']]
    )
    conn.close()
    return jsonify({'message': 'Request submitted', 'id': rid}), 201


@app.route('/api/v1/regularization/<int:rid>/approve', methods=['POST'])
@app.route('/api/regularization/<int:rid>/approve', methods=['POST'])
@admin_required
def approve_regularization(rid):
    conn = get_db()
    conn.execute(
        "UPDATE regularization_requests SET status = 'Approved', approved_by = ?, updated_at = ? WHERE request_id = ? AND status = 'Pending'",
        [session['emp_id'], datetime.now(), rid]
    )
    conn.close()
    return jsonify({'message': 'Approved'}), 200


@app.route('/api/v1/regularization/<int:rid>/reject', methods=['POST'])
@app.route('/api/regularization/<int:rid>/reject', methods=['POST'])
@admin_required
def reject_regularization(rid):
    conn = get_db()
    conn.execute(
        "UPDATE regularization_requests SET status = 'Rejected', approved_by = ?, updated_at = ? WHERE request_id = ? AND status = 'Pending'",
        [session['emp_id'], datetime.now(), rid]
    )
    conn.close()
    return jsonify({'message': 'Rejected'}), 200


# ══════════════════════════════════════════════════════════════════════
#  CSV IMPORT
# ══════════════════════════════════════════════════════════════════════

@app.route('/api/v1/users/import', methods=['POST'])
@app.route('/api/users/import', methods=['POST'])
@admin_required
def import_users_csv():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    f = request.files['file']
    if not f.filename.endswith('.csv'):
        return jsonify({'error': 'CSV file required'}), 400
    try:
        df = pd.read_csv(f)
        required = ['emp_id', 'name', 'email']
        missing = [c for c in required if c not in df.columns]
        if missing:
            return jsonify({'error': f'Missing columns: {missing}'}), 400
        conn = get_db()
        pwd = hash_password('pass123')
        count = 0
        for _, row in df.iterrows():
            eid = str(row['emp_id']).strip().upper()
            if conn.execute("SELECT 1 FROM users WHERE emp_id = ?", [eid]).fetchone():
                continue
            conn.execute(
                "INSERT INTO users (emp_id, name, email, password, role, department, status, first_login, created_at, allow_login, allow_breaks) VALUES (?, ?, ?, ?, ?, ?, 'Active', ?, ?, 1, 1)",
                [eid, str(row.get('name', '')), str(row.get('email', '')), pwd,
                 str(row.get('role', 'Employee')), str(row.get('department', '')),
                 datetime.now(), datetime.now()]
            )
            count += 1
        conn.close()
        return jsonify({'message': f'{count} users imported'}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 400


# ══════════════════════════════════════════════════════════════════════
#  PHASE 2 — ASSET MANAGEMENT
# ══════════════════════════════════════════════════════════════════════

@app.route('/admin/assets')
@hr_or_admin_required
def admin_assets():
    return render_template('assets.html')


@app.route('/api/v1/my-assets')
@app.route('/api/my-assets')
@login_required
def my_assets():
    conn = get_db()
    rows = conn.execute("SELECT asset_id, asset_type, asset_tag, brand, model, serial_number, issued_date, return_date, status, notes FROM assets WHERE emp_id = ? ORDER BY issued_date DESC", [session['emp_id']]).fetchall()
    conn.close()
    return jsonify([{'id': r[0], 'type': r[1], 'tag': r[2], 'brand': r[3], 'model': r[4], 'serial': r[5], 'issued': r[6].isoformat() if r[6] else None, 'returned': r[7].isoformat() if r[7] else None, 'status': r[8], 'notes': r[9]} for r in rows]), 200


@app.route('/api/v1/assets', methods=['GET', 'POST'])
@app.route('/api/assets', methods=['GET', 'POST'])
@admin_required
def assets_api():
    if request.method == 'GET':
        emp = request.args.get('emp_id')
        conn = get_db()
        if emp:
            rows = conn.execute("SELECT a.asset_id, a.emp_id, u.name, a.asset_type, a.asset_tag, a.brand, a.model, a.serial_number, a.issued_date, a.return_date, a.status, a.notes FROM assets a JOIN users u ON a.emp_id = u.emp_id WHERE a.emp_id = ? ORDER BY a.issued_date DESC", [emp]).fetchall()
        else:
            rows = conn.execute("SELECT a.asset_id, a.emp_id, u.name, a.asset_type, a.asset_tag, a.brand, a.model, a.serial_number, a.issued_date, a.return_date, a.status, a.notes FROM assets a JOIN users u ON a.emp_id = u.emp_id ORDER BY a.issued_date DESC").fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'type': r[3], 'tag': r[4], 'brand': r[5], 'model': r[6], 'serial': r[7], 'issued': r[8].isoformat() if r[8] else None, 'returned': r[9].isoformat() if r[9] else None, 'status': r[10], 'notes': r[11]} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('emp_id') or not data.get('asset_type'):
        return jsonify({'error': 'emp_id and asset_type required'}), 400
    aid = gen_id()
    conn = get_db()
    employee = conn.execute("SELECT 1 FROM users WHERE emp_id = ? AND status = 'Active'", [data['emp_id']]).fetchone()
    if not employee:
        conn.close()
        return jsonify({'error': 'Employee not found or inactive'}), 400
    conn.execute("INSERT INTO assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 [aid, data['emp_id'], data['asset_type'], data.get('asset_tag'), data.get('brand'), data.get('model'), data.get('serial_number'),
                  parse_date(data.get('issued_date'), datetime.now().date()), None, 'Issued', data.get('notes')])
    conn.close()
    return jsonify({'message': 'Asset issued', 'id': aid}), 201


@app.route('/api/v1/assets/<int:aid>/return', methods=['POST'])
@app.route('/api/assets/<int:aid>/return', methods=['POST'])
@admin_required
def return_asset(aid):
    conn = get_db()
    conn.execute("UPDATE assets SET return_date = ?, status = 'Returned' WHERE asset_id = ?", [datetime.now().date(), aid])
    conn.close()
    return jsonify({'message': 'Asset returned'}), 200


# ══════════════════════════════════════════════════════════════════════
#  PHASE 2 — RECRUITMENT / ATS
# ══════════════════════════════════════════════════════════════════════

@app.route('/admin/jobs')
@hr_or_admin_required
def admin_jobs():
    return render_template('jobs.html')


@app.route('/admin/candidates')
@hr_or_admin_required
def admin_candidates():
    return render_template('candidates.html')


# ── Job Postings ──────────────────────────────────────────────────

@app.route('/api/v1/jobs', methods=['GET', 'POST'])
@app.route('/api/jobs', methods=['GET', 'POST'])
@admin_required
def jobs_api():
    if request.method == 'GET':
        conn = get_db()
        rows = conn.execute("SELECT job_id, title, department, location, description, requirements, status, created_at FROM job_postings ORDER BY created_at DESC").fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'title': r[1], 'department': r[2], 'location': r[3], 'description': r[4], 'requirements': r[5], 'status': r[6], 'created_at': r[7].isoformat() if r[7] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('title'):
        return jsonify({'error': 'title required'}), 400
    jid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO job_postings VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 [jid, data['title'], data.get('department'), data.get('location'), data.get('description'), data.get('requirements'), 'Open', datetime.now()])
    conn.close()
    return jsonify({'message': 'Job created', 'id': jid}), 201


@app.route('/api/v1/jobs/<int:jid>/close', methods=['POST'])
@app.route('/api/jobs/<int:jid>/close', methods=['POST'])
@admin_required
def close_job(jid):
    conn = get_db()
    conn.execute("UPDATE job_postings SET status = 'Closed' WHERE job_id = ?", [jid])
    conn.close()
    return jsonify({'message': 'Job closed'}), 200


# ── Candidates ────────────────────────────────────────────────────

@app.route('/api/v1/candidates', methods=['GET', 'POST'])
@app.route('/api/candidates', methods=['GET', 'POST'])
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
        return jsonify([{'id': r[0], 'job_id': r[1], 'job_title': r[2] or 'N/A', 'name': r[3], 'email': r[4], 'phone': r[5], 'status': r[6], 'applied_at': r[7].isoformat() if r[7] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('name') or not data.get('email'):
        return jsonify({'error': 'name and email required'}), 400
    cid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO candidates (candidate_id, job_id, name, email, phone, resume_text, status, applied_at) VALUES (?, ?, ?, ?, ?, ?, 'Applied', ?)",
                 [cid, data.get('job_id'), data['name'], data['email'], data.get('phone'), data.get('resume_text', ''), datetime.now()])
    conn.close()
    return jsonify({'message': 'Candidate added', 'id': cid}), 201


@app.route('/api/v1/candidates/<int:cid>/status', methods=['PUT'])
@app.route('/api/candidates/<int:cid>/status', methods=['PUT'])
@hr_or_admin_required
def update_candidate_status(cid):
    data = request.get_json(silent=True) or {}
    status = data.get('status')
    if status not in ('Applied', 'Screened', 'Interviewed', 'Offered', 'Hired', 'Rejected'):
        return jsonify({'error': 'Invalid status'}), 400
    conn = get_db()
    conn.execute("UPDATE candidates SET status = ? WHERE candidate_id = ?", [status, cid])
    conn.close()
    return jsonify({'message': f'Status updated to {status}'}), 200


# ── Interviews ────────────────────────────────────────────────────

@app.route('/api/v1/interviews', methods=['GET', 'POST'])
@app.route('/api/interviews', methods=['GET', 'POST'])
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
        return jsonify([{'id': r[0], 'candidate_id': r[1], 'candidate_name': r[2], 'scheduled_at': r[3].isoformat() if r[3] else None, 'interviewer': r[4], 'mode': r[5], 'feedback': r[6], 'status': r[7]} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('candidate_id') or not data.get('scheduled_at'):
        return jsonify({'error': 'candidate_id and scheduled_at required'}), 400
    iid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO interviews VALUES (?, ?, ?, ?, ?, ?, ?)",
                 [iid, data['candidate_id'], parse_date(data['scheduled_at']), data.get('interviewer'), data.get('mode', 'In-person'), data.get('feedback'), 'Scheduled'])
    conn.close()
    return jsonify({'message': 'Interview scheduled', 'id': iid}), 201


@app.route('/api/v1/interviews/<int:iid>/feedback', methods=['PUT'])
@app.route('/api/interviews/<int:iid>/feedback', methods=['PUT'])
@hr_or_admin_required
def interview_feedback(iid):
    data = request.get_json(silent=True) or {}
    conn = get_db()
    conn.execute("UPDATE interviews SET feedback = ?, status = 'Completed' WHERE interview_id = ?", [data.get('feedback', ''), iid])
    conn.close()
    return jsonify({'message': 'Feedback saved'}), 200


# ── Offer Letters ─────────────────────────────────────────────────

@app.route('/api/v1/offers', methods=['GET', 'POST'])
@app.route('/api/offers', methods=['GET', 'POST'])
@hr_or_admin_required
def offers_api():
    if request.method == 'GET':
        conn = get_db()
        rows = conn.execute("SELECT o.offer_id, o.candidate_id, c.name, c.email, o.offered_salary, o.offer_date, o.status, o.accepted_at, o.notes FROM offer_letters o JOIN candidates c ON o.candidate_id = c.candidate_id ORDER BY o.offer_date DESC").fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'candidate_id': r[1], 'candidate_name': r[2], 'email': r[3], 'salary': float(r[4]) if r[4] else 0, 'offer_date': r[5].isoformat() if r[5] else None, 'status': r[6], 'accepted_at': r[7].isoformat() if r[7] else None, 'notes': r[8]} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('candidate_id') or not data.get('offered_salary'):
        return jsonify({'error': 'candidate_id and offered_salary required'}), 400
    oid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO offer_letters VALUES (?, ?, ?, ?, ?, ?, ?)",
                 [oid, data['candidate_id'], float(data['offered_salary']), datetime.now().date(), 'Pending', None, data.get('notes')])
    conn.close()
    return jsonify({'message': 'Offer sent', 'id': oid}), 201


@app.route('/api/v1/offers/<int:oid>/accept', methods=['POST'])
@app.route('/api/offers/<int:oid>/accept', methods=['POST'])
@hr_or_admin_required
def accept_offer(oid):
    conn = get_db()
    conn.execute("UPDATE offer_letters SET status = 'Accepted', accepted_at = ? WHERE offer_id = ?", [datetime.now(), oid])
    row = conn.execute("SELECT candidate_id FROM offer_letters WHERE offer_id = ?", [oid]).fetchone()
    if row:
        conn.execute("UPDATE candidates SET status = 'Hired' WHERE candidate_id = ?", [row[0]])
    conn.close()
    return jsonify({'message': 'Offer accepted'}), 200


# ══════════════════════════════════════════════════════════════════════
#  PHASE 2 — ONBOARDING & OFFBOARDING
# ══════════════════════════════════════════════════════════════════════

@app.route('/onboarding')
@login_required
def onboarding_page():
    return render_template('onboarding.html')


@app.route('/api/v1/onboarding-tasks', methods=['GET', 'POST'])
@app.route('/api/onboarding-tasks', methods=['GET', 'POST'])
@login_required
def onboarding_api():
    if request.method == 'GET':
        conn = get_db()
        if session.get('role') == 'Admin':
            rows = conn.execute("SELECT t.task_id, t.emp_id, u.name, t.task_name, t.assigned_to, t.status, t.due_date, t.completed_at FROM onboarding_tasks t JOIN users u ON t.emp_id = u.emp_id ORDER BY t.task_id DESC").fetchall()
        else:
            rows = conn.execute("SELECT t.task_id, t.emp_id, u.name, t.task_name, t.assigned_to, t.status, t.due_date, t.completed_at FROM onboarding_tasks t JOIN users u ON t.emp_id = u.emp_id WHERE t.emp_id = ? ORDER BY t.task_id DESC", [session['emp_id']]).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'task': r[3], 'assigned_to': r[4], 'status': r[5], 'due_date': r[6].isoformat() if r[6] else None, 'completed_at': r[7].isoformat() if r[7] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('emp_id') or not data.get('task_name'):
        return jsonify({'error': 'emp_id and task_name required'}), 400
    tid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO onboarding_tasks VALUES (?, ?, ?, ?, ?, ?, ?)",
                 [tid, data['emp_id'], data['task_name'], data.get('assigned_to', 'HR'), 'Pending', parse_date(data.get('due_date')), None])
    conn.close()
    return jsonify({'message': 'Task added', 'id': tid}), 201


@app.route('/api/v1/onboarding-tasks/<int:tid>/complete', methods=['POST'])
@app.route('/api/onboarding-tasks/<int:tid>/complete', methods=['POST'])
@admin_required
def complete_onboarding_task(tid):
    conn = get_db()
    conn.execute("UPDATE onboarding_tasks SET status = 'Completed', completed_at = ? WHERE task_id = ?", [datetime.now(), tid])
    conn.close()
    return jsonify({'message': 'Task completed'}), 200


@app.route('/offboarding')
@login_required
def offboarding_page():
    return render_template('offboarding.html')


@app.route('/api/v1/offboarding-tasks', methods=['GET', 'POST'])
@app.route('/api/offboarding-tasks', methods=['GET', 'POST'])
@login_required
def offboarding_api():
    if request.method == 'GET':
        conn = get_db()
        if session.get('role') == 'Admin':
            rows = conn.execute("SELECT t.task_id, t.emp_id, u.name, t.task_name, t.assigned_to, t.status, t.due_date, t.completed_at FROM offboarding_tasks t JOIN users u ON t.emp_id = u.emp_id ORDER BY t.task_id DESC").fetchall()
        else:
            rows = conn.execute("SELECT t.task_id, t.emp_id, u.name, t.task_name, t.assigned_to, t.status, t.due_date, t.completed_at FROM offboarding_tasks t JOIN users u ON t.emp_id = u.emp_id WHERE t.emp_id = ? ORDER BY t.task_id DESC", [session['emp_id']]).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'task': r[3], 'assigned_to': r[4], 'status': r[5], 'due_date': r[6].isoformat() if r[6] else None, 'completed_at': r[7].isoformat() if r[7] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('emp_id') or not data.get('task_name'):
        return jsonify({'error': 'emp_id and task_name required'}), 400
    tid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO offboarding_tasks VALUES (?, ?, ?, ?, ?, ?, ?)",
                 [tid, data['emp_id'], data['task_name'], data.get('assigned_to', 'HR'), 'Pending', parse_date(data.get('due_date')), None])
    conn.close()
    return jsonify({'message': 'Task added', 'id': tid}), 201


@app.route('/api/v1/offboarding-tasks/<int:tid>/complete', methods=['POST'])
@app.route('/api/offboarding-tasks/<int:tid>/complete', methods=['POST'])
@admin_required
def complete_offboarding_task(tid):
    conn = get_db()
    conn.execute("UPDATE offboarding_tasks SET status = 'Completed', completed_at = ? WHERE task_id = ?", [datetime.now(), tid])
    conn.close()
    return jsonify({'message': 'Task completed'}), 200


@app.route('/api/v1/exit-interviews', methods=['GET', 'POST'])
@app.route('/api/exit-interviews', methods=['GET', 'POST'])
@admin_required
def exit_interviews_api():
    if request.method == 'GET':
        conn = get_db()
        rows = conn.execute("SELECT ei.interview_id, ei.emp_id, u.name, ei.reason, ei.feedback, ei.exit_date, ei.created_at FROM exit_interviews ei JOIN users u ON ei.emp_id = u.emp_id ORDER BY ei.created_at DESC").fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'reason': r[3], 'feedback': r[4], 'exit_date': r[5].isoformat() if r[5] else None, 'created_at': r[6].isoformat() if r[6] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('emp_id') or not data.get('reason') or not data.get('exit_date'):
        return jsonify({'error': 'emp_id, reason, exit_date required'}), 400
    eid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO exit_interviews VALUES (?, ?, ?, ?, ?, ?)",
                 [eid, data['emp_id'], data['reason'], data.get('feedback'), parse_date(data['exit_date']), datetime.now()])
    conn.close()
    return jsonify({'message': 'Exit interview recorded'}), 201


# ══════════════════════════════════════════════════════════════════════
#  PHASE 2 — PAYROLL ENGINE
# ══════════════════════════════════════════════════════════════════════

@app.route('/admin/payroll')
@hr_or_admin_required
def admin_payroll():
    return render_template('payroll.html')


@app.route('/admin/salary-structures')
@hr_or_admin_required
def admin_salary():
    return render_template('salary.html')


# ── Salary Structures ────────────────────────────────────────────

@app.route('/api/v1/salary-structures', methods=['GET', 'POST'])
@app.route('/api/salary-structures', methods=['GET', 'POST'])
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
    conn.execute("INSERT INTO salary_structures VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 [sid, data['emp_id'], float(data['basic']), float(data.get('hra', 0)), float(data.get('allowances', 0)), float(data.get('deductions', 0)),
                  parse_date(data.get('effective_from'), datetime.now().date())])
    conn.close()
    return jsonify({'message': 'Salary structure saved', 'id': sid}), 201


# ── Payroll Runs ─────────────────────────────────────────────────

def calc_payroll_item(emp_id, basic, hra, allowances, deductions):
    gross = basic + hra + allowances
    pf = min(gross * 0.12, 1800)
    esi = gross * 0.0075 if gross <= 21000 else 0
    pt = 200 if gross > 10000 else 0
    total_ded = deductions + pf + esi + pt
    net = gross - total_ded
    return gross, round(total_ded, 2), round(net, 2), round(pf, 2), round(esi, 2), pt


@app.route('/api/v1/payroll-runs', methods=['GET', 'POST'])
@app.route('/api/payroll-runs', methods=['GET', 'POST'])
@hr_or_admin_required
def payroll_runs_api():
    if request.method == 'GET':
        conn = get_db()
        rows = conn.execute("SELECT run_id, month, year, processed_at, status FROM payroll_runs ORDER BY year DESC, month DESC").fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'month': r[1], 'year': r[2], 'processed_at': r[3].isoformat() if r[3] else None, 'status': r[4]} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    month, year = data.get('month'), data.get('year')
    if not month or not year:
        return jsonify({'error': 'month and year required'}), 400
    conn = get_db()
    if conn.execute("SELECT 1 FROM payroll_runs WHERE month = ? AND year = ?", [month, year]).fetchone():
        conn.close()
        return jsonify({'error': 'Payroll already processed for this period'}), 409
    rid = gen_id()
    conn.execute("INSERT INTO payroll_runs VALUES (?, ?, ?, ?, ?)", [rid, month, year, datetime.now(), 'Draft'])
    employees = conn.execute("SELECT u.emp_id, COALESCE(s.basic,0), COALESCE(s.hra,0), COALESCE(s.allowances,0), COALESCE(s.deductions,0) FROM users u LEFT JOIN salary_structures s ON u.emp_id = s.emp_id AND s.effective_from <= ? WHERE u.role = 'Employee'", [datetime.now().date()]).fetchall()
    for e in employees:
        gross, total_ded, net, pf, esi, pt = calc_payroll_item(e[0], float(e[1]), float(e[2]), float(e[3]), float(e[4]))
        conn.execute("INSERT INTO payroll_items (item_id, run_id, emp_id, gross_salary, deductions_total, net_salary, pf, esi, pt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     [gen_id(), rid, e[0], gross, total_ded, net, pf, esi, pt])
    conn.close()
    return jsonify({'message': f'Payroll run created for {month}/{year}'}), 201


@app.route('/api/v1/payroll-runs/<int:rid>/finalize', methods=['POST'])
@app.route('/api/payroll-runs/<int:rid>/finalize', methods=['POST'])
@hr_or_admin_required
def finalize_payroll(rid):
    conn = get_db()
    conn.execute("UPDATE payroll_runs SET status = 'Finalized' WHERE run_id = ?", [rid])
    conn.close()
    return jsonify({'message': 'Payroll finalized'}), 200


@app.route('/api/v1/payroll-runs/<int:rid>/items')
@app.route('/api/payroll-runs/<int:rid>/items')
@hr_or_admin_required
def payroll_items(rid):
    conn = get_db()
    rows = conn.execute(
        "SELECT p.item_id, p.emp_id, u.name, p.gross_salary, p.deductions_total, p.net_salary, p.pf, p.esi, p.pt, p.payslip_generated FROM payroll_items p JOIN users u ON p.emp_id = u.emp_id WHERE p.run_id = ? ORDER BY u.name",
        [rid]
    ).fetchall()
    conn.close()
    return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'gross': float(r[3]), 'deductions': float(r[4]), 'net': float(r[5]), 'pf': float(r[6]), 'esi': float(r[7]), 'pt': float(r[8]), 'payslip_generated': bool(r[9])} for r in rows]), 200


@app.route('/api/v1/payslip/<int:run_id>/<emp_id>')
@app.route('/api/payslip/<int:run_id>/<emp_id>')
@login_required
def get_payslip(run_id, emp_id):
    if session.get('role') != 'Admin' and session['emp_id'] != emp_id:
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


@app.route('/api/v1/my-payslips')
@app.route('/api/my-payslips')
@login_required
def my_payslips():
    conn = get_db()
    rows = conn.execute(
        "SELECT r.run_id, r.month, r.year, p.net_salary, r.status FROM payroll_items p JOIN payroll_runs r ON p.run_id = r.run_id WHERE p.emp_id = ? ORDER BY r.year DESC, r.month DESC",
        [session['emp_id']]
    ).fetchall()
    conn.close()
    return jsonify([{'run_id': r[0], 'month': r[1], 'year': r[2], 'net': float(r[3]), 'status': r[4]} for r in rows]), 200


# ══════════════════════════════════════════════════════════════════════
#  PHASE 3 — PERFORMANCE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════

@app.route('/admin/goals')
@hr_or_admin_required
def admin_goals():
    return render_template('goals.html')


@app.route('/admin/reviews')
@hr_or_admin_required
def admin_reviews():
    return render_template('reviews.html')


@app.route('/goals')
@login_required
def goals_page():
    return render_template('my_goals.html')


# ── Goals ──────────────────────────────────────────────────────────

@app.route('/api/v1/goals', methods=['GET', 'POST'])
@app.route('/api/goals', methods=['GET', 'POST'])
@login_required
def goals_api():
    if request.method == 'GET':
        conn = get_db()
        if session.get('role') == 'Admin':
            rows = conn.execute("SELECT g.goal_id, g.emp_id, u.name, g.title, g.description, g.target_date, g.weight, g.rating, g.status, g.created_at FROM goals g JOIN users u ON g.emp_id = u.emp_id ORDER BY g.created_at DESC").fetchall()
        else:
            rows = conn.execute("SELECT g.goal_id, g.emp_id, u.name, g.title, g.description, g.target_date, g.weight, g.rating, g.status, g.created_at FROM goals g JOIN users u ON g.emp_id = u.emp_id WHERE g.emp_id = ? ORDER BY g.created_at DESC", [session['emp_id']]).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'title': r[3], 'description': r[4], 'target_date': r[5].isoformat() if r[5] else None, 'weight': r[6], 'rating': r[7], 'status': r[8], 'created_at': r[9].isoformat() if r[9] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('title'):
        return jsonify({'error': 'title required'}), 400
    gid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO goals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 [gid, data.get('emp_id', session['emp_id']), data['title'], data.get('description'),
                  parse_date(data.get('target_date')), data.get('weight', 1), None, 'Active', datetime.now()])
    conn.close()
    return jsonify({'message': 'Goal created', 'id': gid}), 201


@app.route('/api/v1/goals/<int:gid>/rate', methods=['PUT'])
@app.route('/api/goals/<int:gid>/rate', methods=['PUT'])
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


@app.route('/api/v1/goals/<int:gid>', methods=['PUT'])
@app.route('/api/goals/<int:gid>', methods=['PUT'])
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

@app.route('/api/v1/performance-reviews', methods=['GET', 'POST'])
@app.route('/api/performance-reviews', methods=['GET', 'POST'])
@hr_or_admin_required
def reviews_api():
    if request.method == 'GET':
        conn = get_db()
        rows = conn.execute(
            "SELECT r.review_id, r.emp_id, u.name, r.reviewer_id, rev.name, r.review_period, r.overall_rating, r.comments, r.status, r.submitted_at FROM performance_reviews r JOIN users u ON r.emp_id = u.emp_id JOIN users rev ON r.reviewer_id = rev.emp_id ORDER BY r.created_at DESC"
        ).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'reviewer_id': r[3], 'reviewer': r[4], 'period': r[5], 'rating': float(r[6]) if r[6] else None, 'comments': r[7], 'status': r[8], 'submitted_at': r[9].isoformat() if r[9] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('emp_id') or not data.get('reviewer_id') or not data.get('review_period'):
        return jsonify({'error': 'emp_id, reviewer_id, review_period required'}), 400
    rid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO performance_reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 [rid, data['emp_id'], data['reviewer_id'], data['review_period'], None, None, 'Draft', datetime.now(), None])
    conn.close()
    return jsonify({'message': 'Review created', 'id': rid}), 201


@app.route('/api/v1/performance-reviews/<int:rid>/submit', methods=['PUT'])
@app.route('/api/performance-reviews/<int:rid>/submit', methods=['PUT'])
@login_required
def submit_review(rid):
    data = request.get_json(silent=True) or {}
    conn = get_db()
    conn.execute("UPDATE performance_reviews SET overall_rating = ?, comments = ?, status = 'Submitted', submitted_at = ? WHERE review_id = ?",
                 [data.get('rating'), data.get('comments'), datetime.now(), rid])
    conn.close()
    return jsonify({'message': 'Review submitted'}), 200


# ── 360 Feedback ──────────────────────────────────────────────────

@app.route('/api/v1/feedback-360', methods=['GET', 'POST'])
@app.route('/api/feedback-360', methods=['GET', 'POST'])
@login_required
def feedback_api():
    if request.method == 'GET':
        conn = get_db()
        if session.get('role') == 'Admin':
            rows = conn.execute("SELECT f.feedback_id, f.emp_id, u.name, f.reviewer_id, rev.name, f.category, f.rating, f.comment, f.submitted_at FROM feedback_360 f JOIN users u ON f.emp_id = u.emp_id JOIN users rev ON f.reviewer_id = rev.emp_id ORDER BY f.submitted_at DESC").fetchall()
        else:
            rows = conn.execute("SELECT f.feedback_id, f.emp_id, u.name, f.reviewer_id, rev.name, f.category, f.rating, f.comment, f.submitted_at FROM feedback_360 f JOIN users u ON f.emp_id = u.emp_id JOIN users rev ON f.reviewer_id = rev.emp_id WHERE f.emp_id = ? ORDER BY f.submitted_at DESC", [session['emp_id']]).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'reviewer_id': r[3], 'reviewer': r[4], 'category': r[5], 'rating': r[6], 'comment': r[7], 'submitted_at': r[8].isoformat() if r[8] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('emp_id') or not data.get('rating'):
        return jsonify({'error': 'emp_id and rating required'}), 400
    fid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO feedback_360 VALUES (?, ?, ?, ?, ?, ?, ?)",
                 [fid, data['emp_id'], session['emp_id'], data.get('category'), data['rating'], data.get('comment'), datetime.now()])
    conn.close()
    return jsonify({'message': 'Feedback submitted'}), 201


# ══════════════════════════════════════════════════════════════════════
#  PHASE 3 — EXPENSE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════

@app.route('/admin/expenses')
@hr_or_admin_required
def admin_expenses():
    return render_template('admin_expenses.html')


@app.route('/expenses')
@login_required
def expenses_page():
    return render_template('expenses.html')


@app.route('/api/v1/expense-categories')
@app.route('/api/expense-categories')
@login_required
def expense_categories():
    conn = get_db()
    rows = conn.execute("SELECT cat_id, name, description FROM expense_categories").fetchall()
    conn.close()
    return jsonify([{'id': r[0], 'name': r[1], 'description': r[2]} for r in rows]), 200


@app.route('/api/v1/expenses', methods=['GET', 'POST'])
@app.route('/api/expenses', methods=['GET', 'POST'])
@login_required
def expenses_api():
    if request.method == 'GET':
        conn = get_db()
        if session.get('role') == 'Admin':
            rows = conn.execute("SELECT c.claim_id, c.emp_id, u.name, c.cat_id, e.name, c.amount, c.description, c.status, c.created_at FROM expense_claims c JOIN users u ON c.emp_id = u.emp_id JOIN expense_categories e ON c.cat_id = e.cat_id ORDER BY c.created_at DESC").fetchall()
        else:
            rows = conn.execute("SELECT c.claim_id, c.emp_id, u.name, c.cat_id, e.name, c.amount, c.description, c.status, c.created_at FROM expense_claims c JOIN users u ON c.emp_id = u.emp_id JOIN expense_categories e ON c.cat_id = e.cat_id WHERE c.emp_id = ? ORDER BY c.created_at DESC", [session['emp_id']]).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'cat_id': r[3], 'category': r[4], 'amount': float(r[5]), 'description': r[6], 'status': r[7], 'created_at': r[8].isoformat() if r[8] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('cat_id') or not data.get('amount'):
        return jsonify({'error': 'cat_id and amount required'}), 400
    cid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO expense_claims VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 [cid, data.get('emp_id', session['emp_id']), data['cat_id'], float(data['amount']), data.get('description'), data.get('receipt_path'), 'Pending', None, datetime.now()])
    conn.close()
    return jsonify({'message': 'Expense claimed', 'id': cid}), 201


@app.route('/api/v1/expenses/<int:eid>/status', methods=['PUT'])
@app.route('/api/expenses/<int:eid>/status', methods=['PUT'])
@admin_required
def update_expense_status(eid):
    data = request.get_json(silent=True) or {}
    status = data.get('status')
    if status not in ('Pending', 'Approved', 'Rejected', 'Paid'):
        return jsonify({'error': 'Invalid status'}), 400
    conn = get_db()
    conn.execute("UPDATE expense_claims SET status = ?, approved_by = ? WHERE claim_id = ?", [status, session['emp_id'], eid])
    conn.close()
    return jsonify({'message': f'Expense {status.lower()}'}), 200


# ══════════════════════════════════════════════════════════════════════
#  PHASE 3 — HELP DESK / TICKETS
# ══════════════════════════════════════════════════════════════════════

@app.route('/admin/tickets')
@hr_or_admin_required
def admin_tickets():
    return render_template('admin_tickets.html')


@app.route('/tickets')
@login_required
def tickets_page():
    return render_template('tickets.html')


@app.route('/api/v1/tickets', methods=['GET', 'POST'])
@app.route('/api/tickets', methods=['GET', 'POST'])
@login_required
def tickets_api():
    if request.method == 'GET':
        conn = get_db()
        if session.get('role') == 'Admin':
            rows = conn.execute("SELECT t.ticket_id, t.emp_id, u.name, t.subject, t.category, t.priority, t.status, t.assigned_to, t.created_at, t.updated_at FROM tickets t JOIN users u ON t.emp_id = u.emp_id ORDER BY t.created_at DESC").fetchall()
        else:
            rows = conn.execute("SELECT t.ticket_id, t.emp_id, u.name, t.subject, t.category, t.priority, t.status, t.assigned_to, t.created_at, t.updated_at FROM tickets t JOIN users u ON t.emp_id = u.emp_id WHERE t.emp_id = ? ORDER BY t.created_at DESC", [session['emp_id']]).fetchall()
        conn.close()
        return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'subject': r[3], 'category': r[4], 'priority': r[5], 'status': r[6], 'assigned_to': r[7], 'created_at': r[8].isoformat() if r[8] else None, 'updated_at': r[9].isoformat() if r[9] else None} for r in rows]), 200
    data = request.get_json(silent=True) or {}
    if not data.get('subject'):
        return jsonify({'error': 'subject required'}), 400
    tid = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO tickets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 [tid, session['emp_id'], data['subject'], data.get('description'), data.get('category'), data.get('priority', 'Medium'),
                  'Open', None, datetime.now(), None, None])
    conn.close()
    return jsonify({'message': 'Ticket created', 'id': tid}), 201


@app.route('/api/v1/tickets/<int:tid>')
@app.route('/api/tickets/<int:tid>')
@login_required
def ticket_detail(tid):
    conn = get_db()
    row = conn.execute("SELECT t.ticket_id, t.emp_id, u.name, t.subject, t.description, t.category, t.priority, t.status, t.assigned_to, t.created_at, t.updated_at, t.resolved_at FROM tickets t JOIN users u ON t.emp_id = u.emp_id WHERE t.ticket_id = ?", [tid]).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Not found'}), 404
    if session.get('role') != 'Admin' and session['emp_id'] != row[1]:
        conn.close()
        return jsonify({'error': 'Forbidden'}), 403
    comments = conn.execute("SELECT c.comment_id, c.emp_id, u.name, c.comment, c.created_at FROM ticket_comments c JOIN users u ON c.emp_id = u.emp_id WHERE c.ticket_id = ? ORDER BY c.created_at", [tid]).fetchall()
    conn.close()
    return jsonify({
        'id': row[0], 'emp_id': row[1], 'employee': row[2], 'subject': row[3], 'description': row[4],
        'category': row[5], 'priority': row[6], 'status': row[7], 'assigned_to': row[8],
        'created_at': row[9].isoformat() if row[9] else None,
        'updated_at': row[10].isoformat() if row[10] else None,
        'resolved_at': row[11].isoformat() if row[11] else None,
        'comments': [{'id': c[0], 'emp_id': c[1], 'name': c[2], 'comment': c[3], 'created_at': c[4].isoformat() if c[4] else None} for c in comments]
    }), 200


@app.route('/api/v1/tickets/<int:tid>/comment', methods=['POST'])
@app.route('/api/tickets/<int:tid>/comment', methods=['POST'])
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
    conn.execute("INSERT INTO ticket_comments VALUES (?, ?, ?, ?, ?)", [cid, tid, session['emp_id'], data['comment'], datetime.now()])
    conn.execute("UPDATE tickets SET updated_at = ? WHERE ticket_id = ?", [datetime.now(), tid])
    conn.close()
    return jsonify({'message': 'Comment added', 'id': cid}), 201


@app.route('/api/v1/tickets/<int:tid>/status', methods=['PUT'])
@app.route('/api/tickets/<int:tid>/status', methods=['PUT'])
@login_required
def update_ticket_status(tid):
    data = request.get_json(silent=True) or {}
    status = data.get('status')
    if status not in ('Open', 'In Progress', 'Resolved', 'Closed'):
        return jsonify({'error': 'Invalid status'}), 400
    conn = get_db()
    now = datetime.now()
    resolved_at = now if status == 'Resolved' else None
    conn.execute("UPDATE tickets SET status = ?, updated_at = ?, resolved_at = ? WHERE ticket_id = ?", [status, now, resolved_at, tid])
    conn.close()
    return jsonify({'message': f'Status set to {status}'}), 200


@app.route('/api/v1/tickets/<int:tid>/assign', methods=['PUT'])
@app.route('/api/tickets/<int:tid>/assign', methods=['PUT'])
@admin_required
def assign_ticket(tid):
    data = request.get_json(silent=True) or {}
    conn = get_db()
    conn.execute("UPDATE tickets SET assigned_to = ?, updated_at = ? WHERE ticket_id = ?", [data.get('assigned_to'), datetime.now(), tid])
    conn.close()
    return jsonify({'message': 'Ticket assigned'}), 200


# ══════════════════════════════════════════════════════════════════════
#  PHASE 3 — DOCUMENT MANAGEMENT
# ══════════════════════════════════════════════════════════════════════

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB


@app.route('/admin/documents')
@hr_or_admin_required
def admin_documents():
    return render_template('admin_documents.html')


@app.route('/documents')
@login_required
def documents_page():
    return render_template('documents.html')


@app.route('/api/v1/documents', methods=['GET'])
@app.route('/api/documents', methods=['GET'])
@login_required
def documents_list():
    conn = get_db()
    if session.get('role') == 'Admin':
        rows = conn.execute("SELECT d.doc_id, d.emp_id, u.name, d.name, d.category, d.file_path, d.file_size, d.uploaded_at FROM documents d JOIN users u ON d.emp_id = u.emp_id ORDER BY d.uploaded_at DESC").fetchall()
    else:
        rows = conn.execute("SELECT d.doc_id, d.emp_id, u.name, d.name, d.category, d.file_path, d.file_size, d.uploaded_at FROM documents d JOIN users u ON d.emp_id = u.emp_id WHERE d.emp_id = ? ORDER BY d.uploaded_at DESC", [session['emp_id']]).fetchall()
    conn.close()
    return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'name': r[3], 'category': r[4], 'file_path': r[5], 'file_size': r[6], 'uploaded_at': r[7].isoformat() if r[7] else None} for r in rows]), 200


@app.route('/api/v1/upload', methods=['POST'])
@app.route('/api/upload', methods=['POST'])
@login_required
def upload_document():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    f = request.files['file']
    if f.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    emp_id = request.form.get('emp_id', session['emp_id'])
    category = request.form.get('category', 'Other')
    filename = f"{int(datetime.now().timestamp())}_{f.filename}"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    f.save(filepath)
    fsize = os.path.getsize(filepath)
    did = gen_id()
    conn = get_db()
    conn.execute("INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?)",
                 [did, emp_id, f.filename, category, filename, fsize, datetime.now()])
    conn.close()
    return jsonify({'message': 'File uploaded', 'id': did, 'path': filename}), 201


@app.route('/api/v1/documents/<int:did>/download')
@app.route('/api/documents/<int:did>/download')
@login_required
def download_document(did):
    conn = get_db()
    row = conn.execute("SELECT file_path, name FROM documents WHERE doc_id = ?", [did]).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    filepath = os.path.join(UPLOAD_FOLDER, row[0])
    if not os.path.exists(filepath):
        return jsonify({'error': 'File not found on disk'}), 404
    return send_file(filepath, as_attachment=True, download_name=row[1])


@app.route('/api/v1/documents/<int:did>', methods=['DELETE'])
@app.route('/api/documents/<int:did>', methods=['DELETE'])
@login_required
def delete_document(did):
    conn = get_db()
    row = conn.execute("SELECT file_path FROM documents WHERE doc_id = ?", [did]).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Not found'}), 404
    conn.execute("DELETE FROM documents WHERE doc_id = ?", [did])
    conn.close()
    filepath = os.path.join(UPLOAD_FOLDER, row[0])
    if os.path.exists(filepath):
        os.remove(filepath)
    return jsonify({'message': 'Document deleted'}), 200


# ══════════════════════════════════════════════════════════════════════
#  PHASE 3 — EMAIL NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

SMTP_HOST = os.getenv('SMTP_HOST', '')
SMTP_PORT = int(os.getenv('SMTP_PORT', '587'))
SMTP_USER = os.getenv('SMTP_USER', '')
SMTP_PASS = os.getenv('SMTP_PASS', '')
EMAIL_FROM = os.getenv('EMAIL_FROM', 'noreply@hrms.com')


def send_email(to, subject, body):
    if not SMTP_HOST:
        logger.info("Email disabled (SMTP_HOST not set) — would send to %s: %s", to, subject)
        return True
    try:
        msg = MIMEMultipart()
        msg['From'] = EMAIL_FROM
        msg['To'] = to
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'html'))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        logger.info("Email sent to %s: %s", to, subject)
        return True
    except Exception as e:
        logger.warning("Email failed to %s: %s", to, e)
        return False


@app.route('/api/v1/send-notification-email', methods=['POST'])
@app.route('/api/send-notification-email', methods=['POST'])
@admin_required
def send_notification_email():
    data = request.get_json(silent=True) or {}
    to = data.get('to')
    subject = data.get('subject', 'HRMS Notification')
    body = data.get('body', '')
    if not to:
        return jsonify({'error': 'recipient required'}), 400
    ok = send_email(to, subject, body)
    if ok:
        return jsonify({'message': 'Email sent'}), 200
    return jsonify({'warning': 'Email sending failed (SMTP may not be configured)'}), 200


# ══════════════════════════════════════════════════════════════════════
#  PHASE 3 — ADVANCED ANALYTICS
# ══════════════════════════════════════════════════════════════════════

@app.route('/admin/analytics')
@hr_or_admin_required
def admin_analytics():
    return render_template('analytics.html')


@app.route('/api/v1/analytics/headcount')
@app.route('/api/analytics/headcount')
@admin_required
def analytics_headcount():
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'Employee'").fetchone()[0]
    dept = conn.execute("SELECT department, COUNT(*) FROM users WHERE role = 'Employee' AND department IS NOT NULL GROUP BY department ORDER BY COUNT(*) DESC").fetchall()
    conn.close()
    return jsonify({'total': total, 'by_department': [{'dept': r[0], 'count': r[1]} for r in dept]}), 200


@app.route('/api/v1/analytics/leave-trends')
@app.route('/api/analytics/leave-trends')
@admin_required
def analytics_leave_trends():
    months = request.args.get('months', 6, type=int)
    conn = get_db()
    rows = conn.execute(f"""
        SELECT strftime('%Y-%m', start_date) as month, leave_type, COUNT(*) as cnt
        FROM leave_requests WHERE status = 'Approved'
        AND start_date >= date('now', '-{months} months')
        GROUP BY month, leave_type ORDER BY month
    """).fetchall()
    conn.close()
    return jsonify([{'month': r[0], 'type': r[1], 'count': r[2]} for r in rows]), 200


@app.route('/api/v1/analytics/attrition-risk')
@app.route('/api/analytics/attrition-risk')
@admin_required
def analytics_attrition():
    conn = get_db()
    rows = conn.execute("""
        SELECT u.emp_id, u.name, u.department, u.designation,
            COALESCE(lr.leave_count, 0) as leave_count,
            COALESCE(reg.reg_count, 0) as reg_count,
            COALESCE(eb.early_break, 0) as early_break
        FROM users u
        LEFT JOIN (SELECT emp_id, COUNT(*) as leave_count FROM leave_requests WHERE status = 'Approved' AND start_date >= date('now', '-3 months') GROUP BY emp_id) lr ON u.emp_id = lr.emp_id
        LEFT JOIN (SELECT emp_id, COUNT(*) as reg_count FROM regularization_requests WHERE status = 'Pending' GROUP BY emp_id) reg ON u.emp_id = reg.emp_id
        LEFT JOIN (SELECT emp_id, COUNT(*) as early_break FROM breaks WHERE break_date >= date('now', '-1 months') AND duration_minutes < 5 GROUP BY emp_id) eb ON u.emp_id = eb.emp_id
        WHERE u.role = 'Employee' ORDER BY (COALESCE(lr.leave_count,0) * 0.5 + COALESCE(reg.reg_count,0) * 2) DESC LIMIT 20
    """).fetchall()
    conn.close()
    return jsonify([{'emp_id': r[0], 'name': r[1], 'department': r[2], 'designation': r[3], 'leave_count': r[4], 'reg_count': r[5], 'early_break': r[6], 'risk_score': round(r[4] * 0.5 + r[5] * 2, 1)} for r in rows]), 200


@app.route('/api/v1/analytics/expense-summary')
@app.route('/api/analytics/expense-summary')
@admin_required
def analytics_expense_summary():
    conn = get_db()
    total = conn.execute("SELECT COALESCE(SUM(amount),0) FROM expense_claims WHERE status IN ('Approved','Paid')").fetchone()[0]
    by_cat = conn.execute("SELECT e.name, COALESCE(SUM(c.amount),0) FROM expense_claims c JOIN expense_categories e ON c.cat_id = e.cat_id WHERE c.status IN ('Approved','Paid') GROUP BY e.name ORDER BY SUM(c.amount) DESC").fetchall()
    pending = conn.execute("SELECT COUNT(*) FROM expense_claims WHERE status = 'Pending'").fetchone()[0]
    conn.close()
    return jsonify({'total': float(total), 'by_category': [{'cat': r[0], 'amount': float(r[1])} for r in by_cat], 'pending_claims': pending}), 200


@app.route('/api/v1/analytics/performance-summary')
@app.route('/api/analytics/performance-summary')
@hr_or_admin_required
def analytics_performance():
    conn = get_db()
    avg_rating = conn.execute("SELECT COALESCE(AVG(overall_rating),0) FROM performance_reviews WHERE status = 'Submitted'").fetchone()[0]
    by_dept = conn.execute("""
        SELECT u.department, COALESCE(AVG(r.overall_rating),0)
        FROM performance_reviews r JOIN users u ON r.emp_id = u.emp_id
        WHERE r.status = 'Submitted' AND u.department IS NOT NULL
        GROUP BY u.department ORDER BY AVG(r.overall_rating) DESC
    """).fetchall()
    conn.close()
    return jsonify({'avg_rating': round(float(avg_rating), 2), 'by_department': [{'dept': r[0], 'avg': round(float(r[1]), 2)} for r in by_dept]}), 200


# ══════════════════════════════════════════════════════════════════════
#  PHASE 3 — FULL PAYROLL (Payslip PDF, Bank File, TDS)
# ══════════════════════════════════════════════════════════════════════

from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from io import BytesIO


def generate_payslip_pdf(run_id, emp_id):
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
        ['Gross Salary', f"₹{float(row[7]):,.2f}"],
        ['PF', f"₹{float(row[10]):,.2f}"],
        ['ESI', f"₹{float(row[11]):,.2f}"],
        ['Professional Tax', f"₹{float(row[12]):,.2f}"],
        ['Other Deductions', f"₹{float(row[8]) - float(row[10]) - float(row[11]) - float(row[12]):,.2f}"],
        ['Total Deductions', f"₹{float(row[8]):,.2f}"],
        ['NET SALARY', f"₹{float(row[9]):,.2f}"],
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


@app.route('/api/v1/payroll-runs/<int:rid>/payslip-pdf/<emp_id>')
@app.route('/api/payroll-runs/<int:rid>/payslip-pdf/<emp_id>')
@login_required
def payslip_pdf(rid, emp_id):
    if session.get('role') != 'Admin' and session['emp_id'] != emp_id:
        return jsonify({'error': 'Forbidden'}), 403
    pdf = generate_payslip_pdf(rid, emp_id)
    if not pdf:
        return jsonify({'error': 'Not found'}), 404
    return send_file(pdf, mimetype='application/pdf', as_attachment=True, download_name=f'payslip_{emp_id}_{rid}.pdf')


@app.route('/api/v1/payroll-runs/<int:rid>/bank-file')
@app.route('/api/payroll-runs/<int:rid>/bank-file')
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
    import csv
    buf = BytesIO()
    writer = csv.writer(buf)
    writer.writerow(['Employee ID', 'Name', 'Net Salary', 'Account Number', 'IFSC'])
    for r in rows:
        writer.writerow([r[0], r[1], f"{float(r[2]):.2f}", '', ''])
    buf.seek(0)
    return send_file(buf, mimetype='text/csv', as_attachment=True, download_name=f'payroll_{rid}.csv')


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


@app.route('/api/v1/payroll-runs/<int:rid>/tds-report')
@app.route('/api/payroll-runs/<int:rid>/tds-report')
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
    annual_est = float(run[1])
    result = []
    for r in rows:
        monthly_gross = float(r[2])
        annual_gross = monthly_gross * 12
        tds = round(calc_tds(annual_gross) / 12, 2)
        result.append({'emp_id': r[0], 'name': r[1], 'monthly_gross': monthly_gross, 'annual_gross': annual_gross, 'tds': tds})
    return jsonify(result), 200


# ══════════════════════════════════════════════════════════════════════
#  LEAVE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════

@app.route('/leaves')
@login_required
def leaves_page():
    return render_template('leaves.html')


@app.route('/admin/leaves')
@hr_or_admin_required
def admin_leaves_page():
    return render_template('admin_leaves.html')


@app.route('/api/v1/leaves', methods=['GET', 'POST'])
@app.route('/api/leaves', methods=['GET', 'POST'])
@login_required
def leaves_api():
    """Create or list leave requests
    ---
    get:
      tags: [Leaves]
      parameters:
        - in: query
          name: status
          type: string
      responses:
        200:
          description: Leave list
    post:
      tags: [Leaves]
      parameters:
        - in: body
          name: body
          schema:
            type: object
            properties:
              leave_type: {type: string}
              start_date: {type: string, format: date}
              end_date: {type: string, format: date}
              reason: {type: string}
      responses:
        201:
          description: Leave created
    """
    emp_id = session['emp_id']
    conn = get_db()

    if request.method == 'GET':
        status_filter = request.args.get('status')
        if session.get('role') == 'Admin':
            query = "SELECT leave_id, emp_id, leave_type, start_date, end_date, reason, status, approved_by, created_at FROM leave_requests"
            params = []
            if status_filter:
                query += " WHERE status = ?"
                params.append(status_filter)
            query += " ORDER BY created_at DESC"
        else:
            query = "SELECT leave_id, emp_id, leave_type, start_date, end_date, reason, status, approved_by, created_at FROM leave_requests WHERE emp_id = ?"
            params = [emp_id]
            if status_filter:
                query += " AND status = ?"
                params.append(status_filter)
            query += " ORDER BY created_at DESC"
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return jsonify([{
            'leave_id': r[0], 'emp_id': r[1], 'leave_type': r[2],
            'start_date': r[3].isoformat(), 'end_date': r[4].isoformat(),
            'reason': r[5], 'status': r[6], 'approved_by': r[7],
            'created_at': r[8].isoformat() if r[8] else None
        } for r in rows]), 200

    data = request.get_json(silent=True) or {}
    lt = data.get('leave_type')
    sd = parse_date(data.get('start_date'))
    ed = parse_date(data.get('end_date'), sd)
    if not lt or not sd or not ed:
        conn.close()
        return jsonify({'error': 'leave_type, start_date, end_date required'}), 400
    if ed < sd:
        sd, ed = ed, sd

    balance = conn.execute(
        "SELECT balance_id, total_days, used_days FROM leave_balance WHERE emp_id = ? AND leave_type = ? AND year = ?",
        [emp_id, lt, sd.year]
    ).fetchone()
    if balance:
        requested = (ed - sd).days + 1
        remaining = balance[1] - balance[2]
        if requested > remaining:
            conn.close()
            return jsonify({'error': f'Insufficient balance. Remaining: {remaining} days'}), 400

    leave_id = gen_id()
    conn.execute(
        "INSERT INTO leave_requests (leave_id, emp_id, leave_type, start_date, end_date, reason, status) VALUES (?, ?, ?, ?, ?, ?, 'Pending')",
        [leave_id, emp_id, lt, sd, ed, data.get('reason', '')]
    )
    conn.close()
    audit_log(emp_id, 'LEAVE_APPLY', f'{lt} leave {sd} to {ed}')
    add_notification(session['emp_id'], 'LEAVE_APPLIED', f'Your {lt} leave ({sd} to {ed}) has been submitted.', '/leaves')
    return jsonify({'message': 'Leave application submitted', 'leave_id': leave_id}), 201


@app.route('/api/v1/leaves/<int:leave_id>/approve', methods=['POST'])
@app.route('/api/leaves/<int:leave_id>/approve', methods=['POST'])
@admin_required
def approve_leave(leave_id):
    """Approve a leave request
    ---
    post:
      tags: [Leaves]
      parameters:
        - in: path
          name: leave_id
          type: integer
      responses:
        200:
          description: Approved
    """
    conn = get_db()
    row = conn.execute(
        "SELECT emp_id, leave_type, start_date, end_date, status FROM leave_requests WHERE leave_id = ?",
        [leave_id]
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Leave not found'}), 404
    if row[4] != 'Pending':
        conn.close()
        return jsonify({'error': 'Leave is not pending'}), 400

    days = (row[3] - row[2]).days + 1
    conn.execute(
        "UPDATE leave_requests SET status = 'Approved', approved_by = ?, updated_at = ? WHERE leave_id = ?",
        [session['emp_id'], datetime.now(), leave_id]
    )
    conn.execute(
        "UPDATE leave_balance SET used_days = used_days + ? WHERE emp_id = ? AND leave_type = ? AND year = ?",
        [days, row[0], row[1], row[2].year]
    )
    conn.close()
    audit_log(session['emp_id'], 'LEAVE_APPROVE', f'Leave {leave_id} approved')
    add_notification(row[0], 'LEAVE_APPROVED', f'Your {row[1]} leave ({row[2]} to {row[3]}) has been approved.', '/leaves')
    return jsonify({'message': 'Leave approved'}), 200


@app.route('/api/v1/leaves/<int:leave_id>/reject', methods=['POST'])
@app.route('/api/leaves/<int:leave_id>/reject', methods=['POST'])
@admin_required
def reject_leave(leave_id):
    """Reject a leave request"""
    conn = get_db()
    row = conn.execute("SELECT emp_id, leave_type, start_date, end_date, status FROM leave_requests WHERE leave_id = ?", [leave_id]).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Not found'}), 404
    if row[4] != 'Pending':
        conn.close()
        return jsonify({'error': 'Leave is not pending'}), 400
    conn.execute(
        "UPDATE leave_requests SET status = 'Rejected', approved_by = ?, updated_at = ? WHERE leave_id = ?",
        [session['emp_id'], datetime.now(), leave_id]
    )
    conn.close()
    audit_log(session['emp_id'], 'LEAVE_REJECT', f'Leave {leave_id} rejected')
    add_notification(row[0], 'LEAVE_REJECTED', f'Your {row[1]} leave ({row[2]} to {row[3]}) has been rejected.', '/leaves')
    return jsonify({'message': 'Leave rejected'}), 200


@app.route('/api/v1/leave-balance')
@app.route('/api/leave-balance')
@login_required
def leave_balance_api():
    """Get leave balance for current user"""
    emp_id = session['emp_id']
    year = datetime.now().year
    conn = get_db()
    rows = conn.execute(
        "SELECT leave_type, total_days, used_days FROM leave_balance WHERE emp_id = ? AND year = ?",
        [emp_id, year]
    ).fetchall()
    conn.close()
    return jsonify([{
        'leave_type': r[0], 'total_days': r[1],
        'used_days': r[2], 'remaining': r[1] - r[2]
    } for r in rows]), 200


# ══════════════════════════════════════════════════════════════════════
#  AUDIT LOG
# ══════════════════════════════════════════════════════════════════════

@app.route('/admin/audit')
@hr_or_admin_required
def audit_page():
    return render_template('admin_audit.html')


@app.route('/api/v1/audit-log')
@app.route('/api/audit-log')
@admin_required
def get_audit_log():
    """View audit log"""
    limit = request.args.get('limit', 200, type=int)
    offset = request.args.get('offset', 0, type=int)
    conn = get_db()
    rows = conn.execute(
        "SELECT log_id, emp_id, action, details, ip_address, created_at FROM audit_log ORDER BY created_at DESC LIMIT ? OFFSET ?",
        [limit, offset]
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    conn.close()
    return jsonify({
        'total': total,
        'data': [{
            'log_id': r[0], 'emp_id': r[1], 'action': r[2],
            'details': r[3], 'ip_address': r[4],
            'created_at': r[5].isoformat() if r[5] else None
        } for r in rows]
    }), 200


# ══════════════════════════════════════════════════════════════════════
#  REPORT EXPORT (CSV / Excel)
# ══════════════════════════════════════════════════════════════════════

@app.route('/api/v1/reports/export')
@app.route('/api/reports/export')
@admin_required
def export_report():
    """Export report as CSV or Excel
    ---
    get:
      tags: [Reports]
      parameters:
        - in: query
          name: start_date
          type: string
        - in: query
          name: end_date
          type: string
        - in: query
          name: format
          type: string
          enum: [csv, xlsx]
      responses:
        200:
          description: File download
    """
    start_date = parse_date(request.args.get('start_date'), datetime.now().date())
    end_date = parse_date(request.args.get('end_date'), start_date)
    fmt = request.args.get('format', 'csv')

    conn = get_db()
    rows = conn.execute("""
        SELECT u.emp_id, u.name, u.department,
               COALESCE((SELECT SUM(total_hours) FROM user_sessions us WHERE us.emp_id = u.emp_id AND us.session_date BETWEEN ? AND ?), 0) AS total_hours,
               COALESCE((SELECT SUM(duration_minutes) FROM breaks b WHERE b.emp_id = u.emp_id AND b.break_date BETWEEN ? AND ? AND b.status = 'Completed'), 0) AS break_minutes,
               COALESCE((SELECT COUNT(*) FROM breaks b WHERE b.emp_id = u.emp_id AND b.break_date BETWEEN ? AND ? AND b.status = 'Completed'), 0) AS break_count
        FROM users u WHERE u.role = 'Employee' ORDER BY u.name
    """, [start_date, end_date, start_date, end_date, start_date, end_date]).fetchall()
    conn.close()

    df = pd.DataFrame(rows, columns=['Employee ID', 'Name', 'Department', 'Total Hours', 'Break Minutes', 'Break Count'])
    df['Total Hours'] = df['Total Hours'].astype(float)
    df['Break Minutes'] = df['Break Minutes'].astype(float)
    df['Productive Hours'] = (df['Total Hours'] - df['Break Minutes'] / 60).round(2)
    df['Efficiency %'] = ((df['Productive Hours'] / df['Total Hours'].replace(0, 1)) * 100).round(1)
    df['Period'] = f'{start_date} to {end_date}'

    if fmt == 'xlsx':
        buf = BytesIO()
        df.to_csv(buf, index=False)
        buf.seek(0)
        return send_file(
            buf, mimetype='text/csv',
            as_attachment=True,
            download_name=f'hrms_report_{start_date}_{end_date}.csv'
        )

    csv_buf = BytesIO()
    df.to_csv(csv_buf, index=False)
    csv_buf.seek(0)
    return send_file(
        csv_buf, mimetype='text/csv',
        as_attachment=True,
        download_name=f'hrms_report_{start_date}_{end_date}.csv'
    )


# ══════════════════════════════════════════════════════════════════════
#  USER BREAK ROUTES (existing, kept for backward compat)
# ══════════════════════════════════════════════════════════════════════

@app.route('/api/start-break', methods=['POST'])
@login_required
def start_break():
    data = request.get_json(silent=True) or {}
    break_type = data.get('break_type')
    emp_id = session['emp_id']
    if not break_type:
        return jsonify({'error': 'Break type required'}), 400
    conn = get_db()
    user = conn.execute("SELECT allow_breaks FROM users WHERE emp_id = ?", [emp_id]).fetchone()
    if user and not user[0]:
        conn.close()
        return jsonify({'error': 'Breaks not allowed'}), 403
    if not conn.execute("SELECT 1 FROM break_types WHERE break_type = ?", [break_type]).fetchone():
        conn.close()
        return jsonify({'error': 'Invalid break type'}), 400
    if conn.execute("SELECT 1 FROM breaks WHERE emp_id = ? AND status = 'Active'", [emp_id]).fetchone():
        conn.close()
        return jsonify({'error': 'Already on a break'}), 409
    break_id = gen_id()
    conn.execute(
        "INSERT INTO breaks (break_id, emp_id, break_type, start_time, break_date, status) VALUES (?, ?, ?, ?, ?, 'Active')",
        [break_id, emp_id, break_type, datetime.now(), datetime.now().date()]
    )
    conn.close()
    return jsonify({'message': 'Break started', 'break_id': break_id, 'break_type': break_type}), 201


@app.route('/api/end-break/<int:break_id>', methods=['POST'])
@login_required
def end_break(break_id):
    emp_id = session['emp_id']
    conn = get_db()
    info = conn.execute(
        "SELECT start_time, break_type FROM breaks WHERE break_id = ? AND emp_id = ?",
        [break_id, emp_id]
    ).fetchone()
    if not info:
        conn.close()
        return jsonify({'error': 'Break not found'}), 404
    end_time = datetime.now()
    duration = int((end_time - info[0]).total_seconds() / 60)
    conn.execute(
        "UPDATE breaks SET end_time = ?, duration_minutes = ?, status = 'Completed' WHERE break_id = ?",
        [end_time, duration, break_id]
    )
    conn.close()
    return jsonify({'message': 'Break ended', 'duration_minutes': duration}), 200


@app.route('/api/user-breaks')
@login_required
def get_user_breaks():
    emp_id = session['emp_id']
    conn = get_db()
    breaks = conn.execute(
        "SELECT break_id, break_type, start_time, end_time, duration_minutes, status FROM breaks WHERE emp_id = ? AND break_date = ? ORDER BY start_time DESC",
        [emp_id, datetime.now().date()]
    ).fetchall()
    conn.close()
    return jsonify([{
        'break_id': b[0], 'break_type': b[1],
        'start_time': b[2].strftime('%H:%M:%S') if b[2] else 'N/A',
        'end_time': b[3].strftime('%H:%M:%S') if b[3] else 'Ongoing',
        'duration_minutes': b[4] or 0, 'status': b[5]
    } for b in breaks]), 200


@app.route('/api/break-types')
@login_required
def get_break_types():
    conn = get_db()
    types = conn.execute("SELECT break_type, daily_limit_minutes, description FROM break_types").fetchall()
    conn.close()
    return jsonify([{
        'break_type': t[0], 'daily_limit_minutes': t[1], 'description': t[2]
    } for t in types]), 200


@app.route('/api/login-hours')
@login_required
def get_login_hours():
    emp_id = session['emp_id']
    conn = get_db()
    sessions = conn.execute(
        "SELECT login_time, logout_time, total_hours, session_date FROM user_sessions WHERE emp_id = ? AND session_date = ? ORDER BY login_time DESC",
        [emp_id, datetime.now().date()]
    ).fetchall()
    conn.close()
    return jsonify([{
        'login_time': s[0].strftime('%H:%M:%S') if s[0] else 'N/A',
        'logout_time': s[1].strftime('%H:%M:%S') if s[1] else 'Active',
        'total_hours': float(s[2]) if s[2] else 0,
        'session_date': s[3].isoformat() if s[3] else None
    } for s in sessions]), 200


# ══════════════════════════════════════════════════════════════════════
#  ADMIN MONITORING ROUTES
# ══════════════════════════════════════════════════════════════════════

@app.route('/api/live-monitoring')
@admin_required
def live_monitoring():
    conn = get_db()
    rows = conn.execute("""
        SELECT u.emp_id, u.name, u.department, b.break_type, b.start_time, b.status
        FROM breaks b JOIN users u ON b.emp_id = u.emp_id
        WHERE b.status = 'Active' AND b.break_date = ?
        ORDER BY b.start_time DESC
    """, [datetime.now().date()]).fetchall()
    conn.close()
    return jsonify([{
        'emp_id': r[0], 'employee_name': r[1], 'department': r[2],
        'break_type': r[3], 'start_time': r[4].strftime('%H:%M:%S'), 'status': r[5]
    } for r in rows]), 200


@app.route('/api/break-summary')
@admin_required
def get_break_summary():
    conn = get_db()
    rows = conn.execute("""
        SELECT u.emp_id, u.name, u.department,
               COUNT(CASE WHEN b.status = 'Active' THEN 1 END),
               COUNT(CASE WHEN b.status = 'Completed' THEN 1 END),
               SUM(CASE WHEN b.status = 'Completed' THEN b.duration_minutes ELSE 0 END)
        FROM users u LEFT JOIN breaks b ON u.emp_id = b.emp_id AND b.break_date = ?
        WHERE u.role = 'Employee' GROUP BY u.emp_id, u.name, u.department ORDER BY u.name
    """, [datetime.now().date()]).fetchall()
    conn.close()
    return jsonify([{
        'emp_id': r[0], 'employee_name': r[1], 'department': r[2],
        'active_breaks': int(r[3] or 0), 'completed_breaks': int(r[4] or 0),
        'total_break_minutes': int(r[5] or 0)
    } for r in rows]), 200


@app.route('/api/disposed-breaks')
@admin_required
def get_disposed_breaks():
    one_hour_ago = datetime.now() - timedelta(hours=1)
    conn = get_db()
    rows = conn.execute("""
        SELECT u.emp_id, u.name, u.department, b.break_type, b.start_time, b.end_time, b.duration_minutes, b.status
        FROM breaks b JOIN users u ON b.emp_id = u.emp_id
        WHERE b.status = 'Completed' AND b.end_time >= ? AND b.break_date = ?
        ORDER BY b.end_time DESC
    """, [one_hour_ago, datetime.now().date()]).fetchall()
    conn.close()
    return jsonify([{
        'emp_id': r[0], 'employee_name': r[1], 'department': r[2],
        'break_type': r[3], 'start_time': r[4].strftime('%H:%M:%S'),
        'end_time': r[5].strftime('%H:%M:%S'), 'duration_minutes': r[6] or 0, 'status': r[7]
    } for r in rows]), 200


@app.route('/api/dashboard-stats')
@admin_required
def get_dashboard_stats():
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'Employee'").fetchone()[0]
    online = conn.execute(
        "SELECT COUNT(DISTINCT emp_id) FROM user_sessions WHERE logout_time IS NULL AND session_date = ?",
        [datetime.now().date()]
    ).fetchone()[0]
    on_break = conn.execute(
        "SELECT COUNT(DISTINCT emp_id) FROM breaks WHERE status = 'Active' AND break_date = ?",
        [datetime.now().date()]
    ).fetchone()[0]
    blocked = conn.execute("SELECT COUNT(*) FROM users WHERE status = 'Blocked'").fetchone()[0]
    pending_leaves = conn.execute("SELECT COUNT(*) FROM leave_requests WHERE status = 'Pending'").fetchone()[0]
    conn.close()
    return jsonify({
        'total_employees': total, 'online': online,
        'on_break': on_break, 'blocked_users': blocked,
        'pending_leaves': pending_leaves
    }), 200


# ══════════════════════════════════════════════════════════════════════
#  USER MANAGEMENT
# ══════════════════════════════════════════════════════════════════════

@app.route('/admin/users')
@admin_required
def admin_users():
    return render_template('admin_users.html')


@app.route('/api/users', methods=['GET'])
@admin_required
def get_users():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    offset = (page - 1) * per_page
    active_only = request.args.get('active', '').lower() in ('1', 'true', 'yes', 'on')
    conn = get_db()
    where_clause = " WHERE status = 'Active'" if active_only else ""
    total = conn.execute(f"SELECT COUNT(*) FROM users{where_clause}").fetchone()[0]
    rows = conn.execute(
        f"SELECT emp_id, name, email, role, status, department, first_login, allow_login, allow_breaks FROM users{where_clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        [per_page, offset]
    ).fetchall()
    conn.close()
    return jsonify({
        'total': total, 'page': page, 'per_page': per_page,
        'data': [{
            'emp_id': r[0], 'name': r[1], 'email': r[2], 'role': r[3],
            'status': r[4], 'department': r[5],
            'first_login': r[6].strftime('%I:%M %p') if r[6] else 'N/A',
            'allow_login': int(r[7]) if r[7] else 1,
            'allow_breaks': int(r[8]) if r[8] else 1
        } for r in rows]
    }), 200


@app.route('/api/users', methods=['POST'])
@admin_required
def add_user():
    data = request.get_json(silent=True) or {}
    if not data.get('emp_id') or not data.get('name') or not data.get('email'):
        return jsonify({'error': 'Missing required fields'}), 400
    if '@' not in data.get('email', ''):
        return jsonify({'error': 'Invalid email'}), 400
    conn = get_db()
    if conn.execute("SELECT 1 FROM users WHERE emp_id = ?", [data['emp_id']]).fetchone():
        conn.close()
        return jsonify({'error': 'Employee ID already exists'}), 409
    pwd = data.get('password', 'pass123')
    conn.execute(
        "INSERT INTO users (emp_id, name, email, password, role, department, status, first_login, created_at, allow_login, allow_breaks) VALUES (?, ?, ?, ?, ?, ?, 'Active', ?, ?, ?, ?)",
        [data['emp_id'], data['name'], data['email'], hash_password(pwd),
         data.get('role', 'Employee'), data.get('department', ''),
         datetime.now(), datetime.now(),
         int(data.get('allow_login', 1)), int(data.get('allow_breaks', 1))]
    )
    conn.close()
    audit_log(session['emp_id'], 'USER_CREATE', f'Created user {data["emp_id"]}')
    return jsonify({'message': 'User added'}), 201


@app.route('/api/users/<emp_id>', methods=['GET'])
@admin_required
def get_user_route(emp_id):
    u = get_user(emp_id)
    if not u:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({
        'emp_id': u[0], 'name': u[1], 'email': u[2], 'role': u[3],
        'status': u[4], 'department': u[5],
        'allow_login': int(u[6]) if u[6] else 1,
        'allow_breaks': int(u[7]) if u[7] else 1
    }), 200


@app.route('/api/users/<emp_id>', methods=['PUT'])
@admin_required
def update_user(emp_id):
    data = request.get_json(silent=True) or {}
    conn = get_db()
    conn.execute(
        "UPDATE users SET name = ?, email = ?, role = ?, department = ?, status = ?, allow_login = ?, allow_breaks = ? WHERE emp_id = ?",
        [data.get('name'), data.get('email'), data.get('role'), data.get('department', ''),
         data.get('status', 'Active'), int(data.get('allow_login', 1)),
         int(data.get('allow_breaks', 1)), emp_id]
    )
    conn.close()
    audit_log(session['emp_id'], 'USER_UPDATE', f'Updated user {emp_id}')
    return jsonify({'message': 'User updated'}), 200


@app.route('/api/users/<emp_id>/block', methods=['POST'])
@admin_required
def block_user(emp_id):
    conn = get_db()
    conn.execute("UPDATE users SET status = 'Blocked' WHERE emp_id = ?", [emp_id])
    conn.close()
    audit_log(session['emp_id'], 'USER_BLOCK', f'Blocked user {emp_id}')
    return jsonify({'message': 'User blocked'}), 200


@app.route('/api/users/<emp_id>/unblock', methods=['POST'])
@admin_required
def unblock_user(emp_id):
    conn = get_db()
    conn.execute("UPDATE users SET status = 'Active' WHERE emp_id = ?", [emp_id])
    conn.close()
    audit_log(session['emp_id'], 'USER_UNBLOCK', f'Unblocked user {emp_id}')
    return jsonify({'message': 'User unblocked'}), 200


# ══════════════════════════════════════════════════════════════════════
#  REPORTS
# ══════════════════════════════════════════════════════════════════════

@app.route('/admin/reports')
@hr_or_admin_required
def admin_reports():
    return render_template('admin_reports.html')


@app.route('/admin/org-chart')
@admin_required
def admin_org_chart():
    return render_template('org_chart.html')


@app.route('/admin/holidays')
@admin_required
def admin_holidays():
    return render_template('holidays.html')


@app.route('/regularization')
@login_required
def regularization_page():
    return render_template('regularization.html')


@app.route('/admin/import-users')
@hr_or_admin_required
def import_users_page():
    return render_template('import_users.html')


@app.route('/api/reports')
@admin_required
def get_reports():
    start_date = parse_date(request.args.get('start_date'), datetime.now().date())
    end_date = parse_date(request.args.get('end_date'), start_date)
    if end_date and end_date < start_date:
        start_date, end_date = end_date, start_date

    conn = get_db()
    summary = conn.execute("""
        SELECT u.emp_id, u.name, u.department,
               (SELECT MIN(login_time) FROM user_sessions us WHERE us.emp_id = u.emp_id AND us.session_date BETWEEN ? AND ?),
               (SELECT MAX(logout_time) FROM user_sessions us WHERE us.emp_id = u.emp_id AND us.session_date BETWEEN ? AND ?),
               COALESCE((SELECT SUM(total_hours) FROM user_sessions us WHERE us.emp_id = u.emp_id AND us.session_date BETWEEN ? AND ?), 0),
               COALESCE((SELECT SUM(duration_minutes) FROM breaks b WHERE b.emp_id = u.emp_id AND b.break_date BETWEEN ? AND ? AND b.status = 'Completed'), 0),
               COALESCE((SELECT COUNT(*) FROM breaks b WHERE b.emp_id = u.emp_id AND b.break_date BETWEEN ? AND ? AND b.status = 'Completed'), 0),
               COALESCE((SELECT COUNT(*) FROM user_sessions us WHERE us.emp_id = u.emp_id AND us.session_date BETWEEN ? AND ?), 0)
        FROM users u WHERE u.role = 'Employee' ORDER BY u.name
    """, [start_date, end_date, start_date, end_date, start_date, end_date,
          start_date, end_date, start_date, end_date, start_date, end_date]).fetchall()

    break_details = conn.execute("""
        SELECT b.break_id, b.emp_id, u.name, u.department, b.break_type, b.start_time, b.end_time, b.duration_minutes, b.break_date, b.status
        FROM breaks b JOIN users u ON b.emp_id = u.emp_id WHERE b.break_date BETWEEN ? AND ? ORDER BY b.break_date DESC, b.start_time DESC
    """, [start_date, end_date]).fetchall()

    session_details = conn.execute("""
        SELECT us.session_id, us.emp_id, u.name, u.department, us.login_time, us.logout_time, us.total_hours, us.session_date
        FROM user_sessions us JOIN users u ON us.emp_id = u.emp_id WHERE us.session_date BETWEEN ? AND ? ORDER BY us.session_date DESC, us.login_time DESC
    """, [start_date, end_date]).fetchall()
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
        'summary': summary_list, 'break_details': break_list, 'session_details': session_list
    }), 200


# ══════════════════════════════════════════════════════════════════════
#  PAGES
# ══════════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    if 'emp_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


# ══════════════════════════════════════════════════════════════════════
#  ERROR HANDLERS
# ══════════════════════════════════════════════════════════════════════

@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Not found'}), 404


@app.errorhandler(429)
def ratelimit_handler(error):
    return jsonify({'error': 'Rate limit exceeded. Try again later.'}), 429


@app.errorhandler(500)
def server_error(error):
    logger.exception("Internal server error")
    return jsonify({'error': 'Internal server error'}), 500


# ══════════════════════════════════════════════════════════════════════
#  SCHEDULED JOBS
# ══════════════════════════════════════════════════════════════════════

def cleanup_expired_tokens():
    try:
        conn = get_db()
        conn.execute("DELETE FROM password_reset_tokens WHERE expires_at < ?", [datetime.now()])
        conn.close()
        logger.info("Cleaned up expired password reset tokens")
    except Exception as e:
        logger.warning("Cleanup failed: %s", e)


# ── Entry point ──────────────────────────────────────────────────────

if __name__ == '__main__':
    if not STARTED:
        scheduler.add_job(cleanup_expired_tokens, 'interval', hours=1)
        scheduler.start()

    sentry_dsn = os.getenv('SENTRY_DSN')
    if sentry_dsn:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        sentry_sdk.init(dsn=sentry_dsn, integrations=[FlaskIntegration()])
        logger.info("Sentry initialized")

    app.run(debug=os.getenv('FLASK_DEBUG', '1') == '1',
            host='0.0.0.0',
            port=int(os.getenv('PORT', 5000)))
