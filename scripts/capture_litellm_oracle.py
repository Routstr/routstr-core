"""Capture golden fixtures of litellm's ``/v1/messages`` translation.

``routstr.upstream.messages_dispatch.dispatch_anthropic_messages`` delegates to
``litellm.anthropic.messages.acreate``. Before litellm is removed this script
records exactly what that call does, so a native implementation can be tested
for parity without litellm installed. litellm is no longer a project
dependency, so regenerate with it pulled in for the one run::

    uv run --with litellm==1.101.2 --with backoff \
        python scripts/capture_litellm_oracle.py

(``backoff``: litellm's native Anthropic-messages path imports
``litellm.proxy``, which needs it.) Notes on what the fixtures show live in
``tests/fixtures/litellm_oracle/NOTES.md``.

Fixtures land in ``tests/fixtures/litellm_oracle/``:

* ``requests.json``          Anthropic body -> wire HTTP request, per provider.
* ``adapter_translate.json`` ``AnthropicAdapter().translate_completion_input_params``
                             output (used by ``gemini_messages.py``).
* ``responses.json``         canned upstream stream -> Anthropic events and the
                             aggregated (``client_stream=False``) message.
* ``errors.json``            upstream HTTP error -> ``UpstreamError``.
* ``meta.json``              litellm/openai versions and the model map sha256.

Capture environment
-------------------
* The ``routstr`` package under test is NOT the working tree: it is
  ``git archive $ORACLE_BASE_REF routstr`` (default ``d65f8cd0``, the last
  commit dispatching via litellm) extracted to a temp dir and put first on
  ``sys.path``. The script refuses to run if that dispatcher does not call
  ``litellm.anthropic.messages.acreate``. Only the vendored model map is read
  from the working tree.
* ``configure_litellm()`` runs first, as at app startup (chat-completions URL
  for Anthropic messages, ``drop_params=True``).
* ``litellm.disable_aiohttp_transport = True`` so every litellm/openai-SDK
  request goes through httpx, where ``respx`` intercepts it. This only swaps the
  transport; payloads and URLs are unchanged. Every case asserts that the
  upstream request was actually intercepted.
* Model registry: ``LITELLM_LOCAL_MODEL_COST_MAP=True`` avoids the network
  fetch at import, then ``litellm.model_cost`` (and the per-provider model sets
  derived from it) is replaced by the vendored
  ``routstr/payment/data/model_cost.json`` (a filtered copy of the remote map
  production litellm fetches). Capability lookups ("supports reasoning", "is an
  OpenAI catalog model") drive which params litellm drops, so the fixtures are
  only valid for that file; its sha256 is recorded in ``meta.json``.
* No network: ``respx`` mocks every host. Unknown hosts get a provider-shaped
  canned success (OpenAI SSE, Anthropic SSE for ``.../messages``, NDJSON for
  Ollama ``/api/chat``, a 1x1 PNG for ``https://img.example.com``).

Normalisation rules (apply the same ones in the parity test)
-------------------------------------------------------------
N1  Generated ids. Any string value that FULLY matches one of ``ID_PATTERNS``
    (``msg_*``, ``toolu_*``, ``call_*``, ``chatcmpl-*``, a bare UUID) is replaced
    with ``"<id>"`` **unless** the exact string occurs somewhere in the case
    input (request body, or canned upstream payload). Pass-through ids stay
    verbatim; only ids invented by the translator are masked.
N2  Secrets. In ``authorization``, ``api-key`` and ``x-api-key`` header values
    the test key ``sk-test`` is replaced with ``<redacted>`` (so
    ``Bearer sk-test`` -> ``Bearer <redacted>``). The keyless sentinel
    ``no-key`` is kept verbatim because it is behaviour, not a secret.
N3  Environment headers. ``x-stainless-os``, ``x-stainless-arch``,
    ``x-stainless-runtime``, ``x-stainless-runtime-version`` and
    ``x-stainless-package-version`` values become ``"<env>"``. Header names are
    lower-cased. All other headers (``user-agent``, ``content-length``,
    ``anthropic-version`` ...) are recorded verbatim; the openai-SDK
    ``x-stainless-*`` headers are SDK artefacts, not upstream requirements.
N4  Request bodies are recorded as parsed JSON (``json``); a non-JSON body is
    recorded as text under ``content``.
N5  Exceptions are recorded as ``{"type", "message"}``; the message has every
    run of a UUID or ``0x`` address replaced by ``<id>``.

Running the script twice must produce byte-identical fixtures.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import io
import json
import logging
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
for _var in (
    "LITELLM_DEBUG",
    "LITELLM_USE_RESPONSES_API_FOR_ANTHROPIC_MESSAGES",
    "LITELLM_STRICT_PARAMS",
    "OPENAI_API_KEY",
    "AZURE_API_KEY",
    "AZURE_API_VERSION",
    "AZURE_API_BASE",
    "DEEPSEEK_API_KEY",
):
    os.environ.pop(_var, None)

ROOT = Path(__file__).resolve().parent.parent
# The commit whose ``routstr`` package still dispatches through litellm. The
# working tree is being ported off litellm, so the oracle imports this
# snapshot (extracted to a temp dir) rather than the live package; otherwise a
# rerun would record the native port against itself.
ORACLE_BASE_REF = os.environ.get(
    "ORACLE_BASE_REF", "d65f8cd0e7a41029ee1326c879b80a80a80f0bba"
)
_BASELINE_DIR = tempfile.TemporaryDirectory(prefix="litellm-oracle-")


def extract_baseline_package() -> Path:
    archive = subprocess.run(
        ["git", "-C", str(ROOT), "archive", ORACLE_BASE_REF, "routstr"],
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(_BASELINE_DIR.name, filter="data")
    return Path(_BASELINE_DIR.name)


BASELINE_ROOT = extract_baseline_package()
sys.path.insert(0, str(BASELINE_ROOT))

import httpx  # noqa: E402
import litellm  # noqa: E402
import respx  # noqa: E402
from litellm.litellm_core_utils.thread_pool_executor import (  # noqa: E402
    executor as _LOGGING_EXECUTOR,
)

from routstr.core.exceptions import UpstreamError  # noqa: E402
from routstr.payment.models import (  # noqa: E402
    Architecture,
    Model,
    Pricing,
    Reasoning,
)
from routstr.upstream.azure import AzureUpstreamProvider  # noqa: E402
from routstr.upstream.base import BaseUpstreamProvider  # noqa: E402
from routstr.upstream.deepseek import DeepSeekUpstreamProvider  # noqa: E402
from routstr.upstream.fireworks import FireworksUpstreamProvider  # noqa: E402
from routstr.upstream.generic import GenericUpstreamProvider  # noqa: E402
from routstr.upstream.groq import GroqUpstreamProvider  # noqa: E402
from routstr.upstream.litellm_routing import configure_litellm  # noqa: E402
from routstr.upstream.messages_dispatch import (  # noqa: E402
    dispatch_anthropic_messages,
    events_from_chunk,
)
from routstr.upstream.ollama import OllamaUpstreamProvider  # noqa: E402
from routstr.upstream.openai import OpenAIUpstreamProvider  # noqa: E402
from routstr.upstream.perplexity import PerplexityUpstreamProvider  # noqa: E402
from routstr.upstream.together import TogetherUpstreamProvider  # noqa: E402
from routstr.upstream.venice import VeniceUpstreamProvider  # noqa: E402
from routstr.upstream.xai import XAIUpstreamProvider  # noqa: E402

VENDORED_MODEL_COST = ROOT / "routstr" / "payment" / "data" / "model_cost.json"


def install_vendored_model_cost() -> str:
    """Make litellm's registry the vendored map the native port reads.

    Production litellm fetches the remote map at import; the vendored file is a
    filtered copy of that map, so capability-dependent choices (dropping
    ``reasoning_effort`` / ``user`` / ``stop`` per provider+model) recorded here
    are the ones the native port can reproduce. Returns the file's sha256.
    """
    raw = VENDORED_MODEL_COST.read_bytes()
    vendored = json.loads(raw)
    populate = inspect.getsource(litellm._populate_provider_model_sets)
    for set_name in sorted(set(re.findall(r"(\w+)\.add\(", populate))):
        model_set = getattr(litellm, set_name, None)
        if isinstance(model_set, set):
            model_set.clear()
    litellm.model_cost.clear()
    litellm.model_cost.update(vendored)
    litellm.add_known_models()
    litellm.utils._invalidate_model_cost_lowercase_map()
    return hashlib.sha256(raw).hexdigest()


def assert_litellm_dispatcher() -> None:
    """Fail loudly unless the imported dispatcher is the litellm one."""
    import routstr.upstream.messages_dispatch as dispatch_module

    source = inspect.getsource(dispatch_module)
    if not Path(dispatch_module.__file__ or "").is_relative_to(BASELINE_ROOT):
        raise RuntimeError(f"routstr imported from {dispatch_module.__file__}")
    if "litellm.anthropic.messages.acreate" not in source:
        raise RuntimeError(
            f"{ORACLE_BASE_REF} does not dispatch through litellm; set ORACLE_BASE_REF"
        )


assert_litellm_dispatcher()
MODEL_COST_SHA256 = install_vendored_model_cost()
configure_litellm()
litellm.disable_aiohttp_transport = True
litellm.suppress_debug_info = True
for _name in ("LiteLLM", "LiteLLM Proxy", "LiteLLM Router", "routstr", "httpx"):
    logging.getLogger(_name).setLevel(logging.CRITICAL)
logging.disable(logging.CRITICAL)

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "litellm_oracle"
API_KEY = "sk-test"
IMAGE_HOST = "img.example.com"
IMAGE_URL = f"https://{IMAGE_HOST}/cat.png"
# 1x1 transparent PNG.
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA"
    "60e6kgAAAABJRU5ErkJggg=="
)
PDF_B64 = "JVBERi0xLjQKJcfsj6IKMSAwIG9iago8PD4+CmVuZG9iagp0cmFpbGVyCjw8Pj4KJSVFT0YK"

ID_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"msg_[A-Za-z0-9_\-]+"),
    re.compile(r"toolu_[A-Za-z0-9_\-]+"),
    re.compile(r"call_[A-Za-z0-9_\-]+"),
    re.compile(r"chatcmpl-[A-Za-z0-9_\-]+"),
    re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),
)
_UUID_RUN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|0x[0-9a-f]+"
)
# Metadata lookups litellm issues besides the completion call itself.
AUX_PATHS: tuple[str, ...] = ("/api/show",)
SETTLE_ROUNDS = 3
SETTLE_STEP_SECONDS = 0.02
SECRET_HEADERS = frozenset({"authorization", "api-key", "x-api-key"})
ENV_HEADERS = frozenset(
    {
        "x-stainless-os",
        "x-stainless-arch",
        "x-stainless-runtime",
        "x-stainless-runtime-version",
        "x-stainless-package-version",
    }
)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def collect_strings(obj: Any, out: set[str] | None = None) -> set[str]:
    out = set() if out is None else out
    if isinstance(obj, str):
        out.add(obj)
        # Strings embedded in JSON text (SSE payloads, tool arguments).
        if obj[:1] in "{[":
            try:
                collect_strings(json.loads(obj), out)
            except ValueError:
                pass
    elif isinstance(obj, dict):
        for key, value in obj.items():
            collect_strings(key, out)
            collect_strings(value, out)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            collect_strings(item, out)
    return out


def normalise(obj: Any, keep: set[str]) -> Any:
    """Rule N1: mask translator-generated ids not present in the input."""
    if isinstance(obj, str):
        if obj not in keep and any(p.fullmatch(obj) for p in ID_PATTERNS):
            return "<id>"
        return obj
    if isinstance(obj, dict):
        return {key: normalise(value, keep) for key, value in obj.items()}
    if isinstance(obj, list):
        return [normalise(item, keep) for item in obj]
    return obj


def normalise_headers(headers: httpx.Headers) -> dict[str, str]:
    """Rules N2 + N3."""
    out: dict[str, str] = {}
    for name, value in headers.multi_items():
        lname = name.lower()
        if lname in SECRET_HEADERS:
            value = value.replace(API_KEY, "<redacted>")
            if "<redacted>" not in value and "no-key" not in value:
                value = "<redacted>"
        elif lname in ENV_HEADERS:
            value = "<env>"
        out[lname] = value
    return out


def describe_exception(exc: BaseException) -> dict[str, str]:
    """Rule N5."""
    return {
        "type": type(exc).__name__,
        "message": _UUID_RUN.sub("<id>", str(exc)),
    }


def upstream_error_dict(exc: UpstreamError) -> dict[str, Any]:
    return {
        "status_code": exc.status_code,
        "code": exc.code,
        "message": exc.message,
        "details": exc.details,
        "from_upstream_response": exc.from_upstream_response,
    }


def request_record(request: httpx.Request) -> dict[str, Any]:
    raw = request.content.decode("utf-8", errors="replace")
    record: dict[str, Any] = {
        "method": request.method,
        "url": str(request.url),
        "headers": normalise_headers(request.headers),
    }
    try:
        record["json"] = json.loads(raw) if raw else None
    except ValueError:
        record["content"] = raw
    return record


# ---------------------------------------------------------------------------
# Models and providers
# ---------------------------------------------------------------------------


REASONING_VARIANTS: dict[str, dict[str, Any] | None] = {
    "none": None,
    "efforts_low_med_high": {
        "supported_efforts": ["low", "medium", "high"],
        "default_effort": "medium",
    },
    "mandatory_low_high": {
        "mandatory": True,
        "supported_efforts": ["low", "high"],
        "default_effort": "high",
    },
}


def make_model(
    model_id: str,
    reasoning_variant: str = "none",
    forwarded_model_id: str | None = None,
) -> Model:
    """Same construction as ``tests/unit/test_messages_litellm_dispatch.py``."""
    reasoning = REASONING_VARIANTS[reasoning_variant]
    return Model(
        id=model_id,
        name=model_id,
        forwarded_model_id=forwarded_model_id or model_id,
        created=0,
        description="",
        context_length=8192,
        architecture=Architecture(
            modality="text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="x",
            instruct_type=None,
        ),
        pricing=Pricing(
            prompt=0.0,
            completion=0.0,
            request=0.0,
            image=0.0,
            web_search=0.0,
            internal_reasoning=0.0,
            max_cost=0.0,
        ),
        sats_pricing=None,
        per_request_limits=None,
        top_provider=None,
        reasoning=Reasoning(**reasoning) if reasoning else None,
    )


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    build: Callable[[], BaseUpstreamProvider]
    models: tuple[str, ...]
    tier: str  # "full" | "core"
    api_key_blank: bool = False
    # Models run against ``REASONING_CASES`` with each reasoning-metadata variant.
    reasoning_models: tuple[str, ...] = ()


PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        "generic-local",
        lambda: GenericUpstreamProvider("http://127.0.0.1:3091/v1", API_KEY),
        (
            "gpt-4o",
            "gpt-5",
            "o3",
            "claude-sonnet-4-5",
            "llama-3.3-70b",
            "qwen3-32b",
            "kimi-k2",
            "deepseek-reasoner",
        ),
        "full",
        reasoning_models=("gpt-5", "deepseek-reasoner", "qwen3-32b"),
    ),
    ProviderSpec(
        "generic-local-keyless",
        lambda: GenericUpstreamProvider("http://127.0.0.1:3091/v1", ""),
        ("llama-3.3-70b",),
        "core",
        api_key_blank=True,
    ),
    ProviderSpec(
        "openai",
        lambda: OpenAIUpstreamProvider(API_KEY),
        ("openai/gpt-4o", "gpt-5", "gpt-5-mini", "o3", "o4-mini", "gpt-4.1"),
        "full",
        reasoning_models=("o3", "gpt-4o"),
    ),
    ProviderSpec(
        "venice",
        lambda: VeniceUpstreamProvider(API_KEY),
        ("venice/llama-3.3-70b", "qwen3-235b", "openai-gpt-oss-120b"),
        "full",
        reasoning_models=("openai-gpt-oss-120b",),
    ),
    ProviderSpec(
        "deepseek",
        lambda: DeepSeekUpstreamProvider(API_KEY),
        ("deepseek/deepseek-chat", "deepseek-reasoner"),
        "full",
        reasoning_models=("deepseek-reasoner",),
    ),
    ProviderSpec(
        "together",
        lambda: TogetherUpstreamProvider(API_KEY),
        (
            "together/meta-llama/Llama-3.3-70B-Instruct-Turbo",
            "moonshotai/Kimi-K2-Instruct",
            "Qwen/Qwen3-235B-A22B-Thinking-2507",
        ),
        "core",
    ),
    ProviderSpec(
        "groq",
        lambda: GroqUpstreamProvider(API_KEY),
        ("groq/llama-3.3-70b-versatile", "qwen/qwen3-32b", "openai/gpt-oss-120b"),
        "core",
        reasoning_models=("openai/gpt-oss-120b",),
    ),
    ProviderSpec(
        "fireworks",
        lambda: FireworksUpstreamProvider(API_KEY),
        (
            "accounts/fireworks/models/llama-v3p3-70b-instruct",
            "accounts/fireworks/models/kimi-k2-instruct",
        ),
        "core",
    ),
    ProviderSpec(
        "xai",
        lambda: XAIUpstreamProvider(API_KEY),
        ("x-ai/grok-4", "x-ai/grok-3-mini"),
        "core",
    ),
    ProviderSpec(
        "perplexity",
        lambda: PerplexityUpstreamProvider(API_KEY),
        ("perplexity/sonar", "sonar-reasoning"),
        "core",
    ),
    ProviderSpec(
        "azure",
        lambda: AzureUpstreamProvider(
            "https://myres.openai.azure.com", API_KEY, api_version="2024-10-21"
        ),
        ("azure/gpt-4o", "o3-mini"),
        "core",
    ),
    ProviderSpec(
        "ollama",
        lambda: OllamaUpstreamProvider("http://localhost:11434"),
        ("ollama/llama3.2", "qwen3:8b"),
        "core",
        api_key_blank=True,
    ),
    ProviderSpec(
        "generic-openrouter",
        lambda: GenericUpstreamProvider("https://openrouter.ai/api/v1", API_KEY),
        ("anthropic/claude-sonnet-4.5", "openai/gpt-4o", "deepseek/deepseek-r1"),
        "core",
        reasoning_models=("anthropic/claude-sonnet-4.5",),
    ),
    ProviderSpec(
        "generic-minimax",
        lambda: GenericUpstreamProvider("https://api.minimax.io/v1", API_KEY),
        ("MiniMax-M2",),
        "core",
    ),
    ProviderSpec(
        "generic-deepseek-host",
        lambda: GenericUpstreamProvider("https://api.deepseek.com", API_KEY),
        ("deepseek-chat",),
        "core",
    ),
    ProviderSpec(
        "generic-anthropic-host",
        lambda: GenericUpstreamProvider("https://api.anthropic.com/v1", API_KEY),
        ("claude-sonnet-4-5",),
        "core",
    ),
    ProviderSpec(
        "generic-cerebras",
        lambda: GenericUpstreamProvider("https://api.cerebras.ai/v1", API_KEY),
        ("llama-3.3-70b", "qwen-3-32b"),
        "core",
    ),
    ProviderSpec(
        "generic-mistral",
        lambda: GenericUpstreamProvider("https://api.mistral.ai/v1", API_KEY),
        ("mistral-large-latest", "magistral-medium-latest"),
        "core",
    ),
    ProviderSpec(
        "generic-nvidia-nim",
        lambda: GenericUpstreamProvider("https://integrate.api.nvidia.com/v1", API_KEY),
        ("meta/llama-3.3-70b-instruct",),
        "core",
    ),
    ProviderSpec(
        "generic-moonshot",
        lambda: GenericUpstreamProvider("https://api.moonshot.ai/v1", API_KEY),
        ("kimi-k2-0905-preview",),
        "core",
    ),
    ProviderSpec(
        "generic-groq-host",
        lambda: GenericUpstreamProvider("https://api.groq.com/openai/v1", API_KEY),
        ("llama-3.3-70b-versatile",),
        "core",
    ),
)


# ---------------------------------------------------------------------------
# Request corpus
# ---------------------------------------------------------------------------

USER_HI = [{"role": "user", "content": "Hello there"}]
WEATHER_TOOL = {
    "name": "get_weather",
    "description": "Get the current weather for a city",
    "input_schema": {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "City name"},
            "unit": {"type": "string", "enum": ["c", "f"]},
        },
        "required": ["city"],
    },
}
TIME_TOOL = {
    "name": "get_time",
    "description": "Current time in a timezone",
    "input_schema": {
        "type": "object",
        "properties": {"tz": {"type": "string"}},
    },
}
TOOL_USE_HISTORY = [
    {"role": "user", "content": "Weather in Paris?"},
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Let me check."},
            {
                "type": "tool_use",
                "id": "toolu_01ABCdef",
                "name": "get_weather",
                "input": {"city": "Paris", "unit": "c"},
            },
        ],
    },
    {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_01ABCdef",
                "content": "18C and sunny",
            }
        ],
    },
]


def _body(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"max_tokens": 256, "messages": USER_HI}
    base.update(fields)
    return base


# (case_id, body, tags). Tag "core" runs on every provider; "matrix" runs on
# every model of every full-tier provider; everything runs on full-tier
# providers with their first model.
CORPUS: tuple[tuple[str, dict[str, Any], frozenset[str]], ...] = (
    ("plain_text", _body(), frozenset({"core", "matrix"})),
    ("plain_text_client_stream", _body(stream=True), frozenset({"core"})),
    (
        "system_string",
        _body(system="You are terse."),
        frozenset({"core"}),
    ),
    (
        "system_list",
        _body(
            system=[
                {"type": "text", "text": "You are terse."},
                {"type": "text", "text": "Answer in English."},
            ]
        ),
        frozenset({"core"}),
    ),
    (
        "system_list_cache_control",
        _body(
            system=[
                {"type": "text", "text": "You are Claude Code."},
                {
                    "type": "text",
                    "text": "Long project context...",
                    "cache_control": {"type": "ephemeral"},
                },
            ]
        ),
        frozenset({"core"}),
    ),
    ("system_blank_string", _body(system="   "), frozenset()),
    (
        "system_blank_blocks",
        _body(
            system=[
                {"type": "text", "text": " "},
                {"type": "text", "text": "Keep me."},
            ]
        ),
        frozenset(),
    ),
    (
        "multi_turn",
        _body(
            messages=[
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello! How can I help?"},
                {"role": "user", "content": [{"type": "text", "text": "Tell a joke"}]},
            ]
        ),
        frozenset({"core"}),
    ),
    (
        "stop_sequences",
        _body(stop_sequences=["\n\nHuman:", "END"]),
        frozenset({"core", "matrix"}),
    ),
    (
        "sampling_params",
        _body(temperature=0.3, top_p=0.9, top_k=40),
        frozenset({"core", "matrix"}),
    ),
    ("temperature_zero", _body(temperature=0), frozenset()),
    (
        "metadata_user_id",
        _body(metadata={"user_id": "user-1234"}),
        frozenset({"core"}),
    ),
    (
        "tools_auto",
        _body(tools=[WEATHER_TOOL, TIME_TOOL], tool_choice={"type": "auto"}),
        frozenset({"core", "matrix"}),
    ),
    ("tools_no_choice", _body(tools=[WEATHER_TOOL]), frozenset()),
    (
        "tools_custom_type",
        _body(tools=[{"type": "custom", **WEATHER_TOOL}]),
        frozenset(),
    ),
    (
        "tools_any",
        _body(tools=[WEATHER_TOOL], tool_choice={"type": "any"}),
        frozenset({"core"}),
    ),
    (
        "tools_named",
        _body(
            tools=[WEATHER_TOOL, TIME_TOOL],
            tool_choice={"type": "tool", "name": "get_weather"},
        ),
        frozenset({"core"}),
    ),
    (
        "tools_none",
        _body(tools=[WEATHER_TOOL], tool_choice={"type": "none"}),
        frozenset(),
    ),
    (
        "tools_disable_parallel",
        _body(
            tools=[WEATHER_TOOL],
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
        ),
        frozenset(),
    ),
    (
        "tools_cache_control",
        _body(
            tools=[
                WEATHER_TOOL,
                {**TIME_TOOL, "cache_control": {"type": "ephemeral"}},
            ]
        ),
        frozenset(),
    ),
    (
        "tool_result_string",
        _body(tools=[WEATHER_TOOL], messages=TOOL_USE_HISTORY),
        frozenset({"core"}),
    ),
    (
        "tool_result_list_text_image",
        _body(
            tools=[WEATHER_TOOL],
            messages=[
                *TOOL_USE_HISTORY[:2],
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_01ABCdef",
                            "content": [
                                {"type": "text", "text": "Radar image:"},
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": PNG_B64,
                                    },
                                },
                            ],
                        },
                        {"type": "text", "text": "Summarise."},
                    ],
                },
            ],
        ),
        frozenset(),
    ),
    (
        "tool_result_is_error",
        _body(
            tools=[WEATHER_TOOL],
            messages=[
                *TOOL_USE_HISTORY[:2],
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_01ABCdef",
                            "content": "city not found",
                            "is_error": True,
                        }
                    ],
                },
            ],
        ),
        frozenset(),
    ),
    (
        "parallel_tool_use_history",
        _body(
            tools=[WEATHER_TOOL, TIME_TOOL],
            messages=[
                {"role": "user", "content": "Weather and time in Rome?"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_A1",
                            "name": "get_weather",
                            "input": {"city": "Rome"},
                        },
                        {
                            "type": "tool_use",
                            "id": "toolu_B2",
                            "name": "get_time",
                            "input": {},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_A1",
                            "content": [{"type": "text", "text": "22C"}],
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_B2",
                            "content": "14:05",
                        },
                    ],
                },
            ],
        ),
        frozenset(),
    ),
    (
        "image_base64",
        _body(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": PNG_B64,
                            },
                        },
                        {"type": "text", "text": "What is this?"},
                    ],
                }
            ]
        ),
        frozenset({"core"}),
    ),
    (
        "image_url",
        _body(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "url", "url": IMAGE_URL}},
                        {"type": "text", "text": "Describe."},
                    ],
                }
            ]
        ),
        frozenset({"core"}),
    ),
    (
        "document_pdf_base64",
        _body(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": PDF_B64,
                            },
                        },
                        {"type": "text", "text": "Summarise the PDF."},
                    ],
                }
            ]
        ),
        frozenset(),
    ),
    (
        "thinking_enabled_1024",
        _body(max_tokens=4096, thinking={"type": "enabled", "budget_tokens": 1024}),
        frozenset({"core", "matrix"}),
    ),
    (
        "thinking_enabled_8192",
        _body(max_tokens=16000, thinking={"type": "enabled", "budget_tokens": 8192}),
        frozenset(),
    ),
    (
        "thinking_enabled_32000",
        _body(max_tokens=64000, thinking={"type": "enabled", "budget_tokens": 32000}),
        frozenset(),
    ),
    (
        "thinking_adaptive",
        _body(max_tokens=4096, thinking={"type": "adaptive"}),
        frozenset(),
    ),
    (
        "thinking_disabled",
        _body(thinking={"type": "disabled"}),
        frozenset({"matrix"}),
    ),
    (
        "thinking_with_effort",
        _body(max_tokens=4096, thinking={"type": "enabled", "effort": "high"}),
        frozenset(),
    ),
    (
        "output_config_effort_low",
        _body(output_config={"effort": "low"}),
        frozenset({"core", "matrix"}),
    ),
    (
        "output_config_effort_max",
        _body(output_config={"effort": "max"}),
        frozenset(),
    ),
    (
        "output_config_format_only",
        _body(
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": {"type": "object", "properties": {}},
                }
            }
        ),
        frozenset(),
    ),
    (
        "reasoning_effort_present",
        _body(reasoning_effort="medium"),
        frozenset({"matrix"}),
    ),
    (
        "reasoning_effort_and_thinking",
        _body(
            max_tokens=4096,
            reasoning_effort="low",
            thinking={"type": "enabled", "budget_tokens": 4096},
        ),
        frozenset(),
    ),
    (
        "cache_control_message_blocks",
        _body(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Big document ...",
                            "cache_control": {"type": "ephemeral"},
                        },
                        {"type": "text", "text": "Question?"},
                    ],
                }
            ]
        ),
        frozenset({"core"}),
    ),
    (
        "cache_control_ttl",
        _body(
            system=[
                {
                    "type": "text",
                    "text": "Sys",
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                }
            ]
        ),
        frozenset(),
    ),
    (
        "assistant_thinking_history",
        _body(
            max_tokens=4096,
            thinking={"type": "enabled", "budget_tokens": 2048},
            messages=[
                {"role": "user", "content": "2+2?"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "Simple arithmetic.",
                            "signature": "sig-abc123==",
                        },
                        {"type": "text", "text": "4"},
                    ],
                },
                {"role": "user", "content": "And 3+3?"},
            ],
        ),
        frozenset({"core"}),
    ),
    (
        "assistant_redacted_thinking_history",
        _body(
            messages=[
                {"role": "user", "content": "Hi"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "redacted_thinking", "data": "EncryptedBlob=="},
                        {"type": "text", "text": "Hello."},
                    ],
                },
                {"role": "user", "content": "Again"},
            ]
        ),
        frozenset(),
    ),
    (
        "thinking_tool_use_history",
        _body(
            max_tokens=4096,
            thinking={"type": "enabled", "budget_tokens": 2048},
            tools=[WEATHER_TOOL],
            messages=[
                {"role": "user", "content": "Weather in Oslo?"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "Need the tool.",
                            "signature": "sig-xyz",
                        },
                        {
                            "type": "tool_use",
                            "id": "toolu_T1",
                            "name": "get_weather",
                            "input": {"city": "Oslo"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_T1",
                            "content": "-3C",
                        }
                    ],
                },
            ],
        ),
        frozenset(),
    ),
    (
        "web_search_only",
        _body(tools=[{"type": "web_search_20250305", "name": "web_search"}]),
        frozenset({"core"}),
    ),
    (
        "web_search_with_function_tool",
        _body(
            tools=[
                {"type": "web_search_20250305", "name": "web_search", "max_uses": 3},
                WEATHER_TOOL,
            ],
            tool_choice={"type": "any"},
        ),
        frozenset(),
    ),
    (
        "web_search_allowed_domains",
        _body(
            tools=[
                {
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "allowed_domains": ["example.com"],
                }
            ]
        ),
        frozenset(),
    ),
    (
        "web_search_forced_choice",
        _body(
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            tool_choice={"type": "tool", "name": "web_search"},
        ),
        frozenset(),
    ),
    ("max_tokens_large", _body(max_tokens=128000), frozenset({"core", "matrix"})),
    (
        "unicode_content",
        _body(
            system="Réponds en français 🇫🇷",
            messages=[{"role": "user", "content": "日本語で「こんにちは」🙂 — ok?"}],
        ),
        frozenset({"core"}),
    ),
    (
        "empty_string_content",
        _body(
            messages=[
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": ""},
                {"role": "user", "content": "Still there?"},
            ]
        ),
        frozenset(),
    ),
    (
        "empty_list_content",
        _body(
            messages=[
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": []},
                {"role": "user", "content": "Hello?"},
            ]
        ),
        frozenset(),
    ),
    (
        "assistant_prefill",
        _body(
            messages=[
                {"role": "user", "content": "Give JSON"},
                {"role": "assistant", "content": "{"},
            ]
        ),
        frozenset(),
    ),
    (
        "extra_unknown_fields",
        _body(
            context_management={"edits": []},
            service_tier="auto",
            mcp_servers=[],
            container="c1",
            anthropic_version="2023-06-01",
            stream=True,
        ),
        frozenset(),
    ),
    (
        "stream_true_with_tools",
        _body(stream=True, tools=[WEATHER_TOOL], temperature=1.0),
        frozenset(),
    ),
)

# Cases that only make sense with model reasoning metadata.
REASONING_CASES: tuple[str, ...] = (
    "plain_text",
    "thinking_enabled_1024",
    "thinking_disabled",
    "output_config_effort_max",
    "reasoning_effort_present",
)


# ---------------------------------------------------------------------------
# Canned upstream responses
# ---------------------------------------------------------------------------


def sse(payloads: list[Any], done: bool = True) -> bytes:
    lines = [
        f"data: {p if isinstance(p, str) else json.dumps(p)}\n\n" for p in payloads
    ]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def openai_chunk(
    delta: dict[str, Any] | None = None,
    finish: str | None = None,
    *,
    usage: dict[str, Any] | None = None,
    cid: str = "chatcmpl-oracle1",
    model: str = "upstream-model",
    choices: bool = True,
) -> dict[str, Any]:
    chunk: dict[str, Any] = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "model": model,
        "choices": (
            [{"index": 0, "delta": delta or {}, "finish_reason": finish}]
            if choices
            else []
        ),
    }
    if usage is not None:
        chunk["usage"] = usage
    return chunk


DEFAULT_OPENAI_SSE = sse(
    [
        openai_chunk({"role": "assistant", "content": "ok"}),
        openai_chunk(
            {},
            "stop",
            usage={"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        ),
    ]
)


def anthropic_sse(events: list[dict[str, Any]]) -> bytes:
    return "".join(
        f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events
    ).encode()


def anthropic_text_events(
    text: str = "ok", model: str = "upstream-model"
) -> list[dict[str, Any]]:
    return [
        {
            "type": "message_start",
            "message": {
                "id": "msg_upstream01",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 5, "output_tokens": 0},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    ]


DEFAULT_ANTHROPIC_SSE = anthropic_sse(anthropic_text_events())
OLLAMA_NDJSON = (
    json.dumps(
        {
            "model": "llama3.2",
            "created_at": "2024-01-01T00:00:00Z",
            "message": {"role": "assistant", "content": "ok"},
            "done": False,
        }
    )
    + "\n"
    + json.dumps(
        {
            "model": "llama3.2",
            "created_at": "2024-01-01T00:00:00Z",
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 5,
            "eval_count": 1,
        }
    )
    + "\n"
).encode()
PNG_BYTES = __import__("base64").b64decode(PNG_B64)


def default_response(request: httpx.Request) -> httpx.Response:
    if request.url.host == IMAGE_HOST:
        return httpx.Response(
            200, headers={"content-type": "image/png"}, content=PNG_BYTES
        )
    path = request.url.path
    if path.endswith("/messages"):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=DEFAULT_ANTHROPIC_SSE,
        )
    if path.endswith("/api/chat") or path.endswith("/api/generate"):
        return httpx.Response(
            200, headers={"content-type": "application/x-ndjson"}, content=OLLAMA_NDJSON
        )
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=DEFAULT_OPENAI_SSE
    )


# ---------------------------------------------------------------------------
# Dispatch harness
# ---------------------------------------------------------------------------


@dataclass
class Capture:
    requests: list[httpx.Request] = field(default_factory=list)


def litellm_prefix(provider: BaseUpstreamProvider) -> str:
    """Baseline-only API: the working tree drops it, so it is not typed there."""
    return cast(str, cast(Any, provider).get_litellm_provider_prefix())


async def _dispatch(
    provider: BaseUpstreamProvider,
    model_obj: Model,
    body: dict[str, Any],
) -> tuple[bool, Any, str | None]:
    return await dispatch_anthropic_messages(
        request_body=json.dumps(body).encode(),
        model_obj=model_obj,
        base_url=provider.base_url,
        api_key=provider.api_key,
        provider_prefix=litellm_prefix(provider),
        transform_model_name=provider.transform_model_name,
        adapt_request=lambda b: provider.adapt_messages_request(b, model_obj),
        transform_stream=provider.transform_messages_stream,
    )


async def run_dispatch(
    provider: BaseUpstreamProvider,
    model_obj: Model,
    body: dict[str, Any],
    responder: Callable[[httpx.Request], httpx.Response],
) -> dict[str, Any]:
    """Dispatch once under respx; return requests made and the outcome."""
    capture = Capture()

    def handler(request: httpx.Request) -> httpx.Response:
        capture.requests.append(request)
        return responder(request)

    outcome: dict[str, Any] = {}
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        router.route().mock(side_effect=handler)
        try:
            client_stream, result, requested_model = await _dispatch(
                provider, model_obj, copy.deepcopy(body)
            )
        except UpstreamError as exc:
            outcome["error"] = upstream_error_dict(exc)
        else:
            outcome["requested_model"] = requested_model
            if client_stream:
                events: list[dict[str, Any]] = []
                try:
                    sse_buffer = b""
                    async for chunk in result:
                        parsed, sse_buffer = events_from_chunk(chunk, sse_buffer)
                        events.extend(parsed)
                except Exception as exc:  # recorded, not swallowed
                    outcome["stream_exception"] = describe_exception(exc)
                outcome["events"] = events
            else:
                outcome["aggregate"] = result
        await _settle(capture)
    outcome["requests"] = capture.requests
    return outcome


async def _settle(capture: Capture) -> None:
    """Let litellm's background logging (cost lookups, Ollama ``/api/show``)
    finish inside this case's mock so it cannot leak into the next case."""
    stable_rounds = 0
    seen = len(capture.requests)
    while stable_rounds < SETTLE_ROUNDS:
        await asyncio.sleep(SETTLE_STEP_SECONDS)
        queue = getattr(_LOGGING_EXECUTOR, "_work_queue", None)
        busy = queue is not None and not queue.empty()
        if len(capture.requests) == seen and not busy:
            stable_rounds += 1
        else:
            stable_rounds = 0
            seen = len(capture.requests)


