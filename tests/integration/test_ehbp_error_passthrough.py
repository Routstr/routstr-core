"""Integration test: encrypted EHBP upstream errors reach the client verbatim.

The proxy is a blind relay for EHBP request *and* response bodies. When the
enclave answers with an encrypted error, wrapping it in the proxy's JSON
envelope replaced the provider's real reason with mojibake. The final upstream
error must instead be relayed with its status, ``Ehbp-Response-Nonce`` and
ciphertext intact so the EHBP client can decrypt it.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.responses import Response
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import ModelRow, UpstreamProviderRow
from routstr.core.error_scope import ERROR_SCOPE_HEADER, ERROR_SCOPE_UPSTREAM
from routstr.core.exceptions import EhbpEncryptedUpstreamError
from routstr.proxy import reinitialize_upstreams

MODEL_ID = "tinfoil-kimi-k2-6"
CIPHERTEXT = b"\x00\x00\x00\x02cb"
RESPONSE_NONCE = "ab" * 32
ARCHITECTURE = {
    "modality": "text",
    "input_modalities": ["text"],
    "output_modalities": ["text"],
    "tokenizer": "unknown",
    "instruct_type": None,
}
PRICING = {
    "prompt": 1e-7,
    "completion": 2e-7,
    "request": 0.0,
    "image": 0.0,
    "web_search": 0.0,
    "internal_reasoning": 0.0,
}


async def _seed_tinfoil_model(session: AsyncSession) -> None:
    provider = UpstreamProviderRow(
        provider_type="tinfoil",
        base_url="https://inference.tinfoil.sh",
        api_key="test-key",
        provider_fee=1.0,
    )
    session.add(provider)
    await session.commit()
    await session.refresh(provider)
    assert provider.id is not None

    session.add(
        ModelRow(
            id=MODEL_ID,
            upstream_provider_id=provider.id,
            name=MODEL_ID,
            description="Tinfoil test model",
            created=0,
            context_length=128000,
            architecture=json.dumps(ARCHITECTURE),
            pricing=json.dumps(PRICING),
            enabled=True,
        )
    )
    await session.commit()
    await reinitialize_upstreams()


def _encrypted_error() -> EhbpEncryptedUpstreamError:
    passthrough = Response(
        content=CIPHERTEXT,
        status_code=404,
        headers={
            "content-type": "application/octet-stream",
            "ehbp-response-nonce": RESPONSE_NONCE,
            ERROR_SCOPE_HEADER: ERROR_SCOPE_UPSTREAM,
        },
    )
    return EhbpEncryptedUpstreamError(
        f"EHBP upstream tinfoil returned 404 for model {MODEL_ID}",
        status_code=404,
        response=passthrough,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_encrypted_ehbp_error_is_relayed_verbatim(
    authenticated_client: AsyncClient,
    integration_session: AsyncSession,
) -> None:
    await _seed_tinfoil_model(integration_session)

    with patch(
        "routstr.proxy.forward_ehbp_request",
        AsyncMock(side_effect=_encrypted_error()),
    ):
        response = await authenticated_client.post(
            "/v1/chat/completions",
            content=b"opaque-request-ciphertext",
            headers={
                "content-type": "application/octet-stream",
                "ehbp-encapsulated-key": "test-encapsulated-key",
                "x-routstr-model": MODEL_ID,
            },
        )

    assert response.status_code == 404
    assert response.content == CIPHERTEXT
    assert response.headers["ehbp-response-nonce"] == RESPONSE_NONCE
    assert response.headers[ERROR_SCOPE_HEADER] == ERROR_SCOPE_UPSTREAM
