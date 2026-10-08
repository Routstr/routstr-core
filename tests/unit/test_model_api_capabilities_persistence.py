"""Read-only endpoint metadata survives storage, overrides, and migration."""

import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core import admin
from routstr.core.db import ModelRow, UpstreamProviderRow

CAPABILITIES = {
    "images": {
        "supported_parameters": {"n": {"type": "range", "min": 1, "max": 1}},
        "supports_streaming": False,
        "endpoints": [
            {
                "provider_slug": "example",
                "provider_tag": "example",
                "supported_parameters": {},
                "allowed_passthrough_parameters": [],
                "supports_streaming": False,
                "pricing": [
                    {
                        "billable": "output_image",
                        "unit": "image",
                        "cost_usd": 0.04,
                        "variant": None,
                    }
                ],
            }
        ],
    }
}


def _payload(model_id: str = "image-model") -> dict:
    return {
        "id": model_id,
        "name": "Edited name",
        "description": "Edited description",
        "created": 0,
        "context_length": 0,
        "architecture": {
            "modality": "text->image",
            "input_modalities": ["text"],
            "output_modalities": ["image"],
            "tokenizer": "unknown",
            "instruct_type": None,
        },
        "pricing": {"prompt": 0, "completion": 0},
    }


@pytest.mark.parametrize("value", [None, {}, CAPABILITIES])
def test_admin_rejects_capability_writes(value: object) -> None:
    payload = {**_payload(), "api_capabilities": value}
    with pytest.raises(ValidationError, match="read-only upstream metadata"):
        admin.ModelCreate.model_validate(payload)
    with pytest.raises(ValidationError, match="read-only upstream metadata"):
        admin.BatchOverrideRequest.model_validate({"models": [payload]})


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_capabilities_roundtrip_and_survive_admin_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, batch: bool
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'roundtrip.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        async with AsyncSession(engine) as session:
            provider = UpstreamProviderRow(
                provider_type="openrouter",
                base_url="https://openrouter.ai/api/v1",
                api_key="test-only",
            )
            session.add(provider)
            await session.commit()
            await session.refresh(provider)
            assert provider.id is not None
            provider_id = provider.id
            payload = _payload()
            for model_id, capabilities in [
                ("image-model", CAPABILITIES),
                ("legacy", None),
            ]:
                session.add(
                    ModelRow(
                        id=model_id,
                        upstream_provider_id=provider_id,
                        name="Original",
                        created=0,
                        description="original",
                        context_length=0,
                        architecture=json.dumps(payload["architecture"]),
                        pricing=json.dumps(payload["pricing"]),
                        api_capabilities=json.dumps(capabilities)
                        if capabilities
                        else None,
                    )
                )
            await session.commit()

        @asynccontextmanager
        async def create_session() -> AsyncIterator[AsyncSession]:
            # Match production create_session's detached read-back contract.
            async with AsyncSession(engine, expire_on_commit=False) as session:
                yield session

        monkeypatch.setattr(admin, "create_session", create_session)
        monkeypatch.setattr(admin, "refresh_model_maps", AsyncMock())
        monkeypatch.setattr(admin, "_refresh_provider_model_paths", AsyncMock())
        # Model parsing belongs to discovery; this pins this writer's behavior
        # independently while the parser is implemented in a parallel change.
        monkeypatch.setattr(admin, "_row_to_model", MagicMock())
        parsed = admin.ModelCreate.model_validate(payload)
        if batch:
            await admin.batch_override_provider_models(
                str(provider_id), admin.BatchOverrideRequest(models=[parsed])
            )
        else:
            await admin.upsert_provider_model(str(provider_id), parsed)

        new_payload = admin.ModelCreate.model_validate(_payload("new-manual"))
        if batch:
            await admin.batch_override_provider_models(
                str(provider_id), admin.BatchOverrideRequest(models=[new_payload])
            )
        else:
            await admin.upsert_provider_model(str(provider_id), new_payload)

        async with AsyncSession(engine) as session:
            manual = await session.get(ModelRow, ("new-manual", provider_id))
            assert manual is not None and manual.api_capabilities is None
            stored = await session.get(ModelRow, ("image-model", provider_id))
            legacy = await session.get(ModelRow, ("legacy", provider_id))
            assert stored is not None and legacy is not None
            assert stored.name == "Edited name"
            assert stored.api_capabilities is not None
            assert json.loads(stored.api_capabilities) == CAPABILITIES
            assert legacy.api_capabilities is None
    finally:
        await engine.dispose()


def _alembic(root: Path, database: Path, command: str, revision: str) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{database}"
    subprocess.run(
        [sys.executable, "-m", "alembic", command, revision],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_capability_migration_preserves_legacy_rows_and_downgrades(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    database = tmp_path / "migration.db"
    previous_head = "424bb59871d4"
    _alembic(root, database, "upgrade", previous_head)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO upstream_providers "
            "(id, slug, provider_type, base_url, api_key, enabled, provider_fee) "
            "VALUES (1, 'legacy', 'openrouter', 'https://example.test', 'test', 1, 1.01)"
        )
        connection.execute(
            "INSERT INTO models "
            "(id, upstream_provider_id, name, created, description, context_length, architecture, pricing, enabled) "
            "VALUES ('legacy', 1, 'legacy', 0, 'legacy', 0, '{}', '{}', 1)"
        )
        connection.commit()
    _alembic(root, database, "upgrade", "8d71c5a2f903")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT api_capabilities FROM models").fetchone() == (
            None,
        )
        columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(models)")
        }
        assert columns["api_capabilities"][3] == 0  # nullable
        connection.execute(
            "UPDATE models SET api_capabilities = ?", (json.dumps(CAPABILITIES),)
        )
        connection.commit()
        assert (
            json.loads(
                connection.execute("SELECT api_capabilities FROM models").fetchone()[0]
            )
            == CAPABILITIES
        )
    _alembic(root, database, "downgrade", previous_head)
    with sqlite3.connect(database) as connection:
        assert "api_capabilities" not in {
            row[1] for row in connection.execute("PRAGMA table_info(models)")
        }
        assert connection.execute("SELECT id FROM models").fetchone() == ("legacy",)
