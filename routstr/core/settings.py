from __future__ import annotations

import asyncio
import json
import os
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
    admin_password: str = Field(default="", env="ADMIN_PASSWORD")

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

    # Network
    cors_origins: list[str] = Field(default_factory=lambda: ["*"], env="CORS_ORIGINS")
    tor_proxy_url: str = Field(default="socks5://127.0.0.1:9050", env="TOR_PROXY_URL")
    providers_refresh_interval_seconds: int = Field(
        default=300, env="PROVIDERS_REFRESH_INTERVAL_SECONDS"
    )
    refund_cache_ttl_seconds: int = Field(default=3600, env="REFUND_CACHE_TTL_SECONDS")

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


def _compute_primary_mint(cashu_mints: list[str]) -> str:
    return cashu_mints[0] if cashu_mints else "https://mint.minibits.cash/Bitcoin"


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
        # Map COST_PER_1K_* -> CUSTOM_PER_1K_*
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
            from ..nip91 import discover_onion_url_from_tor  # type: ignore

            discovered = discover_onion_url_from_tor()
            if discovered:
                base.onion_url = discovered
        except Exception:
            pass
    # Derive NPUB from NSEC if not provided
    if not base.npub and base.nsec:
        try:
            from nostr.key import PrivateKey  # type: ignore

            if base.nsec.startswith("nsec"):
                pk = PrivateKey.from_nsec(base.nsec)
            elif len(base.nsec) == 64:
                pk = PrivateKey(bytes.fromhex(base.nsec))
            else:
                pk = None
            if pk is not None:
                try:
                    base.npub = pk.public_key.bech32()
                except Exception:
                    # Fallback to hex if bech32 not available
                    base.npub = pk.public_key.hex()
        except Exception:
            pass
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
                        data=json.dumps(env_resolved.dict()),
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                await db_session.commit()
                cls._current = settings
                # Update the existing instance in-place for all live importers
                for k, v in env_resolved.dict().items():
                    setattr(settings, k, v)
                return cls._current

            db_id, db_data, _updated_at = row
            try:
                db_json = (
                    json.loads(db_data) if isinstance(db_data, str) else dict(db_data)
                )
            except Exception:
                db_json = {}

            merged_dict: dict[str, Any] = dict(env_resolved.dict())
            merged_dict.update(
                {k: v for k, v in db_json.items() if v not in (None, "")}
            )

            # Ensure primary_mint is consistent with cashu_mints if not explicitly set
            if not merged_dict.get("primary_mint"):
                merged_dict["primary_mint"] = _compute_primary_mint(
                    merged_dict.get("cashu_mints", [])
                )

            if any(k not in db_json for k in merged_dict.keys()):
                await db_session.exec(  # type: ignore
                    text(
                        "UPDATE settings SET data = :data, updated_at = :updated_at WHERE id = 1"
                    ).bindparams(
                        data=json.dumps(merged_dict),
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                await db_session.commit()

            # Update the existing instance in-place for all live importers
            for k, v in merged_dict.items():
                setattr(settings, k, v)
            cls._current = settings
            return cls._current

    @classmethod
    async def update(
        cls, partial: dict[str, Any], db_session: AsyncSession
    ) -> Settings:
        async with cls._lock:
            current = cls.get()
            candidate_dict = {**current.dict(), **partial}
            candidate = Settings(**candidate_dict)
            from sqlmodel import text

            # Ensure primary_mint reflects candidate mints if missing
            if not candidate.primary_mint:
                candidate.primary_mint = _compute_primary_mint(candidate.cashu_mints)

            await db_session.exec(  # type: ignore
                text(
                    "UPDATE settings SET data = :data, updated_at = :updated_at WHERE id = 1"
                ).bindparams(
                    data=json.dumps(candidate.dict()),
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
            # Update in-place
            for k, v in data.items():
                setattr(settings, k, v)
            cls._current = settings
            return settings
