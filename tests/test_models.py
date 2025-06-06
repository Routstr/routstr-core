import pytest
import asyncio
from unittest.mock import patch, AsyncMock
from router.models import Model, Architecture, Pricing, TopProvider, update_sats_pricing, MODELS


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
            instruct_type="chat"
        ),
        pricing=Pricing(
            prompt=0.01,
            completion=0.02,
            request=0.001,
            image=0.0,
            web_search=0.0,
            internal_reasoning=0.0
        ),
        top_provider=TopProvider(
            context_length=4096,
            max_completion_tokens=2048,
            is_moderated=False
        )
    )


@pytest.mark.asyncio
async def test_update_sats_pricing_calculation(sample_model: Model):
    """Test that sats pricing is calculated correctly."""
    # Mock the sats_usd_ask_price function
    with patch("router.models.sats_usd_ask_price", new_callable=AsyncMock) as mock_price:
        mock_price.return_value = 0.0001  # 1 sat = 0.0001 USD
        
        # Temporarily replace MODELS
        original_models = MODELS[:]
        MODELS.clear()
        MODELS.append(sample_model)
        
        # Run one iteration of the pricing update
        sleep_called = asyncio.Event()
        
        async def mock_sleep(duration):
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
                assert sample_model.sats_pricing.prompt == pytest.approx(0.01 / 0.0001)  # 100 sats
                assert sample_model.sats_pricing.completion == pytest.approx(0.02 / 0.0001)  # 200 sats
                assert sample_model.sats_pricing.request == pytest.approx(0.001 / 0.0001)  # 10 sats
                
                # Verify max_cost calculation for model with top_provider
                expected_max_context = 4096 * sample_model.sats_pricing.prompt
                expected_max_completion = 2048 * sample_model.sats_pricing.completion
                assert sample_model.sats_pricing.max_cost == pytest.approx(expected_max_context + expected_max_completion)
                
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
async def test_update_sats_pricing_without_top_provider():
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
            instruct_type=None
        ),
        pricing=Pricing(
            prompt=0.01,
            completion=0.02,
            request=0.001,
            image=0.01,
            web_search=0.005,
            internal_reasoning=0.015
        ),
        top_provider=None
    )
    
    with patch("router.models.sats_usd_ask_price", new_callable=AsyncMock) as mock_price:
        mock_price.return_value = 0.0001  # 1 sat = 0.0001 USD
        
        original_models = MODELS[:]
        MODELS.clear()
        MODELS.append(model_without_top)
        
        sleep_called = asyncio.Event()
        
        async def mock_sleep(duration):
            sleep_called.set()
            raise asyncio.CancelledError()
        
        with patch("asyncio.sleep", side_effect=mock_sleep):
            try:
                task = asyncio.create_task(update_sats_pricing())
                await sleep_called.wait()
                
                assert model_without_top.sats_pricing is not None
                
                # Verify the fallback max_cost calculation
                p = model_without_top.sats_pricing.prompt * 1_000_000
                c = model_without_top.sats_pricing.completion * 32_000
                r = model_without_top.sats_pricing.request * 100_000
                i = model_without_top.sats_pricing.image * 100
                w = model_without_top.sats_pricing.web_search * 1000
                ir = model_without_top.sats_pricing.internal_reasoning * 100
                expected_max = p + c + r + i + w + ir
                
                assert model_without_top.sats_pricing.max_cost == pytest.approx(expected_max)
                
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
async def test_update_sats_pricing_handles_errors():
    """Test that update_sats_pricing handles errors gracefully."""
    with patch("router.models.sats_usd_ask_price", new_callable=AsyncMock) as mock_price:
        mock_price.side_effect = Exception("API Error")
        
        with patch("router.models.logger.error") as mock_log_error:
            sleep_called = asyncio.Event()
            
            async def mock_sleep(duration):
                sleep_called.set()
                raise asyncio.CancelledError()
            
            with patch("asyncio.sleep", side_effect=mock_sleep):
                try:
                    task = asyncio.create_task(update_sats_pricing())
                    await sleep_called.wait()
                    
                    # Verify error was logged
                    mock_log_error.assert_called()
                    
                    # Cancel and await the task
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    
                except asyncio.CancelledError:
                    pass


def test_model_serialization(sample_model: Model):
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