def _main_requests(requests: list[httpx.Request], base_url: str) -> list[httpx.Request]:
    """Completion calls to the provider host, excluding metadata lookups."""
    host = httpx.URL(base_url).host
    return [r for r in requests if r.url.host == host and not _is_aux_request(r)]


def _is_aux_request(request: httpx.Request) -> bool:
    return any(request.url.path.endswith(path) for path in AUX_PATHS)


def _aux_records(
    requests: list[httpx.Request], upstream_model: object
) -> list[dict[str, Any]]:
    """Deduplicated, sorted: litellm repeats these a run-dependent number of
    times from background threads. Lookups naming another model are a previous
    case's background work landing late and are dropped."""
    unique = set()
    for r in requests:
        if not _is_aux_request(r):
            continue
        payload = json.loads(r.content) if r.content else None
        if isinstance(payload, dict) and payload.get("name") not in (
            None,
            upstream_model,
        ):
            continue
        unique.add(
            json.dumps(
                {"method": r.method, "url": str(r.url), "json": payload},
                sort_keys=True,
            )
        )
    return [json.loads(item) for item in sorted(unique)]


# ---------------------------------------------------------------------------
# A. Request side
# ---------------------------------------------------------------------------


async def capture_request_case(
    case_id: str,
    spec: ProviderSpec,
    model_id: str,
    body: dict[str, Any],
    reasoning_variant: str = "none",
) -> dict[str, Any]:
    provider = spec.build()
    model_obj = make_model(model_id, reasoning_variant)
    outcome = await run_dispatch(provider, model_obj, body, default_response)
    keep = collect_strings(body)
    record: dict[str, Any] = {
        "case_id": case_id,
        "provider": spec.name,
        "provider_class": type(provider).__name__,
        "provider_prefix": litellm_prefix(provider),
        "base_url": provider.base_url,
        "api_key_blank": not provider.api_key,
        "model_id": model_id,
        "model_reasoning": REASONING_VARIANTS[reasoning_variant],
        "client_stream": bool(body.get("stream", False)),
        "input_body": body,
    }
    # Other hosts (e.g. litellm's one-off huggingface tokenizer download) are
    # process-order dependent side effects and are not recorded.
    upstream_host = httpx.URL(provider.base_url).host
    requests = [r for r in outcome["requests"] if r.url.host == upstream_host]
    upstream = [r for r in requests if not _is_aux_request(r)]
    side = sorted({str(r.url) for r in outcome["requests"] if r.url.host == IMAGE_HOST})
    if "error" in outcome and not upstream:
        record["expected"] = {"error": outcome["error"]}
        return record
    if not upstream:
        raise RuntimeError(
            f"{spec.name}/{case_id}/{model_id}: no upstream request intercepted "
            f"(outcome={outcome})"
        )
    expected = normalise(request_record(upstream[0]), keep)
    expected["request_count"] = len(upstream)
    aux = _aux_records(requests, (expected.get("json") or {}).get("model"))
    if aux:
        expected["aux_requests"] = aux
    if side:
        expected["side_requests"] = [{"method": "GET", "url": url} for url in side]
    if "error" in outcome:
        expected["dispatch_error"] = outcome["error"]
    if "stream_exception" in outcome:
        expected["stream_exception"] = outcome["stream_exception"]
    record["expected"] = expected
    return record


