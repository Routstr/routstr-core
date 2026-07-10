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
    _prepare_ehbp_upstream_headers,
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
    def test_full_header(self) -> None:
        result = parse_tinfoil_usage_metrics("prompt=67,completion=42,total=109")
        assert result == {
            "prompt_tokens": 67,
            "completion_tokens": 42,
            "total_tokens": 109,
        }

    def test_without_total(self) -> None:
        result = parse_tinfoil_usage_metrics("prompt=10,completion=5")
        assert result == {"prompt_tokens": 10, "completion_tokens": 5}

    def test_none(self) -> None:
        assert parse_tinfoil_usage_metrics(None) is None

    def test_empty(self) -> None:
        assert parse_tinfoil_usage_metrics("") is None

    def test_malformed(self) -> None:
        assert parse_tinfoil_usage_metrics("garbage") is None

    def test_missing_completion(self) -> None:
        assert parse_tinfoil_usage_metrics("prompt=10") is None

    def test_extra_whitespace(self) -> None:
        result = parse_tinfoil_usage_metrics(
            "prompt = 100 , completion = 200 , total = 300"
        )
        assert result == {
            "prompt_tokens": 100,
            "completion_tokens": 200,
            "total_tokens": 300,
        }

    def test_with_model_field(self) -> None:
        result = parse_tinfoil_usage_metrics(
            "prompt=42,completion=10,total=52,model=llama3-3-70b"
        )
        assert result == {
            "prompt_tokens": 42,
            "completion_tokens": 10,
            "total_tokens": 52,
            "model": "llama3-3-70b",
        }

    def test_with_model_no_total(self) -> None:
        result = parse_tinfoil_usage_metrics(
            "prompt=67,completion=42,model=gpt-oss-120b"
        )
        assert result == {
            "prompt_tokens": 67,
            "completion_tokens": 42,
            "model": "gpt-oss-120b",
        }

    def test_model_with_dashes_and_numbers(self) -> None:
        result = parse_tinfoil_usage_metrics(
            "prompt=1,completion=1,total=2,model=kimi-k2-6"
        )
        assert result["model"] == "kimi-k2-6"

    def test_model_with_extra_fields(self) -> None:
        result = parse_tinfoil_usage_metrics(
            "prompt=69,completion=20,total=89,"
            "cached_prompt_tokens=64,uncached_prompt_tokens=5,"
            "model=kimi-k2-6"
        )
        assert result["prompt_tokens"] == 69
        assert result["completion_tokens"] == 20
        assert result["total_tokens"] == 89
        assert result["model"] == "kimi-k2-6"

    def test_old_format_still_works(self) -> None:
        """Headers without the model field (pre-PR #385) still parse."""
        result = parse_tinfoil_usage_metrics(
            "prompt=67,completion=42,total=109"
        )
        assert result == {
            "prompt_tokens": 67,
            "completion_tokens": 42,
            "total_tokens": 109,
        }
        assert "model" not in result


# ---------------------------------------------------------------------------
# _strip_proxy_headers
# ---------------------------------------------------------------------------


class TestStripProxyHeaders:
    def test_strips_all_proxy_only(self) -> None:
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

    def test_all_proxy_only_headers_covered(self) -> None:
        assert _PROXY_ONLY_HEADERS == {
            "x-routstr-model",
            "x-tinfoil-enclave-url",
            "x-tinfoil-request-usage-metrics",
        }


class TestPrepareEHBPUpstreamHeaders:
    def test_strips_client_proxy_headers_before_merging_target_headers(self) -> None:
        headers = {
            "x-routstr-model": "tinfoil-llama3-3-70b",
            "X-Tinfoil-Enclave-Url": "https://enclave.tinfoil.sh",
            "X-Tinfoil-Request-Usage-Metrics": "false",
            "Authorization": "Bearer upstream-key",
            "Ehbp-Encapsulated-Key": "abc123",
        }
        target_headers = {"X-Tinfoil-Request-Usage-Metrics": "true"}

        clean = _prepare_ehbp_upstream_headers(headers, target_headers)

        assert "x-routstr-model" not in clean
        assert "X-Tinfoil-Enclave-Url" not in clean
        assert clean["Authorization"] == "Bearer upstream-key"
        assert clean["Ehbp-Encapsulated-Key"] == "abc123"
        assert clean["X-Tinfoil-Request-Usage-Metrics"] == "true"


