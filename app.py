import os

from hrms import create_app
from hrms.db import get_db
from hrms.helpers import hash_password, gen_id, now_ist, audit_log, get_user, _is_admin

app = create_app()

if __name__ == '__main__':
    import logging
    logger = logging.getLogger('hrms')

    sentry_dsn = os.getenv('SENTRY_DSN')
    if sentry_dsn:
        try:
            import sentry_sdk
            from sentry_sdk.integrations.flask import FlaskIntegration
            sentry_sdk.init(dsn=sentry_dsn, integrations=[FlaskIntegration()])
            logger.info("Sentry initialized")
        except ImportError:
            logger.warning("sentry_sdk not installed, skipping Sentry init")
        except Exception as e:
            logger.warning("Sentry init failed: %s", e)

    app.run(debug=os.getenv('FLASK_DEBUG', '0') == '1',
            host='0.0.0.0',
            port=int(os.getenv('PORT', 5000)))