def _case_plan() -> list[tuple[str, ProviderSpec, str, dict[str, Any], str]]:
    plan: list[tuple[str, ProviderSpec, str, dict[str, Any], str]] = []
    for spec in PROVIDERS:
        first = spec.models[0]
        for case_id, body, tags in CORPUS:
            if spec.tier == "full" or "core" in tags:
                plan.append((case_id, spec, first, body, "none"))
        for model_id in spec.models[1:]:
            for case_id, body, tags in CORPUS:
                if "matrix" in tags:
                    plan.append((case_id, spec, model_id, body, "none"))
    by_id = {case_id: body for case_id, body, _ in CORPUS}
    for spec in PROVIDERS:
        for model_id in spec.reasoning_models:
            for variant in ("efforts_low_med_high", "mandatory_low_high"):
                for case_id in REASONING_CASES:
                    plan.append(
                        (
                            f"{case_id}__reasoning_{variant}",
                            spec,
                            model_id,
                            by_id[case_id],
                            variant,
                        )
                    )
    return plan


async def capture_requests() -> list[dict[str, Any]]:
    records = []
    for case_id, spec, model_id, body, variant in _case_plan():
        records.append(
            await capture_request_case(case_id, spec, model_id, body, variant)
        )
    return records


# ---------------------------------------------------------------------------
# A2. Adapter translation (gemini_messages.py)
# ---------------------------------------------------------------------------

