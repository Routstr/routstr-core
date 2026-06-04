"""Tests for `routstr.upstream.litellm_routing.detect_litellm_prefix`."""

from __future__ import annotations

import pytest

from routstr.upstream.litellm_routing import detect_litellm_prefix


@pytest.mark.parametrize(
    "base_url,expected",
    [
        # The bug case: custom row pointing at Fireworks must NOT route to openai/.
        ("https://api.fireworks.ai/inference/v1", "fireworks_ai/"),
        # Other OpenAI-compatible providers commonly plugged into the custom slot.
        ("https://api.groq.com/openai/v1", "groq/"),
        ("https://api.x.ai/v1", "xai/"),
        ("https://api.deepseek.com/v1", "deepseek/"),
        ("https://api.together.xyz/v1", "together_ai/"),
        ("https://api.perplexity.ai", "perplexity/"),
        ("https://openrouter.ai/api/v1", "openrouter/"),
        ("https://api.mistral.ai/v1", "mistral/"),
        ("https://codestral.mistral.ai/v1", "codestral/"),
        ("https://api.cohere.com/v1", "cohere_chat/"),
        ("https://api.cohere.ai/v1", "cohere_chat/"),
        ("https://api.deepinfra.com/v1/openai", "deepinfra/"),
        ("https://api.cerebras.ai/v1", "cerebras/"),
        ("https://api.sambanova.ai/v1", "sambanova/"),
        ("https://api.moonshot.cn/v1", "moonshot/"),
        ("https://api.moonshot.ai/v1", "moonshot/"),
        ("https://api.studio.nebius.com/v1", "nebius/"),
        ("https://api.novita.ai/v3/openai", "novita/"),
        ("https://api.lambda.ai/v1", "lambda_ai/"),
        ("https://api.aimlapi.com/v1", "aiml/"),
        ("https://api.featherless.ai/v1", "featherless_ai/"),
        ("https://integrate.api.nvidia.com/v1", "nvidia_nim/"),
        ("https://inference.baseten.co/v1", "baseten/"),
        ("https://ai-gateway.vercel.sh/v1", "vercel_ai_gateway/"),
        ("https://api.inference.wandb.ai/v1", "wandb/"),
        ("https://api.poe.com/v1", "poe/"),
        ("https://llm.chutes.ai/v1/", "chutes/"),
        ("https://api.v0.dev/v1", "v0/"),
        ("https://api.hyperbolic.xyz/v1", "hyperbolic/"),
        ("https://api.synthetic.new/openai/v1", "synthetic/"),
        ("https://api.stima.tech/v1", "apertis/"),
        ("https://nano-gpt.com/api/v1", "nano-gpt/"),
        ("https://api.friendli.ai/serverless/v1", "friendliai/"),
        ("https://api.galadriel.com/v1", "galadriel/"),
        ("https://api.llama.com/compat/v1", "meta_llama/"),
        ("https://api.minimax.io/v1", "minimax/"),
        ("https://api.minimaxi.com/v1", "minimax/"),
        ("https://platform.publicai.co/v1", "publicai/"),
        ("https://inference.api.nscale.com/v1", "nscale/"),
        ("https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "dashscope/"),
        ("https://api.endpoints.anyscale.com/v1", "anyscale/"),
        ("https://api.ai21.com/studio/v1", "ai21_chat/"),
        ("https://ark.cn-beijing.volces.com/api/v3", "volcengine/"),
        ("https://api.voyageai.com/v1", "voyage/"),
        ("https://api.jina.ai/v1", "jina_ai/"),
        ("https://api.snowflakecomputing.com", "snowflake/"),
        ("https://my-workspace.databricks.com/serving-endpoints", "databricks/"),
        ("https://huggingface.co/api/inference", "huggingface/"),
        # First-class providers — URL detection still produces the right prefix
        # so subclasses without an explicit override stay correct.
        ("https://api.openai.com/v1", "openai/"),
        ("https://api.anthropic.com/v1", "anthropic/"),
        ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini/"),
        ("https://us-central1-aiplatform.googleapis.com/v1", "vertex_ai/"),
        # Azure ordering: must beat api.openai.com.
        ("https://my-resource.openai.azure.com/openai/deployments/foo", "azure/"),
        # Ollama hints.
        ("http://localhost:11434/v1", "ollama_chat/"),
        ("http://127.0.0.1:11434/v1", "ollama_chat/"),
        ("http://my-ollama-host:11434/v1", "ollama_chat/"),
        # Casing and trailing slash normalisation.
        ("HTTPS://API.FIREWORKS.AI/INFERENCE/V1/", "fireworks_ai/"),
        # Unknown host falls back to openai/ (still OpenAI-compatible by convention).
        ("https://example.com/v1", "openai/"),
        ("", "openai/"),
    ],
)
def test_detect_litellm_prefix(base_url: str, expected: str) -> None:
    assert detect_litellm_prefix(base_url) == expected


def test_detect_litellm_prefix_none_uses_default() -> None:
    assert detect_litellm_prefix(None) == "openai/"


def test_detect_litellm_prefix_custom_default() -> None:
    assert detect_litellm_prefix("https://example.com", default="anthropic/") == (
        "anthropic/"
    )
