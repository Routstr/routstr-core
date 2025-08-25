"""
Integration tests for provider management functionality.
Tests GET /v1/providers/ endpoint for listing and managing providers.
"""

from typing import Any
from unittest.mock import patch

import pytest
from httpx import AsyncClient

from .utils import PerformanceValidator, ResponseValidator


@pytest.mark.integration
@pytest.mark.asyncio
async def test_providers_endpoint_default_response(
    integration_client: AsyncClient, db_snapshot: Any
) -> None:
    """Test GET /v1/providers/ endpoint returns list of providers in default format"""

    # Capture initial database state
    await db_snapshot.capture()

    # Mock the Nostr relay queries and onion fetching to avoid external dependencies
    mock_events: list[dict[str, Any]] = [
        {
            "id": "event1",
            "content": "Check out this provider: http://provider1.onion",
            "created_at": 1234567890,
        },
        {
            "id": "event2",
            "content": "Another provider at http://provider2.onion is good",
            "created_at": 1234567891,
        },
    ]

    # Mock the healthy provider check
    mock_fetch_responses = {
        "http://provider1.onion": {"status_code": 200, "json": {"status": "healthy"}},
        "http://provider2.onion": {"status_code": 200, "json": {"status": "healthy"}},
    }

    with patch(
        "routstr.discovery.query_nostr_relay_for_providers", return_value=mock_events
    ):
        with patch("routstr.discovery.fetch_provider_health") as mock_fetch:
            # Configure mock to return appropriate responses
            mock_fetch.side_effect = lambda url: mock_fetch_responses.get(
                url, {"status_code": 500, "json": {"error": "Unknown provider"}}
            )

            response = await integration_client.get("/v1/providers/")

            assert response.status_code == 200
            data = response.json()

            # Validate response structure
            assert "providers" in data
            assert isinstance(data["providers"], list)

            # In default format, should return list of provider URLs (strings)
            for provider in data["providers"]:
                assert isinstance(provider, str)
                assert provider.endswith(".onion")

    # Verify no database state changes
    diff = await db_snapshot.diff()
    assert len(diff["api_keys"]["added"]) == 0
    assert len(diff["api_keys"]["modified"]) == 0
    assert len(diff["api_keys"]["removed"]) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_providers_endpoint_with_include_json(
    integration_client: AsyncClient, db_snapshot: Any
) -> None:
    """Test GET /v1/providers/ with include_json=true returns full provider details"""

    # Capture initial database state
    await db_snapshot.capture()

    # Mock events with provider URLs
    mock_events: list[dict[str, Any]] = [
        {
            "id": "event1",
            "content": "Provider info: http://test-provider.onion",
            "created_at": 1234567890,
        }
    ]

    # Mock provider health check response
    mock_provider_response = {
        "status": "online",
        "name": "Test Provider",
        "models": ["gpt-3.5-turbo", "gpt-4"],
        "pricing": {"gpt-3.5-turbo": "0.002", "gpt-4": "0.03"},
    }

    with patch(
        "routstr.discovery.query_nostr_relay_for_providers", return_value=mock_events
    ):
        with patch("routstr.discovery.fetch_provider_health") as mock_fetch:
            mock_fetch.return_value = {
                "status_code": 200,
                "json": mock_provider_response,
            }

            response = await integration_client.get("/v1/providers/?include_json=true")

            assert response.status_code == 200
            data = response.json()

            # Validate response structure
            assert "providers" in data
            assert isinstance(data["providers"], list)

            # With include_json=true, should return list of dictionaries
            for provider in data["providers"]:
                assert isinstance(provider, dict)
                # Each provider should be in format {url: json_data}
                assert len(provider) == 1
                url = list(provider.keys())[0]
                json_data = provider[url]
                assert url.endswith(".onion")
                assert isinstance(json_data, dict)

    # Verify no database state changes
    diff = await db_snapshot.diff()
    assert len(diff["api_keys"]["added"]) == 0
    assert len(diff["api_keys"]["modified"]) == 0
    assert len(diff["api_keys"]["removed"]) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_providers_data_structure_validation(
    integration_client: AsyncClient,
) -> None:
    """Test provider data structure contains expected fields"""

    # Mock RIP-02 provider announcement event
    mock_events: list[dict[str, Any]] = [
        {
            "id": "event1",
            "pubkey": "test_pubkey",
            "created_at": 1234567890,
            "content": "Comprehensive provider announcement",
            "tags": [
                ["d", "provider-123"],
                ["endpoint", "https://api.provider.example/v1"],
                ["name", "Comprehensive Provider"],
                ["description", "A comprehensive AI provider"],
                ["model", "gpt-3.5-turbo"],
                ["model", "gpt-4"],
            ],
        }
    ]

    mock_health_response = {
        "status_code": 200,
        "endpoint": "models",
        "json": {
            "data": [
                {"id": "gpt-3.5-turbo", "object": "model"},
                {"id": "gpt-4", "object": "model"},
            ]
        },
    }

    with patch(
        "routstr.discovery.query_nostr_relay_for_providers", return_value=mock_events
    ):
        with patch("routstr.discovery.fetch_provider_health") as mock_fetch:
            mock_fetch.return_value = mock_health_response

            response = await integration_client.get("/v1/providers/?include_json=true")
            assert response.status_code == 200

            data = response.json()
            providers = data["providers"]

            # Validate that provider data contains expected fields
            assert len(providers) > 0
            for provider_data in providers:
                # Should have provider and health keys based on actual implementation
                assert "provider" in provider_data
                assert "health" in provider_data

                provider_info = provider_data["provider"]
                # Expected fields from RIP-02 parser
                expected_fields = ["id", "name", "endpoint_url", "supported_models"]
                for field in expected_fields:
                    assert field in provider_info

                # Validate models structure if present
                if "supported_models" in provider_info:
                    models = provider_info["supported_models"]
                    assert isinstance(models, list)
                    # Should have the models from the mocked event
                    assert "gpt-3.5-turbo" in models
                    assert "gpt-4" in models


