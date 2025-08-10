import asyncio
import pathlib
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

# Add the parent directory to the Python path so we can import router modules
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from router.core.db import DATABASE_URL

config = context.config
if config.config_file_name is None:
    raise ValueError("config_file_name is None")
fileConfig(config.config_file_name)
config.set_main_option("sqlalchemy.url", DATABASE_URL)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection, target_metadata=target_metadata, compare_type=True
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = create_async_engine(DATABASE_URL, poolclass=pool.NullPool)

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    # Check if we're already in an event loop (e.g., being called from FastAPI)
    try:
        loop = asyncio.get_running_loop()
        # If we're in an existing loop, create a new thread to run migrations
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(asyncio.run, run_migrations_online())
            future.result()
    except RuntimeError:
        # No event loop running, we can use asyncio.run directly
        asyncio.run(run_migrations_online())