# ---------------------------------------------------------------------------
# _resolve_ehbp_target_url
# ---------------------------------------------------------------------------


class TestResolveEhbpTargetUrl:
    def test_override_with_enclave_url_for_tinfoil(self) -> None:
        result = _resolve_ehbp_target_url(
            "https://default.example.com/v1/chat/completions",
            "v1/chat/completions",
            {"X-Tinfoil-Enclave-Url": "https://enclave.tinfoil.sh"},
            "tinfoil",
        )
        assert result == "https://enclave.tinfoil.sh/v1/chat/completions"

    def test_override_lowercase_header_for_tinfoil(self) -> None:
        result = _resolve_ehbp_target_url(
            "https://default.example.com/v1/chat/completions",
            "v1/chat/completions",
            {"x-tinfoil-enclave-url": "https://enclave.tinfoil.sh"},
            "tinfoil",
        )
        assert result == "https://enclave.tinfoil.sh/v1/chat/completions"

    def test_no_override(self) -> None:
        default = "https://inference.tinfoil.sh/v1/chat/completions"
        result = _resolve_ehbp_target_url(
            default,
            "v1/chat/completions",
            {},
            "tinfoil",
        )
        assert result == default

    def test_non_tinfoil_provider_ignores_enclave_url(self) -> None:
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
    def test_tinfoil_rejects_unsafe_enclave_url(self, bad_url: str) -> None:
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
    async def test_no_usage_falls_back_to_max_cost(self) -> None:
        model_obj = MagicMock()
        model_obj.id = "llama3-3-70b"
        model_obj.forwarded_model_id = "llama3-3-70b"
        result = await _compute_ehbp_actual_cost(None, model_obj, 100_000)
        assert result["total_msats"] == 100_000
        assert result["input_tokens"] == 0
        assert result["output_tokens"] == 0

    @pytest.mark.asyncio
    async def test_usage_parsed_and_clamped(self) -> None:
        model_obj = MagicMock()
        model_obj.id = "llama3-3-70b"
        model_obj.forwarded_model_id = "llama3-3-70b"
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
            assert result["total_msats"] == 30
            assert result["total_msats"] <= 100_000
            assert result["input_tokens"] == 67
            assert result["output_tokens"] == 42
            assert result["total_tokens"] == 109
            assert result["input_msats"] == 10
            assert result["output_msats"] == 20

    @pytest.mark.asyncio
    async def test_max_cost_data_falls_back(self) -> None:
        model_obj = MagicMock()
        model_obj.id = "llama3-3-70b"
        model_obj.forwarded_model_id = "llama3-3-70b"
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
            assert result["total_msats"] == 50_000
            assert result["input_tokens"] == 0
            assert result["output_tokens"] == 0

    @pytest.mark.asyncio
    async def test_model_match_no_actual_model_key(self) -> None:
        """When the served model matches the requested one, no actual_model key."""
        model_obj = MagicMock()
        model_obj.id = "llama3-3-70b"
        model_obj.forwarded_model_id = "llama3-3-70b"
        with patch(
            "routstr.upstream.ehbp.calculate_cost",
            new_callable=AsyncMock,
        ) as mock_calc:
            from routstr.payment.cost_calculation import CostData

            mock_calc.return_value = CostData(
                base_msats=0,
                input_msats=5,
                output_msats=10,
                total_msats=15,
                total_usd=0.0,
                input_tokens=42,
                output_tokens=10,
            )
            result = await _compute_ehbp_actual_cost(
                "prompt=42,completion=10,total=52,model=llama3-3-70b",
                model_obj,
                100_000,
            )
            assert "actual_model" not in result
            # calculate_cost called with requested model
            call_args = mock_calc.call_args
            assert call_args[0][0]["model"] == "llama3-3-70b"

    @pytest.mark.asyncio
    async def test_alias_match_no_actual_model_key(self) -> None:
        """When the served upstream model matches forwarded_model_id through
        a client-facing alias, no actual_model key is set."""
        model_obj = MagicMock()
        model_obj.id = "tinfoil-glm-5-2"  # client-facing alias
        model_obj.forwarded_model_id = "glm-5-2"  # actual upstream ID
        with patch(
            "routstr.upstream.ehbp.calculate_cost",
            new_callable=AsyncMock,
        ) as mock_calc:
            from routstr.payment.cost_calculation import CostData

            mock_calc.return_value = CostData(
                base_msats=0,
                input_msats=5,
                output_msats=10,
                total_msats=15,
                total_usd=0.0,
                input_tokens=42,
                output_tokens=10,
            )
            # Tinfoil header returns the actual upstream model ID
            result = await _compute_ehbp_actual_cost(
                "prompt=42,completion=10,total=52,model=glm-5-2",
                model_obj,
                100_000,
            )
            assert "actual_model" not in result
            # calculate_cost called with the client-facing model ID (whose
            # pricing includes the correct upstream rates)
            call_args = mock_calc.call_args
            assert call_args[0][0]["model"] == "tinfoil-glm-5-2"

    @pytest.mark.asyncio
    async def test_real_mismatch_uses_actual_model_for_pricing(self) -> None:
        """When the served model differs from the expected upstream model,
        the actual model's pricing is used."""
        model_obj = MagicMock()
        model_obj.id = "tinfoil-gpt-oss-120b"  # client-facing alias
        model_obj.forwarded_model_id = "gpt-oss-120b"  # expected upstream

        actual_model_obj = MagicMock()
        actual_model_obj.id = "tinfoil-llama3-3-70b"  # client-facing of actual
        actual_model_obj.forwarded_model_id = "llama3-3-70b"

        with patch(
            "routstr.proxy.get_model_instance",
            return_value=actual_model_obj,
        ), patch(
            "routstr.upstream.ehbp.calculate_cost",
            new_callable=AsyncMock,
        ) as mock_calc:
            from routstr.payment.cost_calculation import CostData

            mock_calc.return_value = CostData(
                base_msats=0,
                input_msats=20,
                output_msats=40,
                total_msats=60,
                total_usd=0.0,
                input_tokens=42,
                output_tokens=10,
            )
            # Tinfoil served llama3-3-70b instead of gpt-oss-120b
            result = await _compute_ehbp_actual_cost(
                "prompt=42,completion=10,total=52,model=llama3-3-70b",
                model_obj,
                100_000,
            )
            assert result["actual_model"] == "llama3-3-70b"
            assert result["total_msats"] == 60
            # calculate_cost called with the actual model's client-facing ID
            call_args = mock_calc.call_args
            assert call_args[0][0]["model"] == "tinfoil-llama3-3-70b"

    @pytest.mark.asyncio
    async def test_model_mismatch_unknown_model_falls_back(self) -> None:
        """When the served model is not in the registry, use requested model."""
        model_obj = MagicMock()
        model_obj.id = "gpt-oss-120b"
        model_obj.forwarded_model_id = "gpt-oss-120b"

        with patch(
            "routstr.proxy.get_model_instance",
            return_value=None,
        ), patch(
            "routstr.upstream.ehbp.calculate_cost",
            new_callable=AsyncMock,
        ) as mock_calc:
            from routstr.payment.cost_calculation import CostData

            mock_calc.return_value = CostData(
                base_msats=0,
                input_msats=5,
                output_msats=10,
                total_msats=15,
                total_usd=0.0,
                input_tokens=42,
                output_tokens=10,
            )
            result = await _compute_ehbp_actual_cost(
                "prompt=42,completion=10,total=52,model=nonexistent",
                model_obj,
                100_000,
            )
            assert "actual_model" not in result
            # calculate_cost called with the requested model (fallback)
            call_args = mock_calc.call_args
            assert call_args[0][0]["model"] == "gpt-oss-120b"

    @pytest.mark.asyncio
    async def test_old_format_no_model_uses_requested(self) -> None:
        """Old format without model field uses requested model for pricing."""
        model_obj = MagicMock()
        model_obj.id = "llama3-3-70b"
        model_obj.forwarded_model_id = "llama3-3-70b"
        with patch(
            "routstr.upstream.ehbp.calculate_cost",
            new_callable=AsyncMock,
        ) as mock_calc:
            from routstr.payment.cost_calculation import CostData

            mock_calc.return_value = CostData(
                base_msats=0,
                input_msats=5,
                output_msats=10,
                total_msats=15,
                total_usd=0.0,
                input_tokens=67,
                output_tokens=42,
            )
            result = await _compute_ehbp_actual_cost(
                "prompt=67,completion=42,total=109",
                model_obj,
                100_000,
            )
            assert "actual_model" not in result
            call_args = mock_calc.call_args
            assert call_args[0][0]["model"] == "llama3-3-70b"

    @pytest.mark.asyncio
    async def test_case_insensitive_model_match(self) -> None:
        """Casing differences between the header and forwarded_model_id
        should not trigger a spurious mismatch."""
        model_obj = MagicMock()
        model_obj.id = "tinfoil-glm-5-2"
        model_obj.forwarded_model_id = "glm-5-2"  # lowercase
        with patch(
            "routstr.proxy.get_model_instance"
        ) as mock_get_model, patch(
            "routstr.upstream.ehbp.calculate_cost",
            new_callable=AsyncMock,
        ) as mock_calc:
            from routstr.payment.cost_calculation import CostData

            mock_calc.return_value = CostData(
                base_msats=0,
                input_msats=5,
                output_msats=10,
                total_msats=15,
                total_usd=0.0,
                input_tokens=42,
                output_tokens=10,
            )
            # Header returns uppercase — same model, different casing
            result = await _compute_ehbp_actual_cost(
                "prompt=42,completion=10,total=52,model=GLM-5-2",
                model_obj,
                100_000,
            )
            assert "actual_model" not in result
            # No mismatch: requested model pricing used
            call_args = mock_calc.call_args
            assert call_args[0][0]["model"] == "tinfoil-glm-5-2"
            mock_get_model.assert_not_called()

    @pytest.mark.asyncio
    async def test_date_versioned_alias_resolves_to_requested(self) -> None:
        """When the served model is a date-versioned alias that resolves back
        to the requested model, no mismatch is propagated."""
        model_obj = MagicMock()
        model_obj.id = "tinfoil-glm-5-2"
        model_obj.forwarded_model_id = "glm-5-2"

        resolved_model_obj = MagicMock()
        resolved_model_obj.id = "other-provider-glm-5-2"
        resolved_model_obj.forwarded_model_id = "glm-5-2"

        with patch(
            "routstr.proxy.get_model_instance",
            return_value=resolved_model_obj,
        ) as mock_get_model, patch(
            "routstr.upstream.ehbp.calculate_cost",
            new_callable=AsyncMock,
        ) as mock_calc:
            from routstr.payment.cost_calculation import CostData

            mock_calc.return_value = CostData(
                base_msats=0,
                input_msats=5,
                output_msats=10,
                total_msats=15,
                total_usd=0.0,
                input_tokens=42,
                output_tokens=10,
            )
            # Tinfoil returns a date-versioned ID with different casing.
            result = await _compute_ehbp_actual_cost(
                "prompt=42,completion=10,total=52,model=GLM-5-2-20260415",
                model_obj,
                100_000,
            )
            # Registry resolution, rather than unconditional suffix removal,
            # establishes that this alias represents the expected model.
            assert "actual_model" not in result
            assert mock_calc.call_args[0][0]["model"] == "tinfoil-glm-5-2"
            mock_get_model.assert_called_once_with("GLM-5-2-20260415")

    @pytest.mark.asyncio
    async def test_configured_date_version_is_preserved_as_identity(self) -> None:
        """A date suffix in forwarded_model_id is meaningful and preserved."""
        model_obj = MagicMock()
        model_obj.id = "tinfoil-glm-5-2-20260415"
        model_obj.forwarded_model_id = "glm-5-2-20260415"

        with patch(
            "routstr.proxy.get_model_instance"
        ) as mock_get_model, patch(
            "routstr.upstream.ehbp.calculate_cost",
            new_callable=AsyncMock,
        ) as mock_calc:
            from routstr.payment.cost_calculation import CostData

            mock_calc.return_value = CostData(
                base_msats=0,
                input_msats=5,
                output_msats=10,
                total_msats=15,
                total_usd=0.0,
                input_tokens=42,
                output_tokens=10,
            )
            result = await _compute_ehbp_actual_cost(
                "prompt=42,completion=10,total=52,model=GLM-5-2-20260415",
                model_obj,
                100_000,
            )

            assert "actual_model" not in result
            assert (
                mock_calc.call_args[0][0]["model"]
                == "tinfoil-glm-5-2-20260415"
            )
            mock_get_model.assert_not_called()

    @pytest.mark.asyncio
    async def test_different_client_alias_same_upstream_identity(self) -> None:
        """A global alias winner from another provider is not a failover when
        its forwarded model ID matches the requested upstream identity."""
        model_obj = MagicMock()
        model_obj.id = "tinfoil-glm-5-2"
        model_obj.forwarded_model_id = "glm-5-2"

        resolved_model_obj = MagicMock()
        resolved_model_obj.id = "other-provider-glm-5-2"
        resolved_model_obj.forwarded_model_id = "GLM-5-2"

        with patch(
            "routstr.proxy.get_model_instance",
            return_value=resolved_model_obj,
        ) as mock_get_model, patch(
            "routstr.upstream.ehbp.calculate_cost",
            new_callable=AsyncMock,
        ) as mock_calc:
            from routstr.payment.cost_calculation import CostData

            mock_calc.return_value = CostData(
                base_msats=0,
                input_msats=5,
                output_msats=10,
                total_msats=15,
                total_usd=0.0,
                input_tokens=42,
                output_tokens=10,
            )
            result = await _compute_ehbp_actual_cost(
                "prompt=42,completion=10,total=52,model=provider-alias",
                model_obj,
                100_000,
            )

            mock_get_model.assert_called_once_with("provider-alias")
            assert "actual_model" not in result
            assert mock_calc.call_args[0][0]["model"] == "tinfoil-glm-5-2"


