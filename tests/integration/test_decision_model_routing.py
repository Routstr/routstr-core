"""Decision models are refused on text routes before any balance is reserved."""

import uuid
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import ApiKey
from routstr.payment.models import Architecture, Model, Pricing


def _decision_model() -> Model:
    return Model(
        id="laya-rl-agent",
        name="laya-rl-agent",
        created=0,
        description="",
        context_length=32_000,
        architecture=Architecture(
            modality="text->decisions",
            input_modalities=["text"],
            output_modalities=["decisions"],
            tokenizer="Other",
            instruct_type=None,
        ),
        pricing=Pricing(prompt=0.042 / 1_000_000, completion=0.0),
    )


@pytest.mark.asyncio
async def test_decision_model_on_chat_completions_is_refused(
    integration_client: AsyncClient,
    integration_session: AsyncSession,
) -> None:
    key = ApiKey(
        hashed_key=f"test_{uuid.uuid4().hex}",
        balance=1_000_000,
        reserved_balance=0,
        total_spent=0,
    )
    integration_session.add(key)
    await integration_session.commit()

    upstream = MagicMock()
    upstream.prepare_headers = MagicMock(return_value={})

    with patch(
        "routstr.proxy.get_candidates",
        return_value=[(_decision_model(), upstream)],
    ):
        response = await integration_client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer sk-{key.hashed_key}"},
            json={
                "model": "laya-rl-agent",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert response.status_code == 400
    assert "/v1/systemone" in response.text
    upstream.forward_request.assert_not_called()

    await integration_session.refresh(key)
    assert key.balance == 1_000_000
    assert key.reserved_balance == 0
    assert key.total_spent == 0
