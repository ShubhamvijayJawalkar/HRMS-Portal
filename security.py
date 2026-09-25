"""Phase 3a — SRS v2.0 cross-cutting rules CC-06 (auth hardening).

Three additive, backward-compatible protections:

* **Argon2id password hashing** — new hashes use Argon2id with
  OWASP-recommended parameters (m=19456 KiB, t=2, p=1). Legacy v1.0 bcrypt
  hashes still verify and are transparently re-hashed to Argon2id on the
  next successful login (``needs_rehash`` drives that upgrade path).

* **CSRF defense** — a per-session token (``session['csrf_token']``) is
  issued on the first safe request and enforced on every POST/PUT/PATCH/DELETE:
  the token must arrive via the ``X-CSRF-Token`` header, a ``csrf_token``
  form field, or a ``csrf_token`` key in a JSON body. Requests are accepted
  without a token only while the session has no token yet (bootstrap —
  there is nothing established to protect; the response then seeds one).
  To reach browser code without editing every template, ``init_csrf``
  injects a tiny script into HTML responses that wraps ``window.fetch`` and
  auto-attaches the header; native ``<form>`` elements carry a hidden
  ``csrf_token`` field. The pre-existing ``GET /api/csrf-token`` endpoint
  hands the same token to programmatic clients (used by the test harness).

* **Server-side sessions** — set ``REDIS_URL`` to store Flask sessions in
  Redis (the cookie then holds only an opaque session id). When the env var
  is absent — the default for dev and CI — Flask's signed-cookie sessions
  are kept, so nothing else changes.

Enforcement lives on both stacks (DuckDB and PostgreSQL) because it sits in
Flask's request pipeline, above the database layer.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets

import argon2
from argon2.exceptions import InvalidHashError, VerificationError
from flask import jsonify, request, session
from flask.sessions import SecureCookieSessionInterface, SessionMixin

logger = logging.getLogger("hrms")

# ── Argon2id password hashing (CC-06) ──────────────────────────────────

# OWASP's recommended floor for Argon2id: m=19456 KiB, t=2, p=1.
# Argon2id is argon2-cffi's default hash type.
_password_hasher = argon2.PasswordHasher(
    time_cost=2,
    memory_cost=19456,   # KiB
    parallelism=1,
    hash_len=32,
    salt_len=16,
)
_LEGACY_BCRYPT = ("$2a$", "$2b$", "$2y$")


def hash_password(password) -> str:
    """Hash for storage — always Argon2id."""
    return _password_hasher.hash(password if isinstance(password, str) else password.decode())


def check_password(password, stored_hash) -> bool:
    """Verify a candidate password against Argon2id *or* legacy bcrypt."""
    if not stored_hash:
        return False
    if stored_hash.startswith("$argon2"):
        try:
            return _password_hasher.verify(
                stored_hash, password if isinstance(password, str) else password.decode()
            )
        except (VerificationError, InvalidHashError):
            return False
    if stored_hash.startswith(_LEGACY_BCRYPT):
        import bcrypt  # legacy v1.0 dependency stays optional at runtime
        try:
            cand = password if isinstance(password, bytes) else password.encode()
            return bcrypt.checkpw(cand, stored_hash.encode())
        except Exception:
            return False
    return False


def needs_rehash(stored_hash) -> bool:
    """True when the stored hash predates Argon2id (i.e. is bcrypt)."""
    return bool(stored_hash) and not stored_hash.startswith("$argon2")


# ── CSRF (CC-06) ───────────────────────────────────────────────────────

CSRF_KEY = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "csrf_token"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

_FETCH_PATCH = (
    "(function(){if(window.__hrmsCsrf)return;window.__hrmsCsrf=1;"
    "var T="  # token JSON-filled below
)
_BODY_TAG = re.compile(r"</body>", re.IGNORECASE)


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _provided_token():
    """Token carried by the request: header > form field > JSON body."""
    token = request.headers.get(CSRF_HEADER)
    if token:
        return token
    token = request.form.get(CSRF_FIELD)
    if token:
        return token
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        return body.get(CSRF_FIELD) or ""
    return ""


def _csrf_reject():
    if request.is_json or request.path.startswith("/api/"):
        return jsonify({"error": "CSRF token missing or invalid"}), 403
    return "CSRF token missing or invalid", 403


def _fetch_patcher(token: str) -> str:
    return (
        "<script>" + _FETCH_PATCH + json.dumps(token) + ";"
        "var O=window.fetch;"
        "window.fetch=function(u,o){o=o||{};"
        "var m=((o&&o.method)||'GET').toUpperCase();"
        "if(m!=='GET'&&m!=='HEAD'&&m!=='OPTIONS'){"
        "o.headers=Object.assign({},o.headers,{" + json.dumps(CSRF_HEADER) + ":T});}"
        "return O(u,o);};})();</script>"
    )


def init_csrf(app) -> None:
    """Install CSRF enforcement + delivery (page injection, template helper)."""

    @app.before_request
    def _csrf_guard():  # noqa: ANN001 - Flask hook
        if request.path.startswith("/static/"):
            return None
        # Scoped pre-boarding links authenticate with their signed token and
        # intentionally have no normal login/CSRF session. The lifecycle route
        # validates the token before accepting any mutation.
        if request.path.startswith("/api/preboarding/"):
            return None
        established = session.get(CSRF_KEY)

        if request.method in _SAFE_METHODS:
            if not established:
                session[CSRF_KEY] = _new_token()  # seeds cookie on this response
            return None

        if not established:
            # Bootstrap: this session has no token yet, so a cross-site
            # attacker cannot have captured one either. Seed it now; the
            # response carries the cookie and any page token.
            session[CSRF_KEY] = _new_token()
            return None

        provided = _provided_token()
        if not provided or not hmac.compare_digest(provided, established):
            return _csrf_reject()
        return None

    @app.after_request
    def _csrf_deliver(response):  # noqa: ANN001
        token = session.get(CSRF_KEY)
        if token and response.mimetype == "text/html":
            patch = _fetch_patcher(token)
            body = response.get_data(as_text=True)
            match = _BODY_TAG.search(body)
            if match:
                body = body[: match.start()] + patch + body[match.start():]
            else:
                body += patch
            response.set_data(body)
        return response

    @app.context_processor
    def _csrf_template_helper():  # noqa: ANN001
        return {"csrf_token": lambda: session.get(CSRF_KEY, "")}


# ── Server-side sessions (CC-06 / CC-02) ───────────────────────────────

class RedisSession(dict, SessionMixin):
    """Server-side session: the browser only ever holds ``sid``."""

    def __init__(self, initial=None, sid=None):
        super().__init__(initial or {})
        self.sid = sid


class RedisSessionInterface(SecureCookieSessionInterface):
    """Flask session interface backed by Redis (opt-in via REDIS_URL)."""

    def __init__(self, client, prefix: str = "hrms:session:"):
        self.client = client
        self.prefix = prefix

    # -- helpers -------------------------------------------------------
    def _key(self, sid):
        return self.prefix + sid

    def open_session(self, app, request):
        sid = request.cookies.get(self.get_cookie_name(app))
        if not sid:
            return RedisSession()
        try:
            raw = self.client.get(self._key(sid))
        except Exception as exc:  # Redis unreachable -> fail closed on data
            logger.error("Redis session read failed: %s", exc)
            return RedisSession()
        if not raw:
            return RedisSession()
        try:
            data = json.loads(raw)
        except Exception:
            return RedisSession()
        return RedisSession(data, sid=sid)

    def save_session(self, app, session, response):
        name = self.get_cookie_name(app)
        domain = self.get_cookie_domain(app)
        path = self.get_cookie_path(app)
        secure = self.get_cookie_secure(app)
        httponly = self.get_cookie_httponly(app)
        samesite = self.get_cookie_samesite(app)
        ttl = int(app.config["PERMANENT_SESSION_LIFETIME"].total_seconds())

        sid = getattr(session, "sid", None)

        if not session:
            # Cleared (e.g. logout): drop the server copy and the cookie.
            if session.modified and sid:
                try:
                    self.client.delete(self._key(sid))
                except Exception as exc:
                    logger.error("Redis session delete failed: %s", exc)
            if sid:
                response.delete_cookie(name, domain=domain, path=path)
            return

        if not sid:
            sid = secrets.token_urlsafe(32)
            session.sid = sid
        try:
            self.client.set(self._key(sid), json.dumps(dict(session)), ex=ttl)
        except Exception as exc:
            logger.error("Redis session write failed: %s", exc)
            return

        response.set_cookie(
            name,
            sid,
            max_age=ttl,
            expires=self.get_expiration_time(app, session),
            path=path,
            domain=domain,
            secure=secure,
            httponly=httponly,
            samesite=samesite,
        )


def maybe_enable_redis_sessions(app) -> bool:
    """Switch to server-side sessions when REDIS_URL is configured.

    Returns True when Redis sessions are active. A failed connection is
    logged and falls back to signed cookies rather than taking the app down.
    """
    url = os.getenv("REDIS_URL")
    if not url:
        return False
    import redis as redis_lib

    client = redis_lib.Redis.from_url(url, decode_responses=True)
    try:
        client.ping()
    except Exception as exc:
        logger.error(
            "REDIS_URL is set but unreachable (%s) — keeping cookie sessions", exc
        )
        return False
    app.session_interface = RedisSessionInterface(client)
    logger.info("Server-side sessions enabled (Redis)")
    return True