GEMINI_POPPED = (
    "thinking",
    "cache_control",
    "context_management",
    "output_config",
    "mcp_servers",
    "service_tier",
    "anthropic_version",
    "anthropic_beta",
)


def capture_adapter() -> list[dict[str, Any]]:
    from litellm.llms.anthropic.experimental_pass_through.adapters.transformation import (  # noqa: E501
        AnthropicAdapter,
    )

    records = []
    for model in ("gemini-2.5-flash", "gpt-4o"):
        for case_id, body, _tags in CORPUS:
            for variant in ("gemini_preprocessed", "raw"):
                prepared = copy.deepcopy(body)
                prepared.pop("model", None)
                prepared.pop("stream", None)
                if variant == "gemini_preprocessed":
                    for name in GEMINI_POPPED:
                        prepared.pop(name, None)
                record: dict[str, Any] = {
                    "case_id": f"{case_id}__{variant}",
                    "model": model,
                    "input_body": prepared,
                }
                try:
                    translated = AnthropicAdapter().translate_completion_input_params(
                        {"model": model, **copy.deepcopy(prepared)}
                    )
                except Exception as exc:
                    record["expected_exception"] = describe_exception(exc)
                else:
                    record["expected_openai_kwargs"] = normalise(
                        json.loads(json.dumps(translated, default=_jsonable)),
                        collect_strings(prepared),
                    )
                records.append(record)
    return records


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return repr(value)


