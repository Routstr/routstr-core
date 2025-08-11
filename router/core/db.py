import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio.engine import create_async_engine
from sqlmodel import Field, SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from .logging import get_logger

logger = get_logger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///keys.db")


engine = create_async_engine(DATABASE_URL, echo=False)  # echo=True for debugging SQL


class ApiKey(SQLModel, table=True):  # type: ignore
    __tablename__ = "api_keys"

    hashed_key: str = Field(primary_key=True)
    balance: int = Field(default=0, description="Balance in millisatoshis (msats)")
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
    mint_url: str | None = Field(
        default=None,
        description="URL of the mint used to create the cashu-token",
    )


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
