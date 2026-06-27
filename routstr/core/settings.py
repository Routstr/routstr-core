from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from pydantic.v1 import BaseModel, BaseSettings, Field
from sqlmodel.ext.asyncio.session import AsyncSession


class Settings(BaseSettings):
    class Config:
        case_sensitive = True

        @classmethod
        def parse_env_var(cls, field_name: str, raw_value: str) -> Any:  # type: ignore[override]
            if field_name in {"cashu_mints", "cors_origins", "relays"}:
                v = str(raw_value).strip()
                if v == "":
                    return []
                return [p.strip() for p in v.split(",") if p.strip()]
            return raw_value

    # Core
    upstream_base_url: str = Field(default="", env="UPSTREAM_BASE_URL")
    upstream_api_key: str = Field(default="", env="UPSTREAM_API_KEY")

    # Node info
    name: str = Field(default="ARoutstrNode", env="NAME")
    description: str = Field(default="A Routstr Node", env="DESCRIPTION")
    npub: str = Field(default="", env="NPUB")
    http_url: str = Field(default="", env="HTTP_URL")
    onion_url: str = Field(default="", env="ONION_URL")

    # Cashu
    cashu_mints: list[str] = Field(default_factory=list, env="CASHU_MINTS")
    receive_ln_address: str = Field(default="", env="RECEIVE_LN_ADDRESS")
    primary_mint: str = Field(default="", env="PRIMARY_MINT_URL")
    primary_mint_unit: str = Field(default="sat", env="PRIMARY_MINT_UNIT")

    # Lightning payout configuration
    # Minimum available balance (in satoshis) before profit is paid out over
    # Lightning
    min_payout_sat: int = Field(default=210, gt=0, env="MIN_PAYOUT_SAT")
    # Interval (seconds) between periodic payout attempts. Must be positive.
    payout_interval_seconds: int = Field(
        default=900, gt=0, env="PAYOUT_INTERVAL_SECONDS"
    )

    # Pricing
    # Default behavior: derive pricing from MODELS
    # If fixed_pricing is True -> use fixed_cost_per_request and ignore tokens
    # If fixed_per_1k_* are set (non-zero) -> override model token pricing when model-based
    fixed_pricing: bool = Field(default=False, env="FIXED_PRICING")
    fixed_cost_per_request: int = Field(default=1, env="FIXED_COST_PER_REQUEST")
    fixed_per_1k_input_tokens: int = Field(default=0, env="FIXED_PER_1K_INPUT_TOKENS")
    fixed_per_1k_output_tokens: int = Field(default=0, env="FIXED_PER_1K_OUTPUT_TOKENS")
    exchange_fee: float = Field(default=1.005, env="EXCHANGE_FEE")
    upstream_provider_fee: float = Field(default=1.05, env="UPSTREAM_PROVIDER_FEE")
    tolerance_percentage: float = Field(default=1.0, env="TOLERANCE_PERCENTAGE")
    child_key_cost: int = Field(default=0, env="CHILD_KEY_COST")
    # Minimum per-request charge in millisatoshis when model pricing is free/zero
    min_request_msat: int = Field(default=1, env="MIN_REQUEST_MSAT")
    reset_reserved_balance_on_startup: bool = Field(
        default=True, env="RESET_RESERVED_BALANCE_ON_STARTUP"
    )  # deactivate in horizontal scaling setups
    # Reservations older than this are considered leaked (client disconnect,
    # crash, abandoned stream) and released by the background sweeper and the
    # refund endpoint.
    stale_reservation_timeout_seconds: int = Field(
        default=300, env="STALE_RESERVATION_TIMEOUT_SECONDS"
    )
    # Background prune of dead (zero balance, never used) API keys.
    # Interval 0 disables it; min-age is a grace period (default 1 week).
    dead_key_prune_interval_seconds: int = Field(
        default=3600, env="DEAD_KEY_PRUNE_INTERVAL_SECONDS"
    )
    dead_key_min_age_seconds: int = Field(
        default=604_800, env="DEAD_KEY_MIN_AGE_SECONDS"
    )

    # Network
    cors_origins: list[str] = Field(default_factory=lambda: ["*"], env="CORS_ORIGINS")
    tor_proxy_url: str = Field(default="socks5://127.0.0.1:9050", env="TOR_PROXY_URL")
    providers_refresh_interval_seconds: int = Field(
        default=0, env="PROVIDERS_REFRESH_INTERVAL_SECONDS"
    )
    pricing_refresh_interval_seconds: int = Field(
        default=120, env="PRICING_REFRESH_INTERVAL_SECONDS"
    )
    models_refresh_interval_seconds: int = Field(
        default=360, env="MODELS_REFRESH_INTERVAL_SECONDS"
    )
    enable_pricing_refresh: bool = Field(default=True, env="ENABLE_PRICING_REFRESH")
    enable_models_refresh: bool = Field(default=True, env="ENABLE_MODELS_REFRESH")
    refund_cache_ttl_seconds: int = Field(default=3600, env="REFUND_CACHE_TTL_SECONDS")
    refund_sweep_ttl_seconds: int = Field(default=604800, env="REFUND_SWEEP_TTL_SECONDS")

    # Logging
    log_level: str = Field(default="INFO", env="LOG_LEVEL")
    enable_console_logging: bool = Field(default=True, env="ENABLE_CONSOLE_LOGGING")

    # Other
    chat_completions_api_version: str = Field(
        default="", env="CHAT_COMPLETIONS_API_VERSION"
    )
    models_path: str = Field(default="models.json", env="MODELS_PATH")
    source: str = Field(default="", env="SOURCE")

    # Secrets / optional runtime controls
    provider_id: str = Field(default="", env="PROVIDER_ID")
    nsec: str = Field(default="", env="NSEC")

    # Discovery
    relays: list[str] = Field(default_factory=list, env="RELAYS")
    enable_analytics_sharing: bool = Field(
        default=True, env="ENABLE_ANALYTICS_SHARING"
    )

