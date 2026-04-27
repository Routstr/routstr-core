from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, cast
from unittest.mock import patch

import pytest

from routstr.core.db import AsyncSession, ModelRow, UpstreamProviderRow
from routstr.payment.models import Architecture, Model, Pricing
from routstr.proxy import refresh_model_maps
from routstr.upstream.base import BaseUpstreamProvider


@pytest.mark.integration
@pytest.mark.asyncio
async def test_enforce_lowest_provider_fee_for_same_url(
    integration_session: Any,
) -> None:
    """Test that the algorithm selects the provider with the lowest fee when URLs match."""

    # 1. Create two providers with the same URL but different fees
    url = "https://api.example.com"
    p1 = UpstreamProviderRow(
        provider_type="custom",
        base_url=url,
        api_key="key1",
        enabled=True,
        provider_fee=1.01,
    )
    p2 = UpstreamProviderRow(
        provider_type="custom",
        base_url=url,
        api_key="key2",
        enabled=True,
        provider_fee=1.05,
    )

    integration_session.add(p1)
    integration_session.add(p2)
    await integration_session.commit()
    await integration_session.refresh(p1)
    await integration_session.refresh(p2)

    assert p1.id is not None
    assert p2.id is not None

    # 2. Add a model for each provider
    m1 = ModelRow(
        id="model-a",
        name="Model A",
        created=1,
        description="desc",
        context_length=100,
        architecture='{"modality": "text", "input_modalities": ["text"], "output_modalities": ["text"], "tokenizer": "tiktoken", "instruct_type": "chat"}',
        pricing='{"prompt": 1.0, "completion": 1.0}',
        upstream_provider_id=p1.id,
        enabled=True,
    )
    m2 = ModelRow(
        id="model-a",
        name="Model A",
        created=1,
        description="desc",
        context_length=100,
        architecture='{"modality": "text", "input_modalities": ["text"], "output_modalities": ["text"], "tokenizer": "tiktoken", "instruct_type": "chat"}',
        pricing='{"prompt": 1.0, "completion": 1.0}',
        upstream_provider_id=p2.id,
        enabled=True,
    )

    integration_session.add(m1)
    integration_session.add(m2)
    await integration_session.commit()

    # 3. Create mock provider instances
    class MockProvider(BaseUpstreamProvider):
        db_id: int

        def __init__(self, db_id: int, base_url: str, api_key: str, fee: float):
            super().__init__(base_url, api_key, fee)
            self.db_id = db_id
            self.provider_type = "custom"

        def get_cached_models(self) -> list[Model]:
            return [
                Model(
                    id="model-a",
                    name="Model A",
                    created=1,
                    description="desc",
                    context_length=100,
                    architecture=Architecture(
                        modality="text",
                        input_modalities=["text"],
                        output_modalities=["text"],
                        tokenizer="tiktoken",
                        instruct_type="chat",
                    ),
                    pricing=Pricing(prompt=1.0, completion=1.0),
                    enabled=True,
                    upstream_provider_id=self.db_id,
                )
            ]

        async def refresh_models_cache(self, skip_network: bool = False) -> None:
            pass

        def prepare_headers(self, request_headers: dict[str, str]) -> dict[str, str]:
            return request_headers

    # 4. Inject mock providers into the proxy
    from routstr import proxy

    assert p1.id is not None
    assert p2.id is not None

    # Need to patch proxy._upstreams and proxy.create_session
    mp1: MockProvider = MockProvider(p1.id, url, "key1", 1.01)
    mp2: MockProvider = MockProvider(p2.id, url, "key2", 1.05)

    with (
        patch("routstr.proxy._upstreams", [mp1, mp2]),
        patch("routstr.proxy.create_session") as mock_session_factory,
    ):
        # Configure mock_session_factory to return a session that uses the test engine
        @asynccontextmanager
        async def mock_create_session() -> AsyncGenerator[AsyncSession, None]:
            yield integration_session

        mock_session_factory.return_value = mock_create_session()

        await refresh_model_maps()

        # 5. Check which provider is selected for 'model-a'
        provider_map = proxy.get_provider_for_model("model-a")

        # Assertions
        assert provider_map is not None
        assert len(provider_map) >= 1

        # Check the first one, cast to MockProvider to access db_id
        best_provider = cast(MockProvider, provider_map[0])
        assert best_provider.db_id == p1.id
        assert best_provider.provider_fee == 1.01
