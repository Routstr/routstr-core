"""Dialect-portability invariants for the main application DB.

These run without a PostgreSQL server: they compile the real SQLModel metadata
against the PostgreSQL dialect and assert the properties that SQLite silently
papered over. Each test here corresponds to a defect that was live on
PostgreSQL while every SQLite test stayed green (issue #45).

The end-to-end proof against a real server lives in
``tests/integration/test_postgres_compatibility.py``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import BigInteger, Integer
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable
from sqlmodel import SQLModel

from routstr.core import db as db_module

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (
    ROOT
    / "migrations"
    / "versions"
    / "d3c8b21f7a04_widen_money_and_timestamp_columns.py"
)

# Built once here because SQLAlchemy's dialect constructors are untyped.
POSTGRES_DIALECT: Any = postgresql.dialect()  # type: ignore[no-untyped-call]
SQLITE_DIALECT: Any = sqlite.dialect()  # type: ignore[no-untyped-call]


def _load_widening_migration() -> Any:
    spec = importlib.util.spec_from_file_location("_widen_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _table(name: str) -> Any:
    return SQLModel.metadata.tables[name]


def _render(element: Any, dialect: Any) -> str:
    """Compile one element against a dialect and return the rendered SQL."""
    return str(element.compile(dialect=dialect))


def test_migration_and_models_widen_the_same_columns() -> None:
    """The ALTER list and the ORM must not drift apart.

    If they do, a fresh PostgreSQL database (built by the migration) and the
    in-memory metadata (used by ``create_all``) disagree on column width, and
    only one of the two paths overflows.
    """
    widened = _load_widening_migration().WIDENED_COLUMNS

    mismatched = []
    for table_name, column_name, nullable in widened:
        column = _table(table_name).columns[column_name]
        if not isinstance(column.type, BigInteger):
            mismatched.append(
                f"{table_name}.{column_name}: migration widens it, model has "
                f"{column.type!r}"
            )
        if column.nullable != nullable:
            mismatched.append(
                f"{table_name}.{column_name} nullable={column.nullable}, "
                f"migration says {nullable}"
            )
    assert mismatched == []

    # And the other direction, so a column widened in the ORM but left out of
    # the ALTER list cannot pass: ``create_all`` would give it BIGINT while a
    # migrated database kept INT4.
    declared = {(table, column) for table, column, _ in widened}
    in_models = {
        (table.name, column.name)
        for table in SQLModel.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, BigInteger)
    }
    assert in_models - declared == set(), "widened in models but not in migration"
    assert declared - in_models == set(), "widened in migration but not in models"


@pytest.mark.parametrize(
    "table_name, column_name",
    [
        ("api_keys", "balance"),
        ("api_keys", "reserved_balance"),
        ("api_keys", "total_spent"),
        ("api_keys", "total_requests"),
        ("cashu_transactions", "amount"),
        ("lightning_invoices", "amount_sats"),
        ("refunds", "amount_msats"),
        ("reservation_releases", "reserved_msats"),
        ("routstr_fees", "accumulated_msats"),
        ("routstr_fees", "payout_in_progress_msats"),
        ("routstr_fees", "total_paid_msats"),
    ],
)
def test_money_columns_are_64_bit_on_postgresql(
    table_name: str, column_name: str
) -> None:
    """Balances are millisatoshis; INT4 would cap a key at ~0.0215 BTC.

    SQLite stores every INTEGER as 64-bit, so a plain ``int`` field was safe
    there and this ceiling existed only on PostgreSQL.
    """
    column = _table(table_name).columns[column_name]
    rendered = _render(column.type, POSTGRES_DIALECT)
    assert rendered == "BIGINT", f"{table_name}.{column_name} renders as {rendered}"


@pytest.mark.parametrize(
    "table_name, column_name",
    [
        ("api_keys", "created_at"),
        ("api_keys", "key_expiry_time"),
        ("api_keys", "reserved_at"),
        ("api_keys", "validity_date"),
        ("cashu_transactions", "created_at"),
        ("cli_tokens", "expires_at"),
        ("lightning_invoices", "expires_at"),
        ("lightning_invoices", "paid_at"),
        ("refunds", "claimed_at"),
        ("reservation_releases", "created_at"),
        ("routstr_fees", "payout_started_at"),
    ],
)
def test_timestamp_columns_survive_2038_on_postgresql(
    table_name: str, column_name: str
) -> None:
    """Unix seconds in INT4 break on 2038-01-19, and long-dated expiries sooner."""
    column = _table(table_name).columns[column_name]
    assert _render(column.type, POSTGRES_DIALECT) == "BIGINT"


def test_identifier_columns_stay_narrow() -> None:
    """Widening is deliberate, not a blanket sweep over every integer."""
    for table_name, column_name in [
        ("upstream_providers", "id"),
        ("model_paths", "id"),
        ("model_paths", "upstream_provider_id"),
        ("models", "upstream_provider_id"),
        ("models", "context_length"),
    ]:
        column = _table(table_name).columns[column_name]
        assert isinstance(column.type, Integer)
        assert not isinstance(column.type, BigInteger), (
            f"{table_name}.{column_name} was widened unnecessarily"
        )


def test_nsec_state_does_not_require_a_native_postgres_enum() -> None:
    """A bare Python Enum makes PostgreSQL demand a ``nsecstate`` TYPE.

    The migration created this column as VARCHAR and never created that type,
    so every read and write of the secrets singleton — and with it node
    bootstrap — failed with `type "nsecstate" does not exist`.
    """
    ddl = _render(CreateTable(_table("secrets")), POSTGRES_DIALECT)
    assert "nsecstate" not in ddl.lower()
    assert "VARCHAR" in ddl

    column = _table("secrets").columns["nsec_state"]
    assert column.type.native_enum is False
    # Values, not member names, so rows written before the column was typed
    # still load.
    assert set(column.type.enums) == {"legacy", "encrypted", "cleared"}


def test_every_table_compiles_as_postgresql_ddl() -> None:
    """No model may carry DDL that only SQLite can render."""
    for table in SQLModel.metadata.tables.values():
        _render(CreateTable(table), POSTGRES_DIALECT)


def test_model_path_upsert_is_built_for_the_bound_dialect() -> None:
    """``ON CONFLICT`` is spelled per dialect and the constructs are not swappable.

    The SQLite construct does not compile against PostgreSQL: it raises
    ``AttributeError: 'OnConflictDoUpdate' object has no attribute
    'constraint_target'``, which killed every model-path refresh.
    """
    from routstr.upstream import model_paths

    class _Bound:
        def __init__(self, name: str) -> None:
            self.dialect = type("_D", (), {"name": name})()

    class _Session:
        def __init__(self, name: str) -> None:
            self._bind = _Bound(name)

        def get_bind(self) -> Any:
            return self._bind

    postgres_insert = model_paths._upsert(_Session("postgresql"))  # type: ignore[arg-type]
    sqlite_insert = model_paths._upsert(_Session("sqlite"))  # type: ignore[arg-type]

    assert postgres_insert.__class__.__module__.endswith("postgresql.dml")
    assert sqlite_insert.__class__.__module__.endswith("sqlite.dml")

    # Both must actually compile the upsert they will be asked to run.
    conflict = dict(
        index_elements=["model_id", "path", "upstream_provider_id"],
        set_={"updated_at": postgres_insert.excluded.updated_at},
    )
    _render(postgres_insert.on_conflict_do_update(**conflict), POSTGRES_DIALECT)
    _render(
        sqlite_insert.on_conflict_do_update(
            index_elements=conflict["index_elements"],
            set_={"updated_at": sqlite_insert.excluded.updated_at},
        ),
        SQLITE_DIALECT,
    )


def test_clear_alembic_version_uses_the_configured_async_driver() -> None:
    """The recovery path must not need a second, sync-only driver.

    It used to build a sync engine from ``DATABASE_URL.replace("+aiosqlite", "")``,
    which left ``postgresql+asyncpg`` intact and raised ``MissingGreenlet``.
    """
    import inspect

    source = inspect.getsource(db_module._clear_alembic_version)
    assert "create_engine(" not in source.replace("create_async_engine(", "")
    assert 'replace("+aiosqlite"' not in source


def test_sqlite_only_maintenance_stays_scoped_to_the_wallet_directory() -> None:
    """PRAGMA/sqlite3 handling in db.py may only touch ``.wallet/*.sqlite3``."""
    import inspect

    source = inspect.getsource(db_module.fix_cashu_migrations)
    assert ".wallet" in source or "wallet_dir" in source
    assert "*.sqlite3" in source

    init_source = inspect.getsource(db_module.init_db)
    # Gated on the bound dialect, not a DATABASE_URL string prefix.
    assert 'dialect.name == "sqlite"' in init_source
