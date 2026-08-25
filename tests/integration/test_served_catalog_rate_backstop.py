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
from routstr.proxy import reinitialize_upstreams

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
    await reinitialize_upstreams()
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
