"""Unit tests for Tinfoil direct integration.

Covers the EHBP usage-metrics header parser, the proxy header stripping, the
enclave URL override, and the TinfoilUpstreamProvider model fetching/forwarding
target logic.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routstr.upstream.ehbp import (
    _PROXY_ONLY_HEADERS,
    _compute_ehbp_actual_cost,
    _resolve_ehbp_target_url,
    _strip_proxy_headers,
    parse_tinfoil_usage_metrics,
)
from routstr.upstream.tinfoil import (
    TinfoilModel,
    TinfoilUpstreamProvider,
)

# ---------------------------------------------------------------------------
# parse_tinfoil_usage_metrics
# ---------------------------------------------------------------------------


class TestParseTinfoilUsageMetrics:
    def test_full_header(self):
        result = parse_tinfoil_usage_metrics("prompt=67,completion=42,total=109")
        assert result == {
            "prompt_tokens": 67,
            "completion_tokens": 42,
            "total_tokens": 109,
        }

    def test_without_total(self):
        result = parse_tinfoil_usage_metrics("prompt=10,completion=5")
        assert result == {"prompt_tokens": 10, "completion_tokens": 5}

    def test_none(self):
        assert parse_tinfoil_usage_metrics(None) is None

    def test_empty(self):
        assert parse_tinfoil_usage_metrics("") is None

    def test_malformed(self):
        assert parse_tinfoil_usage_metrics("garbage") is None

    def test_missing_completion(self):
        assert parse_tinfoil_usage_metrics("prompt=10") is None

    def test_extra_whitespace(self):
        result = parse_tinfoil_usage_metrics(
            "prompt = 100 , completion = 200 , total = 300"
        )
        assert result == {
            "prompt_tokens": 100,
            "completion_tokens": 200,
            "total_tokens": 300,
        }


# ---------------------------------------------------------------------------
# _strip_proxy_headers
# ---------------------------------------------------------------------------


class TestStripProxyHeaders:
    def test_strips_all_proxy_only(self):
        headers = {
            "x-routstr-model": "tinfoil-llama3-3-70b",
            "X-Tinfoil-Enclave-Url": "https://inference.tinfoil.sh",
            "X-Tinfoil-Request-Usage-Metrics": "true",
            "Authorization": "Bearer secret",
            "Ehbp-Encapsulated-Key": "abc123",
        }
        clean = _strip_proxy_headers(headers)
        assert "x-routstr-model" not in clean
        assert "X-Tinfoil-Enclave-Url" not in clean
        assert "X-Tinfoil-Request-Usage-Metrics" not in clean
        assert clean["Authorization"] == "Bearer secret"
        assert clean["Ehbp-Encapsulated-Key"] == "abc123"

    def test_all_proxy_only_headers_covered(self):
        assert _PROXY_ONLY_HEADERS == {
            "x-routstr-model",
            "x-tinfoil-enclave-url",
            "x-tinfoil-request-usage-metrics",
        }


# ---------------------------------------------------------------------------
# _resolve_ehbp_target_url
# ---------------------------------------------------------------------------


class TestResolveEhbpTargetUrl:
    def test_override_with_enclave_url_for_tinfoil(self):
        result = _resolve_ehbp_target_url(
            "https://default.example.com/v1/chat/completions",
            "v1/chat/completions",
            {"X-Tinfoil-Enclave-Url": "https://enclave.tinfoil.sh"},
            "tinfoil",
        )
        assert result == "https://enclave.tinfoil.sh/v1/chat/completions"

    def test_override_lowercase_header_for_tinfoil(self):
        result = _resolve_ehbp_target_url(
            "https://default.example.com/v1/chat/completions",
            "v1/chat/completions",
            {"x-tinfoil-enclave-url": "https://enclave.tinfoil.sh"},
            "tinfoil",
        )
        assert result == "https://enclave.tinfoil.sh/v1/chat/completions"

    def test_no_override(self):
        default = "https://inference.tinfoil.sh/v1/chat/completions"
        result = _resolve_ehbp_target_url(
            default,
            "v1/chat/completions",
            {},
            "tinfoil",
        )
        assert result == default

    def test_non_tinfoil_provider_ignores_enclave_url(self):
        default = "https://api.ppq.ai/private/v1/chat/completions"
        result = _resolve_ehbp_target_url(
            default,
            "v1/chat/completions",
            {"X-Tinfoil-Enclave-Url": "https://enclave.tinfoil.sh"},
            "ppqai",
        )
        assert result == default

    @pytest.mark.parametrize(
        "bad_url",
        [
            "http://enclave.tinfoil.sh",
            "https://attacker.example",
            "https://tinfoil.sh.attacker.example",
            "https://127.0.0.1",
            "https://enclave.tinfoil.sh:8443",
            "https://user:pass@enclave.tinfoil.sh",
        ],
    )
    def test_tinfoil_rejects_unsafe_enclave_url(self, bad_url):
        from routstr.core.exceptions import UpstreamError

        with pytest.raises(UpstreamError):
            _resolve_ehbp_target_url(
                "https://default.example.com/v1/chat/completions",
                "v1/chat/completions",
                {"X-Tinfoil-Enclave-Url": bad_url},
                "tinfoil",
            )


# ---------------------------------------------------------------------------
# _compute_ehbp_actual_cost
# ---------------------------------------------------------------------------


class TestComputeEhbpActualCost:
    @pytest.mark.asyncio
    async def test_no_usage_falls_back_to_max_cost(self):
        model_obj = MagicMock()
        model_obj.id = "llama3-3-70b"
        result = await _compute_ehbp_actual_cost(None, model_obj, 100_000)
        assert result == 100_000

    @pytest.mark.asyncio
    async def test_usage_parsed_and_clamped(self):
        model_obj = MagicMock()
        model_obj.id = "llama3-3-70b"
        # The actual cost from calculate_cost will be small; we just verify
        # it's clamped to min_request_msat at minimum.
        with patch(
            "routstr.upstream.ehbp.calculate_cost",
            new_callable=AsyncMock,
        ) as mock_calc:
            from routstr.payment.cost_calculation import CostData

            mock_calc.return_value = CostData(
                base_msats=0,
                input_msats=10,
                output_msats=20,
                total_msats=30,
                total_usd=0.0001,
                input_tokens=67,
                output_tokens=42,
            )
            result = await _compute_ehbp_actual_cost(
                "prompt=67,completion=42,total=109",
                model_obj,
                100_000,
            )
            assert result == 30
            assert result <= 100_000

    @pytest.mark.asyncio
    async def test_max_cost_data_falls_back(self):
        model_obj = MagicMock()
        model_obj.id = "llama3-3-70b"
        with patch(
            "routstr.upstream.ehbp.calculate_cost",
            new_callable=AsyncMock,
        ) as mock_calc:
            from routstr.payment.cost_calculation import MaxCostData

            mock_calc.return_value = MaxCostData(
                base_msats=0,
                input_msats=0,
                output_msats=0,
                total_msats=0,
                total_usd=0.0,
                input_tokens=0,
                output_tokens=0,
            )
            result = await _compute_ehbp_actual_cost(
                "prompt=0,completion=0",
                model_obj,
                50_000,
            )
            assert result == 50_000


# ---------------------------------------------------------------------------
# TinfoilUpstreamProvider
# ---------------------------------------------------------------------------


class TestTinfoilUpstreamProvider:
    def test_provider_type_and_defaults(self):
        assert TinfoilUpstreamProvider.provider_type == "tinfoil"
        assert (
            TinfoilUpstreamProvider.default_base_url
            == "https://inference.tinfoil.sh"
        )
        assert TinfoilUpstreamProvider.supports_ehbp is True

    def test_transform_model_name(self):
        provider = TinfoilUpstreamProvider(api_key="test")
        assert provider.transform_model_name("tinfoil/llama3-3-70b") == "llama3-3-70b"
        assert provider.transform_model_name("llama3-3-70b") == "llama3-3-70b"

    def test_get_ehbp_forwarding_target_includes_usage_header(self):
        provider = TinfoilUpstreamProvider(api_key="test")
        model_obj = MagicMock()
        model_obj.id = "llama3-3-70b"
        model_obj.forwarded_model_id = "llama3-3-70b"
        target = provider.get_ehbp_forwarding_target("v1/chat/completions", model_obj)
        assert (
            target.headers["X-Tinfoil-Request-Usage-Metrics"] == "true"
        )
        assert "v1/chat/completions" in target.url

    def test_get_provider_metadata(self):
        meta = TinfoilUpstreamProvider.get_provider_metadata()
        assert meta["id"] == "tinfoil"
        assert meta["name"] == "Tinfoil"
        assert meta["fixed_base_url"] is True

    def test_tinfoil_model_pricing_parses(self):
        data = {
            "id": "llama3-3-70b",
            "context_window": 128000,
            "created": 1721764788,
            "pricing": {
                "inputTokenPricePer1M": 1.75,
                "outputTokenPricePer1M": 2.75,
                "requestPrice": 0,
            },
            "endpoints": ["/v1/chat/completions"],
            "type": "chat",
        }
        tf = TinfoilModel.parse_obj(data)
        assert tf.id == "llama3-3-70b"
        assert tf.pricing.inputTokenPricePer1M == 1.75
        assert tf.pricing.outputTokenPricePer1M == 2.75

    @pytest.mark.asyncio
    async def test_fetch_models_parses_response(self):
        provider = TinfoilUpstreamProvider(api_key="test")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "data": [
                {
                    "id": "llama3-3-70b",
                    "context_window": 128000,
                    "created": 1721764788,
                    "multimodal": False,
                    "pricing": {
                        "inputTokenPricePer1M": 1.75,
                        "outputTokenPricePer1M": 2.75,
                        "requestPrice": 0,
                    },
                    "endpoints": ["/v1/chat/completions"],
                    "type": "chat",
                }
            ]
        }

        with patch("routstr.upstream.tinfoil.httpx.AsyncClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            models = await provider.fetch_models()

        assert len(models) == 1
        assert models[0].id == "llama3-3-70b"
        assert models[0].pricing.prompt == 1.75 / 1_000_000
        assert models[0].pricing.completion == 2.75 / 1_000_000
        assert models[0].context_length == 128000

    @pytest.mark.asyncio
    async def test_fetch_models_handles_error(self):
        provider = TinfoilUpstreamProvider(api_key="test")
        with patch("routstr.upstream.tinfoil.httpx.AsyncClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(side_effect=Exception("network error"))
            mock_client_cls.return_value = mock_client

            models = await provider.fetch_models()

        assert models == []
