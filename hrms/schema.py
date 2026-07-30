import os
import logging
import tempfile
from datetime import timedelta

import bcrypt

from .db import get_db, _scalar
from .helpers import (
    now_ist, gen_id, hash_password,
    _get_shift_date_for_dt, _fix_seed_shift_dates,
)
from .migrations import run_migrations

logger = logging.getLogger('hrms')


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

    # ── Break Approvals ───────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS break_approvals (
            approval_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            break_type VARCHAR NOT NULL,
            break_date DATE NOT NULL,
            reason VARCHAR,
            status VARCHAR DEFAULT 'Pending',
            approved_by VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id),
            FOREIGN KEY (break_type) REFERENCES break_types(break_type)
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

    # ── Audit Log ──────────────────────────────────────────────────
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

    # ── Leave Requests ─────────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS leave_requests (
            leave_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            leave_type VARCHAR NOT NULL,
            start_date DATE NOT NULL,
            end_date DATE NOT NULL,
            year INTEGER NOT NULL DEFAULT 0,
            reason VARCHAR,
            status VARCHAR DEFAULT 'Pending',
            approved_by VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Leave Balance ──────────────────────────────────────────────
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

    # ── Password Reset Tokens ──────────────────────────────────────
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

    # ── Notification Preferences ───────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS notification_preferences (
            pref_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            category VARCHAR NOT NULL,
            in_app INTEGER DEFAULT 1,
            email INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id),
            UNIQUE(emp_id, category)
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

    # ── Assets ─────────────────────────────────────────────────────
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

    # ── Job Postings ───────────────────────────────────────────────
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

    # ── Candidates ─────────────────────────────────────────────────
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

    # ── Interviews ─────────────────────────────────────────────────
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

    # ── Offer Letters ──────────────────────────────────────────────
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

    # ── Onboarding Tasks ───────────────────────────────────────────
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

    # ── Onboarding Document Checklist ─────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS onboarding_checklist (
            checklist_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            doc_type VARCHAR NOT NULL,
            status VARCHAR DEFAULT 'Pending',
            file_name VARCHAR,
            file_path VARCHAR,
            uploaded_at TIMESTAMP,
            reviewed_by VARCHAR,
            reviewed_at TIMESTAMP,
            notes VARCHAR,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Onboarding Workflow Progress ──────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS onboarding_workflow (
            emp_id VARCHAR PRIMARY KEY,
            current_step INTEGER DEFAULT 1,
            step_1_status VARCHAR DEFAULT 'Pending',
            step_2_status VARCHAR DEFAULT 'Pending',
            step_3_status VARCHAR DEFAULT 'Pending',
            step_4_status VARCHAR DEFAULT 'Pending',
            step_5_status VARCHAR DEFAULT 'Pending',
            intro_completed_by VARCHAR,
            created_at TIMESTAMP,
            updated_at TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Offboarding Tasks ──────────────────────────────────────────
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

    # ── Offboarding Workflow Progress ─────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS offboarding_workflow (
            workflow_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            step1_done INTEGER DEFAULT 0,
            step2_done INTEGER DEFAULT 0,
            step3_done INTEGER DEFAULT 0,
            step4_done INTEGER DEFAULT 0,
            step5_done INTEGER DEFAULT 0,
            completed INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    # ── Exit Interviews ────────────────────────────────────────────
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

    # ── Salary Structures ──────────────────────────────────────────
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

    # ── Payroll Runs ───────────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS payroll_runs (
            run_id INTEGER PRIMARY KEY,
            month INTEGER NOT NULL,
            year INTEGER NOT NULL,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status VARCHAR DEFAULT 'Draft'
        )
    ''')

    # ── Payroll Items ──────────────────────────────────────────────
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

    # ── Performance Goals ──────────────────────────────────────────
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

    # ── Performance Reviews ────────────────────────────────────────
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

    # ── 360 Feedback ───────────────────────────────────────────────
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

    # ── Expense Categories ─────────────────────────────────────────
    # ── Payroll Rates ──────────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS payroll_rates (
            rate_id INTEGER PRIMARY KEY,
            label VARCHAR NOT NULL,
            rate_type VARCHAR NOT NULL,
            value DECIMAL(10,2) NOT NULL,
            effective_from DATE NOT NULL,
            effective_to DATE,
            description VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # ── Expense Categories ─────────────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS expense_categories (
            cat_id INTEGER PRIMARY KEY,
            name VARCHAR NOT NULL,
            description VARCHAR
        )
    ''')

    # ── Expense Claims ─────────────────────────────────────────────
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

    # ── Help Desk Tickets ──────────────────────────────────────────
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

    # ── Ticket Comments ────────────────────────────────────────────
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

    # ── Document Uploads metadata ──────────────────────────────────
    conn.execute('''
        CREATE TABLE IF NOT EXISTS documents (
            doc_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            category VARCHAR DEFAULT 'Other',
            file_path VARCHAR,
            file_size INTEGER,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            context VARCHAR DEFAULT 'General',
            doc_type VARCHAR,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id)
        )
    ''')

    conn.close()

    # Run versioned migrations
    run_migrations()

    # Seed data
    _seed_data()

    logger.info("Database initialized")


