"""Encrypt/hash/fingerprint helpers for secrets at rest (issue #553).

Thin wrapper over ``cryptography`` so nothing else in the codebase touches
Fernet/scrypt/HMAC directly:

- :func:`encrypt`/:func:`decrypt` — Fernet symmetric encryption, keyed by the
  mandatory master key. Ciphertext is self-describing (``fernet:v1:`` prefix) so
  a value can be told apart from legacy plaintext and so reading it under the
  wrong key surfaces as a hard error rather than silent corruption.
- :func:`hash_password`/:func:`verify_password` — salted scrypt hashing. This is
  *key-independent*: it never reads the master key, so password login and the
  recovery script keep working even when the key is missing.

Key custody is flexible but encryption is not optional. The key comes from the
``ROUTSTR_SECRET_KEY`` env var, else a persisted key file
(``ROUTSTR_SECRET_KEY_FILE``, defaulting beside the SQLite database so it persists
on the same volume as the data); when neither is set, :func:`encrypt` generates
one to the key file and prints a one-time notice, so an existing node upgrades
without breaking instead of refusing to boot. Reading is strict — :func:`decrypt`
never generates a key (a new key could not match existing ciphertext) and fails
fast with the generation command when none is configured. A malformed
``ROUTSTR_SECRET_KEY`` is an operator error and always fails fast.
"""

import base64
import hashlib
import hmac
import os
import secrets
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

_PREFIX = "fernet:v1:"
_GEN_COMMAND = (
    'python -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())"'
)

# Where an auto-generated master key is persisted when the operator supplies no
# ``ROUTSTR_SECRET_KEY``. Defaults beside the SQLite database so it rides whatever
# volume already persists the data (a container recreate would otherwise generate
# a fresh key and be unable to decrypt existing secrets); falls back to the
# working directory when the DB location is unknown. Override the exact path with
# ``ROUTSTR_SECRET_KEY_FILE``.
_KEY_FILE_ENV = "ROUTSTR_SECRET_KEY_FILE"
_DEFAULT_KEY_FILE = "routstr_secret.key"

# Minimum admin-password length, enforced wherever a password is set/changed
# (admin endpoints + the recovery script) so the policy lives in one place.
MIN_PASSWORD_LENGTH = 8

# scrypt parameters; packed into each hash so verification is parameter-free.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_SALT_BYTES = 16


def _database_dir() -> Path | None:
    """Directory of the SQLite database file, or ``None`` when it has no on-disk
    location (a non-SQLite URL or ``:memory:``).

    Read from ``DATABASE_URL`` at call time and parsed here rather than importing
    ``routstr.core.db`` — that module builds the engine at import, which the
    crypto layer must not drag in. Mirrors db.py's ``DATABASE_URL`` default.
    """
    url_str = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///keys.db")
    try:
        url = make_url(url_str)
    except ArgumentError:
        return None
    if url.get_backend_name() != "sqlite" or not url.database:
        return None
    if url.database == ":memory:":
        return None
    return Path(url.database).parent


def _key_file_path() -> Path:
    """Where the auto-generated master key is read from / written to.

    ``ROUTSTR_SECRET_KEY_FILE`` wins; otherwise the key sits beside the SQLite
    database so it persists on the same volume as the data, falling back to the
    working directory when the DB location is unknown.
    """
    override = os.environ.get(_KEY_FILE_ENV)
    if override:
        return Path(override)
    directory = _database_dir()
    return (directory or Path()) / _DEFAULT_KEY_FILE


def _read_key_file(path: Path) -> str | None:
    try:
        stored = path.read_text().strip()
    except OSError:
        return None
    if not stored:
        return None
    _repair_key_file_perms(path)
    return stored


def _repair_key_file_perms(path: Path) -> None:
    # A master key must never be group/other-readable. On POSIX, tighten loose
    # permissions to owner-only (0600) rather than trust — or hard-fail on — a
    # world-readable key; a friendlier repair keeps an upgrading node booting.
    if os.name != "posix":
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & 0o077:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _load_secret_key() -> str | None:
    """The configured key without provisioning: env var, then the key file."""
    return os.environ.get("ROUTSTR_SECRET_KEY") or _read_key_file(_key_file_path())


