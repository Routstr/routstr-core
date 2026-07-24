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

from pathlib import Path

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


def test_decrypt_without_any_key_fails_fast_with_generation_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Reading secrets is strict: with no key in env AND no key file, decrypt
    # fails fast with the generation command rather than silently minting a new
    # key (a fresh key could never match already-encrypted ciphertext). Only the
    # encrypt path auto-provisions; the read path never does.
    monkeypatch.delenv("ROUTSTR_SECRET_KEY", raising=False)
    monkeypatch.setenv("ROUTSTR_SECRET_KEY_FILE", str(tmp_path / "absent.key"))
    with pytest.raises(RuntimeError) as exc:
        vault.decrypt("fernet:v1:not-real-ciphertext")
    msg = str(exc.value)
    assert "ROUTSTR_SECRET_KEY" in msg
    assert "Fernet.generate_key" in msg


def test_malformed_key_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROUTSTR_SECRET_KEY", "not-a-valid-fernet-key")
    with pytest.raises(RuntimeError):
        vault.encrypt("x")


def test_malformed_env_key_does_not_self_provision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A malformed env key is an operator mistake, not an unset key: it must fail
    # loudly, never silently generate a different key to a file (which would hide
    # the mistake and could brick secrets the operator meant to key differently).
    monkeypatch.setenv("ROUTSTR_SECRET_KEY", "not-a-valid-fernet-key")
    key_file = tmp_path / "routstr_secret.key"
    monkeypatch.setenv("ROUTSTR_SECRET_KEY_FILE", str(key_file))
    with pytest.raises(RuntimeError):
        vault.encrypt("x")
    assert not key_file.exists()


# --- auto-provisioned key file (non-breaking upgrade path) -----------------


@pytest.fixture
def generated_key_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """No env key; the key file points at a fresh, empty tmp location.

    Exercises what an existing node hits when it upgrades without setting
    ROUTSTR_SECRET_KEY: the master key is auto-generated and persisted here so
    boot does not break, while secrets are still never written in plaintext.
    """
    monkeypatch.delenv("ROUTSTR_SECRET_KEY", raising=False)
    key_file = tmp_path / "routstr_secret.key"
    monkeypatch.setenv("ROUTSTR_SECRET_KEY_FILE", str(key_file))
    return key_file


def test_encrypt_without_key_generates_and_persists_key_file(
    generated_key_file: Path,
) -> None:
    # Encryption at rest stays mandatory, but a missing key is provisioned rather
    # than fatal: encrypt generates a key, writes it to the key file (owner-only),
    # and the value round-trips — decrypt, still with no env key, reads the same
    # file key back.
    key_file = generated_key_file
    assert not key_file.exists()

    token = vault.encrypt("nsec1secret")

    assert token.startswith("fernet:v1:")
    assert key_file.exists()
    assert key_file.stat().st_mode & 0o077 == 0  # not group/other-accessible
    assert vault.decrypt(token) == "nsec1secret"


