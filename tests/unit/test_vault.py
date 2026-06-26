"""Tests for ``routstr.core.vault`` — the secret encrypt/hash/fingerprint helpers.

Specifies the primitives that the rest of the secret-storage work (issue #553)
builds on, independent of any database or app wiring:

- ``encrypt``/``decrypt`` — Fernet symmetric encryption emitting self-describing
  ``fernet:v1:`` ciphertext, so a value can be told apart from legacy plaintext
  and from ciphertext written under a different ``ROUTSTR_SECRET_KEY`` (which
  surfaces as a hard ``InvalidToken`` rather than silent corruption).
- ``hash_password``/``verify_password`` — salted scrypt hashing that is
  *key-independent* (does not depend on ``ROUTSTR_SECRET_KEY``), so password
  login and the recovery script keep working even if the key is lost.
- a missing/malformed ``ROUTSTR_SECRET_KEY`` fails fast with the generation
  command in the message.
"""

import pytest
from cryptography.fernet import InvalidToken

from routstr.core import vault

# Two distinct, valid Fernet keys held fixed so ciphertext/fingerprints are
# reproducible across runs and we can exercise the wrong-key path.
KEY_A = "l_Tkp-7xmjcQ-IFhr6qhILrU8HPRbEmYMrfSbo_5srU="
KEY_B = "_Teyrky_iToeDK51Tj1FsI9MJ340_cqKGmeher-a7MQ="


def _use_key(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    monkeypatch.setenv("ROUTSTR_SECRET_KEY", key)


# --- encrypt / decrypt -----------------------------------------------------


def test_encrypt_decrypt_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_key(monkeypatch, KEY_A)
    assert vault.decrypt(vault.encrypt("nsec1secret")) == "nsec1secret"


def test_encrypt_emits_self_describing_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_key(monkeypatch, KEY_A)
    assert vault.encrypt("x").startswith("fernet:v1:")


def test_encrypt_is_non_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fernet embeds a random IV/timestamp: equal plaintext -> different
    # ciphertext. This is exactly why upstream-key equality needs a blind index.
    _use_key(monkeypatch, KEY_A)
    assert vault.encrypt("same") != vault.encrypt("same")


def test_is_encrypted_distinguishes_ciphertext_from_plaintext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_key(monkeypatch, KEY_A)
    assert vault.is_encrypted(vault.encrypt("x")) is True
    assert vault.is_encrypted("sk-plaintext-api-key") is False
    assert vault.is_encrypted("") is False


def test_decrypt_rejects_unprefixed_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Guards the migration paths: a legacy plaintext value must never be
    # mistaken for ciphertext and "decrypted".
    _use_key(monkeypatch, KEY_A)
    with pytest.raises(ValueError):
        vault.decrypt("not-encrypted")


def test_decrypt_with_wrong_key_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The fail-fast signal: ciphertext written under KEY_A cannot be read under
    # KEY_B -> InvalidToken (bootstrap turns this into a clear startup error).
    _use_key(monkeypatch, KEY_A)
    token = vault.encrypt("secret")
    _use_key(monkeypatch, KEY_B)
    with pytest.raises(InvalidToken):
        vault.decrypt(token)


# --- password hashing (key-independent) ------------------------------------


def test_hash_and_verify_password(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_key(monkeypatch, KEY_A)
    stored = vault.hash_password("correct horse")
    assert vault.verify_password("correct horse", stored) is True
    assert vault.verify_password("wrong", stored) is False


def test_password_hash_is_salted(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_key(monkeypatch, KEY_A)
    a = vault.hash_password("pw")
    b = vault.hash_password("pw")
    assert a != b
    assert vault.verify_password("pw", a) is True
    assert vault.verify_password("pw", b) is True


def test_verify_password_rejects_malformed_stored_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A garbage or non-scrypt stored value must verify to False, never raise.
    _use_key(monkeypatch, KEY_A)
    assert vault.verify_password("pw", "") is False
    assert vault.verify_password("pw", "not-a-hash") is False
    assert vault.verify_password("pw", "bcrypt:1:2:3:x:y") is False


def test_password_hashing_is_key_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # scrypt does not use ROUTSTR_SECRET_KEY, so login and the recovery script
    # work even when the key is missing.
    monkeypatch.delenv("ROUTSTR_SECRET_KEY", raising=False)
    stored = vault.hash_password("pw")
    assert vault.verify_password("pw", stored) is True


# --- fail-fast on missing/malformed key ------------------------------------


def test_missing_key_fails_fast_with_generation_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ROUTSTR_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError) as exc:
        vault.encrypt("x")
    msg = str(exc.value)
    assert "ROUTSTR_SECRET_KEY" in msg
    assert "Fernet.generate_key" in msg


def test_malformed_key_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROUTSTR_SECRET_KEY", "not-a-valid-fernet-key")
    with pytest.raises(RuntimeError):
        vault.encrypt("x")
