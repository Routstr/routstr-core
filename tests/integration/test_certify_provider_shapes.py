"""Certification probes must send the request the proxy would send.

Each provider type reshapes requests through its hooks (paths, auth headers,
query params, model-name transforms). The mocked upstream here answers only
the proxy-shaped request, so a probe that hand-builds an OpenAI-style call
fails ``endpoint.reachable`` / ``usage.capture`` instead of passing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest
import respx
from httpx import AsyncClient, Response
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import UpstreamProviderRow
from routstr.proxy import reinitialize_upstreams

from .test_certify_endpoint import (
    _admin_headers,
    _find_row,
    _mock_chat_response,
    _model_row,
    _pin_sats_usd,  # noqa: F401 - autouse: pins the sats/USD quote
)


@dataclass(frozen=True)
class Shape:
    provider_type: str
    base_url: str
    model_id: str
    models_url: str
    chat_url: str
    upstream_model: str
    auth_header: tuple[str, str]
    params: dict[str, str] = field(default_factory=dict)
    api_version: str | None = None


SHAPES = [
    Shape(
        provider_type="azure",
        base_url="https://res.openai.azure.com",
        model_id="gpt-4o",
        models_url="https://res.openai.azure.com/openai/models",
        chat_url=(
            "https://res.openai.azure.com/openai/deployments/gpt-4o/chat/completions"
        ),
        upstream_model="gpt-4o",
        auth_header=("api-key", "test-key"),
        params={"api-version": "2024-10-21"},
        api_version="2024-10-21",
    ),
    Shape(
        provider_type="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        model_id="gemini-2.5-flash",
        models_url="https://generativelanguage.googleapis.com/v1beta/openai/models",
        chat_url=(
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        ),
        upstream_model="gemini-2.5-flash",
        auth_header=("authorization", "Bearer test-key"),
    ),
    Shape(
        provider_type="ollama",
        base_url="http://ollama.test:11434",
        model_id="llama3",
        models_url="http://ollama.test:11434/v1/models",
        chat_url="http://ollama.test:11434/v1/chat/completions",
        upstream_model="llama3",
        auth_header=("authorization", "Bearer test-key"),
    ),
    Shape(
        provider_type="anthropic",
        base_url="https://api.anthropic.com/v1",
        model_id="claude-sonnet-4.5",
        models_url="https://api.anthropic.com/v1/models",
        chat_url="https://api.anthropic.com/v1/chat/completions",
        upstream_model="claude-sonnet-4-5-20250929",
        auth_header=("authorization", "Bearer test-key"),
    ),
]


async def _seed(session: AsyncSession, shape: Shape) -> int:
    provider = UpstreamProviderRow(
        provider_type=shape.provider_type,
        base_url=shape.base_url,
        api_key="test-key",
        api_version=shape.api_version,
        provider_fee=1.0,
    )
    session.add(provider)
    await session.commit()
    await session.refresh(provider)
    assert provider.id is not None
    session.add(_model_row(provider.id, model_id=shape.model_id))
    await session.commit()
    with patch("routstr.payment.models.sats_usd_price", return_value=0.0005):
        await reinitialize_upstreams()
    return provider.id


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("shape", SHAPES, ids=[s.provider_type for s in SHAPES])
async def test_certify_matches_proxy_request(
    shape: Shape,
    integration_client: AsyncClient,
    integration_session: AsyncSession,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        provider_id = await _seed(integration_session, shape)
        models_route = mock.get(shape.models_url).mock(
            return_value=Response(200, json={"data": [{"id": shape.model_id}]})
        )
        chat_route = mock.post(shape.chat_url).mock(
            return_value=Response(200, json=_mock_chat_response(model=shape.model_id))
        )

        resp = await integration_client.post(
            f"/admin/api/upstream-providers/{provider_id}/certify",
            headers=_admin_headers(),
            json={"model_id": shape.model_id, "check_cache": True},
        )

    assert resp.status_code == 200, resp.text
    rows = resp.json()["rows"]
    assert _find_row(rows, "endpoint.reachable")["status"] == "ok"
    assert _find_row(rows, "usage.capture")["status"] == "ok"
    assert _find_row(rows, "cost.prompt_completion")["status"] == "ok"

    assert models_route.call_count == 1
    # The one-token probe plus both cache-probe completions.
    assert chat_route.call_count == 3
    header, value = shape.auth_header
    for call in [*models_route.calls, *chat_route.calls]:
        assert call.request.headers.get(header) == value
        for key, expected in shape.params.items():
            assert call.request.url.params.get(key) == expected
    for call in chat_route.calls:
        assert json.loads(call.request.content)["model"] == shape.upstream_model


def test_shape_body_keeps_a_single_cache_control_marker() -> None:
    """The cache probe's own marker must not be stamped a second time."""
    from routstr.payment.models import Architecture, Model, Pricing
    from routstr.upstream.anthropic import AnthropicUpstreamProvider
    from routstr.upstream.certification import shape_body
    from routstr.upstream.certification_cache import _request_body

    model = Model(
        id="claude-sonnet-4.5",
        name="claude-sonnet-4.5",
        created=0,
        description="",
        context_length=8192,
        architecture=Architecture(
            modality="text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="unknown",
            instruct_type=None,
        ),
        pricing=Pricing(prompt=1e-6, completion=2e-6),
        sats_pricing=None,
        per_request_limits=None,
        top_provider=None,
        enabled=True,
        upstream_provider_id=1,
        canonical_slug=None,
    )
    upstream = AnthropicUpstreamProvider(api_key="test-key")
    body = _request_body(model.id, "prefix", "cache_control", None)

    shaped = shape_body(body, upstream, model)

    assert json.dumps(shaped).count('"cache_control"') == 1
    assert shaped["model"] == "claude-sonnet-4-5-20250929"
