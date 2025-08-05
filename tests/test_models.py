import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from router.payment.models import (
    MODELS,
    Architecture,
    Model,
    Pricing,
    TopProvider,
    update_sats_pricing,
)


@pytest.fixture
def sample_model() -> Model:
    """Create a sample model for testing."""
    return Model(
        id="test-model",
        name="Test Model",
        created=1700000000,
        description="A test model",
        context_length=4096,
        architecture=Architecture(
            modality="text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="test_tokenizer",
            instruct_type="chat",
        ),
        pricing=Pricing(
            prompt=0.01,
            completion=0.02,
            request=0.001,
            image=0.0,
            web_search=0.0,
            internal_reasoning=0.0,
        ),
        top_provider=TopProvider(
            context_length=4096, max_completion_tokens=2048, is_moderated=False
        ),
    )


@pytest.mark.asyncio
async def test_update_sats_pricing_calculation(sample_model: Model) -> None:
    """Test that sats pricing is calculated correctly."""
    # Mock the sats_usd_ask_price function
    with patch(
        "router.payment.models.sats_usd_ask_price", new_callable=AsyncMock
    ) as mock_price:
        mock_price.return_value = 0.0001  # 1 sat = 0.0001 USD

        # Temporarily replace MODELS
        original_models = MODELS[:]
        MODELS.clear()
        MODELS.append(sample_model)

        # Run one iteration of the pricing update
        sleep_called = asyncio.Event()

        async def mock_sleep(duration: float) -> None:
            sleep_called.set()
            raise asyncio.CancelledError()

        with patch("asyncio.sleep", side_effect=mock_sleep):
            try:
                # Create and run the task
                task = asyncio.create_task(update_sats_pricing())

                # Wait for the first iteration to complete
                await sleep_called.wait()

                # Check that sats pricing was calculated
                assert sample_model.sats_pricing is not None

                # Verify calculations (prices in USD / sats_to_usd)
                assert sample_model.sats_pricing.prompt == pytest.approx(
                    0.01 / 0.0001
                )  # 100 sats
                assert sample_model.sats_pricing.completion == pytest.approx(
                    0.02 / 0.0001
                )  # 200 sats
                assert sample_model.sats_pricing.request == pytest.approx(
                    0.001 / 0.0001
                )  # 10 sats

                assert sample_model.top_provider is not None
                assert sample_model.top_provider.context_length is not None
                assert sample_model.top_provider.max_completion_tokens is not None

                assert sample_model.sats_pricing.max_cost == pytest.approx(
                    (
                        sample_model.top_provider.context_length
                        - sample_model.top_provider.max_completion_tokens
                    )
                    * sample_model.sats_pricing.prompt
                    + sample_model.top_provider.max_completion_tokens
                    * sample_model.sats_pricing.completion
                )

                # Cancel and await the task
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            except asyncio.CancelledError:
                pass
            finally:
                # Restore original models
                MODELS.clear()
                MODELS.extend(original_models)


@pytest.mark.asyncio
async def test_update_sats_pricing_without_top_provider() -> None:
    """Test sats pricing calculation for models without top_provider."""
    model_without_top = Model(
        id="test-model-no-top",
        name="Test Model No Top",
        created=1700000000,
        description="A test model without top provider",
        context_length=8192,
        architecture=Architecture(
            modality="text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="test_tokenizer",
            instruct_type=None,
        ),
        pricing=Pricing(
            prompt=0.01,
            completion=0.02,
            request=0.001,
            image=0.01,
            web_search=0.005,
            internal_reasoning=0.015,
        ),
        top_provider=None,
    )

    with patch(
        "router.payment.models.sats_usd_ask_price", new_callable=AsyncMock
    ) as mock_price:
        mock_price.return_value = 0.0001  # 1 sat = 0.0001 USD

        original_models = MODELS[:]
        MODELS.clear()
        MODELS.append(model_without_top)

        sleep_called = asyncio.Event()

        async def mock_sleep(duration: float) -> None:
            sleep_called.set()
            raise asyncio.CancelledError()

        with patch("asyncio.sleep", side_effect=mock_sleep):
            try:
                task = asyncio.create_task(update_sats_pricing())
                await sleep_called.wait()

                assert model_without_top.sats_pricing is not None

                # Verify the fallback max_cost calculation
                assert model_without_top.sats_pricing.max_cost == pytest.approx(
                    model_without_top.context_length
                    * 0.8
                    * model_without_top.sats_pricing.prompt
                    + model_without_top.context_length
                    * 0.2
                    * model_without_top.sats_pricing.completion
                )

                # Cancel and await the task
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            except asyncio.CancelledError:
                pass
            finally:
                MODELS.clear()
                MODELS.extend(original_models)


@pytest.mark.asyncio
async def test_update_sats_pricing_handles_errors() -> None:
    """Test that update_sats_pricing handles errors gracefully."""
    with patch(
        "router.payment.models.sats_usd_ask_price", new_callable=AsyncMock
    ) as mock_price:
        mock_price.side_effect = Exception("API Error")

        error_printed = False
        original_print = print

        def mock_print(*args: Any, **kwargs: Any) -> None:
            nonlocal error_printed
            message = " ".join(str(a) for a in args)
            if "API Error" in message and "Error updating sats pricing" in message:
                error_printed = True
            original_print(*args, **kwargs)

        with patch("builtins.print", side_effect=mock_print):
            sleep_called = asyncio.Event()

            async def mock_sleep(duration: float) -> None:
                sleep_called.set()
                raise asyncio.CancelledError()

            with patch("asyncio.sleep", side_effect=mock_sleep):
                try:
                    task = asyncio.create_task(update_sats_pricing())
                    await sleep_called.wait()

                    # Verify error was printed
                    assert error_printed

                    # Cancel and await the task
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                except asyncio.CancelledError:
                    pass


def test_model_serialization(sample_model: Model) -> None:
    """Test that models can be serialized and deserialized correctly."""
    model_dict = sample_model.dict()

    # Verify all fields are present
    assert model_dict["id"] == "test-model"
    assert model_dict["name"] == "Test Model"
    assert model_dict["pricing"]["prompt"] == 0.01
    assert model_dict["architecture"]["modality"] == "text"
    assert model_dict["top_provider"]["context_length"] == 4096

    # Test deserialization
    new_model = Model(**model_dict)
    assert new_model.id == sample_model.id
    assert new_model.pricing.prompt == pytest.approx(sample_model.pricing.prompt)