# ---------------------------------------------------------------------------
# B. Response side
# ---------------------------------------------------------------------------

TOOL_ARG_CHUNKS = [
    openai_chunk({"role": "assistant", "content": None}),
    openai_chunk(
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_w1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": ""},
                }
            ]
        }
    ),
    openai_chunk(
        {"tool_calls": [{"index": 0, "function": {"arguments": '{"city": '}}]}
    ),
    openai_chunk(
        {
            "tool_calls": [
                {
                    "index": 1,
                    "id": "call_t2",
                    "type": "function",
                    "function": {"name": "get_time", "arguments": '{"tz"'},
                }
            ]
        }
    ),
    openai_chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"Paris"}'}}]}),
    openai_chunk({"tool_calls": [{"index": 1, "function": {"arguments": ': "UTC"}'}}]}),
    openai_chunk(
        {},
        "tool_calls",
        usage={"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60},
    ),
]

RESPONSE_STREAMS: dict[str, list[Any]] = {
    "text_only": [
        openai_chunk({"role": "assistant", "content": ""}),
        openai_chunk({"content": "Hello"}),
        openai_chunk({"content": ", world"}),
        openai_chunk({}, "stop"),
        openai_chunk(
            choices=False,
            usage={"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        ),
    ],
    "text_usage_cached_tokens": [
        openai_chunk({"role": "assistant", "content": "Cached!"}),
        openai_chunk(
            {},
            "stop",
            usage={
                "prompt_tokens": 1200,
                "completion_tokens": 2,
                "total_tokens": 1202,
                "prompt_tokens_details": {"cached_tokens": 1024},
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        ),
    ],
    "reasoning_content_then_text": [
        openai_chunk({"role": "assistant", "content": None, "reasoning_content": ""}),
        openai_chunk({"reasoning_content": "Let me think. "}),
        openai_chunk({"reasoning_content": "2+2 is 4."}),
        openai_chunk({"content": "4"}),
        openai_chunk(
            {},
            "stop",
            usage={
                "prompt_tokens": 8,
                "completion_tokens": 12,
                "total_tokens": 20,
                "completion_tokens_details": {"reasoning_tokens": 10},
            },
        ),
    ],
    "reasoning_field_then_text": [
        openai_chunk({"role": "assistant", "content": "", "reasoning": "Hmm, "}),
        openai_chunk({"content": "", "reasoning": "thinking hard."}),
        openai_chunk({"content": "Answer."}),
        openai_chunk(
            {},
            "stop",
            usage={"prompt_tokens": 8, "completion_tokens": 6, "total_tokens": 14},
        ),
    ],
    "reasoning_details_then_text": [
        openai_chunk(
            {
                "role": "assistant",
                "content": "",
                "reasoning": "Step one.",
                "reasoning_details": [
                    {"type": "reasoning.text", "text": "Step one.", "index": 0}
                ],
            }
        ),
        openai_chunk({"content": "Done."}),
        openai_chunk(
            {},
            "stop",
            usage={"prompt_tokens": 4, "completion_tokens": 4, "total_tokens": 8},
        ),
    ],
    "parallel_tool_calls_split_args": TOOL_ARG_CHUNKS,
    "text_then_tool_call": [
        openai_chunk({"role": "assistant", "content": "Checking."}),
        openai_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_x9",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city":"Rome"}',
                        },
                    }
                ]
            }
        ),
        openai_chunk(
            {},
            "tool_calls",
            usage={"prompt_tokens": 20, "completion_tokens": 9, "total_tokens": 29},
        ),
    ],
    "tool_call_empty_args": [
        openai_chunk({"role": "assistant", "content": None}),
        openai_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_e0",
                        "type": "function",
                        "function": {"name": "get_time", "arguments": ""},
                    }
                ]
            }
        ),
        openai_chunk(
            {},
            "tool_calls",
            usage={"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},
        ),
    ],
    "finish_length": [
        openai_chunk({"role": "assistant", "content": "Truncat"}),
        openai_chunk(
            {},
            "length",
            usage={"prompt_tokens": 5, "completion_tokens": 64, "total_tokens": 69},
        ),
    ],
    "finish_content_filter": [
        openai_chunk({"role": "assistant", "content": "I can"}),
        openai_chunk(
            {},
            "content_filter",
            usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        ),
    ],
    "finish_tool_calls_without_tools": [
        openai_chunk({"role": "assistant", "content": "odd"}),
        openai_chunk({}, "tool_calls"),
    ],
    "usage_only_final_chunk_no_choices": [
        openai_chunk({"role": "assistant", "content": "Hi"}),
        openai_chunk({}, "stop"),
        openai_chunk(
            choices=False,
            usage={"prompt_tokens": 7, "completion_tokens": 1, "total_tokens": 8},
        ),
    ],
    "no_usage_at_all": [
        openai_chunk({"role": "assistant", "content": "Hi"}),
        openai_chunk({}, "stop"),
    ],
    "usage_with_cost_fields": [
        openai_chunk({"role": "assistant", "content": "Priced"}),
        openai_chunk({}, "stop"),
        openai_chunk(
            choices=False,
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "total_tokens": 110,
                "cost": 0.00123,
                "is_byok": False,
                "cost_details": {
                    "upstream_inference_cost": None,
                    "upstream_inference_prompt_cost": 0.001,
                    "upstream_inference_completions_cost": 0.00023,
                },
                "prompt_tokens_details": {"cached_tokens": 0},
            },
        ),
    ],
    "error_object_mid_stream": [
        openai_chunk({"role": "assistant", "content": "Partial"}),
        {
            "error": {
                "message": "Upstream overloaded",
                "type": "server_error",
                "code": "overloaded",
            }
        },
    ],
    "id_and_model_change": [
        openai_chunk(
            {"role": "assistant", "content": "A"}, cid="chatcmpl-first", model="m-1"
        ),
        openai_chunk({"content": "B"}, cid="chatcmpl-second", model="m-2"),
        openai_chunk(
            {},
            "stop",
            cid="chatcmpl-second",
            model="m-2",
            usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        ),
    ],
    "empty_stream_done_only": [],
    "role_only_then_stop": [
        openai_chunk({"role": "assistant"}),
        openai_chunk(
            {},
            "stop",
            usage={"prompt_tokens": 3, "completion_tokens": 0, "total_tokens": 3},
        ),
    ],
    "encrypted_reasoning_trailer": [
        openai_chunk(
            {"role": "assistant", "content": "", "reasoning_content": "Plan."}
        ),
        openai_chunk({"content": "Answer."}),
        openai_chunk({"reasoning_content": "__ENCRYPTED_REASONING__gAAAAABblob"}),
        openai_chunk(
            {},
            "stop",
            usage={"prompt_tokens": 6, "completion_tokens": 9, "total_tokens": 15},
        ),
    ],
}

