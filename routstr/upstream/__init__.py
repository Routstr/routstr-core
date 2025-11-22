from .anthropic import AnthropicUpstreamProvider
from .azure import AzureUpstreamProvider
from .base import BaseUpstreamProvider
from .fireworks import FireworksUpstreamProvider
from .generic import GenericUpstreamProvider
from .groq import GroqUpstreamProvider
from .ollama import OllamaUpstreamProvider
from .openai import OpenAIUpstreamProvider
from .openrouter import OpenRouterUpstreamProvider
from .perplexity import PerplexityUpstreamProvider
from .xai import XAIUpstreamProvider

upstream_provider_classes: list[type[BaseUpstreamProvider]] = [
    AnthropicUpstreamProvider,
    AzureUpstreamProvider,
    FireworksUpstreamProvider,
    GenericUpstreamProvider,
    GroqUpstreamProvider,
    OllamaUpstreamProvider,
    OpenAIUpstreamProvider,
    OpenRouterUpstreamProvider,
    PerplexityUpstreamProvider,
    XAIUpstreamProvider,
]
"""List of all upstream classes"""

__all__ = [
    "BaseUpstreamProvider",
    *[cls.__name__ for cls in upstream_provider_classes],
    "upstream_provider_classes",
]
