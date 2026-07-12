from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "d7e8f9a0b1c2_unique_token_type_cashu_transactions.py"
)
_spec = importlib.util.spec_from_file_location(
    "cashu_transaction_uniqueness_migration", _MIGRATION_PATH
)
assert _spec is not None and _spec.loader is not None
migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migration)


def test_duplicate_merge_preserves_state_and_missing_linkage() -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    transactions = sa.Table(
        "cashu_transactions",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("token", sa.String, nullable=False),
        sa.Column("amount", sa.Integer, nullable=False),
        sa.Column("unit", sa.String, nullable=False),
        sa.Column("mint_url", sa.String),
        sa.Column("type", sa.String, nullable=False),
        sa.Column("request_id", sa.String),
        sa.Column("created_at", sa.Integer, nullable=False),
        sa.Column("collected", sa.Boolean, nullable=False),
        sa.Column("swept", sa.Boolean, nullable=False),
        sa.Column("source", sa.String, nullable=False),
        sa.Column("api_key_hashed_key", sa.String),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(
            transactions.insert(),
            [
                {
                    "id": "oldest",
                    "token": "cashuAduplicate",
                    "amount": 100,
                    "unit": "sat",
                    "mint_url": "",
                    "type": "out",
                    "request_id": "",
                    "created_at": 1,
                    "collected": False,
                    "swept": False,
                    "source": "",
                    "api_key_hashed_key": "",
                },
                {
                    "id": "newer",
                    "token": "cashuAduplicate",
                    "amount": 100,
                    "unit": "sat",
                    "mint_url": "https://mint.example",
                    "type": "out",
                    "request_id": "request-newer",
                    "created_at": 2,
                    "collected": True,
                    "swept": False,
                    "source": "apikey",
                    "api_key_hashed_key": "hashed-key",
                },
                {
                    "id": "newest",
                    "token": "cashuAduplicate",
                    "amount": 100,
                    "unit": "sat",
                    "mint_url": "https://other.example",
                    "type": "out",
                    "request_id": "request-newest",
                    "created_at": 3,
                    "collected": False,
                    "swept": True,
                    "source": "x-cashu",
                    "api_key_hashed_key": "other-key",
                },
            ],
        )

        migration._merge_duplicate_transactions(connection)
        rows = connection.execute(sa.select(transactions)).mappings().all()

    assert rows == [
        {
            "id": "oldest",
            "token": "cashuAduplicate",
            "amount": 100,
            "unit": "sat",
            "mint_url": "https://mint.example",
            "type": "out",
            "request_id": "request-newer",
            "created_at": 1,
            "collected": True,
            "swept": True,
            "source": "apikey",
            "api_key_hashed_key": "hashed-key",
        }
    ]
