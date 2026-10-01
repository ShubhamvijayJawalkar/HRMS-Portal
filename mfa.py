"""FR-AUTH-11 — TOTP multi-factor authentication.

The canonical schema shipped ``mfa_credentials`` (an encrypted secret, unique
per employee, an ``enabled`` flag) and **no code anywhere read or wrote it** —
no enrolment, no challenge, no gate. This module is that missing service layer.

Two policy decisions, recorded here because the SRS asks for MFA without
answering either:

**Who must enrol.** :data:`MANDATORY_ROLES` — Admin, Super Admin, HR and
Finance. Everyone else may enrol from their profile but is not stopped if they
do not. MFA is worth most exactly where a single compromised password reaches
the whole company, and making it a product decision to opt in for every employee
would ship a control most of the workforce would route around.

**How a locked-out user returns.** An audited Admin reset
(``POST /api/admin/users/<emp_id>/mfa/reset``), *not* self-service recovery
codes. This is the weaker answer and it is worth being honest about why: a lost
phone then needs an Admin, and the reset route is a social-engineering target,
because "reset my MFA" is exactly what an attacker asks for after stealing a
password. Two things follow from that and are implemented deliberately: the reset
does not disclose whether the target actually had MFA enabled (the response is
identical either way, so it cannot be used to enumerate who is protected), and it
is audited with before/after. Recovery codes need a schema column and were left
for a second pass rather than half-built here.

Enrolment is two-phase and the phase matters. ``POST /api/mfa/enrol`` writes a
row with ``enabled = FALSE`` and returns the secret; only a correct code from
the authenticator flips it to TRUE. A row that exists but is not enabled is
therefore *not* a credential: it cannot be used to complete a login challenge,
so an attacker who reaches the enrol endpoint with a stolen password still
cannot get in — the worst they can do is destroy the target's enrolment, which
an audited Admin reset is the remedy for. Writing the row before the code
arrives is what makes the confirm step stateless; holding the secret in the
session instead would put a live authenticator secret in a cookie.
"""

from __future__ import annotations

import io
import logging
import os
from datetime import datetime, timedelta

import pyotp

logger = logging.getLogger(__name__)

# Roles that cannot use the application without a second factor. See the module
# docstring for why this set and not "everyone".
MANDATORY_ROLES = frozenset({'Admin', 'Super Admin', 'HR', 'Finance'})

ISSUER = 'HRMS Portal'

# A 6-digit TOTP code is 10^6 possibilities and a valid_window of 1 admits 3 of
# them at any instant (clock drift either side of the counter), so 10^6 / 3 is
# ~333k attempts per code — with no attempt limit that is a real brute-force
# route, not a theoretical one. Five attempts per challenge is the cap; the code
# is only valid for 30s, so an attacker gets ~16 codes to work through.
MAX_CHALLENGE_ATTEMPTS = 5

# A pending password step (or a fresh enrolment) is short-lived on purpose: it
# holds the right to complete a login, so it must not outlive the interaction.
PENDING_TTL = timedelta(minutes=10)

_MISSING_KEY = (
    'MFA_ENCRYPTION_KEY is not set, so authenticator secrets cannot be '
    'encrypted at rest. Refusing to run MFA rather than storing them in the '
    'clear.'
)


def requires_enrolment(role: str | None) -> bool:
    """Must this role have a second factor before it can use the app?"""
    return role in MANDATORY_ROLES


# ── Secret encryption ──────────────────────────────────────────────────────
# The secret is a bearer credential: anyone holding it can mint a valid code
# forever. `secret_encrypted` in the schema is NOT NULL, so there was never an
# intent to store it in the clear. The key is a dedicated env var rather than a
# derivation from SECRET_KEY, for the ordinary reason: key separation, so that
# rotating the session/cookie signing key does not require re-encrypting every
# authenticator, and a compromise of one does not imply the other.

def _fernet():
    from cryptography.fernet import Fernet, InvalidToken

    raw = (os.getenv('MFA_ENCRYPTION_KEY') or '').strip()
    if not raw:
        raise RuntimeError(_MISSING_KEY)
    try:
        return Fernet(raw.encode()), InvalidToken
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            'MFA_ENCRYPTION_KEY is not a valid Fernet key. Generate one with '
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        ) from exc


def encrypt_secret(secret: str) -> str:
    fernet, _ = _fernet()
    return fernet.encrypt(secret.encode()).decode()


def decrypt_secret(stored: str) -> str:
    fernet, InvalidToken = _fernet()
    try:
        return fernet.decrypt(stored.encode()).decode()
    except InvalidToken:
        # Not recoverable: an authenticator secret that will not decrypt has to
        # be re-enrolled, so this is a state change the caller must handle, not
        # a silent empty string that would make every code fail with no clue.
        raise RuntimeError(
            'Stored MFA secret could not be decrypted with MFA_ENCRYPTION_KEY. '
            'Re-enrolment is required; an Admin reset does it.'
        ) from None