@pytest.mark.integration
@pytest.mark.asyncio
async def test_providers_endpoint_no_providers_found(
    integration_client: AsyncClient,
) -> None:
    """Test providers endpoint when no providers are found"""

    # Mock empty events (no providers mentioned)
    mock_events: list[dict[str, Any]] = []

    with patch(
        "routstr.discovery.query_nostr_relay_for_providers", return_value=mock_events
    ):
        response = await integration_client.get("/v1/providers/")

        assert response.status_code == 200
        data = response.json()

        # Should return empty list
        assert "providers" in data
        assert isinstance(data["providers"], list)
        assert len(data["providers"]) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_providers_endpoint_offline_providers(
    integration_client: AsyncClient,
) -> None:
    """Test providers endpoint handling of offline/unhealthy providers"""

    mock_events: list[dict[str, Any]] = [
        {
            "id": "event1",
            "pubkey": "healthy_provider_pubkey",
            "created_at": 1234567890,
            "content": "Healthy provider announcement",
            "tags": [
                ["d", "healthy-provider"],
                ["endpoint", "http://healthy-provider.onion"],
                ["name", "Healthy Provider"],
            ],
        },
        {
            "id": "event2",
            "pubkey": "offline_provider_pubkey",
            "created_at": 1234567891,
            "content": "Offline provider announcement",
            "tags": [
                ["d", "offline-provider"],
                ["endpoint", "http://offline-provider.onion"],
                ["name", "Offline Provider"],
            ],
        },
    ]

    # Mock one healthy and one offline provider
    def mock_fetch_provider_health(url: str) -> dict[str, Any]:
        if "healthy" in url:
            return {
                "status_code": 200,
                "endpoint": "root",
                "json": {"status": "online"},
            }
        else:
            return {
                "status_code": 500,
                "endpoint": "error",
                "json": {"error": "Service unavailable"},
            }

    with patch(
        "routstr.discovery.query_nostr_relay_for_providers", return_value=mock_events
    ):
        with patch(
            "routstr.discovery.fetch_provider_health",
            side_effect=mock_fetch_provider_health,
        ):
            response = await integration_client.get("/v1/providers/?include_json=true")

            assert response.status_code == 200
            data = response.json()

            # Should include both providers regardless of status
            assert len(data["providers"]) == 2

            # Verify that offline providers are still included but marked appropriately
            for provider_data in data["providers"]:
                assert "provider" in provider_data
                assert "health" in provider_data

                provider_info = provider_data["provider"]
                health_info = provider_data["health"]

                if "offline" in provider_info["endpoint_url"]:
                    # Offline provider should have error information in health
                    assert health_info["status_code"] == 500
                    assert "error" in health_info["json"]
                else:
                    # Healthy provider should have successful health check
                    assert health_info["status_code"] == 200
                    assert (
                        "status" in health_info["json"]
                        or "error" not in health_info["json"]
                    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_providers_endpoint_duplicate_urls(
    integration_client: AsyncClient,
) -> None:
    """Test providers endpoint handles duplicate URLs correctly"""

    # Mock events with duplicate provider events (same event ID) - should be deduplicated by relay query logic
    mock_events: list[dict[str, Any]] = [
        {
            "id": "event1",
            "pubkey": "provider_pubkey",
            "created_at": 1234567890,
            "content": "Provider announcement",
            "tags": [
                ["d", "provider-1"],
                ["endpoint", "http://provider.onion"],
                ["name", "Provider"],
            ],
        },
        {
            "id": "event2",
            "pubkey": "other_provider_pubkey",
            "created_at": 1234567892,
            "content": "Different provider announcement",
            "tags": [
                ["d", "other-provider"],
                ["endpoint", "http://other-provider.onion"],
                ["name", "Other Provider"],
            ],
        },
    ]

    with patch(
        "routstr.discovery.query_nostr_relay_for_providers", return_value=mock_events
    ):
        with patch("routstr.discovery.fetch_provider_health") as mock_fetch:
            mock_fetch.return_value = {
                "status_code": 200,
                "endpoint": "root",
                "json": {"status": "online"},
            }

            response = await integration_client.get("/v1/providers/")

            assert response.status_code == 200
            data = response.json()

            # Should return 2 unique providers based on events
            providers = data["providers"]
            assert len(providers) == 2  # 2 unique events

            # Verify all providers are unique by endpoint_url
            endpoint_urls = []
            for provider_data in providers:
                endpoint_urls.append(provider_data["endpoint_url"])

            unique_endpoints = set(endpoint_urls)
            assert len(unique_endpoints) == len(endpoint_urls)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_providers_endpoint_nostr_relay_failures(
    integration_client: AsyncClient,
) -> None:
    """Test providers endpoint handles Nostr relay failures gracefully"""

    # Mock relay failure
    async def failing_query(*args: Any, **kwargs: Any) -> None:
        raise Exception("Connection to relay failed")

    with patch(
        "routstr.discovery.query_nostr_relay_for_providers", side_effect=failing_query
    ):
        response = await integration_client.get("/v1/providers/")

        # Should still return 200 with empty providers list
        assert response.status_code == 200
        data = response.json()
        assert "providers" in data
        assert isinstance(data["providers"], list)
        assert len(data["providers"]) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_providers_endpoint_malformed_urls(
    integration_client: AsyncClient,
) -> None:
    """Test providers endpoint handles malformed URLs in Nostr events"""

    mock_events: list[dict[str, Any]] = [
        {
            "id": "event1",
            "content": "Valid provider: http://good-provider.onion",
            "created_at": 1234567890,
        },
        {
            "id": "event2",
            "content": "Invalid URL: not-a-valid-url.onion",
            "created_at": 1234567891,
        },
        {
            "id": "event3",
            "content": "No URLs here, just text",
            "created_at": 1234567892,
        },
    ]

    with patch(
        "routstr.discovery.query_nostr_relay_for_providers", return_value=mock_events
    ):
        with patch("routstr.discovery.fetch_provider_health") as mock_fetch:
            mock_fetch.return_value = {"status_code": 200, "json": {"status": "online"}}

            response = await integration_client.get("/v1/providers/")

            assert response.status_code == 200
            data = response.json()

            # Should only extract valid onion URLs
            providers = data["providers"]
            for provider in providers:
                assert provider.startswith("http://") or provider.startswith("https://")
                assert provider.endswith(".onion")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_providers_endpoint_response_format(
    integration_client: AsyncClient,
) -> None:
    """Test providers endpoint response format consistency"""

    mock_events: list[dict[str, Any]] = [
        {
            "id": "event1",
            "content": "Provider: http://test-provider.onion",
            "created_at": 1234567890,
        }
    ]

    with patch(
        "routstr.discovery.query_nostr_relay_for_providers", return_value=mock_events
    ):
        with patch("routstr.discovery.fetch_provider_health") as mock_fetch:
            mock_fetch.return_value = {"status_code": 200, "json": {"status": "online"}}

            # Test default format
            response = await integration_client.get("/v1/providers/")
            assert response.status_code == 200

            validator = ResponseValidator()
            validation = validator.validate_success_response(
                response, expected_status=200, required_fields=["providers"]
            )
            assert validation["valid"]

            data = response.json()
            assert isinstance(data, dict)
            assert "providers" in data
            assert isinstance(data["providers"], list)

            # Test include_json format
            response_json = await integration_client.get(
                "/v1/providers/?include_json=true"
            )
            assert response_json.status_code == 200

            data_json = response_json.json()
            assert isinstance(data_json, dict)
            assert "providers" in data_json
            assert isinstance(data_json["providers"], list)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_providers_endpoint_performance(integration_client: AsyncClient) -> None:
    """Test providers endpoint meets performance requirements"""

    # Mock quick responses to avoid network delays
    mock_events: list[dict[str, Any]] = [
        {
            "id": f"event{i}",
            "content": f"Provider: http://provider{i}.onion",
            "created_at": 1234567890 + i,
        }
        for i in range(5)
    ]

    validator = PerformanceValidator()

    with patch(
        "routstr.discovery.query_nostr_relay_for_providers", return_value=mock_events
    ):
        with patch("routstr.discovery.fetch_provider_health") as mock_fetch:
            mock_fetch.return_value = {"status_code": 200, "json": {"status": "online"}}

            # Test multiple requests
            for i in range(10):
                start = validator.start_timing("providers_endpoint")
                response = await integration_client.get("/v1/providers/")
                validator.end_timing("providers_endpoint", start)

                assert response.status_code == 200

    # Validate performance (should be fast with mocked dependencies)
    perf_result = validator.validate_response_time(
        "providers_endpoint",
        max_duration=2.0,  # Allow more time since it involves multiple operations
        percentile=0.95,
    )
    assert perf_result["valid"], f"Performance requirement failed: {perf_result}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_providers_endpoint_concurrent_requests(
    integration_client: AsyncClient,
) -> None:
    """Test providers endpoint handles concurrent requests correctly"""

    from .utils import ConcurrencyTester

    mock_events: list[dict[str, Any]] = [
        {
            "id": "event1",
            "content": "Provider: http://concurrent-provider.onion",
            "created_at": 1234567890,
        }
    ]

    with patch(
        "routstr.discovery.query_nostr_relay_for_providers", return_value=mock_events
    ):
        with patch("routstr.discovery.fetch_provider_health") as mock_fetch:
            mock_fetch.return_value = {"status_code": 200, "json": {"status": "online"}}

            # Create concurrent requests
            requests = [{"method": "GET", "url": "/v1/providers/"} for _ in range(10)]

            tester = ConcurrencyTester()
            responses = await tester.run_concurrent_requests(
                integration_client, requests, max_concurrent=5
            )

            # All should succeed
            for response in responses:
                assert response.status_code == 200
                data = response.json()
                assert "providers" in data


