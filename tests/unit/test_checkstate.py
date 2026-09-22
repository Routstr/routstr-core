import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from cashu.core.base import ProofSpentState

from routstr import checkstate
from routstr.checkstate import _learned_sizes, filter_unspent_proofs
from routstr.mint import MintRateGuard, fail_fast_mint_operations


@pytest.fixture(autouse=True)
def isolate():
    _learned_sizes.clear()
    MintRateGuard._guards.clear()
    yield
    _learned_sizes.clear()
    MintRateGuard._guards.clear()


def proofs(count):
    return [Mock(Y=str(i)) for i in range(count)]


def response(batch):
    return SimpleNamespace(
        states=[SimpleNamespace(Y=p.Y, state=ProofSpentState.unspent) for p in batch]
    )


def rejection(status):
    request = httpx.Request("POST", "https://mint.test/v1/checkstate")
    return httpx.HTTPStatusError(
        "rejected",
        request=request,
        response=httpx.Response(
            status,
            request=request,
            headers={"content-type": "text/html", "retry-after": "120"},
        ),
    )


def wallet(check):
    return Mock(
        url="https://mint.test",
        check_proof_state=AsyncMock(side_effect=check),
        set_reserved_for_send=AsyncMock(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [413, 500])
async def test_adapts_and_reuses_size_without_skipping_proofs(status):
    checked = []

    async def check(batch):
        if len(batch) > 120:
            raise rejection(status)
        checked.extend(batch)
        return response(batch)

    w = wallet(check)
    ps = proofs(1001)
    assert await filter_unspent_proofs(ps, w) == ps
    assert checked == ps
    sizes = [len(c.args[0]) for c in w.check_proof_state.await_args_list]
    assert sizes[:5] == [1000, 500, 250, 125, 62]
    w.check_proof_state.reset_mock()
    assert await filter_unspent_proofs(ps, w) == ps
    assert max(len(c.args[0]) for c in w.check_proof_state.await_args_list) == 62


@pytest.mark.asyncio
async def test_size_fallback_works_inside_cooldown_probe_under_wallet_guard():
    async def check(batch):
        if len(batch) > 2:
            raise rejection(500)
        return response(batch)

    w = wallet(check)
    MintRateGuard.get(w.url).apply_cooldown(0, reason="transport")
    async with fail_fast_mint_operations():
        ps = proofs(8)
        assert await filter_unspent_proofs(ps, w) == ps
    assert MintRateGuard.get(w.url).cooldown_remaining() == 0


@pytest.mark.asyncio
async def test_429_is_not_a_size_signal():
    w = wallet(Mock(side_effect=rejection(429)))
    with pytest.raises(httpx.HTTPStatusError):
        await filter_unspent_proofs(proofs(1000), w, retry_on_rate_limit=False)
    assert w.check_proof_state.await_count == 1
    assert not _learned_sizes
    assert MintRateGuard.get(w.url).cooldown_remaining() > 100


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 422, 503])
async def test_other_http_errors_are_not_split(status):
    w = wallet(Mock(side_effect=rejection(status)))
    with pytest.raises(httpx.HTTPStatusError):
        await filter_unspent_proofs(proofs(10), w)
    assert w.check_proof_state.await_count == 1


@pytest.mark.asyncio
async def test_singleton_failure_is_bounded_and_does_not_poison_cache():
    w = wallet(Mock(side_effect=rejection(500)))
    with pytest.raises(httpx.HTTPStatusError):
        await filter_unspent_proofs(proofs(1000), w)
    assert [len(c.args[0]) for c in w.check_proof_state.await_args_list] == [
        1000,
        500,
        250,
        125,
        62,
        31,
        15,
        7,
        3,
        1,
    ]
    assert not _learned_sizes
    w.set_reserved_for_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_budget_counts_successes_and_failures():
    w = wallet(response)
    with (
        patch.object(checkstate, "_DEFAULT_BATCH_SIZE", 1),
        patch.object(checkstate, "_MAX_REQUESTS", 2),
        pytest.raises(ValueError, match="budget"),
    ):
        await filter_unspent_proofs(proofs(3), w)
    assert w.check_proof_state.await_count == 2
    w.set_reserved_for_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_total_deadline_cancels_slow_check():
    async def check(batch):
        await asyncio.Event().wait()

    w = wallet(check)
    with (
        patch.object(checkstate, "_SCAN_TIMEOUT_SECONDS", 0.01),
        pytest.raises(TimeoutError),
    ):
        await filter_unspent_proofs(proofs(1), w)
    assert w.check_proof_state.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("malformation", ["missing", "reordered", "unknown"])