# ---------------------------------------------------------------------------
# TinfoilUpstreamProvider
# ---------------------------------------------------------------------------


class TestTinfoilUpstreamProvider:
    def test_provider_type_and_defaults(self) -> None:
        assert TinfoilUpstreamProvider.provider_type == "tinfoil"
        assert (
            TinfoilUpstreamProvider.default_base_url
            == "https://inference.tinfoil.sh"
        )
        assert TinfoilUpstreamProvider.supports_ehbp is True

    def test_transform_model_name(self) -> None:
        provider = TinfoilUpstreamProvider(api_key="test")
        assert provider.transform_model_name("tinfoil/llama3-3-70b") == "llama3-3-70b"
        assert provider.transform_model_name("llama3-3-70b") == "llama3-3-70b"

    def test_get_ehbp_forwarding_target_includes_usage_header(self) -> None:
        provider = TinfoilUpstreamProvider(api_key="test")
        model_obj = MagicMock()
        model_obj.id = "llama3-3-70b"
        model_obj.forwarded_model_id = "llama3-3-70b"
        target = provider.get_ehbp_forwarding_target("v1/chat/completions", model_obj)
        assert (
            target.headers["X-Tinfoil-Request-Usage-Metrics"] == "true"
        )
        assert "v1/chat/completions" in target.url

    def test_get_provider_metadata(self) -> None:
        meta = TinfoilUpstreamProvider.get_provider_metadata()
        assert meta["id"] == "tinfoil"
        assert meta["name"] == "Tinfoil"
        assert meta["fixed_base_url"] is True

    def test_tinfoil_model_pricing_parses(self) -> None:
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
    async def test_fetch_models_parses_response(self) -> None:
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
    async def test_fetch_models_handles_error(self) -> None:
        provider = TinfoilUpstreamProvider(api_key="test")
        with patch("routstr.upstream.tinfoil.httpx.AsyncClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(side_effect=Exception("network error"))
            mock_client_cls.return_value = mock_client

            models = await provider.fetch_models()

        assert models == []
