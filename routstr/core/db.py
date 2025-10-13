import os
from contextlib import asynccontextmanager
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

    @property
    def total_balance(self) -> int:
        return self.balance - self.reserved_balance


class ModelRow(SQLModel, table=True):  # type: ignore
    __tablename__ = "models"
    id: str = Field(primary_key=True)
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
    upstream_provider_id: int | None = Field(
        default=None, foreign_key="upstream_providers.id"
    )
    upstream_provider: "UpstreamProviderRow" = Relationship(back_populates="models")


class UpstreamProviderRow(SQLModel, table=True):  # type: ignore
    __tablename__ = "upstream_providers"
    id: int | None = Field(default=None, primary_key=True)
    provider_type: str = Field(
        description="Provider type: generic, openai, azure, openrouter"
    )
    base_url: str = Field(unique=True, description="Base URL of the upstream API")
    api_key: str = Field(description="API key for the upstream provider")
    api_version: str | None = Field(
        default=None, description="API version for Azure OpenAI"
    )
    enabled: bool = Field(default=True, description="Whether this provider is enabled")
    models: list["ModelRow"] = Relationship(back_populates="upstream_provider")


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
