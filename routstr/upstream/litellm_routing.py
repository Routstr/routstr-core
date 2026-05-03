"""Map an upstream `base_url` to the correct litellm provider prefix.

Used by `BaseUpstreamProvider.get_litellm_provider_prefix` so that custom /
generic provider rows (which inherit the base class default) get routed to
the right litellm backend instead of falling back to `openai/`.

The table is compiled from litellm 1.74's
`litellm/litellm_core_utils/get_llm_provider_logic.py` (the
`openai_compatible_endpoints` table) plus the providers documented at
https://docs.litellm.ai/docs/providers. Substring match is used so that
URLs with paths, ports, regional subdomains, etc. all resolve correctly.

Order matters: more specific needles must appear before more generic ones
(e.g. ``openai.azure.com`` before ``api.openai.com``).
"""

from __future__ import annotations

from urllib.parse import urlsplit

DEFAULT_PREFIX = "openai/"

LITELLM_HOST_PREFIX_MAP: tuple[tuple[str, str], ...] = (
    # Azure must win over api.openai.com because the host ends with
    # `openai.azure.com` and we don't want it picked up as plain OpenAI.
    ("openai.azure.com", "azure/"),
    # Google
    ("generativelanguage.googleapis.com", "gemini/"),
    ("aiplatform.googleapis.com", "vertex_ai/"),
    # First-class providers with native litellm prefixes
    ("api.openai.com", "openai/"),
    ("api.anthropic.com", "anthropic/"),
    ("api.groq.com", "groq/"),
    ("api.fireworks.ai", "fireworks_ai/"),
    ("api.x.ai", "xai/"),
    ("api.perplexity.ai", "perplexity/"),
    ("openrouter.ai", "openrouter/"),
    ("api.deepseek.com", "deepseek/"),
    ("api.together.xyz", "together_ai/"),
    ("codestral.mistral.ai", "codestral/"),
    ("api.mistral.ai", "mistral/"),
    ("api.cohere.com", "cohere_chat/"),
    ("api.cohere.ai", "cohere_chat/"),
    ("api.deepinfra.com", "deepinfra/"),
    ("api.endpoints.anyscale.com", "anyscale/"),
    ("api.cerebras.ai", "cerebras/"),
    ("inference.baseten.co", "baseten/"),
    ("api.sambanova.ai", "sambanova/"),
    ("api.ai21.com", "ai21_chat/"),
    ("api.friendli.ai", "friendliai/"),
    ("api.galadriel.com", "galadriel/"),
    ("api.llama.com", "meta_llama/"),
    ("api.featherless.ai", "featherless_ai/"),
    ("inference.api.nscale.com", "nscale/"),
    ("dashscope-intl.aliyuncs.com", "dashscope/"),
    ("api.moonshot.ai", "moonshot/"),
    ("api.moonshot.cn", "moonshot/"),
    ("api.minimax.io", "minimax/"),
    ("api.minimaxi.com", "minimax/"),
    ("platform.publicai.co", "publicai/"),
    ("api.synthetic.new", "synthetic/"),
    ("api.stima.tech", "apertis/"),
    ("nano-gpt.com", "nano-gpt/"),
    ("api.poe.com", "poe/"),
    ("llm.chutes.ai", "chutes/"),
    ("api.v0.dev", "v0/"),
    ("api.lambda.ai", "lambda_ai/"),
    ("api.hyperbolic.xyz", "hyperbolic/"),
    ("ai-gateway.vercel.sh", "vercel_ai_gateway/"),
    ("api.inference.wandb.ai", "wandb/"),
    ("integrate.api.nvidia.com", "nvidia_nim/"),
    ("api.studio.nebius.com", "nebius/"),
    ("api.novita.ai", "novita/"),
    ("ark.cn-beijing.volces.com", "volcengine/"),
    ("api.voyageai.com", "voyage/"),
    ("api.jina.ai", "jina_ai/"),
    ("api.aimlapi.com", "aiml/"),
    ("api.snowflakecomputing.com", "snowflake/"),
    ("databricks.com", "databricks/"),
    ("huggingface.co", "huggingface/"),
)

# Substrings that indicate an Ollama deployment regardless of port/scheme.
OLLAMA_HOST_HINTS: tuple[str, ...] = (
    "localhost:11434",
    "127.0.0.1:11434",
    "ollama",
)


def detect_litellm_prefix(
    base_url: str | None, default: str = DEFAULT_PREFIX
) -> str:
    """Return the litellm provider prefix (`"<provider>/"`) for `base_url`.

    Falls back to `default` when the host doesn't match any known provider.
    The default is `openai/` because every unmatched OpenAI-compatible
    server is, by definition, an OpenAI-compatible server.
    """
    if not base_url:
        return default

    parsed = urlsplit(base_url)
    host = parsed.netloc.lower() or base_url.lower()

    for needle, prefix in LITELLM_HOST_PREFIX_MAP:
        if needle in host:
            return prefix

    if any(hint in host for hint in OLLAMA_HOST_HINTS):
        return "ollama_chat/"

    return default