NO_DONE_STREAMS = frozenset({"text_only", "error_object_mid_stream"})

ANTHROPIC_STREAMS: dict[str, list[dict[str, Any]]] = {
    "anthropic_text": anthropic_text_events("Hello from native"),
    "anthropic_thinking_signature_text": [
        {
            "type": "message_start",
            "message": {
                "id": "msg_native02",
                "type": "message",
                "role": "assistant",
                "model": "deepseek-reasoner",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 8,
                },
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "Reasoning..."},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "c2lnbmF0dXJl"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "Result."},
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 30},
        },
        {"type": "message_stop"},
    ],
    "anthropic_tool_use": [
        {
            "type": "message_start",
            "message": {
                "id": "msg_native03",
                "type": "message",
                "role": "assistant",
                "model": "deepseek-chat",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 50, "output_tokens": 0},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_native1",
                "name": "get_weather",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"city":'},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '"Oslo"}'},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 15},
        },
        {"type": "message_stop"},
    ],
    "anthropic_ping_and_error": [
        *anthropic_text_events("x")[:3],
        {"type": "ping"},
        {
            "type": "error",
            "error": {"type": "overloaded_error", "message": "Overloaded"},
        },
    ],
}

RESPONSE_PROVIDER_PLAN: tuple[
    tuple[str, Callable[[], BaseUpstreamProvider], str, str, tuple[str, ...]], ...
] = (
    # (provider label, factory, model id, wire format, stream names)
    (
        "generic-local",
        lambda: GenericUpstreamProvider("http://127.0.0.1:3091/v1", API_KEY),
        "gpt-4o",
        "openai",
        tuple(RESPONSE_STREAMS),
    ),
    (
        "venice",
        lambda: VeniceUpstreamProvider(API_KEY),
        "openai-gpt-oss-120b",
        "openai",
        (
            "text_only",
            "reasoning_content_then_text",
            "reasoning_field_then_text",
            "encrypted_reasoning_trailer",
            "parallel_tool_calls_split_args",
            "usage_with_cost_fields",
        ),
    ),
    (
        "deepseek",
        lambda: DeepSeekUpstreamProvider(API_KEY),
        "deepseek-reasoner",
        "anthropic",
        tuple(ANTHROPIC_STREAMS),
    ),
    (
        "generic-minimax",
        lambda: GenericUpstreamProvider("https://api.minimax.io/v1", API_KEY),
        "MiniMax-M2",
        "anthropic",
        ("anthropic_text", "anthropic_tool_use"),
    ),
    (
        "generic-openrouter",
        lambda: GenericUpstreamProvider("https://openrouter.ai/api/v1", API_KEY),
        "anthropic/claude-sonnet-4.5",
        "openai",
        (
            "text_only",
            "reasoning_field_then_text",
            "reasoning_details_then_text",
            "usage_with_cost_fields",
        ),
    ),
)

