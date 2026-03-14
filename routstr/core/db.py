import os
import pathlib
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError
from sqlalchemy import UniqueConstraint
from sqlalchemy.ext.asyncio.engine import create_async_engine
from sqlmodel import Field, Relationship, SQLModel, func, select, update
from sqlmodel.ext.asyncio.session import AsyncSession

from .logging import get_logger

logger = get_logger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///keys.db")


engine = create_async_engine(DATABASE_URL, echo=False)  # echo=True for debugging SQL


class ApiKey(SQLModel, table=True):  # type: ignore
    __tablename__ = "api_keys"

    hashed_key: str = Field(primary_key=True)
    balance: int = Field(default=0, description="Balance in millisatoshis (msats)")
    reserved_balance: int = Field(
        default=0, description="Reserved balance in millisatoshis (msats)"
    )
    refund_address: str | None = Field(
        default=None,
        description="Lightning address to refund remaining balance after key expires",
    )
    key_expiry_time: int | None = Field(
        default=None,
        description="Unix-timestamp after which the cashu-token's balance gets refunded to the refund_address",
    )
    total_spent: int = Field(
        default=0, description="Total spent in millisatoshis (msats)"
    )
    total_requests: int = Field(default=0)
    refund_mint_url: str | None = Field(
        default=None,
        description="URL of the mint used to create the cashu-token",
    )
    refund_currency: str | None = Field(
        default=None,
        description="Currency of the cashu-token",
    )
    parent_key_hash: str | None = Field(
        default=None, foreign_key="api_keys.hashed_key", index=True
    )
    balance_limit: int | None = Field(
        default=None,
        description="Max spendable balance in msats for this key (mostly for child keys)",
    )
    balance_limit_reset: str | None = Field(
        default=None,
        description="Reset policy for balance limit (manual, daily, monthly, etc.)",
    )
    balance_limit_reset_date: int | None = Field(
        default=None,
        description="Unix timestamp of the last time the balance limit was reset",
    )
    validity_date: int | None = Field(
        default=None,
        description="Unix timestamp after which the key is no longer valid",
    )

    @property
    def total_balance(self) -> int:
        return self.balance - self.reserved_balance


async def reset_all_reserved_balances(session: AsyncSession) -> None:
    logger.info("Resetting all reserved balances to 0")
    stmt = update(ApiKey).values(reserved_balance=0)
    await session.exec(stmt)  # type: ignore[call-overload]
    await session.commit()
    logger.info("Reserved balances reset successfully")


class ModelRow(SQLModel, table=True):  # type: ignore
    __tablename__ = "models"
    id: str = Field(primary_key=True)
    upstream_provider_id: int = Field(
        primary_key=True, foreign_key="upstream_providers.id", ondelete="CASCADE"
    )
    name: str = Field()
    created: int = Field()
    description: str = Field()
    context_length: int = Field()
    architecture: str = Field()
    pricing: str = Field()
    sats_pricing: str | None = Field(default=None)
    per_request_limits: str | None = Field(default=None)
    top_provider: str | None = Field(default=None)
    canonical_slug: str | None = Field(default=None, description="Canonical model slug")
    alias_ids: str | None = Field(
        default=None, description="JSON array of model alias IDs"
    )
    enabled: bool = Field(default=True, description="Whether this model is enabled")
    upstream_provider: "UpstreamProviderRow" = Relationship(back_populates="models")


class LightningInvoice(SQLModel, table=True):  # type: ignore
    __tablename__ = "lightning_invoices"

    id: str = Field(primary_key=True, description="Unique invoice identifier")
    bolt11: str = Field(description="BOLT11 invoice string", unique=True)
    amount_sats: int = Field(description="Amount in satoshis")
    description: str = Field(description="Invoice description")
    payment_hash: str = Field(description="Payment hash for tracking", unique=True)
    status: str = Field(
        default="pending", description="pending, paid, expired, cancelled"
    )
    api_key_hash: str | None = Field(
        default=None, description="Associated API key hash for topup operations"
    )
    purpose: str = Field(description="create or topup")
    created_at: int = Field(
        default_factory=lambda: int(time.time()), description="Unix timestamp"
    )
    expires_at: int = Field(description="Unix timestamp when invoice expires")
    paid_at: int | None = Field(default=None, description="Unix timestamp when paid")


class CashuTransaction(SQLModel, table=True):  # type: ignore
    __tablename__ = "cashu_transactions"

    id: str = Field(
        primary_key=True,
        default_factory=lambda: uuid.uuid4().hex,
        description="Unique transaction identifier",
    )
    token: str = Field(description="Serialized Cashu token")
    amount: int = Field(description="Amount in the token's unit")
    unit: str = Field(description="Token unit (sat or msat)")
    mint_url: str | None = Field(default=None, description="Mint URL for the token")
    type: str = Field(default="out", description="Transaction type: in or out")
    request_id: str | None = Field(default=None, description="Associated request ID")
    created_at: int = Field(
        default_factory=lambda: int(time.time()),
        description="Unix timestamp",
    )
    collected: bool = Field(default=False)
    swept: bool = Field(default=False)


