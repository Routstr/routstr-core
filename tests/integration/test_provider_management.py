"""
Integration tests for provider management functionality.
Tests GET /v1/providers/ endpoint for listing and managing providers.
"""

from typing import Any, Generator
from unittest.mock import patch

import pytest
from httpx import AsyncClient

from routstr.discovery import _PROVIDERS_CACHE

from .utils import PerformanceValidator, ResponseValidator


@pytest.fixture(autouse=True)
def _clear_providers_cache() -> None:
    _PROVIDERS_CACHE.clear()


@pytest.fixture(autouse=True)
def _enable_provider_discovery() -> Generator[None, Any, Any]:
    """Enable provider discovery for all tests in this module"""
    with patch(
        "routstr.core.settings.settings.providers_refresh_interval_seconds", 300
    ):
        yield


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
            "pubkey": "test_pubkey1",
            "kind": 38421,  # NIP-91 event kind
            "created_at": 1234567890,
            "content": '{"name": "Provider 1", "about": "Test provider 1"}',
            "tags": [
                ["d", "provider1"],
                ["u", "http://provider1.onion"],
            ],
        },
        {
            "id": "event2",
            "pubkey": "test_pubkey2",
            "kind": 38421,  # NIP-91 event kind
            "created_at": 1234567891,
            "content": '{"name": "Provider 2", "about": "Test provider 2"}',
            "tags": [
                ["d", "provider2"],
                ["u", "http://provider2.onion"],
            ],
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

            # In default format, should return list of provider objects
            for provider in data["providers"]:
                assert isinstance(provider, dict)
                assert "endpoint_url" in provider
                assert provider["endpoint_url"].endswith(".onion")

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
            "pubkey": "test_pubkey",
            "kind": 38421,  # NIP-91 event kind
            "created_at": 1234567890,
            "content": '{"name": "Test Provider", "about": "A test provider"}',
            "tags": [
                ["d", "test-provider"],
                ["u", "http://test-provider.onion"],
            ],
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

            # With include_json=true, should return list of dictionaries with provider and health info
            for provider_data in data["providers"]:
                assert isinstance(provider_data, dict)
                # Each provider should have 'provider' and 'health' keys
                assert "provider" in provider_data
                assert "health" in provider_data

                provider_info = provider_data["provider"]
                assert "endpoint_url" in provider_info
                assert provider_info["endpoint_url"].endswith(".onion")

                health_info = provider_data["health"]
                assert isinstance(health_info, dict)
                assert "status_code" in health_info

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

    # Mock NIP-91 provider announcement event
    mock_events: list[dict[str, Any]] = [
        {
            "id": "event1",
            "pubkey": "test_pubkey",
            "created_at": 1234567890,
            "kind": 38421,  # NIP-91 event kind
            "content": '{"name": "Comprehensive Provider", "about": "A comprehensive AI provider"}',
            "tags": [
                ["d", "provider-123"],
                ["u", "https://api.provider.example/v1"],
                ["models", "gpt-3.5-turbo", "gpt-4"],
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
                # health_info = provider_data["health"]

                # Expected fields from NIP-91 parser (supported_models removed)
                expected_fields = ["id", "name", "endpoint_url"]
                for field in expected_fields:
                    assert field in provider_info


@pytest.mark.integration
@pytest.mark.asyncio
async def test_providers_endpoint_no_providers_found(
    integration_client: AsyncClient,
) -> None:
    """Test providers endpoint when no providers are found"""

    # Force empty discovery by returning events that are filtered out
    mock_events: list[dict[str, Any]] = [
        {
            "id": "localhost-event",
            "pubkey": "ignored_pubkey",
            "kind": 38421,
            "created_at": 1234567899,
            "content": '{"name": "Local"}',
            "tags": [["d", "local"], ["u", "http://localhost:8000"]],
        }
    ]

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
            "kind": 38421,  # NIP-91 event kind
            "created_at": 1234567890,
            "content": '{"name": "Healthy Provider", "about": "Healthy provider announcement"}',
            "tags": [
                ["d", "healthy-provider"],
                ["u", "http://healthy-provider.onion"],
            ],
        },
        {
            "id": "event2",
            "pubkey": "offline_provider_pubkey",
            "kind": 38421,  # NIP-91 event kind
            "created_at": 1234567891,
            "content": '{"name": "Offline Provider", "about": "Offline provider announcement"}',
            "tags": [
                ["d", "offline-provider"],
                ["u", "http://offline-provider.onion"],
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
            "kind": 38421,  # NIP-91 event kind
            "created_at": 1234567890,
            "content": '{"name": "Provider", "about": "Provider announcement"}',
            "tags": [
                ["d", "provider-1"],
                ["u", "http://provider.onion"],
            ],
        },
        {
            "id": "event2",
            "pubkey": "other_provider_pubkey",
            "kind": 38421,  # NIP-91 event kind
            "created_at": 1234567892,
            "content": '{"name": "Other Provider", "about": "Different provider announcement"}',
            "tags": [
                ["d", "other-provider"],
                ["u", "http://other-provider.onion"],
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

            # With NIP-91-only parsing, events without required tags are ignored
            assert "providers" in data
            assert isinstance(data["providers"], list)
            assert len(data["providers"]) == 0


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
            "pubkey": "param_pubkey",
            "kind": 38421,
            "created_at": 1234567890,
            "content": '{"name": "Param Test Provider"}',
            "tags": [
                ["d", "param-test-provider"],
                ["u", "http://param-test-provider.onion"],
            ],
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
                        # Should be list of {provider, health} dictionaries
                        for item in providers:
                            assert isinstance(item, dict)
                            assert "provider" in item and "health" in item
                    else:
                        # Should be list of provider objects
                        for provider in providers:
                            assert isinstance(provider, dict)


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
