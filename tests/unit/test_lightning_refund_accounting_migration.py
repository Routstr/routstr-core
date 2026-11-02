import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_refund_accounting_migration_preserves_existing_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations/versions/6f41b289e3a7_add_lightning_refund_accounting.py"
    )
    spec = importlib.util.spec_from_file_location("refund_accounting_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE refunds (id TEXT PRIMARY KEY, amount_msats INTEGER NOT NULL, status TEXT NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO refunds VALUES ('existing', 3958, 'ambiguous')"
        )
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(connection))
        )
        migration.upgrade()
        row = connection.exec_driver_sql(
            "SELECT id, amount_msats, status, sent_msats, input_msats, spent_msats FROM refunds"
        ).one()
        assert tuple(row) == ("existing", 3958, "ambiguous", None, None, None)
        migration.downgrade()
        assert {c["name"] for c in sa.inspect(connection).get_columns("refunds")} == {
            "id",
            "amount_msats",
            "status",
        }
        assert tuple(connection.exec_driver_sql("SELECT * FROM refunds").one()) == (
            "existing",
            3958,
            "ambiguous",
        )
    engine.dispose()
