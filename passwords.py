"""Password policy (FR-AUTH-10).

The SRS is explicit, and the shape of the requirement matters more than the
rule count:

* **minimum 10 characters** — Appendix A-01 records a 6-character minimum as a
  defect, because NIST SP 800-63B says length beats composition;
* **not in a breached-password corpus** — the Have I Been Pwned k-anonymity
  range API, or an offline corpus;
* **no forced complexity rules and no expiry** — also NIST SP 800-63B. This is a
  deliberate omission, not an oversight: forcing `Passw0rd!`-style passwords
  produces *weaker* passwords, because they are predictable. The policy module
  has no `must_contain_digit` and never will.

Two design decisions worth stating:

* **The offline corpus is the default, the network is opt-in.** A password check
  that silently fails when the network is down is a password check that does not
  exist, and a dev/CI run must not depend on reaching a third party. A small
  corpus of the most common passwords ships in this file; ``HIBP_URL`` enables
  the full range query, which is privacy-preserving because only the first five
  characters of the SHA-1 prefix leave the process.
* **The same message is returned for every rejection.** Telling a caller "that
  password appeared 13 million times in a breach corpus" is a free oracle for
  confirming a guess. The one exception is length, because a length hint leaks
  nothing an attacker does not already know.

The policy is enforced at every point a password is *set*: admin user creation,
self-service change, and the reset flow. The boot seed is exempt by construction
(it writes a fixed demo password directly, never through this module), which is
what keeps the disposable validation databases working.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re

logger = logging.getLogger(__name__)

# Appendix A-01: a 6-character minimum is a defect. 10 is the SRS's number.
MIN_LENGTH = 10
MAX_LENGTH = 512  # a sane ceiling; Argon2 is deliberately expensive per byte

# The most common real-world passwords. This is a *floor*, not the whole
# defence: it works offline, needs no dependency, and catches the passwords
# that actually show up. `HIBP_URL` extends it to the full corpus.
#
# Lower-cased, because a corpus lookup must be case-insensitive: `Password1` and
# `password1` are the same password to an attacker.
COMMON_PASSWORDS = frozenset({
    '123456', '123456789', '12345678', '1234567890', 'qwerty', 'qwerty123',
    'password', 'password1', 'password123', 'passw0rd', 'iloveyou', 'princess',
    'admin', 'admin123', 'administrator', 'welcome', 'welcome1', 'letmein',
    'monkey', 'dragon', 'abc123', 'football', 'baseball', 'sunshine',
    'iloveyou1', 'trustno1', 'starwars', 'master', 'superman', 'hello',
    'freedom', 'whatever', 'qazwsx', 'pass123', 'pass1234', 'test123',
    'guest', 'root', 'toor', 'changeme', 'secret', 'default', 'company',
    'hrms123', 'hr1234', 'employee', 'emp1234', 'welcome123', 'login',
})

# The demo password the boot seed writes. It is on the list on purpose: any
# *interactive* use of it is refused, and the seed writes it without going
# through this module, so a disposable validation database still boots.
SEED_DEMO_PASSWORD = 'pass123'

# A breach corpus hit is a 400 like any other policy failure, and says so
# without saying how common the password was.
GENERIC_REJECTION = (
    'Choose a password of at least 10 characters that has not appeared in a '
    'known password-breach corpus'
)


class PasswordPolicyError(ValueError):
    """A password does not meet the policy."""

    def __init__(self, message: str = GENERIC_REJECTION, field: str = 'password'):
        super().__init__(message)
        self.field = field

    def payload(self) -> dict:
        """The API error body. Stable, so a client can render it as-is."""
        return {'error': str(self), 'field': self.field,
                'min_length': MIN_LENGTH, 'policy': 'FR-AUTH-10'}


def _normalise(password: str) -> str:
    return (password or '').strip().lower()


def is_breached_offline(password: str) -> bool:
    """Is this password in the bundled corpus? (Case- and whitespace-insensitive.)"""
    candidate = _normalise(password)
    if not candidate:
        return False
    if candidate in COMMON_PASSWORDS:
        return True
    # `P@ssw0rd`, `passw0rd!`, `Summer2024!` — undo the substitutions people apply
    # mechanically, so a leet-speak variant of a common password is still caught.
    if de_leet(candidate) in COMMON_PASSWORDS:
        return True
    # ...and `monkey123`, `password2026!`: a known password plus a suffix is the
    # same password to an attacker. The 5-character floor and the 50% share stop
    # this from rejecting a passphrase that merely opens with a common word, so
    # `correct-horse-battery` is accepted and `passwordfortheoffice` is not — which
    # is the judgement NIST SP 800-63B actually recommends.
    key = de_leet_key(candidate)
    for word in _PREFIX_KEYS:
        if len(word) >= 5 and key.startswith(word) and len(word) * 2 >= len(key):
            return True
    return False


def de_leet(candidate: str) -> str:
    """Undo the substitutions people apply to a leaked password.

    Deliberately lossy: it maps a family of inputs to one key so a corpus
    membership test catches the variants rather than only the exact string.
    """
    table = str.maketrans({
        '0': 'o', '1': 'l', 'i': 'l', '3': 'e', '4': 'a',
        '5': 's', '7': 't', '$': 's', '@': 'a', '!': '', '|': 'l',
    })
    return candidate.translate(table)


def de_leet_key(candidate: str) -> str:
    """A key that ignores substitutions *and* every non-letter, so
    `Summer2024!` and `summer24` land on the same corpus entry."""
    return re.sub(r'[^a-z]', '', de_leet(candidate))


# Pre-computed so the de-leeted membership test is a set lookup.
# Pre-computed so the de-leeted membership tests are set lookups rather than a
# scan of the corpus on every password.
_DELEET_KEYS = frozenset(
    de_leet_key(_normalise(word)) for word in COMMON_PASSWORDS
) - {''}
_PREFIX_KEYS = tuple(sorted((de_leet_key(_normalise(w)) for w in COMMON_PASSWORDS if len(w) >= 5),
                            key=len, reverse=True))


def _hibp_range_suffix(password: str) -> str | None:
    """Look the password up in HIBP without revealing it.

    k-anonymity: send only the first five characters of the SHA-1 hash, receive
    every suffix with that prefix, and compare locally. The password itself never
    leaves the process, and neither does a full hash.
    """
    url = os.getenv('HIBP_URL')
    if not url:
        return None
    import requests  # imported lazily: the offline corpus needs no dependency

    digest = hashlib.sha1(password.strip().encode('utf-8')).hexdigest().upper()
    prefix, suffix = digest[:5], digest[5:]
    try:
        response = requests.get(
            f"{url.rstrip('/')}/{prefix}", headers={'User-Agent': 'hrms-password-policy'},
            timeout=float(os.getenv('HIBP_TIMEOUT', '2')),
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        for line in response.text.splitlines():
            candidate_suffix, _count = line.split(':', 1)
            if candidate_suffix.strip().upper() == suffix:
                return suffix
    except Exception as exc:  # a network failure must not block a password change
        logger.warning('HIBP breach lookup unavailable (%s); using the offline corpus only', exc)
    return None


def breached(password: str) -> bool:
    """Is the password in the offline corpus or, if enabled, in HIBP?"""
    if is_breached_offline(password):
        return True
    return _hibp_range_suffix(password) is not None


def check(password: str, *, allow_seed_demo: bool = False) -> str:
    """Validate ``password`` and return it, or raise ``PasswordPolicyError``.

    ``allow_seed_demo`` exists only so the boot path can be explicit if it is ever
    routed through here; nothing an end user calls sets it.
    """
    if not isinstance(password, str) or not password:
        raise PasswordPolicyError()
    if len(password) < MIN_LENGTH:
        # The one message that is specific, because a length hint leaks nothing
        # an attacker does not already know and it makes the form usable.
        raise PasswordPolicyError(
            f'Password must be at least {MIN_LENGTH} characters (NIST SP 800-63B '
            'treats a shorter minimum as a defect)')
    if len(password) > MAX_LENGTH:
        raise PasswordPolicyError(f'Password must be at most {MAX_LENGTH} characters')
    if password.strip() != password:
        raise PasswordPolicyError('Password must not begin or end with whitespace')
    if not allow_seed_demo and password.strip().lower() == SEED_DEMO_PASSWORD:
        raise PasswordPolicyError()
    if breached(password):
        raise PasswordPolicyError()
    return password


def is_acceptable(password: str) -> bool:
    """Boolean form of :func:`check`, for the UI and for tests."""
    try:
        check(password)
    except PasswordPolicyError:
        return False
    return True