RESPONSE_REQUEST_BODY = {
    "max_tokens": 512,
    "messages": [{"role": "user", "content": "Hi"}],
    "tools": [WEATHER_TOOL, TIME_TOOL],
}


def _stream_bytes(fmt: str, name: str) -> tuple[bytes, list[str], list[Any]]:
    payloads: list[Any]
    if fmt == "anthropic":
        payloads = list(ANTHROPIC_STREAMS[name])
        raw = anthropic_sse(payloads)
    else:
        payloads = list(RESPONSE_STREAMS[name])
        raw = sse(payloads, done=name not in NO_DONE_STREAMS)
    return raw, raw.decode().split("\n"), payloads


async def capture_responses() -> list[dict[str, Any]]:
    records = []
    for label, factory, model_id, fmt, names in RESPONSE_PROVIDER_PLAN:
        for name in names:
            raw, lines, payloads = _stream_bytes(fmt, name)
            headers = {"content-type": "text/event-stream"}

            def responder(request: httpx.Request, raw: bytes = raw) -> httpx.Response:
                if request.url.host == IMAGE_HOST:
                    return default_response(request)
                return httpx.Response(200, headers=headers, content=raw)

            keep = collect_strings(payloads)
            record: dict[str, Any] = {
                "case_id": f"{label}__{name}",
                "provider": label,
                "requested_model": model_id,
                "upstream": {
                    "status": 200,
                    "headers": headers,
                    "sse_lines": lines,
                },
            }
            provider = factory()
            model_obj = make_model(model_id)
            streamed = await run_dispatch(
                provider,
                model_obj,
                {**RESPONSE_REQUEST_BODY, "stream": True},
                responder,
            )
            if not _main_requests(streamed["requests"], provider.base_url):
                raise RuntimeError(f"responses/{label}/{name}: no request intercepted")
            if "error" in streamed:
                record["expected_stream_error"] = streamed["error"]
            record["expected_events"] = normalise(streamed.get("events", []), keep)
            if "stream_exception" in streamed:
                record["expected_stream_exception"] = streamed["stream_exception"]

            aggregated = await run_dispatch(
                provider, model_obj, dict(RESPONSE_REQUEST_BODY), responder
            )
            if "error" in aggregated:
                record["expected_aggregate_error"] = aggregated["error"]
                record["expected_aggregate"] = None
            else:
                record["expected_aggregate"] = normalise(
                    json.loads(json.dumps(aggregated["aggregate"], default=_jsonable)),
                    keep,
                )
            records.append(record)
    return records


