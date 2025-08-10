"""
List of all environment variables used in the codebase.
This file is a reference for the settings database model.
"""

from typing import Any, NotRequired, TypedDict


class EnvironmentVariableConfig(TypedDict):
    default: Any
    description: str
    type: str
    locations: list[str]
    required: NotRequired[bool]
    sensitive: NotRequired[bool]
    choices: NotRequired[list[str]]


ENVIRONMENT_VARIABLES: dict[str, EnvironmentVariableConfig] = {
    # Database
    "DATABASE_URL": {
        "default": "sqlite+aiosqlite:///keys.db",
        "description": "Database connection URL",
        "type": "str",
        "locations": ["router/core/db.py"],
    },
    # API/Upstream
    "UPSTREAM_BASE_URL": {
        "default": "",
        "description": "Base URL for upstream API provider",
        "type": "str",
        "locations": ["router/payment/helpers.py"],
        "required": True,
    },
    "UPSTREAM_API_KEY": {
        "default": "",
        "description": "API key for upstream provider",
        "type": "str",
        "locations": ["router/payment/helpers.py"],
        "required": True,
    },
    "BASE_URL": {
        "default": "https://openrouter.ai/api/v1",
        "description": "Base URL for OpenRouter API",
        "type": "str",
        "locations": ["router/payment/models.py", "scripts/models_meta.py"],
    },
    # Pricing/Fees
    "EXCHANGE_FEE": {
        "default": "1.005",
        "description": "Exchange fee multiplier (0.5% default)",
        "type": "float",
        "locations": ["router/payment/price.py"],
    },
    "UPSTREAM_PROVIDER_FEE": {
        "default": "1.05",
        "description": "Upstream provider fee multiplier (5% default)",
        "type": "float",
        "locations": ["router/payment/price.py"],
    },
    "COST_PER_REQUEST": {
        "default": "1",
        "description": "Cost per request in sats (converted to msats)",
        "type": "int",
        "locations": ["router/payment/cost_caculation.py"],
    },
    "COST_PER_1K_INPUT_TOKENS": {
        "default": "0",
        "description": "Cost per 1K input tokens in sats (converted to msats)",
        "type": "int",
        "locations": ["router/payment/cost_caculation.py"],
    },
    "COST_PER_1K_OUTPUT_TOKENS": {
        "default": "0",
        "description": "Cost per 1K output tokens in sats (converted to msats)",
        "type": "int",
        "locations": ["router/payment/cost_caculation.py"],
    },
    "MODEL_BASED_PRICING": {
        "default": "false",
        "description": "Enable model-based pricing",
        "type": "bool",
        "locations": ["router/payment/cost_caculation.py"],
    },
    # App Info
    "NAME": {
        "default": "ARoutstrNode",
        "description": "Application name",
        "type": "str",
        "locations": ["router/core/main.py"],
    },
    "DESCRIPTION": {
        "default": "A Routstr Node",
        "description": "Application description",
        "type": "str",
        "locations": ["router/core/main.py"],
    },
    "NPUB": {
        "default": "",
        "description": "Nostr public key",
        "type": "str",
        "locations": ["router/core/main.py"],
    },
    "HTTP_URL": {
        "default": "",
        "description": "HTTP URL for the service",
        "type": "str",
        "locations": ["router/core/main.py"],
    },
    "ONION_URL": {
        "default": "",
        "description": "Onion URL for the service",
        "type": "str",
        "locations": ["router/core/main.py"],
    },
    # Security/Admin
    "ADMIN_PASSWORD": {
        "default": "",
        "description": "Admin dashboard password",
        "type": "str",
        "locations": ["router/core/admin.py"],
        "sensitive": True,
    },
    # Cashu/Mints
    "CASHU_MINTS": {
        "default": "https://mint.minibits.cash/Bitcoin",
        "description": "Comma-separated list of trusted Cashu mints",
        "type": "str",
        "locations": ["router/wallet.py", "router/core/main.py"],
    },
    # Logging
    "LOG_LEVEL": {
        "default": "INFO",
        "description": "Logging level",
        "type": "str",
        "locations": ["router/core/logging.py"],
        "choices": ["TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    },
    "ENABLE_CONSOLE_LOGGING": {
        "default": "true",
        "description": "Enable console logging",
        "type": "bool",
        "locations": ["router/core/logging.py"],
    },
    # CORS
    "CORS_ORIGINS": {
        "default": "*",
        "description": "Comma-separated list of allowed CORS origins",
        "type": "str",
        "locations": ["router/core/main.py"],
    },
    # Models
    "MODELS_PATH": {
        "default": "models.json",
        "description": "Path to models configuration file",
        "type": "str",
        "locations": ["router/payment/models.py"],
    },
    "SOURCE": {
        "default": "",
        "description": "Source filter for model fetching",
        "type": "str",
        "locations": ["router/payment/models.py", "scripts/models_meta.py"],
    },
    # Proxy
    "TOR_PROXY_URL": {
        "default": "socks5://127.0.0.1:9050",
        "description": "URL for Tor proxy",
        "type": "str",
        "locations": ["router/discovery.py"],
    },
    # Testing/Development (excluded from production settings)
    # These are handled separately in test environments
}