def test_generated_key_warns_operator_with_path_not_value(
    generated_key_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The notice names the file to back up and shouts the back-up imperative so an
    # upgrading operator cannot miss it, but it MUST NOT echo the key value: the
    # secret lives in the 0600 file, and printing it would leak it into captured
    # stdout / aggregated container logs.
    key_file = generated_key_file
    vault.encrypt("x")

    out = capsys.readouterr().out
    assert "ROUTSTR_SECRET_KEY" in out
    assert str(key_file) in out
    assert key_file.read_text().strip() not in out
    assert "BACK UP" in out.upper()


def test_existing_key_file_is_reused_and_warns_only_once(
    generated_key_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Once the key exists, later encrypts reuse it (never rotate a key that
    # secrets were already encrypted under) and stay silent (no repeated notice).
    key_file = generated_key_file
    vault.encrypt("a")
    first_key = key_file.read_text()
    capsys.readouterr()  # drain the one-time notice

    vault.encrypt("b")

    assert key_file.read_text() == first_key
    assert capsys.readouterr().out == ""


def test_generated_key_is_published_atomically(
    generated_key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The key must appear at its final path only as a complete file: it is written
    # to a temp file and atomically linked into place. If the publish (link) step
    # fails — e.g. the process crashes — the destination must be absent, never a
    # half-written or empty file that a later boot would read as a corrupt key and
    # then refuse to decrypt every secret. No temp debris is left behind.
    key_file = generated_key_file

    def boom(src: object, dst: object) -> None:
        raise OSError("crash during atomic publish")

    monkeypatch.setattr(vault.os, "link", boom)

    with pytest.raises(OSError):
        vault.encrypt("x")

    assert not key_file.exists()
    assert list(key_file.parent.iterdir()) == []


def test_racing_worker_adopts_winners_key_without_clobber(
    generated_key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two workers auto-generate a key concurrently. The first to link "wins" and
    # its key is the one on disk; a worker that loses the link race must adopt the
    # winner's key — secrets may already be encrypted under it — rather than
    # clobber it or crash. Force the race deterministically: the winner publishes
    # its key at the final path just as this worker tries to link, so os.link
    # raises FileExistsError and the loser reads the winner's key back.
    key_file = generated_key_file

    def winner_links_first(src: object, dst: object) -> None:
        key_file.write_text(KEY_A)  # the winner's already-published key
        raise FileExistsError

    monkeypatch.setattr(vault.os, "link", winner_links_first)

    token = vault.encrypt("secret")

    # The winner's key stays put and the loser encrypted under it, so the value
    # round-trips under KEY_A even though this worker had generated its own key.
    assert key_file.read_text().strip() == KEY_A
    assert vault.decrypt(token) == "secret"
    # The losing worker left no temp debris behind.
    assert [p.name for p in key_file.parent.iterdir()] == [key_file.name]


def test_loose_key_file_perms_are_tightened_on_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A key file left group/other-readable (e.g. written under a loose umask
    # before this hardening, or by a careless operator) is a leaked-secret risk.
    # Reading it repairs the permissions to owner-only rather than trusting a
    # world-readable master key, while still using the key so boot is not broken.
    monkeypatch.delenv("ROUTSTR_SECRET_KEY", raising=False)
    key_file = tmp_path / "routstr_secret.key"
    key_file.write_text(KEY_A)
    key_file.chmod(0o644)
    monkeypatch.setenv("ROUTSTR_SECRET_KEY_FILE", str(key_file))

    token = vault.encrypt("secret")  # reads the loose file, repairs its perms

    assert key_file.stat().st_mode & 0o077 == 0
    assert vault.decrypt(token) == "secret"


def test_env_key_takes_precedence_over_key_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An explicit env key wins over a persisted file key (an operator-supplied
    # key from a secrets manager overrides the auto-generated one), and the file
    # is left untouched.
    key_file = tmp_path / "routstr_secret.key"
    key_file.write_text(KEY_B)
    monkeypatch.setenv("ROUTSTR_SECRET_KEY_FILE", str(key_file))
    monkeypatch.setenv("ROUTSTR_SECRET_KEY", KEY_A)

    token = vault.encrypt("x")

    assert vault.decrypt(token) == "x"  # env key (A) decrypts it
    monkeypatch.setenv("ROUTSTR_SECRET_KEY", KEY_B)
    with pytest.raises(InvalidToken):
        vault.decrypt(token)  # the file key (B) does not
    assert key_file.read_text() == KEY_B  # file key never used or overwritten


def test_generated_key_defaults_beside_the_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # With no env key and no explicit key-file path, the key is provisioned next
    # to the SQLite database, so it rides whatever volume already persists the
    # data instead of landing in the working directory (where a container
    # recreate would lose it and brick decryption).
    monkeypatch.delenv("ROUTSTR_SECRET_KEY", raising=False)
    monkeypatch.delenv("ROUTSTR_SECRET_KEY_FILE", raising=False)
    monkeypatch.chdir(tmp_path)  # isolate the working-dir fallback from the repo
    db_dir = tmp_path / "data"
    db_dir.mkdir()
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_dir}/routstr.db")

    token = vault.encrypt("beside-the-db")

    assert (db_dir / "routstr_secret.key").exists()
    assert not (tmp_path / "routstr_secret.key").exists()  # not the working dir
    assert vault.decrypt(token) == "beside-the-db"
