"""The Routstr top-up threshold has to state its unit.

``topup_amount_limit`` was already plain sats — it goes straight to
``send_token(amount, "sat", ...)`` and is added to the balance for logging — but
its sibling ``topup_threshold`` was compared as ``balance >= threshold * 1000``.
Two keys from one settings blob, two different units, and nothing naming either.
An operator asking for "top up below 1000 sats" got one below 1,000,000.

``topup_threshold_sats`` says what it means. Legacy values keep their effective
trigger point: reinterpreting them as sats would silently drop the trigger a
thousandfold and let a provider run dry.
"""

import json
from typing import Any
from unittest.mock import patch

import pytest

from routstr.core.db import UpstreamProviderRow, create_session
from routstr.upstream import auto_topup as auto_topup_module
from routstr.upstream.auto_topup import (
    _check_and_topup,
    validate_routstr_auto_topup_settings,
)

from .test_routstr_auto_topup_claim import _patch_wallet, _peer, _sent_tokens

# No module-level asyncio mark: the settings cases are sync, and the suite
# already runs asyncio in auto mode.

TOPUP_SATS = 50


@pytest.fixture(autouse=True)
def _forget_legacy_hints() -> Any:
    # The hint fires once per provider for the life of the process. Reach it
    # through the module: a sibling test reloads auto_topup, which rebinds the
    # set, so a name imported here would go on clearing the old one.
    auto_topup_module._legacy_threshold_hinted.clear()
    yield
    auto_topup_module._legacy_threshold_hinted.clear()


async def _seed(**topup_settings: Any) -> UpstreamProviderRow:
    row = UpstreamProviderRow(
        id=1,
        slug="peer-1",
        provider_type="routstr",
        base_url="https://peer.test",
        api_key="secret",
        enabled=True,
        provider_settings=json.dumps(
            {
                "auto_topup": True,
                "topup_amount_limit": TOPUP_SATS,
                "topup_mint_url": "https://mint.test",
                **topup_settings,
            }
        ),
    )
    async with create_session() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


async def _topped_up(row: UpstreamProviderRow, balance: float) -> bool:
    peer = _peer(balance)
    with _patch_wallet(auto_topup_module, peer, "cashu-token-1"):
        await _check_and_topup(row)
    return bool(await _sent_tokens())


async def test_explicit_sats_threshold_is_compared_against_a_sats_balance(
    patched_db_engine: Any,
) -> None:
    row = await _seed(topup_threshold_sats=1000)
    assert await _topped_up(row, 1500.0) is False


async def test_explicit_sats_threshold_tops_up_below_the_stated_amount(
    patched_db_engine: Any,
) -> None:
    row = await _seed(topup_threshold_sats=1000)
    assert await _topped_up(row, 500.0) is True


@pytest.mark.parametrize(
    ("balance", "expected"),
    [(1500.0, False), (500.0, True)],
)
async def test_legacy_threshold_keeps_its_effective_trigger_point(
    patched_db_engine: Any, balance: float, expected: bool
) -> None:
    # 1 x 1000 == the 1000 sats the old comparison actually used. Reading it as
    # 1 sat instead would leave the peer to run dry.
    row = await _seed(topup_threshold=1)
    assert await _topped_up(row, balance) is expected


async def test_explicit_sats_threshold_overrides_the_legacy_key(
    patched_db_engine: Any,
) -> None:
    row = await _seed(topup_threshold=1, topup_threshold_sats=100)
    assert await _topped_up(row, 500.0) is False


async def test_legacy_threshold_reports_the_sats_value_to_migrate_to(
    patched_db_engine: Any,
) -> None:
    row = await _seed(topup_threshold=1)
    # The app logger does not propagate to root, so caplog would see nothing.
    with patch.object(auto_topup_module, "logger") as log:
        await _topped_up(row, 5000.0)
        hints = [
            c for c in log.warning.call_args_list if "topup_threshold_sats" in c.args[0]
        ]
        assert len(hints) == 1
        assert hints[0].kwargs["extra"]["threshold_sats"] == 1000.0

        # The scheduler runs every minute; the hint must not run with it.
        await _topped_up(row, 5000.0)
        assert (
            sum("topup_threshold_sats" in c.args[0] for c in log.warning.call_args_list)
            == 1
        )


def test_settings_accept_the_sats_threshold_on_its_own() -> None:
    assert (
        validate_routstr_auto_topup_settings(
            {
                "auto_topup": True,
                "topup_threshold_sats": 1000,
                "topup_amount_limit": TOPUP_SATS,
                "topup_mint_url": "https://mint.test",
            }
        )
        is None
    )


@pytest.mark.parametrize("value", [0, -1, True, float("inf"), "1000", None])
def test_settings_reject_an_unusable_sats_threshold(value: object) -> None:
    assert validate_routstr_auto_topup_settings(
        {
            "auto_topup": True,
            "topup_threshold_sats": value,
            "topup_amount_limit": TOPUP_SATS,
            "topup_mint_url": "https://mint.test",
        }
    )


def test_settings_require_one_of_the_threshold_keys() -> None:
    assert validate_routstr_auto_topup_settings(
        {
            "auto_topup": True,
            "topup_amount_limit": TOPUP_SATS,
            "topup_mint_url": "https://mint.test",
        }
    )
