"""
Integration tests for general information endpoints that don't require authentication.
Tests GET /, GET /v1/models, and GET /admin/ endpoints.
"""

from typing import Any

import pytest
from httpx import AsyncClient

from .utils import PerformanceValidator


@pytest.mark.integration
@pytest.mark.asyncio
async def test_root_endpoint_structure_and_performance(
    integration_client: AsyncClient, db_snapshot: Any
) -> None:
    """Test GET / endpoint response structure and performance requirements"""

    # Capture initial database state
    await db_snapshot.capture()

    # Test performance
    validator = PerformanceValidator()

    # Run multiple requests to get reliable timing
    responses = []
    for i in range(10):
        start = validator.start_timing("root_endpoint")
        response = await integration_client.get("/v1/info")
        duration = validator.end_timing("root_endpoint", start)
        responses.append(response)

        # Each individual request should be fast
        assert duration < 1.0, f"Single request took {duration:.3f}s (too slow)"

    # All requests should succeed
    for response in responses:
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"

    # Validate performance requirement: 95th percentile < 500ms
    perf_result = validator.validate_response_time(
        "root_endpoint", max_duration=0.5, percentile=0.95
    )
    assert perf_result["valid"], (
        f"Performance requirement failed: 95th percentile was "
        f"{perf_result['percentile_time']:.3f}s (required < 0.5s)"
    )

    # Validate response structure using the last response
    response = responses[-1]
    data = response.json()

    # Required fields in response
    required_fields = [
        "name",
        "description",
        "version",
        "npub",
        "mints",
        "http_url",
        "onion_url",
    ]
    for field in required_fields:
        assert field in data, f"Missing required field: {field}"

    # Validate field types
    assert isinstance(data["name"], str)
    assert isinstance(data["description"], str)
    assert isinstance(data["version"], str)
    assert isinstance(data["npub"], str)
    assert isinstance(data["mints"], list)
    assert isinstance(data["http_url"], str)
    assert isinstance(data["onion_url"], str)

    # Ensure models field is not present (removed as per issue #184)
    assert "models" not in data, "Models field should not be present in base URL output"

    # Verify no database state changes
    diff = await db_snapshot.diff()
    assert len(diff["api_keys"]["added"]) == 0
    assert len(diff["api_keys"]["removed"]) == 0
    assert len(diff["api_keys"]["modified"]) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_root_endpoint_environment_variables(
    integration_client: AsyncClient,
    test_mode: str,
) -> None:
    """Test that root endpoint reflects environment variable configuration"""

    response = await integration_client.get("/v1/info")
    assert response.status_code == 200

    data = response.json()

    # Check that environment variables are reflected in response
    # In mock mode, URLs are adjusted to localhost
    if test_mode == "docker":
        assert "http://mint:3338" in data["mints"]
    else:
        assert "http://localhost:3338" in data["mints"]

    # Name should have a default value or be configurable
    assert len(data["name"]) > 0

    # Description should have a default value
    assert len(data["description"]) > 0

    # Version should be set
    assert len(data["version"]) > 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_models_endpoint_structure_and_performance(
    integration_client: AsyncClient, db_snapshot: Any
) -> None:
    """Test GET /v1/models endpoint with OpenAI-compatible structure"""

    # Capture initial database state
    await db_snapshot.capture()

    # Test performance
    validator = PerformanceValidator()

    # Run multiple requests for performance measurement
    responses = []
    for i in range(10):
        start = validator.start_timing("models_endpoint")
        response = await integration_client.get("/v1/models")
        duration = validator.end_timing("models_endpoint", start)
        responses.append(response)

        # Each request should be reasonably fast
        assert duration < 1.0, f"Models request took {duration:.3f}s (too slow)"

    # All requests should succeed
    for response in responses:
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"

    # Validate performance requirement
    perf_result = validator.validate_response_time(
        "models_endpoint", max_duration=0.5, percentile=0.95
    )
    assert perf_result["valid"], (
        f"Models endpoint performance failed: 95th percentile was "
        f"{perf_result['percentile_time']:.3f}s (required < 0.5s)"
    )

    # Validate response structure
    response = responses[-1]
    data = response.json()

    # Should have OpenAI-compatible structure
    assert "data" in data
    assert isinstance(data["data"], list)

    # Validate each model structure
    for model in data["data"]:
        # Required OpenAI model fields
        required_fields = ["id", "name", "created"]
        for field in required_fields:
            assert field in model, f"Model missing required field: {field}"

        # Validate field types
        assert isinstance(model["id"], str)
        assert isinstance(model["name"], str)
        assert isinstance(model["created"], (int, float))

        # Check for additional expected fields
        optional_fields = [
            "description",
            "context_length",
            "architecture",
            "pricing",
            "sats_pricing",
        ]
        for field in optional_fields:
            if field in model:
                if field == "pricing" or field == "sats_pricing":
                    # Pricing fields can be dict or None
                    assert isinstance(model[field], (dict, type(None)))
                elif field == "context_length":
                    assert isinstance(model[field], (int, type(None)))
                elif field == "architecture":
                    assert isinstance(model[field], (dict, type(None)))

    # Verify no database state changes
    diff = await db_snapshot.diff()
    assert len(diff["api_keys"]["added"]) == 0
    assert len(diff["api_keys"]["removed"]) == 0
    assert len(diff["api_keys"]["modified"]) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_models_endpoint_pricing_structure(
    integration_client: AsyncClient,
) -> None:
    """Test that models endpoint includes proper pricing information"""

    response = await integration_client.get("/v1/models")
    assert response.status_code == 200

    data = response.json()

    # If models exist, validate pricing structure
    for model in data["data"]:
        if "pricing" in model and model["pricing"]:
            pricing = model["pricing"]

            # Common pricing fields
            expected_pricing_fields = ["prompt", "completion", "request"]
            for field in expected_pricing_fields:
                if field in pricing:
                    # Should be numeric string or number
                    assert isinstance(pricing[field], (str, int, float))

        if "sats_pricing" in model and model["sats_pricing"]:
            sats_pricing = model["sats_pricing"]

            # Sats pricing should be numeric
            for key, value in sats_pricing.items():
                if value is not None:
                    assert isinstance(value, (int, float, str))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_models_endpoint_accept_headers(integration_client: AsyncClient) -> None:
    """Test models endpoint with different Accept headers"""

    # Test JSON accept header (should work)
    response = await integration_client.get(
        "/v1/models", headers={"Accept": "application/json"}
    )
    assert response.status_code == 200
    assert "application/json" in response.headers["content-type"]
    data = response.json()
    assert "data" in data

    # Test HTML accept header (should still return JSON)
    response = await integration_client.get(
        "/v1/models", headers={"Accept": "text/html"}
    )
    assert response.status_code == 200
    # Endpoint always returns JSON regardless of Accept header
    assert "application/json" in response.headers["content-type"]

    # Test wildcard accept header
    response = await integration_client.get("/v1/models", headers={"Accept": "*/*"})
    assert response.status_code == 200
    assert "application/json" in response.headers["content-type"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_admin_endpoint_unauthenticated(
    integration_client: AsyncClient, db_snapshot: Any
) -> None:
    """Test GET /admin/ endpoint redirects to /"""
    await db_snapshot.capture()

    response = await integration_client.get("/admin/")

    assert response.status_code == 307
    assert response.headers.get("location") == "/"

    diff = await db_snapshot.diff()
    assert len(diff["api_keys"]["added"]) == 0
    assert len(diff["api_keys"]["removed"]) == 0
    assert len(diff["api_keys"]["modified"]) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_all_info_endpoints_no_database_changes(
    integration_client: AsyncClient, db_snapshot: Any
) -> None:
    """Verify that all info endpoints don't modify database state"""

    # Capture initial state
    initial_state = await db_snapshot.capture()

    # Make requests to all info endpoints
    endpoints = ["/v1/info", "/v1/models"]

    for endpoint in endpoints:
        response = await integration_client.get(endpoint)
        assert response.status_code == 200

        # Check no database changes after each request
        diff = await db_snapshot.diff()
        assert len(diff["api_keys"]["added"]) == 0, (
            f"Endpoint {endpoint} added API keys"
        )
        assert len(diff["api_keys"]["removed"]) == 0, (
            f"Endpoint {endpoint} removed API keys"
        )
        assert len(diff["api_keys"]["modified"]) == 0, (
            f"Endpoint {endpoint} modified API keys"
        )

    # Final verification - database state should be identical
    final_state = await db_snapshot.capture()
    assert final_state == initial_state


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_info_endpoint_requests(
    integration_client: AsyncClient,
) -> None:
    """Test concurrent requests to info endpoints don't cause issues"""

    from .utils import ConcurrencyTester

    # Create concurrent requests to all endpoints
    requests = []
    for endpoint in ["/", "/v1/models"]:
        for _ in range(5):  # 5 requests per endpoint
            # Use /v1/info instead of / for JSON API
            url = "/v1/info" if endpoint == "/" else endpoint
            requests.append({"method": "GET", "url": url})

    # Execute concurrently
    tester = ConcurrencyTester()
    responses = await tester.run_concurrent_requests(
        integration_client, requests, max_concurrent=10
    )

    # All should succeed
    assert len(responses) == 10  # 2 endpoints × 5 requests each
    for response in responses:
        assert response.status_code == 200
        assert "application/json" in response.headers["content-type"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_info_endpoints_response_consistency(
    integration_client: AsyncClient,
) -> None:
    """Test that info endpoints return consistent responses across multiple calls"""

    # Test root endpoint consistency
    responses = []
    for _ in range(5):
        response = await integration_client.get("/v1/info")
        assert response.status_code == 200
        responses.append(response.json())

    # All responses should be identical (assuming no background updates)
    first_response = responses[0]
    for response in responses[1:]:
        # Core fields should remain consistent
        for field in ["name", "description", "version"]:
            assert response[field] == first_response[field]  # type: ignore[index]

    # Test models endpoint consistency
    model_responses = []
    for _ in range(5):
        response = await integration_client.get("/v1/models")
        assert response.status_code == 200
        model_responses.append(response.json())

    # Model structure should be consistent
    first_models = model_responses[0]["data"]
    for response in model_responses[1:]:
        models = response["data"]  # type: ignore[index]
        assert len(models) == len(first_models)

        # Model IDs should be the same
        first_ids = {m["id"] for m in first_models}
        response_ids = {m["id"] for m in models}
        assert first_ids == response_ids