# ── Code verification ───────────────────────────────────────────────────────

def generate_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(emp_id: str, name: str, secret: str) -> str:
    """The ``otpauth://`` URI an authenticator app scans or is pasted."""
    return pyotp.TOTP(secret).provisioning_uri(name=name or emp_id, issuer_name=ISSUER)


def qr_png(uri: str):
    """The provisioning URI as a PNG.

    Enrolment by transcribing a base32 secret is the step where people give up
    and fall back to "I'll do it later", which for a mandatory role means they
    cannot work at all. A scannable code is not a nicety here.
    """
    import qrcode

    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


def verify_code(secret: str, code) -> bool:
    """Is ``code`` currently valid for ``secret``?

    ``valid_window=1`` tolerates one step (30s) of clock drift in either
    direction. Without it a correct code is refused whenever the phone and the
    server straddle a step boundary, which reads to the user as "my code is
    wrong" and trains people to type the next code instead.
    """
    if not code:
        return False
    digits = str(code).strip().replace(' ', '')
    # Exactly 6 digits. pyotp would return False for other lengths anyway, but
    # rejecting them here keeps a wrong-shape input from consuming an attempt
    # and reads clearly in the log.
    if len(digits) != 6 or not digits.isdigit():
        return False
    return bool(pyotp.TOTP(secret).verify(digits, valid_window=1))


# ── Storage ─────────────────────────────────────────────────────────────────
# NOTE: every write below names its columns explicitly. A bare
# `INSERT INTO mfa_credentials VALUES (...)` would be the defect that made
# POST /api/goals return 500 on every backend.

def status_for(conn, emp_id: str) -> dict:
    """What the UI needs to know: enrolled, and whether it is compulsory."""
    row = conn.execute(
        "SELECT enabled FROM mfa_credentials WHERE emp_id = ?", [emp_id]
    ).fetchone()
    user = conn.execute("SELECT role FROM users WHERE emp_id = ?", [emp_id]).fetchone()
    role = user[0] if user else None
    return {
        'emp_id': emp_id,
        'role': role,
        'enrolled': bool(row and row[0]),
        'mandatory': requires_enrolment(role),
    }


def is_enrolled(conn, emp_id: str) -> bool:
    row = conn.execute(
        "SELECT enabled FROM mfa_credentials WHERE emp_id = ?", [emp_id]
    ).fetchone()
    return bool(row and row[0])


def begin_enrolment(conn, emp_id: str, name: str) -> dict:
    """Create (or re-create) an unconfirmed enrolment and return its secret.

    A user who is already enrolled cannot re-run this: silently replacing a
    working secret is a denial-of-service that needs no credentials beyond a
    stolen password. Disabling an optional factor and starting again is the
    supported route; a mandatory one goes through the Admin reset.
    """
    if is_enrolled(conn, emp_id):
        raise EnrolmentConflict(
            'Multi-factor authentication is already enabled. Disable it first, '
            'or ask an administrator to reset it.'
        )
    secret = generate_secret()
    uri = provisioning_uri(emp_id, name, secret)
    encrypted = encrypt_secret(secret)
    conn.execute("DELETE FROM mfa_credentials WHERE emp_id = ?", [emp_id])
    # `cred_id` is allocated explicitly. On the compatibility schema it is a bare
    # `INTEGER PRIMARY KEY` with no default and no sequence, so omitting the
    # column inserts NULL and PostgreSQL rejects that outright; on v2.0 it is
    # `GENERATED BY DEFAULT AS IDENTITY`, which accepts an explicit value — and
    # `_next_generated_id` consumes that identity's own sequence rather than
    # picking a number itself, so an explicit id can never leave the sequence
    # behind the data (CC-01).
    #
    # `enabled` is written as an int, not TRUE/FALSE. The flag is INTEGER on the
    # compatibility schema and BOOLEAN on v2.0 `public`, and db_backend rewrites
    # 0/1 to false/true for the columns that are actually boolean — so one value
    # serves both. Writing the SQL literal TRUE would be rejected outright by
    # PostgreSQL on the INTEGER column.
    from app import _next_generated_id  # lazy: app imports this module

    cred_id = _next_generated_id(conn, 'mfa_credentials', 'cred_id')
    conn.execute(
        "INSERT INTO mfa_credentials (cred_id, emp_id, secret_encrypted, enabled) "
        "VALUES (?, ?, ?, 0)",
        [cred_id, emp_id, encrypted],
    )
    return {'secret': secret, 'uri': uri}