# ---------------------------------------------------------------------------
# B2. Upstream HTTP errors
# ---------------------------------------------------------------------------

ERROR_RESPONSES: tuple[tuple[str, int, dict[str, str], Any], ...] = (
    (
        "400_json_error",
        400,
        {"content-type": "application/json"},
        {
            "error": {
                "message": "Invalid value for 'max_tokens'",
                "type": "invalid_request_error",
                "param": "max_tokens",
                "code": "invalid_value",
            }
        },
    ),
    (
        "400_context_length",
        400,
        {"content-type": "application/json"},
        {
            "error": {
                "message": "This model's maximum context length is 8192 tokens.",
                "type": "invalid_request_error",
                "code": "context_length_exceeded",
            }
        },
    ),
    (
        "401_unauthorized",
        401,
        {"content-type": "application/json"},
        {
            "error": {
                "message": "Incorrect API key provided",
                "type": "invalid_request_error",
                "code": "invalid_api_key",
            }
        },
    ),
    (
        "402_payment_required",
        402,
        {"content-type": "application/json"},
        {"error": {"message": "Insufficient credits", "code": 402}},
    ),
    (
        "404_model_not_found",
        404,
        {"content-type": "application/json"},
        {
            "error": {
                "message": "The model `nope` does not exist",
                "type": "invalid_request_error",
                "code": "model_not_found",
            }
        },
    ),
    (
        "429_rate_limit_retry_after",
        429,
        {"content-type": "application/json", "retry-after": "1"},
        {
            "error": {
                "message": "Rate limit reached for requests",
                "type": "requests",
                "code": "rate_limit_exceeded",
            }
        },
    ),
    (
        "429_quota",
        429,
        {"content-type": "application/json"},
        {
            "error": {
                "message": "You exceeded your current quota, please check your plan and billing details.",
                "type": "insufficient_quota",
                "code": "insufficient_quota",
            }
        },
    ),
    (
        "500_internal",
        500,
        {"content-type": "application/json"},
        {"error": {"message": "Internal server error", "type": "server_error"}},
    ),
    (
        "503_unavailable_text",
        503,
        {"content-type": "text/plain"},
        "Service Unavailable",
    ),
    (
        "anthropic_shaped_400",
        400,
        {"content-type": "application/json"},
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "messages: field required",
            },
        },
    ),
)

ERROR_PROVIDER_PLAN: tuple[tuple[str, Callable[[], BaseUpstreamProvider], str], ...] = (
    (
        "generic-local",
        lambda: GenericUpstreamProvider("http://127.0.0.1:3091/v1", API_KEY),
        "gpt-4o",
    ),
    ("deepseek", lambda: DeepSeekUpstreamProvider(API_KEY), "deepseek-chat"),
    ("venice", lambda: VeniceUpstreamProvider(API_KEY), "llama-3.3-70b"),
)


async def capture_errors() -> list[dict[str, Any]]:
    records = []
    for label, factory, model_id in ERROR_PROVIDER_PLAN:
        for name, status, headers, payload in ERROR_RESPONSES:
            content = (
                payload.encode()
                if isinstance(payload, str)
                else json.dumps(payload).encode()
            )

            def responder(
                request: httpx.Request,
                status: int = status,
                headers: dict[str, str] = headers,
                content: bytes = content,
            ) -> httpx.Response:
                return httpx.Response(status, headers=headers, content=content)

            record: dict[str, Any] = {
                "case_id": f"{label}__{name}",
                "provider": label,
                "requested_model": model_id,
                "upstream": {
                    "status": status,
                    "headers": headers,
                    "body": payload,
                },
            }
            for client_stream in (False, True):
                body = {
                    "max_tokens": 64,
                    "messages": [{"role": "user", "content": "Hi"}],
                    "stream": client_stream,
                }
                provider = factory()
                outcome = await run_dispatch(
                    provider, make_model(model_id), body, responder
                )
                attempts = len(_main_requests(outcome["requests"], provider.base_url))
                if attempts == 0:
                    raise RuntimeError(f"errors/{label}/{name}: no request intercepted")
                key = "client_stream" if client_stream else "client_non_stream"
                result: dict[str, Any] = {"attempts": attempts}
                if "error" in outcome:
                    result["error"] = outcome["error"]
                else:
                    result["unexpected_success"] = normalise(
                        json.loads(
                            json.dumps(
                                outcome.get("aggregate") or outcome.get("events"),
                                default=_jsonable,
                            )
                        ),
                        set(),
                    )
                    if "stream_exception" in outcome:
                        result["stream_exception"] = outcome["stream_exception"]
                record[key] = result
            records.append(record)
    return records


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def write_fixture(name: str, data: Any) -> None:
    path = FIXTURE_DIR / name
    path.write_text(
        json.dumps(
            data, indent=1, sort_keys=False, ensure_ascii=False, default=_jsonable
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {path.relative_to(ROOT)} ({len(data)} cases)")


def write_meta() -> None:
    from importlib.metadata import version

    meta = {
        "oracle_base_ref": ORACLE_BASE_REF,
        "litellm_version": version("litellm"),
        "openai_version": version("openai"),
        "httpx_version": version("httpx"),
        "model_cost_file": str(VENDORED_MODEL_COST.relative_to(ROOT)),
        "model_cost_sha256": MODEL_COST_SHA256,
        "model_cost_entries": len(litellm.model_cost),
        "id_placeholder": "<id>",
        "id_patterns": [p.pattern for p in ID_PATTERNS],
    }
    (FIXTURE_DIR / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")


async def main() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    write_meta()
    write_fixture("requests.json", await capture_requests())
    write_fixture("adapter_translate.json", capture_adapter())
    write_fixture("responses.json", await capture_responses())
    write_fixture("errors.json", await capture_errors())


if __name__ == "__main__":
    asyncio.run(main())
