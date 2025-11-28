import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncGenerator

from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio.engine import create_async_engine
from sqlmodel import Field, Relationship, SQLModel, func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from .logging import get_logger

logger = get_logger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///keys.db")


engine = create_async_engine(DATABASE_URL, echo=False)  # echo=True for debugging SQL


class TemporaryCredit(SQLModel, table=True):  # type: ignore
    __tablename__ = "temporary_credit"

    hashed_key: str = Field(primary_key=True)
    balance: int = Field(default=0, description="Balance in msats")
    reserved_balance: int = Field(default=0, description="Blocked balance in msats")
    created: datetime | None = Field(None, description="Timestamp of creation")
    refund_address: str | None = Field(
        None, description="Address to refund on expiration"
    )
    refund_mint_url: str | None = Field(
        default=None, description="Mint used to issue the refund token"
    )
    refund_currency: str | None = Field(None, description="Currency of the cashu-token")
    refund_expiration_time: int | None = Field(
        None, description="Refund not allowed after timeout"
    )

    @property
    def total_balance_msat(self) -> int:
        return self.balance - self.reserved_balance

    @property
    def total_balance_sat(self) -> int:
        return self.total_balance_msat // 1000

    @property
    def total_balance(self) -> int:
        return self.total_balance_msat

    @property
    def api_key(self) -> str:
        return "sk-" + self.hashed_key


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
    enabled: bool = Field(default=True, description="Whether this model is enabled")
    upstream_provider: "UpstreamProviderRow" = Relationship(back_populates="models")


class UpstreamProviderRow(SQLModel, table=True):  # type: ignore
    __tablename__ = "upstream_providers"
    id: int | None = Field(default=None, primary_key=True)
    provider_type: str = Field(
        description="Provider type: custom, openai, anthropic, azure, openrouter, etc."
    )
    base_url: str = Field(unique=True, description="Base URL of the upstream API")
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
    query = select(func.sum(TemporaryCredit.balance)).where(
        TemporaryCredit.refund_mint_url == mint_url,
        TemporaryCredit.refund_currency == unit,
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


def run_migrations() -> None:
    """Run Alembic migrations programmatically."""
    import pathlib

    try:
        logger.info("Starting database migrations")

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
        logger.info("Running migrations to latest revision")
        command.upgrade(alembic_cfg, "head")

        logger.info("Database migrations completed successfully")

    except Exception as e:
        logger.error(
            "Database migration failed",
            extra={"error": str(e), "error_type": type(e).__name__},
        )
        raise