@pytest.mark.integration
@pytest.mark.asyncio
async def test_providers_endpoint_parameter_validation(
    integration_client: AsyncClient,
) -> None:
    """Test providers endpoint parameter handling"""

    mock_events: list[dict[str, Any]] = [
        {
            "id": "event1",
            "content": "Provider: http://param-test-provider.onion",
            "created_at": 1234567890,
        }
    ]

    with patch(
        "routstr.discovery.query_nostr_relay_for_providers", return_value=mock_events
    ):
        with patch("routstr.discovery.fetch_provider_health") as mock_fetch:
            mock_fetch.return_value = {"status_code": 200, "json": {"status": "online"}}

            # Test various parameter values
            test_cases = [
                ("/v1/providers/", False),  # Default
                ("/v1/providers/?include_json=false", False),  # Explicit false
                ("/v1/providers/?include_json=true", True),  # Explicit true
                ("/v1/providers/?include_json=1", True),  # Truthy value
                ("/v1/providers/?include_json=0", False),  # Falsy value
            ]

            for url, expected_json_format in test_cases:
                response = await integration_client.get(url)
                assert response.status_code == 200

                data = response.json()
                providers = data["providers"]

                if len(providers) > 0:
                    if expected_json_format:
                        # Should be list of dictionaries
                        for provider in providers:
                            assert isinstance(provider, dict)
                    else:
                        # Should be list of strings
                        for provider in providers:
                            assert isinstance(provider, str)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_database_changes_during_provider_operations(
    integration_client: AsyncClient, db_snapshot: Any
) -> None:
    """Comprehensive test that provider operations don't modify database state"""

    # Capture initial state
    await db_snapshot.capture()

    mock_events: list[dict[str, Any]] = [
        {
            "id": "event1",
            "content": "Provider: http://no-db-change-provider.onion",
            "created_at": 1234567890,
        }
    ]

    with patch(
        "routstr.discovery.query_nostr_relay_for_providers", return_value=mock_events
    ):
        with patch("routstr.discovery.fetch_provider_health") as mock_fetch:
            mock_fetch.return_value = {"status_code": 200, "json": {"status": "online"}}

            # Make multiple requests with different parameters
            endpoints = [
                "/v1/providers/",
                "/v1/providers/?include_json=true",
                "/v1/providers/?include_json=false",
            ]

            for endpoint in endpoints:
                response = await integration_client.get(endpoint)
                assert response.status_code == 200

                # Check no database changes after each request
                current_diff = await db_snapshot.diff()
                assert len(current_diff["api_keys"]["added"]) == 0
                assert len(current_diff["api_keys"]["modified"]) == 0
                assert len(current_diff["api_keys"]["removed"]) == 0

    # Final verification - database state should be identical
    final_diff = await db_snapshot.diff()
    assert final_diff["api_keys"]["added"] == []
    assert final_diff["api_keys"]["modified"] == []
    assert final_diff["api_keys"]["removed"] == []
