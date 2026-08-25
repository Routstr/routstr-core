"""The served catalog is the last guard between a stored row and a charge.

Stored pricing is JSON written by whatever produced the row — an upstream
import, an operator, a legacy migration, or a foreign writer that never passed
the admin edge. So the read path cannot assume a stored rate is a number: it
must decline to serve a row it cannot bill on, and it must survive a row it
cannot read at all rather than taking the whole catalog down with it.

The admin listing is deliberately exempt: it includes disabled models and is the
one view that still shows the operator the row that needs repair.
"""

from __future__ import annotations

import json

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import ModelRow, UpstreamProviderRow
from routstr.payment.models import list_models

_ARCHITECTURE = json.dumps(
    {
        "modality": "text",
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "tokenizer": "unknown",
        "instruct_type": None,
    }
)


async def _make_provider(session: AsyncSession) -> int:
    provider = UpstreamProviderRow(
        provider_type="generic",
        base_url="https://served-upstream.example/v1",
        api_key="test-key",
        provider_fee=1.0,
    )
    session.add(provider)
    await session.commit()
    await session.refresh(provider)
    assert provider.id is not None
    return provider.id


async def _insert_row(
    session: AsyncSession,
    provider_id: int,
    *,
    model_id: str,
    pricing: dict[str, object],
) -> None:
    session.add(
        ModelRow(
            id=model_id,
            name=model_id,
            description="d",
            created=0,
            context_length=8192,
            architecture=_ARCHITECTURE,
            pricing=json.dumps(pricing),
            upstream_provider_id=provider_id,
            enabled=True,
            forwarded_model_id=model_id,
        )
    )
    await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_rate",
    [float("nan"), float("inf"), -1.0],
    ids=["nan", "inf", "negative"],
)
async def test_served_catalog_excludes_a_malformed_stored_rate(
    integration_session: AsyncSession, bad_rate: float
) -> None:
    """A stored rate that is not a number must not be advertised.

    Zero is a real price and a free model is servable, but a negative or
    non-finite rate is not a price at all: serving it advertises a rate the cost
    calculation cannot bill on, so every request falls through to the flat
    maximum reservation — or, for a negative rate, bills an amount settlement
    credits back to the caller.
    """
    provider_id = await _make_provider(integration_session)
    await _insert_row(
        integration_session,
        provider_id,
        model_id="good",
        pricing={"prompt": 1e-06, "completion": 2e-06},
    )
    await _insert_row(
        integration_session,
        provider_id,
        model_id="bad-rate",
        pricing={"prompt": bad_rate, "completion": 2e-06},
    )

    served = {m.id for m in await list_models(integration_session, provider_id)}

    assert served == {"good"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_free_stored_price_is_still_served(
    integration_session: AsyncSession,
) -> None:
    """Zero is a real price. Rejecting malformed rates must not also drop a row
    priced at zero, which is a free model and not a broken one."""
    provider_id = await _make_provider(integration_session)
    await _insert_row(
        integration_session,
        provider_id,
        model_id="free",
        pricing={"prompt": 0.0, "completion": 0.0},
    )

    served = {m.id for m in await list_models(integration_session, provider_id)}

    assert served == {"free"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_admin_listing_still_shows_a_malformed_stored_rate(
    integration_session: AsyncSession,
) -> None:
    """The operator has to be able to see the row that needs fixing.

    The backstop keeps a malformed row out of the *served* catalog. The listing
    that includes disabled models is the one view where the row must still
    appear, or the operator loses the ability to repair it.
    """
    provider_id = await _make_provider(integration_session)
    await _insert_row(
        integration_session,
        provider_id,
        model_id="bad-rate",
        pricing={"prompt": -1.0, "completion": 2e-06},
    )

    listed = {
        m.id
        for m in await list_models(
            integration_session, provider_id, include_disabled=True
        )
    }

    assert listed == {"bad-rate"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_one_unreadable_stored_price_does_not_blank_the_catalog(
    integration_session: AsyncSession,
) -> None:
    """A single unparseable row must cost that row, not every model on the node.

    Stored pricing is JSON written by whatever produced the row, so a
    non-numeric rate is reachable from a legacy import or a foreign writer.
    Parsing it raises out of the row-to-model conversion, and because the
    conversion ran inside the catalog loop the exception took the whole listing
    with it — one bad row and the node advertised nothing at all.
    """
    provider_id = await _make_provider(integration_session)
    await _insert_row(
        integration_session,
        provider_id,
        model_id="good",
        pricing={"prompt": 1e-06, "completion": 2e-06},
    )
    await _insert_row(
        integration_session,
        provider_id,
        model_id="unreadable",
        pricing={"prompt": "not-a-number", "completion": 2e-06},
    )

    served = {m.id for m in await list_models(integration_session, provider_id)}

    assert served == {"good"}


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    [
        "image",
        "web_search",
        "internal_reasoning",
        "input_cache_read",
        "input_cache_write",
    ],
)
async def test_served_catalog_excludes_a_malformed_auxiliary_rate(
    integration_session: AsyncSession, field: str
) -> None:
    """The backstop covers every billable rate, not only the token rates.

    A price whose ``prompt``/``completion`` are sound can still carry a
    malformed request, image, search, reasoning or cache rate — the catalog
    import filter never inspects those — and the request that hits one is billed
    against it just the same.
    """
    provider_id = await _make_provider(integration_session)
    await _insert_row(
        integration_session,
        provider_id,
        model_id="good",
        pricing={"prompt": 1e-06, "completion": 2e-06},
    )
    await _insert_row(
        integration_session,
        provider_id,
        model_id="bad-aux",
        pricing={"prompt": 1e-06, "completion": 2e-06, field: -1.0},
    )

    served = {m.id for m in await list_models(integration_session, provider_id)}

    assert served == {"good"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_negative_request_rate_is_clamped_on_read_and_still_served(
    integration_session: AsyncSession,
) -> None:
    """``request`` is the one billable rate the row-to-model conversion repairs.

    It clamps a negative stored ``request`` to zero before the price is built,
    so the backstop never sees one and the row is served at a zero request rate
    — money-safe, and the reason ``request`` is absent from the list of rates
    above. Pinned here so that if the clamp goes, this rate joins that list
    rather than quietly becoming the one unguarded field.
    """
    provider_id = await _make_provider(integration_session)
    await _insert_row(
        integration_session,
        provider_id,
        model_id="neg-request",
        pricing={"prompt": 1e-06, "completion": 2e-06, "request": -1.0},
    )

    served = await list_models(integration_session, provider_id)

    assert [m.id for m in served] == ["neg-request"]
    assert served[0].pricing.request == 0.0