async def store_cashu_transaction(
    token: str,
    amount: int,
    unit: str,
    mint_url: str | None = None,
    typ: str = "out",
    request_id: str | None = None,
    collected: bool = False,
    created_at: int | None = None,
) -> None:
    try:
        async with create_session() as session:
            tx = CashuTransaction(
                token=token,
                amount=amount,
                unit=unit,
                mint_url=mint_url,
                type=typ,
                request_id=request_id,
                collected=collected,
                created_at=created_at or int(time.time()),
            )
            session.add(tx)
            await session.commit()
    except Exception as e:
        logger.warning(
            f"Failed to store cashu transaction: {e} (type={typ})",
            extra={"error": str(e), "type": typ},
        )


class UpstreamProviderRow(SQLModel, table=True):  # type: ignore
    __tablename__ = "upstream_providers"
    __table_args__ = (
        UniqueConstraint(
            "base_url", "api_key", name="uq_upstream_providers_base_url_api_key"
        ),
    )
    id: int | None = Field(default=None, primary_key=True)
    provider_type: str = Field(
        description="Provider type: custom, openai, anthropic, azure, openrouter, etc."
    )
    base_url: str = Field(description="Base URL of the upstream API")
    api_key: str = Field(description="API key for the upstream provider")
    api_version: str | None = Field(
        default=None, description="API version for Azure OpenAI"
    )
    enabled: bool = Field(default=True, description="Whether this provider is enabled")
    provider_fee: float = Field(
        default=1.01, description="Provider fee multiplier (default 1%)"
    )
    provider_settings: str | None = Field(
        default=None, description="JSON string for provider-specific settings"
    )
    models: list["ModelRow"] = Relationship(
        back_populates="upstream_provider",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


async def balances_for_mint_and_unit(
    db_session: AsyncSession, mint_url: str, unit: str
) -> int:
    query = select(func.sum(ApiKey.balance)).where(
        ApiKey.refund_mint_url == mint_url, ApiKey.refund_currency == unit
    )
    result = await db_session.exec(query)
    return result.one() or 0


async def init_db() -> None:
    """Initializes the database and creates tables if they don't exist."""
    async with engine.begin() as conn:
        if DATABASE_URL.startswith("sqlite"):
            await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        await conn.run_sync(SQLModel.metadata.create_all)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session


@asynccontextmanager
async def create_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session


def fix_cashu_migrations() -> None:
    """
    Fixes Cashu wallet migrations that are not idempotent.
    This specifically addresses the 'duplicate column name: public_keys' error
    in the keysets table of Cashu's internal SQLite databases.
    """
    project_root = pathlib.Path(__file__).resolve().parents[2]
    wallet_dir = project_root / ".wallet"

    if not wallet_dir.exists() or not wallet_dir.is_dir():
        return

    logger.info("Checking Cashu wallet databases for migration idempotency")

    for db_file in wallet_dir.glob("*.sqlite3"):
        try:
            conn = sqlite3.connect(db_file)
            cursor = conn.cursor()

            # Check if keysets table exists
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='keysets'"
            )
            if not cursor.fetchone():
                conn.close()
                continue

            # Check if public_keys column exists
            cursor.execute("PRAGMA table_info(keysets)")
            columns = [info[1] for info in cursor.fetchall()]

            if "public_keys" not in columns:
                logger.info(f"Adding missing public_keys column to {db_file.name}")
                cursor.execute("ALTER TABLE keysets ADD COLUMN public_keys TEXT")
                conn.commit()

            conn.close()
        except Exception as e:
            logger.warning(f"Could not check/fix Cashu database {db_file}: {e}")


def _clear_alembic_version() -> None:
    """Clear the alembic_version table so stamp/upgrade can proceed."""
    sync_url = DATABASE_URL.replace("+aiosqlite", "")
    from sqlalchemy import create_engine, text

    eng = create_engine(sync_url)
    with eng.begin() as conn:
        conn.execute(text("DELETE FROM alembic_version"))
    eng.dispose()


def run_migrations() -> None:
    """Run Alembic migrations programmatically."""
    try:
        # Run Cashu migration fix first
        fix_cashu_migrations()

        # Get the path to the alembic.ini file
        project_root = pathlib.Path(__file__).resolve().parents[2]
        alembic_ini_path = project_root / "alembic.ini"

        if not alembic_ini_path.exists():
            raise FileNotFoundError(
                f"Alembic configuration file not found at {alembic_ini_path}"
            )

        # Create Alembic config object
        alembic_cfg = Config(str(alembic_ini_path))

        # Set the database URL in the config
        alembic_cfg.set_main_option("sqlalchemy.url", DATABASE_URL)

        try:
            command.upgrade(alembic_cfg, "head")
        except CommandError as e:
            if "Can't locate revision" in str(e):
                logger.warning(
                    "Database stamped with unknown revision (likely from another branch). "
                    "Re-stamping to current head.",
                    extra={"error": str(e)},
                )
                _clear_alembic_version()
                command.stamp(alembic_cfg, "head")
            else:
                raise

        logger.info("Database migrations completed successfully")

    except Exception as e:
        logger.error(
            "Database migration failed",
            extra={"error": str(e), "error_type": type(e).__name__},
        )
        raise
