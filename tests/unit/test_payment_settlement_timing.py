from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

import routstr.upstream.base as base_module


@pytest.mark.asyncio
async def test_payment_settlement_logs_its_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def settle(*_args: Any, **_kwargs: Any) -> dict[str, int]:
        return {"total_cost": 1}

    log_info = Mock()
    monkeypatch.setattr(base_module, "_adjust_payment_for_tokens", settle)
    monkeypatch.setattr(base_module.logger, "info", log_info)
    key = SimpleNamespace(hashed_key="abcdefgh1234")

    result = await base_module.adjust_payment_for_tokens(key, {}, object(), 10)

    assert result == {"total_cost": 1}
    log_info.assert_called_once()
    (message,) = log_info.call_args.args
    extra = log_info.call_args.kwargs["extra"]
    assert message == "Payment settlement finished"
    assert extra["settlement_duration_ms"] >= 0
    assert extra["settlement_succeeded"] is True


@pytest.mark.asyncio
async def test_payment_settlement_logs_failure_without_swallowing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(*_args: Any, **_kwargs: Any) -> dict:
        raise RuntimeError("database locked")

    log_info = Mock()
    monkeypatch.setattr(base_module, "_adjust_payment_for_tokens", fail)
    monkeypatch.setattr(base_module.logger, "info", log_info)
    key = SimpleNamespace(hashed_key="abcdefgh1234")

    with pytest.raises(RuntimeError, match="database locked"):
        await base_module.adjust_payment_for_tokens(key, {}, object(), 10)

    extra = log_info.call_args.kwargs["extra"]
    assert extra["settlement_duration_ms"] >= 0
    assert extra["settlement_succeeded"] is False