def _normalize_settings_data(data: dict[str, Any]) -> dict[str, Any]:
    """Discard unknown keys from persisted settings."""
    normalized: dict[str, Any] = {}
    known_fields = Settings.__fields__

    for key, value in data.items():
        if key in known_fields:
            normalized[key] = value

    return normalized


# Secrets are credentials, not config: they live in the encrypted/hashed Secret
# store (and decrypted in-memory for runtime use), never in the persisted
# settings blob. ``admin_password`` is gone from the model entirely; ``nsec``
# remains a live field but is stripped from every blob write so it is never
# written back to plaintext. ``upstream_api_key`` is intentionally *not* here:
# it has no encrypted home yet (it is node-scoped today but really belongs on a
# provider), so stripping it would lose it on the next restart. It stays in the
# blob as before; encrypting it is follow-up work. See ``bootstrap_secrets`` and
# ``routstr.core.vault``.
SECRET_FIELDS = frozenset({"admin_password", "nsec"})


def _strip_secret_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``data`` without any secret fields (for persistence)."""
    return {k: v for k, v in data.items() if k not in SECRET_FIELDS}


def _apply_to_live_settings(data: dict[str, Any]) -> None:
    """Apply ``data`` onto the live ``settings`` for all in-process importers.

    Secrets are owned by ``bootstrap_secrets`` (which decrypts the nsec into
    memory before this runs) — they are never persisted to the blob, so ``data``
    re-derived from the secret-free blob carries empty secret values. Skip those
    empty overwrites so a live secret is never clobbered; a non-empty value
    (legacy env, or a not-yet-stripped blob mid-migration) is still applied.
    """
    live = settings.dict()
    for k, v in data.items():
        if k in SECRET_FIELDS and not v and live.get(k):
            continue
        setattr(settings, k, v)


def _compute_primary_mint(cashu_mints: list[str]) -> str:
    return cashu_mints[0] if cashu_mints else "https://mint.minibits.cash/Bitcoin"


def derive_npub_from_nsec(nsec: str) -> str | None:
    """Derive the npub (bech32) from an nsec or 64-char hex private key, or None.

    Parsing is delegated to :func:`routstr.nostr.listing.nsec_to_keypair`, the
    single place that knows the nsec/hex formats (and already returns ``None`` on
    any unusable input); this only bech32-encodes the resulting public key. The
    contract stays "return None on unusable input", so a bad key never crashes
    boot.
    """
    try:
        from nostr.key import PublicKey  # type: ignore

        from ..nostr.listing import nsec_to_keypair
    except ImportError:
        return None

    keypair = nsec_to_keypair(nsec)
    if keypair is None:
        return None
    _privkey_hex, pubkey_hex = keypair

    try:
        return PublicKey(bytes.fromhex(pubkey_hex)).bech32()
    except (ValueError, AttributeError):
        return None


def resolve_bootstrap() -> Settings:
    base = Settings()  # Reads env with custom parse_env_var
    # Back-compat env mapping
    try:
        # Map MODEL_BASED_PRICING -> fixed_pricing (inverted)
        if "MODEL_BASED_PRICING" in os.environ and "FIXED_PRICING" not in os.environ:
            mbp_raw = os.environ.get("MODEL_BASED_PRICING", "").strip().lower()
            mbp = mbp_raw in {"1", "true", "yes", "on"}
            base.fixed_pricing = not mbp
        # Map COST_PER_REQUEST -> fixed_cost_per_request if new not provided
        if (
            "COST_PER_REQUEST" in os.environ
            and "FIXED_COST_PER_REQUEST" not in os.environ
        ):
            try:
                base.fixed_cost_per_request = int(
                    os.environ["COST_PER_REQUEST"].strip()
                )
            except Exception:
                pass
        # Map COST_PER_1K_* -> FIXED_PER_1K_*
        if (
            "COST_PER_1K_INPUT_TOKENS" in os.environ
            and "FIXED_PER_1K_INPUT_TOKENS" not in os.environ
        ):
            try:
                base.fixed_per_1k_input_tokens = int(
                    os.environ["COST_PER_1K_INPUT_TOKENS"].strip()
                )
            except Exception:
                pass
        if (
            "COST_PER_1K_OUTPUT_TOKENS" in os.environ
            and "FIXED_PER_1K_OUTPUT_TOKENS" not in os.environ
        ):
            try:
                base.fixed_per_1k_output_tokens = int(
                    os.environ["COST_PER_1K_OUTPUT_TOKENS"].strip()
                )
            except Exception:
                pass
    except Exception:
        pass
    if not base.onion_url:
        try:
            from ..nostr.listing import discover_onion_url_from_tor  # type: ignore

            discovered = discover_onion_url_from_tor()
            if discovered:
                base.onion_url = discovered
        except Exception:
            pass
    # Derive NPUB from NSEC if not provided
    if not base.npub and base.nsec:
        npub = derive_npub_from_nsec(base.nsec)
        if npub:
            base.npub = npub
    if not base.cors_origins:
        base.cors_origins = ["*"]
    if not base.primary_mint:
        base.primary_mint = _compute_primary_mint(base.cashu_mints)
    return base


class SettingsRow(BaseModel):
    id: int
    data: dict[str, Any]
    updated_at: datetime | None = None


# Single, concrete settings instance that callers import directly
settings: Settings = resolve_bootstrap()


class SettingsService:
    _current: Settings | None = None
    _lock: asyncio.Lock = asyncio.Lock()

    @classmethod
    def get(cls) -> Settings:
        if cls._current is None:
            raise RuntimeError("SettingsService not initialized")
        return cls._current

    @classmethod
    async def initialize(cls, db_session: AsyncSession) -> Settings:
        async with cls._lock:
            from sqlmodel import text

            await db_session.exec(  # type: ignore
                text(
                    "CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, data TEXT NOT NULL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
                )
            )

            row = await db_session.exec(  # type: ignore
                text("SELECT id, data, updated_at FROM settings WHERE id = 1")
            )
            row = row.first()
            env_resolved = resolve_bootstrap()

            if row is None:
                await db_session.exec(  # type: ignore
                    text(
                        "INSERT INTO settings (id, data, updated_at) VALUES (1, :data, :updated_at)"
                    ).bindparams(
                        data=json.dumps(_strip_secret_fields(env_resolved.dict())),
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                await db_session.commit()
                cls._current = settings
                # Update the existing instance in-place for all live importers
                _apply_to_live_settings(env_resolved.dict())
                return cls._current

            db_id, db_data, _updated_at = row
            try:
                db_json_raw = (
                    json.loads(db_data) if isinstance(db_data, str) else dict(db_data)
                )
                if not isinstance(db_json_raw, dict):
                    db_json_raw = {}
            except Exception:
                db_json_raw = {}
            db_json = _normalize_settings_data(db_json_raw)

            valid_fields = set(env_resolved.dict().keys())
            merged_dict: dict[str, Any] = dict(env_resolved.dict())
            merged_dict.update(
                {k: v for k, v in db_json.items() if v not in (None, "", [], {}) and k in valid_fields}
            )
            merged_dict = Settings(**merged_dict).dict()

            # Ensure primary_mint is consistent with cashu_mints if not explicitly set
            if not merged_dict.get("primary_mint"):
                merged_dict["primary_mint"] = _compute_primary_mint(
                    merged_dict.get("cashu_mints", [])
                )

            # Keep npub consistent with the live nsec. bootstrap_secrets may hold
            # the decrypted nsec (from the encrypted store) even when neither env
            # nor the blob carries an nsec/npub; derive from that live value so
            # initialize never wipes a known public key back to empty, leaving a
            # private key with no matching npub.
            if not merged_dict.get("npub") and settings.nsec:
                derived_npub = derive_npub_from_nsec(settings.nsec)
                if derived_npub:
                    merged_dict["npub"] = derived_npub

            # Persist without secrets; compare against the stripped target so a
            # legacy blob that still carries plaintext secrets gets rewritten
            # (and thereby sunset) even when its non-secret values are unchanged.
            persisted = _strip_secret_fields(merged_dict)
            if db_json_raw != persisted:
                await db_session.exec(  # type: ignore
                    text(
                        "UPDATE settings SET data = :data, updated_at = :updated_at WHERE id = 1"
                    ).bindparams(
                        data=json.dumps(persisted),
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                await db_session.commit()

            # Update the existing instance in-place for all live importers
            # (keeps the decrypted nsec live in memory).
            _apply_to_live_settings(merged_dict)
            cls._current = settings
            return cls._current

    @classmethod
    async def update(
        cls, partial: dict[str, Any], db_session: AsyncSession
    ) -> Settings:
        async with cls._lock:
            current = cls.get()
            candidate_dict = {**current.dict(), **_normalize_settings_data(partial)}
            candidate = Settings(**candidate_dict)
            from sqlmodel import text

            # Ensure primary_mint reflects candidate mints if missing
            if not candidate.primary_mint:
                candidate.primary_mint = _compute_primary_mint(candidate.cashu_mints)

            await db_session.exec(  # type: ignore
                text(
                    "UPDATE settings SET data = :data, updated_at = :updated_at WHERE id = 1"
                ).bindparams(
                    data=json.dumps(_strip_secret_fields(candidate.dict())),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await db_session.commit()
            # Update in-place
            for k, v in candidate.dict().items():
                setattr(settings, k, v)
            cls._current = settings
            return settings

    @classmethod
    async def reload_from_db(cls, db_session: AsyncSession) -> Settings:
        async with cls._lock:
            from sqlmodel import text

            row = await db_session.exec(text("SELECT data FROM settings WHERE id = 1"))  # type: ignore
            row = row.first()
            if row is None:
                raise RuntimeError("Settings row missing")
            (data_str,) = row
            data = json.loads(data_str) if isinstance(data_str, str) else dict(data_str)
            valid_fields = set(settings.dict().keys())
            # Update in-place
            for k, v in data.items():
                if k in valid_fields:
                    setattr(settings, k, v)
            cls._current = settings
            return settings


async def _read_raw_settings_blob(db_session: AsyncSession) -> dict[str, Any]:
    """Best-effort read of the raw persisted settings JSON (may not exist yet)."""
    from sqlmodel import text

    try:
        result = await db_session.exec(  # type: ignore
            text("SELECT data FROM settings WHERE id = 1")
        )
        row = result.first()
    except Exception:
        return {}
    if row is None:
        return {}
    (data_str,) = row
    try:
        data = json.loads(data_str) if isinstance(data_str, str) else dict(data_str)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _legacy_plaintext(
    raw_blob: dict[str, Any], env_name: str, blob_key: str
) -> str | None:
    """Legacy plaintext for a secret: env first, then the old settings blob."""
    env_value = os.environ.get(env_name)
    if env_value:
        return env_value
    blob_value = raw_blob.get(blob_key)
    if isinstance(blob_value, str) and blob_value:
        return blob_value
    return None


async def bootstrap_secrets(db_session: AsyncSession) -> None:
    """Move node secrets into the encrypted/hashed Secret store at startup.

    Per secret:
      * column already set -> use it (the nsec is decrypted into the in-memory
        ``settings``; a wrong ROUTSTR_SECRET_KEY surfaces as a clear fail-fast).
      * column empty but legacy plaintext exists (env, or the old settings
        blob) -> transform it (hash the password / encrypt the nsec) into the
        column.
      * nothing (admin password only) -> generate a strong random password,
        hash it, and log it once with the /admin URL.
    """
    from cryptography.fernet import InvalidToken

    from . import vault
    from .db import get_secret

    raw_blob = await _read_raw_settings_blob(db_session)
    secret = await get_secret(db_session)
    changed = False

    # Admin password — one-way scrypt hash.
    if secret.admin_password_hash is None:
        legacy_password = _legacy_plaintext(
            raw_blob, "ADMIN_PASSWORD", "admin_password"
        )
        if legacy_password:
            secret.admin_password_hash = vault.hash_password(legacy_password)
        else:
            generated = secrets.token_urlsafe(24)
            secret.admin_password_hash = vault.hash_password(generated)
            admin_url = (settings.http_url or "http://localhost:8000").rstrip("/")
            # Print to stdout rather than the logger: the operator must see this
            # once (e.g. `docker compose logs`), but it must not be persisted
            # into the on-disk log files the logger also writes to.
            print(
                "No admin password set; generated a temporary one (shown only "
                f"now): {generated}\nLog in at {admin_url}/admin and change it "
                "from the dashboard settings.",
                flush=True,
            )
        changed = True

    # Nostr nsec — reversible Fernet encryption.
    if secret.encrypted_nsec is not None:
        try:
            settings.nsec = vault.decrypt(secret.encrypted_nsec)
        except InvalidToken as exc:
            raise RuntimeError(
                "Stored nsec cannot be decrypted with the current "
                "ROUTSTR_SECRET_KEY. The key changed, or this database came from "
                "another node. Restore the original ROUTSTR_SECRET_KEY to recover."
            ) from exc
    else:
        legacy_nsec = _legacy_plaintext(raw_blob, "NSEC", "nsec")
        if legacy_nsec:
            # The node has a Nostr identity to protect. Encryption at rest is
            # mandatory: without a key we fail fast (clear, actionable boot
            # error) rather than silently persisting the nsec in plaintext.
            if not os.environ.get("ROUTSTR_SECRET_KEY"):
                raise RuntimeError(
                    "An nsec is configured but ROUTSTR_SECRET_KEY is not set. "
                    "The key is required to encrypt the Nostr identity at rest. "
                    "Generate one and set it in the environment:\n"
                    '    python -c "from cryptography.fernet import Fernet; '
                    'print(Fernet.generate_key().decode())"'
                )
            secret.encrypted_nsec = vault.encrypt(legacy_nsec)
            settings.nsec = legacy_nsec
            changed = True

    # Derive npub from whatever nsec we now hold, if not already known.
    if settings.nsec and not settings.npub:
        npub = derive_npub_from_nsec(settings.nsec)
        if npub:
            settings.npub = npub

    if changed:
        secret.updated_at = int(time.time())
        db_session.add(secret)
        await db_session.commit()
