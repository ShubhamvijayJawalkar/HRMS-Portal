import atexit
import logging
import os
import secrets
from datetime import timedelta

from dotenv import load_dotenv
from flask import Flask

load_dotenv()

from .db import DB_FILE, close_db, get_db, health_status
from .decorators import (
    admin_required,
    department_required,
    hr_or_admin_required,
    login_required,
    manager_or_admin_required,
)
from .extensions import STARTED, limiter, scheduler
from .helpers import (
    _is_admin,
    add_notification,
    audit_log,
    check_password,
    gen_id,
    get_user,
    hash_password,
    notify_admins,
    now_ist,
    send_email,
)
from .payroll import open_review_cycle
from .schema import init_db

log_level = getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper(), logging.INFO)
logging.basicConfig(format='%(asctime)s [%(levelname)s] %(name)s: %(message)s', level=log_level)
logger = logging.getLogger('hrms')


def _load_secret_key():
    key = os.getenv('SECRET_KEY', '')
    if key and key not in ('change-me-to-random-string', 'hrms_secret_key_2024', 'change-me-in-production'):
        return key
    key_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.secret_key')
    try:
        with open(key_file, 'r') as f:
            stored = f.read().strip()
            if stored:
                return stored
    except (FileNotFoundError, IOError):
        pass
    new_key = secrets.token_hex(32)
    try:
        with open(key_file, 'w') as f:
            f.write(new_key)
        logger.info("Generated persistent SECRET_KEY at %s", key_file)
    except IOError:
        logger.warning("Could not write .secret_key file; sessions will not persist across restarts")
    return new_key


def cleanup_expired_tokens():
    try:
        conn = get_db()
        conn.execute("DELETE FROM password_reset_tokens WHERE expires_at < ?", [now_ist()])
        conn.execute(
            "UPDATE breaks SET end_time = ?, duration_minutes = ?, status = 'Orphaned' WHERE status = 'Active' AND start_time < ?",
            [now_ist(), 0, now_ist() - timedelta(hours=12)]
        )
        conn.execute("COMMIT")
        logger.info("Cleaned up expired tokens and orphaned breaks")
    except Exception as e:
        logger.warning("Cleanup failed: %s", e)


def create_app():
    app = Flask(__name__, template_folder='../templates')

    app.secret_key = _load_secret_key()

    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['SESSION_COOKIE_SECURE'] = (os.getenv('FLASK_ENV') == 'production')
    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)

    if os.getenv('FLASK_ENV') == 'production':
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    limiter.init_app(app)

    try:
        from flasgger import Swagger
        swagger_config = {
            'headers': [],
            'specs': [{'endpoint': 'apispec', 'route': '/apispec.json',
                       'rule_filter': lambda rule: rule.rule.startswith('/api/'),
                       'model_filter': lambda tag: True}],
            'static_url_path': '/flasgger_static', 'swagger_ui': True, 'specs_route': '/docs/',
        }
        Swagger(app, config=swagger_config, template={
            'info': {'title': 'HRMS API', 'description': 'Human Resource Management System', 'version': '1.0.0'},
            'securityDefinitions': {'sessionAuth': {'type': 'apiKey', 'name': 'Cookie', 'in': 'header'}}
        })
    except Exception:
        logger.warning("Flasgger/Swagger not available, skipping docs endpoint")

    from .analytics import analytics_bp
    from .assets import assets_bp
    from .ats import ats_bp
    from .attendance import attendance_bp
    from .audit import audit_bp
    from .auth import auth_bp
    from .documents import documents_bp
    from .expenses import expenses_bp
    from .leaves import leaves_bp
    from .notifications import notifications_bp
    from .offboarding import offboarding_bp
    from .onboarding import onboarding_bp
    from .payroll import payroll_bp
    from .performance import performance_bp
    from .reports import reports_bp
    from .tickets import tickets_bp
    from .users import users_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(attendance_bp)
    app.register_blueprint(leaves_bp)
    app.register_blueprint(payroll_bp)
    app.register_blueprint(performance_bp)
    app.register_blueprint(expenses_bp)
    app.register_blueprint(tickets_bp)
    app.register_blueprint(documents_bp)
    app.register_blueprint(onboarding_bp)
    app.register_blueprint(offboarding_bp)
    app.register_blueprint(ats_bp)
    app.register_blueprint(analytics_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(audit_bp)
    app.register_blueprint(notifications_bp)
    app.register_blueprint(assets_bp)

    _db_inited = False

    @app.route('/api/__health')
    def health_check():
        from flask import jsonify
        status = health_status()
        try:
            conn = get_db()
            conn.execute("SELECT 1")
            status['db_ok'] = True
        except Exception as e:
            status['db_ok'] = False
            status['db_error'] = str(e)
        return jsonify(status)

    _db_inited = False

    @app.before_request
    def _ensure_db():
        nonlocal _db_inited
        if not _db_inited:
            try:
                init_db()
                _db_inited = True
            except Exception as e:
                logger.critical("Database initialization failed: %s", e)
                raise

    @app.errorhandler(404)
    def not_found(error):
        from flask import jsonify
        return jsonify({'error': 'Not found'}), 404

    @app.errorhandler(429)
    def rate_limited(error):
        from flask import jsonify
        return jsonify({'error': 'Too many requests. Please try again later.'}), 429

    @app.errorhandler(500)
    def server_error(error):
        logger.exception("Internal server error")
        from flask import jsonify
        return jsonify({'error': 'Internal server error'}), 500

    @app.after_request
    def set_security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['X-XSS-Protection'] = '1; mode=block'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        if os.getenv('FLASK_ENV') == 'production':
            response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
        return response

    global STARTED
    if not STARTED:
        try:
            scheduler.add_job(cleanup_expired_tokens, 'interval', hours=1)
            scheduler.add_job(open_review_cycle, 'cron', month='3,6,9,12', day=1, hour=2)
            scheduler.start()
            STARTED = True
            logger.info("Scheduler started")
            def _shutdown():
                scheduler.shutdown(wait=False)
                close_db()
            atexit.register(_shutdown)
        except Exception as e:
            logger.warning("Scheduler failed to start: %s", e)

    return app