async def test_invalid_response_fails_closed(malformation):
    def check(batch):
        result = response(batch)
        if malformation == "missing":
            result.states.pop()
        elif malformation == "reordered":
            result.states.reverse()
        else:
            result.states[0].state = "UNKNOWN"
        return result

    w = wallet(check)
    with pytest.raises(ValueError, match="Invalid proof-state"):
        await filter_unspent_proofs(proofs(3), w)
    w.set_reserved_for_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_unspent_proofs_are_spendable():
    ps = proofs(3)
    states = [ProofSpentState.unspent, ProofSpentState.pending, ProofSpentState.spent]
    w = wallet(
        lambda batch: SimpleNamespace(
            states=[SimpleNamespace(Y=p.Y, state=s) for p, s in zip(batch, states)]
        )
    )
    assert await filter_unspent_proofs(ps, w) == ps[:1]
    w.set_reserved_for_send.assert_awaited_once_with(ps[2:], reserved=True)


@pytest.mark.asyncio
async def test_learned_size_is_per_mint_and_expires():
    w = wallet(response)
    ps = proofs(5)
    _learned_sizes[w.url] = (1, 0)
    other = wallet(response)
    other.url = "https://other.test"
    _learned_sizes[other.url] = (1, float("inf"))
    with patch.object(checkstate, "_DEFAULT_BATCH_SIZE", 2):
        assert await filter_unspent_proofs(ps, w) == ps
        assert [len(c.args[0]) for c in w.check_proof_state.await_args_list] == [
            2,
            2,
            1,
        ]
        assert await filter_unspent_proofs(ps, other) == ps
        assert [len(c.args[0]) for c in other.check_proof_state.await_args_list] == [
            1
        ] * 5


@pytest.mark.asyncio
async def test_smaller_later_batch_failure_does_not_skip_or_return_partial():
    ps = proofs(9)
    checked = []

    def check(batch):
        if batch[0] is not ps[0] and len(batch) > 1:
            raise rejection(500)
        checked.extend(batch)
        return response(batch)

    w = wallet(check)
    with patch.object(checkstate, "_DEFAULT_BATCH_SIZE", 4):
        assert await filter_unspent_proofs(ps, w) == ps
    assert checked == ps


@pytest.mark.parametrize("status", [413, 500])
@pytest.mark.parametrize("body", [{"detail": "too big"}, "<html>error</html>"])
def test_wallet_adapter_preserves_checkstate_http_status(status, body):
    from routstr.wallet import Wallet

    request = httpx.Request("POST", "https://mint.test/v1/checkstate")
    reply = (
        httpx.Response(status, request=request, json=body)
        if isinstance(body, dict)
        else httpx.Response(status, request=request, text=body)
    )
    with pytest.raises(httpx.HTTPStatusError) as error:
        Wallet.raise_on_error_request(reply)
    assert error.value.response is reply


@pytest.mark.asyncio
async def test_default_batch_fits_real_sdk_model():
    from cashu.core.base import Proof
    from cashu.core.models import PostCheckStateRequest

    limit = PostCheckStateRequest.model_json_schema()["properties"]["Ys"]["maxItems"]
    ps = [
        Proof(id="00", amount=1, secret=f"sdk-{i}", C="02" + "00" * 32)
        for i in range(limit + 1)
    ]
    sizes = []

    def check(batch):
        payload = PostCheckStateRequest(Ys=[p.Y for p in batch])
        sizes.append(len(payload.Ys))
        return response(batch)

    w = wallet(check)
    assert await filter_unspent_proofs(ps, w) == ps
    assert sizes == [limit, 1]


@pytest.mark.asyncio
async def test_scan_deadline_opens_cooldown_for_next_guarded_scan():
    from routstr.mint import MintCooldownError

    async def check(batch):
        await asyncio.Event().wait()

    w = wallet(check)
    with patch.object(checkstate, "_SCAN_TIMEOUT_SECONDS", 0.01):
        async with fail_fast_mint_operations():
            with pytest.raises(TimeoutError):
                await filter_unspent_proofs(proofs(1), w)
            with pytest.raises(MintCooldownError):
                await filter_unspent_proofs(proofs(1), w)
    assert w.check_proof_state.await_count == 1
    assert MintRateGuard.get(w.url).cooldown_reason() == "transport"


@pytest.mark.asyncio
async def test_external_cancellation_does_not_open_cooldown():
    started = asyncio.Event()

    async def check(batch):
        started.set()
        await asyncio.Event().wait()

    w = wallet(check)
    task = asyncio.create_task(filter_unspent_proofs(proofs(1), w))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert MintRateGuard.get(w.url).cooldown_remaining() == 0


@pytest.mark.asyncio
async def test_scan_deadline_preserves_longer_rate_limit_cooldown():
    w = wallet(response)
    guard = MintRateGuard.get(w.url)
    guard.apply_rate_limit_cooldown(120)
    until = guard._cooldown_until
    with patch.object(checkstate, "_SCAN_TIMEOUT_SECONDS", 0.01):
        with pytest.raises(TimeoutError):
            await filter_unspent_proofs(proofs(1), w)
    assert guard._cooldown_until == until
    assert guard.cooldown_reason() == "rate_limited"
    w.check_proof_state.assert_not_awaited()
