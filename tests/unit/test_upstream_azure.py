"""Tests for Azure upstream provider request normalization."""

from routstr.payment.models import Architecture, Model, Pricing
from routstr.upstream.azure import AzureUpstreamProvider


def create_test_model(model_id: str, canonical_slug: str | None = None) -> Model:
    return Model(
        id=model_id,
        name=model_id,
        created=0,
        description="test",
        context_length=8192,
        architecture=Architecture(
            modality="text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="test",
            instruct_type=None,
        ),
        pricing=Pricing(
            prompt=0.001,
            completion=0.001,
            request=0.0,
            image=0.0,
            web_search=0.0,
            internal_reasoning=0.0,
        ),
        canonical_slug=canonical_slug,
    )


def test_prepare_headers_uses_azure_api_key_header() -> None:
    provider = AzureUpstreamProvider(
        base_url="https://example.openai.azure.com",
        api_key="azure-key",
        api_version="2024-02-15-preview",
    )

    headers = provider.prepare_headers({"Authorization": "Bearer user-token"})

    assert headers["api-key"] == "azure-key"
    assert "Authorization" not in headers
    assert "authorization" not in headers


def test_prepare_params_normalizes_azure_api_version() -> None:
    provider = AzureUpstreamProvider(
        base_url="https://example.openai.azure.com",
        api_key="azure-key",
        api_version="\ufeff v1 ",
    )

    params = provider.prepare_params("chat/completions", {})

    assert params["api-version"] == "2024-02-15-preview"


def test_normalize_request_path_includes_deployment_id() -> None:
    provider = AzureUpstreamProvider(
        base_url="https://example.openai.azure.com/openai/v1",
        api_key="azure-key",
        api_version="2024-02-15-preview",
    )
    model = create_test_model("azure/gpt-4o", canonical_slug="deploy-gpt4o")

    path = provider.normalize_request_path("v1/chat/completions", model)

    assert path == "openai/deployments/deploy-gpt4o/chat/completions"


def test_get_request_base_url_strips_openai_v1_suffix() -> None:
    provider = AzureUpstreamProvider(
        base_url="https://example.openai.azure.com/openai/v1",
        api_key="azure-key",
        api_version="2024-02-15-preview",
    )
    model = create_test_model("azure/gpt-4o")

    base_url = provider.get_request_base_url("chat/completions", model)

    assert base_url == "https://example.openai.azure.com"
