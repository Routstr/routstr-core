"""Encrypt/hash/fingerprint helpers for secrets at rest (issue #553).

Thin wrapper over ``cryptography`` so nothing else in the codebase touches
Fernet/scrypt/HMAC directly:

- :func:`encrypt`/:func:`decrypt` — Fernet symmetric encryption, keyed by the
  mandatory ``ROUTSTR_SECRET_KEY``. Ciphertext is self-describing (``fernet:v1:``
  prefix) so a value can be told apart from legacy plaintext and so reading it
  under the wrong key surfaces as a hard error rather than silent corruption.
- :func:`hash_password`/:func:`verify_password` — salted scrypt hashing. This is
  *key-independent*: it never reads ``ROUTSTR_SECRET_KEY``, so password login and
  the recovery script keep working even when the key is missing.

A missing or malformed ``ROUTSTR_SECRET_KEY`` fails fast with the generation
command in the message.
"""

import base64
import hashlib
import hmac
import os
import secrets

from cryptography.fernet import Fernet

_PREFIX = "fernet:v1:"
_GEN_COMMAND = (
    'python -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())"'
)

# Minimum admin-password length, enforced wherever a password is set/changed
# (admin endpoints + the recovery script) so the policy lives in one place.
MIN_PASSWORD_LENGTH = 8

# scrypt parameters; packed into each hash so verification is parameter-free.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_SALT_BYTES = 16


def _require_secret_key() -> str:
    key = os.environ.get("ROUTSTR_SECRET_KEY")
    if not key:
        raise RuntimeError(
            "ROUTSTR_SECRET_KEY is not set. It is required to encrypt secrets at "
            "rest. Generate one with:\n    " + _GEN_COMMAND
        )
    return key


def get_fernet() -> Fernet:
    """Build a :class:`Fernet` from ``ROUTSTR_SECRET_KEY`` (fails fast)."""
    key = _require_secret_key()
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            "ROUTSTR_SECRET_KEY is malformed; it must be a url-safe base64 "
            "32-byte Fernet key. Generate one with:\n    " + _GEN_COMMAND
        ) from exc


def encrypt(plaintext: str) -> str:
    """Encrypt ``plaintext`` into a self-describing ``fernet:v1:`` token."""
    token = get_fernet().encrypt(plaintext.encode()).decode()
    return _PREFIX + token


def is_encrypted(value: str) -> bool:
    """True if ``value`` carries the ``fernet:v1:`` prefix this module emits."""
    return value.startswith(_PREFIX)


def decrypt(ciphertext: str) -> str:
    """Decrypt a ``fernet:v1:`` token.

    Raises ``ValueError`` for an unprefixed value (so legacy plaintext is never
    mistaken for ciphertext) and ``InvalidToken`` when the value was written
    under a different ``ROUTSTR_SECRET_KEY``.
    """
    if not is_encrypted(ciphertext):
        raise ValueError("value is not fernet:v1: ciphertext")
    token = ciphertext[len(_PREFIX) :]
    return get_fernet().decrypt(token.encode()).decode()


def hash_password(password: str) -> str:
    """Salted scrypt hash, self-describing as ``scrypt:n:r:p:salt:hash``."""
    salt = secrets.token_bytes(_SCRYPT_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return ":".join(
        [
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            base64.b64encode(salt).decode(),
            base64.b64encode(derived).decode(),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of ``password`` against a :func:`hash_password` value."""
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split(":")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        derived = hashlib.scrypt(
            password.encode(),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived, expected)
