import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from routstr import node_coordination
from routstr.core.db import CashuTransaction, create_session
from routstr.upstream.auto_topup import (
    _routstr_topup_lock_path,
    get_routstr_auto_topup_state,
    release_routstr_auto_topup_state,
)

pytestmark = pytest.mark.asyncio


async def _seed_pending(provider_id: int = 1) -> CashuTransaction:
    transaction = CashuTransaction(
        id="routstr-reservation-1",
        token="cashu-bearer-token",
        amount=50,
        unit="sat",
        type="out",
        request_id=f"routstr-auto-topup:{provider_id}",
        mint_url="https://mint.test",
        collected=False,
        swept=False,
        source="auto_topup",
    )
    async with create_session() as session:
        session.add(transaction)
        await session.commit()
    return transaction


async def test_release_endpoint_requires_explicit_confirmation(
    patched_db_engine: Any,
) -> None:
    from routstr.core.admin import (
        ReleaseRoutstrAutoTopupRequest,
        release_routstr_auto_topup_api,
    )

    with patch("routstr.core.admin._require_routstr_provider", new=AsyncMock()):
        with pytest.raises(HTTPException) as caught:
            await release_routstr_auto_topup_api(
                1,
                ReleaseRoutstrAutoTopupRequest(
                    confirmed_token_was_not_used=False,
                    state_token="routstr-reservation-1",
                ),
            )

    assert caught.value.status_code == 400


async def test_state_hides_bearer_token(patched_db_engine: Any) -> None:
    transaction = await _seed_pending()

    state = await get_routstr_auto_topup_state(1)

    assert state == {
        "active": True,
        "state_token": transaction.id,
        "created_at": transaction.created_at,
        "amount": 50,
        "unit": "sat",
        "mint_url": "https://mint.test",
    }
    assert "token" not in state


async def test_release_requires_the_reviewed_state_token(
    patched_db_engine: Any,
) -> None:
    await _seed_pending()

    with patch(
        "routstr.upstream.auto_topup.release_token_reservation", new=AsyncMock()
    ) as release:
        outcome = await release_routstr_auto_topup_state(1, state_token="stale")

    assert outcome.released is False
    assert outcome.reason == "stale_state"
    release.assert_not_awaited()


async def test_release_waits_for_active_topup(
    patched_db_engine: Any,
) -> None:
    transaction = await _seed_pending()
    with patch(
        "routstr.upstream.auto_topup.release_token_reservation", new=AsyncMock()
    ):
        async with node_coordination.exclusive_lock(_routstr_topup_lock_path(1)):
            release = asyncio.create_task(
                release_routstr_auto_topup_state(1, state_token=transaction.id)
            )
            await asyncio.sleep(0.05)
            assert not release.done()
        outcome = await release

    assert outcome.released is True


async def test_release_frees_token_and_closes_pending_row(
    patched_db_engine: Any,
) -> None:
    transaction = await _seed_pending()

    with patch(
        "routstr.upstream.auto_topup.release_token_reservation", new=AsyncMock()
    ) as release:
        outcome = await release_routstr_auto_topup_state(1, state_token=transaction.id)

    assert outcome.released is True
    release.assert_awaited_once_with("cashu-bearer-token")
    async with create_session() as session:
        stored = await session.get(CashuTransaction, transaction.id)
    assert stored is not None and stored.swept is True
    assert await get_routstr_auto_topup_state(1) == {"active": False}
