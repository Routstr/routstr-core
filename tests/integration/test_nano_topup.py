"""XNO top-ups: /v2/nano/invoice quotes, /v2/nano/claim credits once."""

import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import ApiKey, NanoTopup
from routstr.core.main import app
from routstr.core.settings import settings
from routstr.nano import NanoBlock, NanoRpcError, get_nano_rpc

NODE = "nano_3t6k35gi95xu6tergt6p69ck76ogmitsa8mnijtpxm9fkcm736xtoncuohr3"
OTHER = "nano_1111111111111111111111111111111111111111111111111111hifc8npp"
RAW_PER_SAT = 10**27 + 5
HASH_A = "A" * 64
HASH_B = "B" * 64


class FakeRpc:
    def __init__(self) -> None:
        self.blocks: dict[str, NanoBlock] = {}
        self.fail = False
        self.calls: list[str] = []

    def send(
        self,
        block_hash: str,
        amount_raw: int,
        *,
        destination: str = NODE,
        confirmed: bool = True,
        subtype: str = "send",
        timestamp: int | None = None,
    ) -> None:
        self.blocks[block_hash] = NanoBlock(
            hash=block_hash,
            confirmed=confirmed,
            subtype=subtype,
            destination=destination,
            amount_raw=amount_raw,
            local_timestamp=int(time.time()) if timestamp is None else timestamp,
        )

    async def block_info(self, block_hash: str) -> NanoBlock:
        self.calls.append(block_hash)
        if self.fail or block_hash not in self.blocks:
            raise NanoRpcError("block_info error: Block not found")
        return self.blocks[block_hash]


@pytest.fixture
def nano_enabled() -> Iterator[None]:
    with (
        patch.object(settings, "receive_nano_address", NODE),
        patch.object(settings, "nano_rpc_url", "http://nano-node.test"),
        patch.object(settings, "nano_raw_per_sat", str(RAW_PER_SAT)),
    ):
        yield


@pytest.fixture
def rpc(integration_app: FastAPI) -> Iterator[FakeRpc]:
    # Routes resolve overrides on the app that registered them.
    fake = FakeRpc()
    app.dependency_overrides[get_nano_rpc] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_nano_rpc, None)


@pytest.fixture
async def api_key(integration_session: AsyncSession) -> str:
    key = ApiKey(hashed_key="nanotopupkey", balance=1_000_000)
    integration_session.add(key)
    await integration_session.commit()
    return "sk-nanotopupkey"


