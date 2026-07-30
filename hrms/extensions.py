from apscheduler.schedulers.background import BackgroundScheduler
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

scheduler = BackgroundScheduler()
limiter = Limiter(key_func=get_remote_address, storage_uri="memory://")
STARTED = False
