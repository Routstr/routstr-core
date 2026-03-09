import os
import pathlib
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from alembic import command
from alembic.config import Config
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


def _get_table_columns(cursor: sqlite3.Cursor, table: str) -> set[str]:
    """Return the set of column names for a table, or empty set if table doesn't exist."""
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    if not cursor.fetchone():
        return set()
    cursor.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cursor.fetchall()}


def _detect_schema_version(cursor: sqlite3.Cursor) -> int:
    """
    Detect the actual schema version of a cashu wallet database by inspecting
    which columns/tables exist, matching them to migration milestones.

    Cashu wallet migrations (m000-m015) add columns non-idempotently.
    We detect the highest migration that has already been fully applied.
    """
    proofs_cols = _get_table_columns(cursor, "proofs")
    keysets_cols = _get_table_columns(cursor, "keysets")

    if not proofs_cols:
        return 0  # no proofs table means nothing has run

    # Walk backwards from the highest migration to find the actual version.
    # Each check tests for schema artifacts introduced by that migration.

    # m015: mints table
    mints_cols = _get_table_columns(cursor, "mints")
    if mints_cols:
        return 15

    # m014: bolt11_mint_quotes.key column
    mint_quotes_cols = _get_table_columns(cursor, "bolt11_mint_quotes")
    if "key" in mint_quotes_cols:
        return 14

    # m013: bolt11_mint_quotes table
    if mint_quotes_cols:
        return 13

    # m012: keysets.input_fee_ppk
    if "input_fee_ppk" in keysets_cols:
        return 12

    # m011: keysets.unit
    if "unit" in keysets_cols:
        return 11

    # m010: proofs.dleq, proofs.mint_id
    if "mint_id" in proofs_cols:
        return 10
    if "dleq" in proofs_cols:
        return 10

    # m009: keysets.counter, proofs.derivation_path
    if "derivation_path" in proofs_cols:
        return 9

    # m008: keysets.public_keys
    if "public_keys" in keysets_cols:
        return 8

    # m007: nostr table
    nostr_cols = _get_table_columns(cursor, "nostr")
    if nostr_cols:
        return 7

    # m006: invoices table
    invoices_cols = _get_table_columns(cursor, "invoices")
    if invoices_cols:
        return 6

    # m005: proofs.id column, keysets table
    if "id" in proofs_cols and keysets_cols:
        return 5

    # m004: p2sh table
    p2sh_cols = _get_table_columns(cursor, "p2sh")
    if p2sh_cols:
        return 4

    # m003: proofs.send_id
    if "send_id" in proofs_cols:
        return 3

    # m002: proofs.reserved
    if "reserved" in proofs_cols:
        return 2

    # m001: proofs table exists
    return 1


def fix_cashu_migrations() -> None:
    """
    Fixes Cashu wallet migrations that are not idempotent.

    Cashu's migration runner uses ALTER TABLE ADD COLUMN without checking
    if the column already exists. If the dbversions table is out of sync
    with the actual schema, re-running migrations crashes.

    This function detects the real schema version by inspecting existing
    columns/tables and updates dbversions accordingly so cashu skips
    already-applied migrations.
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

            detected_version = _detect_schema_version(cursor)
            if detected_version == 0:
                conn.close()
                continue

            # Ensure dbversions table and row exist
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='dbversions'"
            )
            if not cursor.fetchone():
                conn.close()
                continue

            cursor.execute("SELECT version FROM dbversions WHERE db = 'wallet'")
            row = cursor.fetchone()
            if row is None:
                conn.close()
                continue

            recorded_version = row[0]
            if recorded_version < detected_version:
                logger.info(
                    f"Fixing {db_file.name}: dbversions says v{recorded_version} "
                    f"but schema is v{detected_version}"
                )
                cursor.execute(
                    "UPDATE dbversions SET version = ? WHERE db = 'wallet'",
                    (detected_version,),
                )
                conn.commit()

            conn.close()
        except Exception as e:
            logger.warning(f"Could not check/fix Cashu database {db_file}: {e}")


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

        # Run migrations to the latest revision
        command.upgrade(alembic_cfg, "head")

        logger.info("Database migrations completed successfully")

    except Exception as e:
        logger.error(
            "Database migration failed",
            extra={"error": str(e), "error_type": type(e).__name__},
        )
        raise
