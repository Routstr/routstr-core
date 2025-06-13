from contextlib import asynccontextmanager
import os
from typing import AsyncGenerator
import logging
from sqlmodel import Field, SQLModel
from sqlalchemy.ext.asyncio.engine import create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession


DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///keys.db")


logger = logging.getLogger(__name__)

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


async def init_db() -> None:
    """Initializes the database and creates tables if they don't exist."""
    logger.info("Initializing database at %s", DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    logger.info("Database initialized")


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session


@asynccontextmanager
async def create_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session