def _warn_generated_key(path: Path) -> None:
    # stdout, not the logger: the operator must see this once (e.g. in
    # ``docker compose logs``), but it must never be persisted into the on-disk
    # log files the logger also writes. Mirrors the generated-admin-password
    # notice so an upgrade cannot silently create an unbacked key.
    print(
        "No ROUTSTR_SECRET_KEY was set; generated one to encrypt node secrets at "
        f"rest and saved it to {path}.\n"
        "!! BACK UP THIS FILE. If it is lost, the encrypted secrets cannot be "
        "recovered and will have to be re-entered.\n"
        "To manage the key yourself (e.g. from a secrets manager) set "
        "ROUTSTR_SECRET_KEY in the environment instead; the value is in the file "
        "above.",
        flush=True,
    )


def _generate_and_persist_key(path: Path) -> str:
    """Generate a Fernet key, persist it owner-only and atomically, warn once.

    The key is written to a temp file in the same directory, flushed durably,
    then ``os.link``-ed into place. ``os.link`` publishes the complete file in a
    single atomic step — a crash mid-write leaves only the temp file (which is
    removed), never a half-written or empty key at the final path that a later
    boot would read as corrupt. It also refuses to overwrite an existing key, so
    a racing worker that generated first keeps ownership (secrets may already be
    encrypted under its key); the loser adopts that key instead of clobbering it.
    """
    key = Fernet.generate_key().decode()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=".routstr_secret.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(key)  # mkstemp already created it 0600
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            # A concurrent worker linked its key in first; adopt theirs rather
            # than clobber a key that secrets may already be encrypted under.
            existing = _read_key_file(path)
            if existing:
                return existing
            raise
        _fsync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)
    _warn_generated_key(path)
    return key


def _fsync_dir(directory: Path) -> None:
    # Persist the new directory entry so the linked key survives a crash right
    # after publish. Best-effort: not every platform lets you fsync a directory.
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def ensure_secret_key() -> str:
    """Return the master key, provisioning one if the operator supplied none.

    Precedence: the ``ROUTSTR_SECRET_KEY`` env var, then the persisted key file,
    otherwise a freshly generated key written to the key file (with a one-time
    operator notice). This keeps encryption at rest mandatory while letting an
    existing node upgrade without setting a key first. A malformed env key is
    left to fail at :func:`get_fernet` — it is an operator error, not an unset
    key, so it must not trigger silent self-provisioning.
    """
    env_key = os.environ.get("ROUTSTR_SECRET_KEY")
    if env_key:
        return env_key
    path = _key_file_path()
    return _read_key_file(path) or _generate_and_persist_key(path)


def _fernet_from_key(key: str) -> Fernet:
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            "ROUTSTR_SECRET_KEY is malformed; it must be a url-safe base64 "
            "32-byte Fernet key. Generate one with:\n    " + _GEN_COMMAND
        ) from exc


def get_fernet() -> Fernet:
    """Build a :class:`Fernet` from the configured key (env var or key file).

    Strict: this never generates a key, so already-encrypted ciphertext is never
    shadowed by a fresh key. A read with no key configured fails fast with the
    generation command.
    """
    key = _load_secret_key()
    if not key:
        raise RuntimeError(
            "ROUTSTR_SECRET_KEY is not set. It is required to encrypt secrets at "
            "rest. Generate one with:\n    " + _GEN_COMMAND
        )
    return _fernet_from_key(key)


def encrypt(plaintext: str) -> str:
    """Encrypt ``plaintext`` into a self-describing ``fernet:v1:`` token.

    Provisions a master key (env var, key file, or a freshly generated one) so an
    upgrading node never has to set one before its first secret is stored; the
    value is always encrypted, never persisted in plaintext.
    """
    fernet = _fernet_from_key(ensure_secret_key())
    return _PREFIX + fernet.encrypt(plaintext.encode()).decode()


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
        n_int, r_int, p_int = int(n), int(r), int(p)
        # Cap the work factor at the parameters this module emits. scrypt's
        # memory cost grows with N*r, so an oversized N/r in a tampered or
        # corrupt stored hash could turn a single login into an OOM/DoS.
        if n_int > _SCRYPT_N or r_int > _SCRYPT_R or p_int > _SCRYPT_P:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        derived = hashlib.scrypt(
            password.encode(),
            salt=salt,
            n=n_int,
            r=r_int,
            p=p_int,
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived, expected)
