import logging

import duckdb

from .db import get_db

logger = logging.getLogger('hrms')


def migration_010_seed_holidays(conn):
    from .helpers import now_ist
    now = now_ist()
    current_year = now.year
    existing_years = conn.execute("SELECT DISTINCT year FROM holidays").fetchall()
    existing_years_set = {r[0] for r in existing_years}
    if current_year in existing_years_set:
        logger.info("Holidays already exist for %d, skipping seed", current_year)
        return
    holidays = [
        (f'{current_year}-01-01', 'New Year', 'National'),
        (f'{current_year}-01-26', 'Republic Day', 'National'),
        (f'{current_year}-08-15', 'Independence Day', 'National'),
        (f'{current_year}-11-01', 'Diwali', 'Optional'),
        (f'{current_year}-12-25', 'Christmas', 'Optional'),
    ]
    base = 9000000 + (now.microsecond % 100000)
    rows = []
    for i, (date_str, name, htype) in enumerate(holidays):
        exists = conn.execute("SELECT 1 FROM holidays WHERE holiday_date = ? AND name = ?", [date_str, name]).fetchone()
        if not exists:
            rows.append([base + i, name, date_str, current_year, htype])
    if rows:
        conn.executemany("INSERT INTO holidays VALUES (?, ?, ?, ?, ?)", rows)
        conn.commit()
        logger.info("Seeded %d holidays for %d", len(rows), current_year)
    else:
        logger.info("No new holidays to seed for %d", current_year)


MIGRATIONS = [
    {
        'id': '001',
        'description': 'Initial schema - all CREATE TABLE statements',
        'sql': None,
    },
    {
        'id': '002',
        'description': 'Add year column to leave_requests',
        'sql': "ALTER TABLE leave_requests ADD COLUMN year INTEGER DEFAULT 0",
    },
    {
        'id': '003',
        'description': 'Add designation, manager_emp_id, phone, date_of_birth, date_of_joining, address, emergency_contact_name, emergency_contact_phone, shift_start, shift_end to users',
        'sql': None,
        'fn': lambda conn: _migration_003_add_user_columns(conn),
    },
    {
        'id': '004',
        'description': 'Add candidate_id to users',
        'sql': "ALTER TABLE users ADD COLUMN candidate_id INTEGER",
    },
    {
        'id': '005',
        'description': 'Add offer_id to users',
        'sql': "ALTER TABLE users ADD COLUMN offer_id INTEGER",
    },
    {
        'id': '006',
        'description': 'Create offboarding_workflow table',
        'sql': None,
    },
    {
        'id': '007',
        'description': 'Unify documents: add context and doc_type columns, migrate data from employee_documents and onboarding_checklist',
        'sql': None,
    },
    {
        'id': '008',
        'description': 'Create notification_preferences table',
        'sql': '''CREATE TABLE IF NOT EXISTS notification_preferences (
            pref_id INTEGER PRIMARY KEY,
            emp_id VARCHAR NOT NULL,
            category VARCHAR NOT NULL,
            in_app INTEGER DEFAULT 1,
            email INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (emp_id) REFERENCES users(emp_id),
            UNIQUE(emp_id, category)
        )''',
    },
    {
        'id': '009',
        'description': 'Create payroll_rates table',
        'sql': '''CREATE TABLE IF NOT EXISTS payroll_rates (
            rate_id INTEGER PRIMARY KEY,
            label VARCHAR NOT NULL,
            rate_type VARCHAR NOT NULL,
            value DECIMAL(10,2) NOT NULL,
            effective_from DATE NOT NULL,
            effective_to DATE,
            description VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''',
    },
    {
        'id': '010',
        'description': 'Seed holidays for current year if missing',
        'fn': migration_010_seed_holidays,
    },
]


def migration_007_unified_documents():
    conn = get_db()
    try:
        for col in ['context', 'doc_type']:
            try:
                conn.execute(f"ALTER TABLE documents ADD COLUMN {col} VARCHAR")
            except Exception:
                pass
        conn.execute("UPDATE documents SET context = 'General' WHERE context IS NULL")
        rows = conn.execute("SELECT doc_id, emp_id, doc_type, file_name, uploaded_at FROM employee_documents").fetchall()
        for r in rows:
            try:
                conn.execute(
                    "INSERT INTO documents (emp_id, name, category, file_path, file_size, uploaded_at, context, doc_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [r[1], r[3] or r[2], r[2], None, None, r[4], 'Personal', r[2]]
                )
            except Exception:
                pass
        rows2 = conn.execute("SELECT emp_id, doc_type, file_name, file_path, uploaded_at FROM onboarding_checklist WHERE file_name IS NOT NULL").fetchall()
        for r in rows2:
            try:
                conn.execute(
                    "INSERT INTO documents (emp_id, name, category, file_path, file_size, uploaded_at, context, doc_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [r[0], r[2] or r[1], r[1], r[3], None, r[4], 'Onboarding', r[1]]
                )
            except Exception:
                pass
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def _migration_003_add_user_columns(conn):
    for col in ['designation', 'manager_emp_id', 'phone', 'date_of_birth', 'date_of_joining', 'address', 'emergency_contact_name', 'emergency_contact_phone', 'shift_start', 'shift_end']:
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} VARCHAR")
        except duckdb.CatalogException:
            pass


def run_migrations():
    conn = get_db()
    try:
        _ensure_migration_table(conn)
        applied = _get_applied_migrations(conn)

        for migration in MIGRATIONS:
            if migration['id'] not in applied:
                if migration.get('fn'):
                    migration['fn'](conn)
                    logger.info("Migration %s applied: %s", migration['id'], migration['description'])
                elif migration['id'] == '007':
                    migration_007_unified_documents()
                elif migration['sql']:
                    try:
                        conn.execute(migration['sql'])
                        logger.info("Migration %s applied: %s", migration['id'], migration['description'])
                    except duckdb.CatalogException as e:
                        logger.warning("Migration %s skipped (column may already exist): %s", migration['id'], e)
                    except Exception as e:
                        logger.error("Migration %s FAILED: %s", migration['id'], e)
                        raise
                conn.execute(
                    "INSERT INTO schema_migrations (migration_id, description) VALUES (?, ?)",
                    [migration['id'], migration['description']]
                )
                conn.commit()
    finally:
        conn.close()


def _ensure_migration_table(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS schema_migrations (
            migration_id VARCHAR PRIMARY KEY,
            description VARCHAR,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')


def _get_applied_migrations(conn):
    rows = conn.execute("SELECT migration_id FROM schema_migrations ORDER BY migration_id").fetchall()
    return {r[0] for r in rows}