def _seed_data():
    pwd_hash = bcrypt.hashpw(b'pass123', bcrypt.gensalt()).decode()
    conn = get_db()
    now = now_ist()

    # ── Seed Users ────────────────────────────────────────────────
    result = _scalar("SELECT COUNT(*) FROM users", conn=conn)
    if result == 0:
        conn.execute(
            "INSERT INTO users (emp_id, name, email, password, role, department, designation, phone, date_of_joining, status, allow_login, allow_breaks, first_login, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ['EMP001', 'Shubham Jawalkar', 'shubham@company.com', pwd_hash,
             'Super Admin', 'MIS', 'Tech Lead', '9876543210', now.date(),
             'Active', 1, 1, now, now]
        )
        conn.execute(
            "INSERT INTO users (emp_id, name, email, password, role, department, designation, phone, date_of_joining, manager_emp_id, status, allow_login, allow_breaks, first_login, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ['EMP002', 'Sachin Bhakte', 'sachinbhakte@gmail.com', pwd_hash,
             'Employee', 'Operations', 'Jr Developer', '9876543211', now.date(),
             'EMP001', 'Active', 1, 1, now, now]
        )
        conn.execute(
            "INSERT INTO users (emp_id, name, email, password, role, department, designation, phone, date_of_joining, manager_emp_id, status, allow_login, allow_breaks, first_login, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ['EMP003', 'Priya Sharma', 'priya@company.com', pwd_hash,
             'HR', 'HR', 'HR Manager', '9876543212', now.date(),
             'EMP001', 'Active', 1, 1, now, now]
        )
        conn.execute(
            "INSERT INTO users (emp_id, name, email, password, role, department, designation, phone, date_of_joining, manager_emp_id, status, allow_login, allow_breaks, first_login, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ['EMP004', 'Rahul Verma', 'rahul@company.com', pwd_hash,
             'Employee', 'IT', 'System Admin', '9876543213', now.date(),
             'EMP001', 'Active', 1, 1, now, now]
        )
        conn.execute(
            "INSERT INTO users (emp_id, name, email, password, role, department, designation, phone, date_of_joining, manager_emp_id, status, allow_login, allow_breaks, first_login, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ['EMP005', 'Neha Patil', 'neha@company.com', pwd_hash,
             'Employee', 'Admin', 'Office Admin', '9876543214', now.date(),
             'EMP001', 'Active', 1, 1, now, now]
        )
        conn.execute(
            "INSERT INTO users (emp_id, name, email, password, role, department, designation, phone, date_of_joining, manager_emp_id, status, allow_login, allow_breaks, first_login, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ['EMP006', 'Amit Deshmukh', 'amit@company.com', pwd_hash,
             'Employee', 'Finance', 'Accountant', '9876543215', now.date(),
             'EMP001', 'Active', 1, 1, now, now]
        )

    # ── Seed Expense Categories ───────────────────────────────────
    result = _scalar("SELECT COUNT(*) FROM expense_categories", conn=conn)
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

    # ── Migrate: add new columns if missing ───────────────────────
    for col in ['designation', 'manager_emp_id', 'phone', 'date_of_birth', 'date_of_joining', 'address', 'emergency_contact_name', 'emergency_contact_phone', 'shift_start', 'shift_end']:
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} VARCHAR")
        except Exception:
            pass

    # ── Migrate: insert missing seed users ─────────────────────────
    seed_users = [
        ['EMP003', 'Priya Sharma', 'priya@company.com', 'HR', 'HR', 'HR Manager', '9876543212'],
        ['EMP004', 'Rahul Verma', 'rahul@company.com', 'Employee', 'IT', 'System Admin', '9876543213'],
        ['EMP005', 'Neha Patil', 'neha@company.com', 'Employee', 'Admin', 'Office Admin', '9876543214'],
        ['EMP006', 'Amit Deshmukh', 'amit@company.com', 'Employee', 'Finance', 'Accountant', '9876543215'],
    ]
    existing = {r[0] for r in conn.execute("SELECT emp_id FROM users").fetchall()}
    for su in seed_users:
        if su[0] not in existing:
            conn.execute(
                "INSERT INTO users (emp_id, name, email, password, role, department, designation, phone, date_of_joining, manager_emp_id, status, allow_login, allow_breaks, first_login, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [su[0], su[1], su[2], pwd_hash, su[3], su[4], su[5], su[6],
                 now.date(), 'EMP001', 'Active', 1, 1, now, now]
            )

    result = _scalar("SELECT COUNT(*) FROM break_types", conn=conn)
    if result == 0:
        conn.executemany(
            "INSERT INTO break_types VALUES (?, ?, ?)",
            [('Tea', 15, 'Tea Break - 15 minutes'),
             ('Lunch', 60, 'Lunch Break - 1 hour'),
             ('Personal', 30, 'Personal Break - 30 minutes')]
        )

    # ── Seed Leave Balance ─────────────────────────────────────────
    result = _scalar("SELECT COUNT(*) FROM leave_balance", conn=conn)
    if result == 0:
        year = now.year
        bid = int(now.timestamp() * 1000) % 1000000
        for emp in conn.execute("SELECT emp_id FROM users").fetchall():
            bid += 1
            conn.execute("INSERT INTO leave_balance VALUES (?, ?, ?, ?, ?, ?)", [bid, emp[0], 'Casual', 12, 0, year])
            bid += 1
            conn.execute("INSERT INTO leave_balance VALUES (?, ?, ?, ?, ?, ?)", [bid, emp[0], 'Sick', 10, 0, year])
            bid += 1
            conn.execute("INSERT INTO leave_balance VALUES (?, ?, ?, ?, ?, ?)", [bid, emp[0], 'Annual', 20, 0, year])

    # ── Seed sample rows for all major modules ────────────────────
    base_id = int(now.timestamp() * 1000) % 1000000

    if _scalar("SELECT COUNT(*) FROM user_sessions", conn=conn) < 2:
        two_days_ago = (now - timedelta(days=2)).date()
        try:
            conn.execute(
                "INSERT INTO user_sessions (session_id, emp_id, login_time, logout_time, total_hours, session_date) VALUES (?, ?, ?, ?, ?, ?)",
                [base_id + 1, 'EMP001', now - timedelta(days=2, hours=8), now - timedelta(days=2), 8.0, two_days_ago]
            )
        except Exception:
            pass
        try:
            conn.execute(
                "INSERT INTO user_sessions (session_id, emp_id, login_time, logout_time, total_hours, session_date) VALUES (?, ?, ?, ?, ?, ?)",
                [base_id + 2, 'EMP002', now - timedelta(days=2, hours=6), now - timedelta(days=2, hours=1), 5.0, two_days_ago]
            )
        except Exception:
            pass

    if _scalar("SELECT COUNT(*) FROM breaks", conn=conn) < 2:
        two_days_ago = (now - timedelta(days=2)).date()
        try:
            conn.execute(
                "INSERT INTO breaks (break_id, emp_id, break_type, start_time, end_time, duration_minutes, break_date, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [base_id + 3, 'EMP001', 'Tea', now - timedelta(days=2, minutes=30), now - timedelta(days=2, minutes=15), 15, two_days_ago, 'Completed']
            )
        except Exception:
            pass
        try:
            conn.execute(
                "INSERT INTO breaks (break_id, emp_id, break_type, start_time, end_time, duration_minutes, break_date, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [base_id + 4, 'EMP002', 'Lunch', now - timedelta(days=2, hours=1), now - timedelta(days=2, minutes=30), 30, two_days_ago, 'Completed']
            )
        except Exception:
            pass

    if _scalar("SELECT COUNT(*) FROM audit_log", conn=conn) < 2:
        try:
            conn.execute(
                "INSERT INTO audit_log (log_id, emp_id, action, details, ip_address, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                [base_id + 5, 'EMP001', 'LOGIN', 'User signed in', '127.0.0.1', now]
            )
        except Exception:
            pass
        try:
            conn.execute(
                "INSERT INTO audit_log (log_id, emp_id, action, details, ip_address, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                [base_id + 6, 'EMP002', 'PROFILE_UPDATE', 'Updated profile', '127.0.0.1', now - timedelta(hours=1)]
            )
        except Exception:
            pass

    if _scalar("SELECT COUNT(*) FROM leave_requests", conn=conn) < 2:
        try:
            conn.execute(
                "INSERT INTO leave_requests (leave_id, emp_id, leave_type, start_date, end_date, year, reason, status, approved_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [base_id + 7, 'EMP002', 'Casual', (now + timedelta(days=3)).date(), (now + timedelta(days=4)).date(), (now + timedelta(days=3)).year, 'Personal work', 'Pending', None, now, now]
            )
        except Exception:
            pass
        try:
            conn.execute(
                "INSERT INTO leave_requests (leave_id, emp_id, leave_type, start_date, end_date, year, reason, status, approved_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [base_id + 8, 'EMP002', 'Sick', (now + timedelta(days=10)).date(), (now + timedelta(days=12)).date(), (now + timedelta(days=10)).year, 'Medical appointment', 'Approved', 'EMP001', now, now]
            )
        except Exception:
            pass

    if _scalar("SELECT COUNT(*) FROM password_reset_tokens", conn=conn) < 2:
        try:
            conn.execute(
                "INSERT INTO password_reset_tokens (token_id, emp_id, token, expires_at, used, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                [base_id + 9, 'EMP002', 'reset-token-001', now + timedelta(hours=2), 0, now]
            )
        except Exception:
            pass
        try:
            conn.execute(
                "INSERT INTO password_reset_tokens (token_id, emp_id, token, expires_at, used, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                [base_id + 10, 'EMP002', 'reset-token-002', now + timedelta(hours=4), 0, now]
            )
        except Exception:
            pass

    if _scalar("SELECT COUNT(*) FROM employee_documents", conn=conn) < 2:
        try:
            conn.execute(
                "INSERT INTO employee_documents (doc_id, emp_id, doc_type, file_name, uploaded_at) VALUES (?, ?, ?, ?, ?)",
                [base_id + 11, 'EMP001', 'Offer Letter', 'offer-letter.pdf', now]
            )
        except Exception:
            pass
        try:
            conn.execute(
                "INSERT INTO employee_documents (doc_id, emp_id, doc_type, file_name, uploaded_at) VALUES (?, ?, ?, ?, ?)",
                [base_id + 12, 'EMP002', 'ID Proof', 'aadhaar.pdf', now - timedelta(days=1)]
            )
        except Exception:
            pass

    if _scalar("SELECT COUNT(*) FROM dependents", conn=conn) < 2:
        try:
            conn.execute(
                "INSERT INTO dependents (dependent_id, emp_id, name, relationship, date_of_birth) VALUES (?, ?, ?, ?, ?)",
                [base_id + 13, 'EMP001', 'Ananya', 'Spouse', (now - timedelta(days=365*30)).date()]
            )
        except Exception:
            pass
        try:
            conn.execute(
                "INSERT INTO dependents (dependent_id, emp_id, name, relationship, date_of_birth) VALUES (?, ?, ?, ?, ?)",
                [base_id + 14, 'EMP002', 'Riya', 'Child', (now - timedelta(days=365*7)).date()]
            )
        except Exception:
            pass

    if _scalar("SELECT COUNT(*) FROM notifications", conn=conn) < 2:
        try:
            conn.execute(
                "INSERT INTO notifications (notification_id, emp_id, type, message, related_link, is_read, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [base_id + 15, 'EMP001', 'Leave', 'Your leave request is pending', '/leaves', 0, now]
            )
        except Exception:
            pass
        try:
            conn.execute(
                "INSERT INTO notifications (notification_id, emp_id, type, message, related_link, is_read, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [base_id + 16, 'EMP002', 'Profile', 'Please update your profile', '/profile', 0, now - timedelta(hours=2)]
            )
        except Exception:
            pass

    if _scalar("SELECT COUNT(*) FROM regularization_requests", conn=conn) < 2:
        try:
            conn.execute(
                "INSERT INTO regularization_requests (request_id, emp_id, request_date, reason, status, approved_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [base_id + 17, 'EMP002', now.date(), 'Late arrival', 'Pending', None, now, now]
            )
        except Exception:
            pass
        try:
            conn.execute(
                "INSERT INTO regularization_requests (request_id, emp_id, request_date, reason, status, approved_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [base_id + 18, 'EMP002', (now - timedelta(days=1)).date(), 'Forgot punch', 'Approved', 'EMP001', now - timedelta(days=1), now]
            )
        except Exception:
            pass

    if _scalar("SELECT COUNT(*) FROM assets", conn=conn) < 2:
        try:
            conn.execute(
                "INSERT INTO assets (asset_id, emp_id, asset_type, asset_tag, brand, model, serial_number, issued_date, return_date, status, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [base_id + 19, 'EMP002', 'Laptop', 'LAP-001', 'Dell', 'Latitude 5430', 'SN-1001', (now - timedelta(days=30)).date(), None, 'Issued', 'Primary workstation']
            )
        except Exception:
            pass
        try:
            conn.execute(
                "INSERT INTO assets (asset_id, emp_id, asset_type, asset_tag, brand, model, serial_number, issued_date, return_date, status, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [base_id + 20, 'EMP002', 'Phone', 'PH-001', 'Samsung', 'Galaxy S24', 'SN-1002', (now - timedelta(days=10)).date(), None, 'Issued', 'Company phone']
            )
        except Exception:
            pass

    if _scalar("SELECT COUNT(*) FROM job_postings", conn=conn) < 2:
        conn.execute(
            "INSERT INTO job_postings (job_id, title, department, location, description, requirements, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 21, 'Software Engineer', 'Engineering', 'Pune', 'Build scalable apps', 'Python, Flask', 'Open', now]
        )
        conn.execute(
            "INSERT INTO job_postings (job_id, title, department, location, description, requirements, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 22, 'HR Specialist', 'HR', 'Remote', 'Support employee lifecycle', 'People operations', 'Open', now]
        )

    if _scalar("SELECT COUNT(*) FROM candidates", conn=conn) < 2:
        conn.execute(
            "INSERT INTO candidates (candidate_id, job_id, name, email, phone, resume_text, status, applied_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 23, base_id + 21, 'Kavya Rao', 'kavya@example.com', '9999999001', 'Experienced backend engineer', 'Applied', now]
        )
        conn.execute(
            "INSERT INTO candidates (candidate_id, job_id, name, email, phone, resume_text, status, applied_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [base_id + 24, base_id + 22, 'Mihir Shah', 'mihir@example.com', '9999999002', 'HR operations background', 'Screening', now - timedelta(days=1)]
        )

    if _scalar("SELECT COUNT(*) FROM interviews", conn=conn) < 2:
        try:
            cand1 = _scalar("SELECT COUNT(*) FROM candidates WHERE candidate_id = ?", conn=conn, params=[base_id + 23])
            cand2 = _scalar("SELECT COUNT(*) FROM candidates WHERE candidate_id = ?", conn=conn, params=[base_id + 24])
            if cand1:
                conn.execute(
                    "INSERT INTO interviews (interview_id, candidate_id, scheduled_at, interviewer, mode, feedback, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [base_id + 25, base_id + 23, now + timedelta(days=2), 'EMP001', 'Virtual', 'Strong technical skills', 'Scheduled']
                )
            if cand2:
                conn.execute(
                    "INSERT INTO interviews (interview_id, candidate_id, scheduled_at, interviewer, mode, feedback, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [base_id + 26, base_id + 24, now + timedelta(days=3), 'EMP002', 'In-person', 'Good fit', 'Scheduled']
                )
        except Exception:
            pass

    if _scalar("SELECT COUNT(*) FROM offer_letters", conn=conn) < 2:
        try:
            cand1 = _scalar("SELECT COUNT(*) FROM candidates WHERE candidate_id = ?", conn=conn, params=[base_id + 23])
            cand2 = _scalar("SELECT COUNT(*) FROM candidates WHERE candidate_id = ?", conn=conn, params=[base_id + 24])
            if cand1:
                conn.execute(
                    "INSERT INTO offer_letters (offer_id, candidate_id, offered_salary, offer_date, status, accepted_at, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [base_id + 27, base_id + 23, 1800000.00, now.date(), 'Pending', None, 'Standard package']
                )
            if cand2:
                conn.execute(
                    "INSERT INTO offer_letters (offer_id, candidate_id, offered_salary, offer_date, status, accepted_at, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [base_id + 28, base_id + 24, 1200000.00, (now - timedelta(days=1)).date(), 'Accepted', now, 'Offer accepted']
                )
        except Exception:
            pass

    if _scalar("SELECT COUNT(*) FROM onboarding_tasks", conn=conn) < 2:
        try:
            e2 = _scalar("SELECT COUNT(*) FROM users WHERE emp_id = 'EMP002'", conn=conn)
            if e2:
                conn.execute(
                    "INSERT INTO onboarding_tasks (task_id, emp_id, task_name, assigned_to, status, due_date, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [base_id + 29, 'EMP002', 'Laptop setup', 'EMP001', 'Pending', (now + timedelta(days=2)).date(), None]
                )
                conn.execute(
                    "INSERT INTO onboarding_tasks (task_id, emp_id, task_name, assigned_to, status, due_date, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [base_id + 30, 'EMP002', 'HR paperwork', 'EMP002', 'Completed', (now - timedelta(days=1)).date(), now - timedelta(hours=3)]
                )
        except Exception:
            pass

    if _scalar("SELECT COUNT(*) FROM offboarding_tasks", conn=conn) < 2:
        try:
            e2 = _scalar("SELECT COUNT(*) FROM users WHERE emp_id = 'EMP002'", conn=conn)
            if e2:
                conn.execute(
                    "INSERT INTO offboarding_tasks (task_id, emp_id, task_name, assigned_to, status, due_date, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [base_id + 31, 'EMP002', 'Collect company assets', 'EMP001', 'Pending', (now + timedelta(days=5)).date(), None]
                )
                conn.execute(
                    "INSERT INTO offboarding_tasks (task_id, emp_id, task_name, assigned_to, status, due_date, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [base_id + 32, 'EMP002', 'Revoke access', 'EMP001', 'Completed', (now - timedelta(days=1)).date(), now - timedelta(days=1)]
                )
        except Exception:
            pass

    if _scalar("SELECT COUNT(*) FROM offboarding_workflow", conn=conn) < 2:
        try:
            e2 = _scalar("SELECT COUNT(*) FROM users WHERE emp_id = 'EMP002'", conn=conn)
            if e2:
                conn.execute(
                    "INSERT INTO offboarding_workflow (workflow_id, emp_id, step1_done) VALUES (?, ?, ?)",
                    [base_id + 55, 'EMP002', 1]
                )
                conn.execute(
                    "INSERT INTO offboarding_workflow (workflow_id, emp_id, step1_done) VALUES (?, ?, ?)",
                    [base_id + 56, 'EMP005', 0]
                )
        except Exception:
            pass

    if _scalar("SELECT COUNT(*) FROM exit_interviews", conn=conn) < 2:
        try:
            e2 = _scalar("SELECT COUNT(*) FROM users WHERE emp_id = 'EMP002'", conn=conn)
            if e2:
                conn.execute(
                    "INSERT INTO exit_interviews (interview_id, emp_id, reason, feedback, exit_date, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    [base_id + 33, 'EMP002', 'Career change', 'Positive experience', (now - timedelta(days=2)).date(), now - timedelta(days=2)]
                )
                conn.execute(
                    "INSERT INTO exit_interviews (interview_id, emp_id, reason, feedback, exit_date, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    [base_id + 34, 'EMP002', 'Relocation', 'Clear onboarding', (now - timedelta(days=5)).date(), now - timedelta(days=5)]
                )
        except Exception:
            pass

    if _scalar("SELECT COUNT(*) FROM salary_structures", conn=conn) < 2:
        try:
            conn.execute(
                "INSERT INTO salary_structures (struct_id, emp_id, basic, hra, allowances, deductions, effective_from) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [base_id + 35, 'EMP002', 30000.00, 9000.00, 4000.00, 1500.00, (now - timedelta(days=30)).date()]
            )
            conn.execute(
                "INSERT INTO salary_structures (struct_id, emp_id, basic, hra, allowances, deductions, effective_from) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [base_id + 36, 'EMP002', 28000.00, 8400.00, 3200.00, 1200.00, (now - timedelta(days=60)).date()]
            )
        except Exception:
            pass

    if _scalar("SELECT COUNT(*) FROM payroll_runs", conn=conn) < 2:
        try:
            conn.execute(
                "INSERT INTO payroll_runs (run_id, month, year, processed_at, status) VALUES (?, ?, ?, ?, ?)",
                [base_id + 37, now.month, now.year, now, 'Draft']
            )
            conn.execute(
                "INSERT INTO payroll_runs (run_id, month, year, processed_at, status) VALUES (?, ?, ?, ?, ?)",
                [base_id + 38, now.month - 1 if now.month > 1 else 12, now.year if now.month > 1 else now.year - 1, now - timedelta(days=30), 'Finalized']
            )
        except Exception:
            pass

    if _scalar("SELECT COUNT(*) FROM payroll_items", conn=conn) < 2:
        try:
            runs = conn.execute("SELECT run_id FROM payroll_runs ORDER BY run_id LIMIT 2").fetchall()
            if len(runs) >= 2:
                conn.execute(
                    "INSERT INTO payroll_items (item_id, run_id, emp_id, gross_salary, deductions_total, net_salary, pf, esi, pt, payslip_generated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [base_id + 39, runs[0][0], 'EMP002', 50000.00, 5000.00, 45000.00, 2500.00, 1500.00, 200.00, 0]
                )
                conn.execute(
                    "INSERT INTO payroll_items (item_id, run_id, emp_id, gross_salary, deductions_total, net_salary, pf, esi, pt, payslip_generated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [base_id + 40, runs[1][0], 'EMP002', 48000.00, 4800.00, 43200.00, 2400.00, 1400.00, 200.00, 1]
                )
        except Exception:
            pass

    if _scalar("SELECT COUNT(*) FROM goals", conn=conn) < 2:
        try:
            conn.execute(
                "INSERT INTO goals (goal_id, emp_id, title, description, target_date, weight, rating, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [base_id + 41, 'EMP002', 'Improve delivery', 'Ship one feature per sprint', (now + timedelta(days=30)).date(), 5, 4, 'Active', now]
            )
            conn.execute(
                "INSERT INTO goals (goal_id, emp_id, title, description, target_date, weight, rating, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [base_id + 42, 'EMP002', 'Customer support excellence', 'Maintain SLA', (now + timedelta(days=45)).date(), 4, 5, 'Active', now]
            )
        except Exception:
            pass

    if _scalar("SELECT COUNT(*) FROM performance_reviews", conn=conn) < 2:
        try:
            conn.execute(
                "INSERT INTO performance_reviews (review_id, emp_id, reviewer_id, review_period, overall_rating, comments, status, created_at, submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [base_id + 43, 'EMP002', 'EMP001', 'Q2 2026', 4.2, 'Strong execution', 'Submitted', now - timedelta(days=5), now - timedelta(days=3)]
            )
            conn.execute(
                "INSERT INTO performance_reviews (review_id, emp_id, reviewer_id, review_period, overall_rating, comments, status, created_at, submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [base_id + 44, 'EMP002', 'EMP001', 'Q2 2026', 4.6, 'Excellent ownership', 'Draft', now - timedelta(days=2), None]
            )
        except Exception:
            pass

    if _scalar("SELECT COUNT(*) FROM feedback_360", conn=conn) < 2:
        try:
            conn.execute(
                "INSERT INTO feedback_360 (feedback_id, emp_id, reviewer_id, category, rating, comment, submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [base_id + 45, 'EMP002', 'EMP002', 'Collaboration', 5, 'Great teammate', now - timedelta(days=1)]
            )
            conn.execute(
                "INSERT INTO feedback_360 (feedback_id, emp_id, reviewer_id, category, rating, comment, submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [base_id + 46, 'EMP002', 'EMP002', 'Communication', 4, 'Clear updates', now - timedelta(days=2)]
            )
        except Exception:
            pass

    if _scalar("SELECT COUNT(*) FROM expense_claims", conn=conn) < 2:
        try:
            conn.execute(
                "INSERT INTO expense_claims (claim_id, emp_id, cat_id, amount, description, receipt_path, status, approved_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [base_id + 47, 'EMP002', 1, 1250.00, 'Mumbai travel', 'travel.pdf', 'Pending', None, now]
            )
            conn.execute(
                "INSERT INTO expense_claims (claim_id, emp_id, cat_id, amount, description, receipt_path, status, approved_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [base_id + 48, 'EMP002', 2, 850.00, 'Client lunch', 'food.pdf', 'Approved', 'EMP001', now - timedelta(days=2)]
            )
        except Exception:
            pass

    if _scalar("SELECT COUNT(*) FROM tickets", conn=conn) < 2:
        try:
            conn.execute(
                "INSERT INTO tickets (ticket_id, emp_id, subject, description, category, priority, status, assigned_to, created_at, updated_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [base_id + 49, 'EMP002', 'VPN access issue', 'Unable to connect to VPN', 'IT', 'High', 'Open', 'EMP001', now, now, None]
            )
            conn.execute(
                "INSERT INTO tickets (ticket_id, emp_id, subject, description, category, priority, status, assigned_to, created_at, updated_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [base_id + 50, 'EMP002', 'Payroll question', 'Need pay slip clarification', 'HR', 'Medium', 'Resolved', 'EMP002', now - timedelta(days=1), now - timedelta(hours=2), now - timedelta(hours=1)]
            )
        except Exception:
            pass

    if _scalar("SELECT COUNT(*) FROM ticket_comments", conn=conn) < 2:
        try:
            t1 = _scalar("SELECT COUNT(*) FROM tickets WHERE ticket_id = ?", conn=conn, params=[base_id + 49])
            t2 = _scalar("SELECT COUNT(*) FROM tickets WHERE ticket_id = ?", conn=conn, params=[base_id + 50])
            if t1:
                conn.execute(
                    "INSERT INTO ticket_comments (comment_id, ticket_id, emp_id, comment, created_at) VALUES (?, ?, ?, ?, ?)",
                    [base_id + 51, base_id + 49, 'EMP001', 'We are looking into it', now]
                )
            if t2:
                conn.execute(
                    "INSERT INTO ticket_comments (comment_id, ticket_id, emp_id, comment, created_at) VALUES (?, ?, ?, ?, ?)",
                    [base_id + 52, base_id + 50, 'EMP002', 'Shared the payslip details', now - timedelta(hours=1)]
                )
        except Exception:
            pass

    if _scalar("SELECT COUNT(*) FROM documents", conn=conn) < 2:
        try:
            conn.execute(
                "INSERT INTO documents (doc_id, emp_id, name, category, file_path, file_size, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [base_id + 53, 'EMP001', 'Offer Letter', 'Offer Letter', '/uploads/offer.pdf', 204800, now]
            )
            conn.execute(
                "INSERT INTO documents (doc_id, emp_id, name, category, file_path, file_size, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [base_id + 54, 'EMP002', 'ID Proof', 'ID Proof', '/uploads/id.pdf', 153600, now - timedelta(days=1)]
            )
        except Exception:
            pass

    # ── Seed Payroll Rates ─────────────────────────────────────────
    if _scalar("SELECT COUNT(*) FROM payroll_rates", conn=conn) == 0:
        rate_id = int(now.timestamp() * 1000) % 1000000
        payroll_rate_seed = [
            [rate_id + 1, 'PF Employee Contribution %', 'pf_rate', 12.00, now.date(), None, 'PF contribution as % of gross'],
            [rate_id + 2, 'PF Max Contribution (monthly)', 'pf_max', 1800.00, now.date(), None, 'Monthly PF cap'],
            [rate_id + 3, 'ESI Employee Contribution %', 'esi_rate', 0.75, now.date(), None, 'ESI contribution as % of gross'],
            [rate_id + 4, 'ESI Max Gross Threshold', 'esi_max_gross', 21000.00, now.date(), None, 'Gross threshold for ESI eligibility'],
            [rate_id + 5, 'PT Gross Threshold', 'pt_threshold', 10000.00, now.date(), None, 'Min gross for Professional Tax'],
            [rate_id + 6, 'PT Amount', 'pt_amount', 200.00, now.date(), None, 'Professional Tax amount'],
            [rate_id + 7, 'TDS 0% slab max', 'tds_slab_0_max', 300000.00, now.date(), None, 'Income up to this is tax-free'],
            [rate_id + 8, 'TDS 5% slab max', 'tds_slab_1_max', 600000.00, now.date(), None, 'Income range 3-6L taxed at 5%'],
            [rate_id + 9, 'TDS 10% slab max', 'tds_slab_2_max', 900000.00, now.date(), None, 'Income range 6-9L taxed at 10%'],
            [rate_id + 10, 'TDS 15% slab max', 'tds_slab_3_max', 1200000.00, now.date(), None, 'Income range 9-12L taxed at 15%'],
            [rate_id + 11, 'TDS 20% slab max', 'tds_slab_4_max', 1500000.00, now.date(), None, 'Income range 12-15L taxed at 20%'],
            [rate_id + 12, 'TDS Rate Slab 1 (5%)', 'tds_rate_1', 5.00, now.date(), None, 'Tax rate for 3-6L slab'],
            [rate_id + 13, 'TDS Rate Slab 2 (10%)', 'tds_rate_2', 10.00, now.date(), None, 'Tax rate for 6-9L slab'],
            [rate_id + 14, 'TDS Rate Slab 3 (15%)', 'tds_rate_3', 15.00, now.date(), None, 'Tax rate for 9-12L slab'],
            [rate_id + 15, 'TDS Rate Slab 4 (20%)', 'tds_rate_4', 20.00, now.date(), None, 'Tax rate for 12-15L slab'],
            [rate_id + 16, 'TDS Rate Slab 5 (30%)', 'tds_rate_5', 30.00, now.date(), None, 'Tax rate for income above 15L'],
        ]
        conn.executemany(
            "INSERT INTO payroll_rates (rate_id, label, rate_type, value, effective_from, effective_to, description) VALUES (?, ?, ?, ?, ?, ?, ?)",
            payroll_rate_seed
        )

    # ── Seed Notification Preferences ──────────────────────────────
    if _scalar("SELECT COUNT(*) FROM notification_preferences", conn=conn) == 0:
        categories = ['Onboarding', 'Leaves', 'Expenses', 'Tickets', 'Payroll']
        for emp_row in conn.execute("SELECT emp_id FROM users").fetchall():
            for cat in categories:
                try:
                    conn.execute(
                        "INSERT INTO notification_preferences (pref_id, emp_id, category, in_app, email) VALUES (?, ?, ?, 1, 1)",
                        [gen_id(), emp_row[0], cat]
                    )
                except Exception:
                    pass

    # ── Assign default shift 09:00-18:00 to all employees ────────
    conn.execute("UPDATE users SET shift_start = '09:00', shift_end = '18:00' WHERE shift_start IS NULL")

    # ── Fix seed session/break dates to use shift-based dates ─────
    _fix_seed_shift_dates(conn, now)

    # ── Migration: move seed sessions/breaks to 2 days ago
    two_days_ago = (now - timedelta(days=2)).date()
    seed_emps = ['EMP001', 'EMP002']
    for eid in seed_emps:
        conn.execute(
            "UPDATE user_sessions SET session_date = ?, login_time = login_time - INTERVAL '2 days', logout_time = logout_time - INTERVAL '2 days' WHERE emp_id = ? AND session_date >= ?",
            [two_days_ago, eid, two_days_ago]
        )
        conn.execute(
            "UPDATE breaks SET break_date = ?, start_time = start_time - INTERVAL '2 days', end_time = end_time - INTERVAL '2 days' WHERE emp_id = ? AND break_date >= ?",
            [two_days_ago, eid, two_days_ago]
        )

    conn.close()
