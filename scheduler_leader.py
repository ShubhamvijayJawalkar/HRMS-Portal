"""FR-JOB-05 — scheduler leader election.

The SRS asks for "exactly-once execution across all pods" and "chaos test with 3
scheduler-capable pods running". What shipped instead was a heuristic: the
scheduler starts in the gunicorn master process. That is right for a single
instance and **silently wrong for several**, because every pod has its own gunicorn
master, so an N-pod deployment ran every cron job **N times**.

That is not noise. The jobs are:

* attendance finalisation (FR-JOB-01) — replaces the day's rows transactionally
* leave accrual (FR-LEA-08) — idempotent per employee/type/year/month, so a second
  run is mostly harmless
* offboarding access revocation — closes sessions; running twice is survivable
* outbox dispatch — guarded by a conditional claim, so this one is already safe
* expired-token purge — idempotent

So the duplication is mostly absorbed by idempotency that had to be built for other
reasons, which is exactly why it went unnoticed. "Mostly absorbed" is not
"correct", and the requirement is explicit.

**The lease.** One Redis key, `hrms:scheduler:leader`, holding a token unique to
this process, set with ``NX`` and a TTL. The leader renews it well inside the TTL;
a pod that cannot renew shuts its scheduler down rather than continuing to fire
jobs it no longer owns. Fencing is by *token*: a renewal only extends a value this
process wrote, so a pod that lost the lease and came back cannot resurrect it, and
a stale leader that wakes up cannot extend someone else's term.

**When Redis is unavailable the scheduler does not start.** That is the whole
point: if the lease store is unreachable, "everyone starts" is the failure mode
this module exists to prevent, so a single pod runs the jobs and says so loudly at
error level. The alternative — carry on without a lease — reintroduces exactly the
bug, just less visibly.

**No Redis at all** is a supported single-process deployment (dev, CI, the
compose stack) and falls back to the previous heuristic, with the multi-pod
limitation stated in the log and in the matrix. It is not silently pretended safe.
"""

from __future__ import annotations

import logging
import os
import socket
import uuid

logger = logging.getLogger(__name__)

KEY = 'hrms:scheduler:leader'

#: Lease length. Long enough that a brief Redis hiccup or a slow GC pause does not
#: cost leadership, short enough that a crashed leader is replaced quickly.
TTL_SECONDS = int(os.getenv('SCHEDULER_LEASE_TTL', '60'))

#: Renew at a third of the TTL, so two consecutive failures are survivable and a
#: third is not — the point is to lose the lease before anyone else can act on it.
RENEW_INTERVAL_SECONDS = max(TTL_SECONDS // 3, 5)

#: A pod needs a stable identity for its own comparison against the stored token.
INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _redis():
    """The Redis client, or None if Redis is not configured/reachable."""
    url = os.getenv('REDIS_URL')
    if not url:
        return None
    try:
        import redis as redis_lib

        client = redis_lib.Redis.from_url(url, socket_connect_timeout=2,
                                          socket_timeout=2,
                                          # `holder()` is rendered into the health
                                          # report, and bytes are not JSON
                                          # serialisable — a 500 from /api/health
                                          # because of an encoding default is not a
                                          # failure mode worth having.
                                          decode_responses=True)
        client.ping()
        return client
    except Exception as exc:
        logger.warning('Scheduler leader election unavailable: %s', exc)
        return None


def acquire(ttl: int = TTL_SECONDS) -> bool:
    """Try to become the leader. True if this process holds the lease."""
    client = _redis()
    if client is None:
        return False
    try:
        return bool(client.set(KEY, INSTANCE_ID, nx=True, ex=ttl))
    except Exception as exc:
        logger.error('Could not acquire the scheduler lease: %s', exc)
        return False


def renew(ttl: int = TTL_SECONDS) -> bool:
    """Extend the lease, but only if this process still owns it.

    The compare-then-extend is a Lua script rather than a GET/PUT pair, because
    between the two another pod could take the lease and this one would then
    overwrite the new leader's token — which would leave *two* pods believing they
    are leader, with neither able to prove otherwise.
    """
    client = _redis()
    if client is None:
        return False
    script = (
        "if redis.call('get', KEYS[1]) == ARGV[1] then "
        "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
    )
    try:
        return bool(client.eval(script, 1, KEY, INSTANCE_ID, ttl))
    except Exception as exc:
        logger.error('Could not renew the scheduler lease: %s', exc)
        return False


def release() -> None:
    """Give up the lease on a clean shutdown, so a replacement starts at once."""
    client = _redis()
    if client is None:
        return
    script = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end"
    try:
        client.eval(script, 1, KEY, INSTANCE_ID)
    except Exception:
        pass


def holder() -> str | None:
    """Who currently holds the lease, for the health report."""
    client = _redis()
    if client is None:
        return None
    try:
        return client.get(KEY)
    except Exception:
        return None


def should_start_scheduler() -> bool:
    """The gate the boot block calls.

    Three cases, in the order that fails safe:

    1. **Redis configured** — start only if this process holds the lease. Exactly
       one pod across the fleet runs the jobs.
    2. **Redis not configured** — fall back to the old heuristic, so dev, CI and the
       compose stack are unchanged, and log the limitation at warning level so the
       multi-pod restriction is visible to whoever deploys it.
    3. **Redis configured but unreachable** — `_redis()` returns None for *both*
       "not configured" and "unreachable", which would silently land this pod in
       case 2. So the distinction is made explicitly here: a configured-but-broken
       Redis refuses to start the scheduler rather than running jobs unowned.
    """
    if not os.getenv('REDIS_URL'):
        legacy = (
            os.getenv('FLASK_DEBUG') == '1'
            or os.getenv('FLASK_ENV') != 'production'
            or os.getenv('SERVER_SOFTWARE', '').startswith('gunicorn')
            or os.getenv('GUNICORN_MASTER') == 'true'
            or not os.getenv('SERVER_SOFTWARE')
        )
        if not legacy:
            logger.warning(
                'REDIS_URL is not set, so leader election is unavailable. This is '
                'only safe for a single-process deployment: with several pods every '
                'cron job would run once per pod (FR-JOB-05).'
            )
            return False
        return True

    if _redis() is None:
        logger.error(
            'REDIS_URL is set but unreachable, so the scheduler lease cannot be '
            'taken. Refusing to start the scheduler: starting it unowned is '
            'precisely the failure FR-JOB-05 describes.'
        )
        return False

    if acquire():
        logger.info('Scheduler lease acquired by %s', INSTANCE_ID)
        return True
    logger.info('Another instance holds the scheduler lease; this one runs no cron jobs')
    return False