async def _invoice(client: AsyncClient, api_key: str, sats: int = 500) -> Any:
    resp = await client.post(
        "/v2/nano/invoice",
        json={"amount_sats": sats},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _claim(
    client: AsyncClient, invoice_id: str, block_hash: str
) -> tuple[int, Any]:
    resp = await client.post(
        "/v2/nano/claim", json={"invoice_id": invoice_id, "block_hash": block_hash}
    )
    return resp.status_code, resp.json()


async def _balance(session: AsyncSession, api_key: str) -> int:
    key = await session.get(ApiKey, api_key[3:])
    assert key is not None
    await session.refresh(key)
    return key.balance


@pytest.mark.asyncio
async def test_routes_answer_404_when_not_configured(
    integration_client: AsyncClient, api_key: str
) -> None:
    with patch.object(settings, "receive_nano_address", ""):
        for path, body in [
            ("/v2/nano/invoice", {"amount_sats": 500}),
            ("/v2/nano/claim", {"invoice_id": "x", "block_hash": HASH_A}),
            ("/v2/nano/invoice", {"not": "valid"}),
        ]:
            resp = await integration_client.post(
                path, json=body, headers={"Authorization": f"Bearer {api_key}"}
            )
            assert resp.status_code == 404
            assert resp.json()["error"] == {
                "message": f"Path '{path}' not found",
                "type": "not_found",
                "code": 404,
            }


@pytest.mark.asyncio
async def test_no_rate_means_no_offer(
    integration_client: AsyncClient, api_key: str, nano_enabled: None
) -> None:
    with patch.object(settings, "nano_raw_per_sat", ""):
        resp = await integration_client.post(
            "/v2/nano/invoice",
            json={"amount_sats": 500},
            headers={"Authorization": f"Bearer {api_key}"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_invoice_quotes_raw_for_node_address(
    integration_client: AsyncClient, api_key: str, nano_enabled: None
) -> None:
    invoice = await _invoice(integration_client, api_key, 500)
    amount_raw = int(invoice["amount_raw"])
    assert invoice["address"] == NODE
    assert isinstance(invoice["amount_raw"], str)
    assert amount_raw >= 500 * RAW_PER_SAT
    assert amount_raw - 500 * RAW_PER_SAT < 2_000_000
    assert invoice["uri"] == f"nano:{NODE}?amount={invoice['amount_raw']}"
    assert invoice["expires_at"] > int(time.time())


@pytest.mark.asyncio
async def test_invoice_requires_known_api_key(
    integration_client: AsyncClient, nano_enabled: None
) -> None:
    resp = await integration_client.post("/v2/nano/invoice", json={"amount_sats": 1})
    assert resp.status_code == 401
    resp = await integration_client.post(
        "/v2/nano/invoice",
        json={"amount_sats": 1},
        headers={"Authorization": "Bearer sk-missing"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "topup_api_key_not_found"


@pytest.mark.asyncio
async def test_claim_credits_once_and_refuses_replay(
    integration_client: AsyncClient,
    integration_session: AsyncSession,
    api_key: str,
    nano_enabled: None,
    rpc: FakeRpc,
) -> None:
    invoice = await _invoice(integration_client, api_key, 500)
    rpc.send(HASH_A, int(invoice["amount_raw"]))

    status, body = await _claim(integration_client, invoice["invoice_id"], HASH_A)
    assert status == 200, body
    assert body["status"] == "paid" and body["amount_sats"] == 500
    assert await _balance(integration_session, api_key) == 1_000_000 + 500_000

    status, body = await _claim(integration_client, invoice["invoice_id"], HASH_A)
    assert status == 409

    second = await _invoice(integration_client, api_key, 500)
    status, body = await _claim(
        integration_client, second["invoice_id"], HASH_A.lower()
    )
    assert status == 409
    assert body["error"]["code"] == "nano_block_already_claimed"
    assert await _balance(integration_session, api_key) == 1_000_000 + 500_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("block", "status", "code"),
    [
        ({"confirmed": False}, 409, "nano_block_unconfirmed"),
        ({"subtype": "receive"}, 400, "nano_block_not_send"),
        ({"destination": OTHER}, 400, "nano_wrong_destination"),
        ({"short": True}, 400, "nano_amount_mismatch"),
        ({"other_tag": True}, 400, "nano_amount_mismatch"),
        ({"late": True}, 400, "nano_invoice_expired"),
        ({"early": True}, 400, "nano_invoice_expired"),
        ({"rpc_error": True}, 502, "nano_rpc_error"),
    ],
)
async def test_claim_refusals_do_not_credit(
    integration_client: AsyncClient,
    integration_session: AsyncSession,
    api_key: str,
    nano_enabled: None,
    rpc: FakeRpc,
    block: dict[str, Any],
    status: int,
    code: str,
) -> None:
    invoice = await _invoice(integration_client, api_key, 500)
    amount = int(invoice["amount_raw"])
    if block.pop("short", False):
        amount -= 1_000_000
    if block.pop("other_tag", False):
        amount += 1
    timestamp = None
    if block.pop("late", False):
        timestamp = invoice["expires_at"] + 1
    if block.pop("early", False):
        timestamp = invoice["expires_at"] - 7200
    rpc.fail = block.pop("rpc_error", False)
    rpc.send(HASH_B, amount, timestamp=timestamp, **block)

    got_status, body = await _claim(integration_client, invoice["invoice_id"], HASH_B)
    assert got_status == status, body
    assert body["error"]["code"] == code
    assert await _balance(integration_session, api_key) == 1_000_000
    row = await integration_session.get(NanoTopup, invoice["invoice_id"])
    assert row is not None
    await integration_session.refresh(row)
    assert row.status == "pending" and row.block_hash is None


@pytest.mark.asyncio
async def test_overpayment_with_matching_tag_credits_quoted_amount(
    integration_client: AsyncClient,
    integration_session: AsyncSession,
    api_key: str,
    nano_enabled: None,
    rpc: FakeRpc,
) -> None:
    invoice = await _invoice(integration_client, api_key, 500)
    rpc.send(HASH_A, int(invoice["amount_raw"]) + 10**30)
    status, _ = await _claim(integration_client, invoice["invoice_id"], HASH_A)
    assert status == 200
    assert await _balance(integration_session, api_key) == 1_000_000 + 500_000


@pytest.mark.asyncio
async def test_claim_rejects_malformed_hash_and_unknown_invoice(
    integration_client: AsyncClient, nano_enabled: None, rpc: FakeRpc
) -> None:
    status, body = await _claim(integration_client, "nope", "xyz")
    assert status == 400 and body["error"]["code"] == "invalid_block_hash"
    status, body = await _claim(integration_client, "nope", HASH_A)
    assert status == 404 and body["error"]["code"] == "invoice_not_found"
    assert rpc.calls == []


@pytest.mark.asyncio
async def test_invoice_claim_then_balance_info(
    integration_client: AsyncClient,
    testmint_wallet: Any,
    create_api_key: Any,
    nano_enabled: None,
    rpc: FakeRpc,
) -> None:
    api_key, balance = await create_api_key(
        integration_client, testmint_wallet, amount=1000
    )
    invoice = await _invoice(integration_client, api_key, 2100)
    rpc.send(HASH_A, int(invoice["amount_raw"]))

    status, body = await _claim(integration_client, invoice["invoice_id"], HASH_A)
    assert status == 200, body

    info = await integration_client.get(
        "/v1/balance/info", headers={"Authorization": f"Bearer {api_key}"}
    )
    assert info.status_code == 200
    assert info.json()["balance"] == balance + 2_100_000
