"""The background startup bootstrap must leave a default node with live models.

A default node gets its provider from env and has no ``ModelRow`` rows (only
admin overrides write them), so models can only come from the upstream itself.
"""

import asyncio
from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest

from routstr.core.db import UpstreamProviderRow
from routstr.payment.models import Architecture, Model, Pricing


def _make_model(model_id: str) -> Model:
    return Model(
        id=model_id,
        name=model_id,
        created=0,
        description="",
        context_length=128_000,
        architecture=Architecture(
            modality="text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="cl100k_base",
            instruct_type=None,
        ),
        pricing=Pricing(prompt=0.000001, completion=0.000002),
    )


@pytest.fixture
def reset_upstreams() -> Iterator[None]:
    import routstr.proxy as proxy

    saved = proxy._upstreams
    yield
    proxy._upstreams = saved


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bootstrap_fetches_models_without_stored_rows(
    integration_session: object,
    patched_db_engine: None,
    reset_upstreams: None,
) -> None:
    from routstr.core.main import _bootstrap_providers_and_pricing
    from routstr.proxy import get_upstreams
    from routstr.upstream.generic import GenericUpstreamProvider

    integration_session.add(  # type: ignore[attr-defined]
        UpstreamProviderRow(
            provider_type="generic",
            base_url="http://upstream.test/v1",
            api_key="sk-test",
            enabled=True,
            provider_fee=1.0,
        )
    )
    await integration_session.commit()  # type: ignore[attr-defined]

    fetched = [_make_model("gpt-4o-mini"), _make_model("gpt-4o")]
    with (
        patch.object(
            GenericUpstreamProvider,
            "fetch_models",
            new=AsyncMock(return_value=fetched),
        ),
        patch("routstr.payment.price._update_prices", new=AsyncMock()),
        patch("routstr.payment.models._update_sats_pricing_once", new=AsyncMock()),
    ):
        await _bootstrap_providers_and_pricing()

    upstreams = get_upstreams()
    assert len(upstreams) == 1
    assert {m.id for m in upstreams[0].get_cached_models()} == {
        "gpt-4o-mini",
        "gpt-4o",
    }


@pytest.mark.asyncio
async def test_run_after_waits_for_task_even_when_it_fails() -> None:
    from routstr.core.main import _run_after

    gate = asyncio.Event()
    started = asyncio.Event()

    async def bootstrap() -> None:
        await gate.wait()
        raise RuntimeError("bootstrap failed")

    async def start() -> None:
        started.set()

    bootstrap_task = asyncio.create_task(bootstrap())
    runner = asyncio.create_task(_run_after(bootstrap_task, start))
    await asyncio.sleep(0)
    assert not started.is_set()

    gate.set()
    await runner
    assert started.is_set()
    assert isinstance(bootstrap_task.exception(), RuntimeError)