def confirm_enrolment(conn, emp_id: str, code) -> bool:
    """Turn an unconfirmed enrolment live. False = the code did not match."""
    row = conn.execute(
        "SELECT secret_encrypted, enabled FROM mfa_credentials WHERE emp_id = ?",
        [emp_id],
    ).fetchone()
    if not row:
        return False
    if row[1]:
        # Already confirmed. Confirming twice is a benign double-click rather
        # than a failure, so the caller gets the outcome it wanted either way.
        #
        # This check is deliberately *only* on the truthy side. The natural
        # shorthand — `if not row or not row[1]` — folds "not yet confirmed"
        # into the same branch, and since an unconfirmed row has `enabled = 0`
        # that returns False without ever looking at the code: every correct
        # first-time confirmation is rejected, and the error reads as "that code
        # is not correct" when the code was right.
        return True
    if not verify_code(decrypt_secret(row[0]), code):
        return False
    # Conditional on `enabled = 0` so two concurrent confirms converge rather
    # than both reporting success for a write only one of them performed.
    conn.execute(
        "UPDATE mfa_credentials SET enabled = 1 WHERE emp_id = ? AND enabled = 0",
        [emp_id],
    )
    return True


def verify_challenge(conn, emp_id: str, code) -> bool:
    """Check a login challenge code for an already-enrolled employee."""
    row = conn.execute(
        "SELECT secret_encrypted FROM mfa_credentials "
        "WHERE emp_id = ? AND enabled = 1",
        [emp_id],
    ).fetchone()
    if not row:
        return False
    try:
        secret = decrypt_secret(row[0])
    except RuntimeError:
        # An undecryptable secret is an operator problem (wrong key), not a
        # wrong code. Failing closed is the only safe direction, and it is
        # logged at error level so it is visible.
        logger.error('MFA secret for %s could not be decrypted', emp_id)
        return False
    if verify_code(secret, code):
        conn.execute(
            "UPDATE mfa_credentials SET last_used_at = ? WHERE emp_id = ?",
            [datetime.now(), emp_id],
        )
        return True
    return False


def disable(conn, emp_id: str) -> bool:
    """Remove the credential. False = there was none."""
    existed = conn.execute(
        "SELECT 1 FROM mfa_credentials WHERE emp_id = ?", [emp_id]
    ).fetchone()
    conn.execute("DELETE FROM mfa_credentials WHERE emp_id = ?", [emp_id])
    return bool(existed)


def reset(conn, emp_id: str) -> bool:
    """Admin recovery: drop the credential so the target can enrol again.

    Same effect as :func:`disable` but a separate name, because the audit record
    and the caller are different — this one is a support action on someone
    else's account, and a reader of the log should be able to tell that from a
    user turning their own factor off.
    """
    return disable(conn, emp_id)


# ── Pending-login state ─────────────────────────────────────────────────────

def start_pending(session, emp_id: str, name: str, role: str, step: str,
                  department: str = '') -> None:
    """Record that the password step passed but the second factor has not.

    The identity is stored under ``mfa_pending`` and **not** as the session's
    ``emp_id``. That is deliberate and is the hinge of the whole flow:
    ``login_required`` and every role gate key off ``session['emp_id']``, so a
    half-authenticated session is refused everywhere by construction, rather
    than each route having to remember to check for it.

    One consequence worth stating, because it looks like a bug and is not: a
    gate denial calls ``session.clear()``, which also discards ``mfa_pending``.
    A user who left a stale authenticated tab open while signing in elsewhere
    therefore loses the parked step and is asked to sign in again. That is the
    right direction — a parked password step is a live credential, and the
    cheapest way to bound its life is to let any refusal end it.
    """
    session['mfa_pending'] = {
        'emp_id': emp_id,
        'name': name,
        'role': role,
        'department': department or '',
        'step': step,
        'attempts': 0,
        'started': datetime.now().isoformat(),
    }


def pending(session) -> dict | None:
    """The pending MFA state, or None if there is none or it has expired."""
    state = session.get('mfa_pending')
    if not state:
        return None
    try:
        started = datetime.fromisoformat(state.get('started', ''))
    except (TypeError, ValueError):
        return None
    if datetime.now() - started > PENDING_TTL:
        return None
    return state


def clear_pending(session) -> None:
    session.pop('mfa_pending', None)


def note_wrong_code(session) -> tuple[int, bool]:
    """Record a wrong code. Returns ``(attempts, locked)``.

    The cap is applied *here* rather than at each call site because both the
    challenge and the confirmation step need it, and two copies of
    "count it, compare it, maybe throw the login away" is how a route ends up
    answering 401 on the sixth attempt instead of 429.
    """
    state = session.get('mfa_pending') or {}
    state['attempts'] = int(state.get('attempts', 0)) + 1
    attempts = state['attempts']
    locked = attempts >= MAX_CHALLENGE_ATTEMPTS
    if locked:
        # The parked password step is the credential being brute-forced, so it
        # goes with the attempt counter. Retrying needs the password again.
        clear_pending(session)
    else:
        session['mfa_pending'] = state
    return attempts, locked


class EnrolmentConflict(Exception):
    """Already enrolled — the caller turns this into a 409."""
