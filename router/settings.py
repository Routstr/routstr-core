import os


def require_env(key: str) -> str:
    value = os.getenv(key)
    if value is None or value == "":
        raise RuntimeError(f"Missing required environment variable: {key}")
    return value


def get_env(key: str, default: str | None = None) -> str:
    return os.getenv(key, default)

# Proxy configuration
UPSTREAM_BASE_URL = require_env("UPSTREAM_BASE_URL")
UPSTREAM_API_KEY = get_env("UPSTREAM_API_KEY", "")

# Pricing and auth
COST_PER_REQUEST = int(get_env("COST_PER_REQUEST", "1")) * 1000
COST_PER_1K_INPUT_TOKENS = int(get_env("COST_PER_1K_INPUT_TOKENS", "0")) * 1000
COST_PER_1K_OUTPUT_TOKENS = int(get_env("COST_PER_1K_OUTPUT_TOKENS", "0")) * 1000
MODEL_BASED_PRICING = get_env("MODEL_BASED_PRICING", "false").lower() == "true"

# Cashu wallet
RECEIVE_LN_ADDRESS = require_env("RECEIVE_LN_ADDRESS")
MINT = get_env("MINT", "https://mint.minibits.cash/Bitcoin")
MINIMUM_PAYOUT = int(get_env("MINIMUM_PAYOUT", "100"))
REFUND_PROCESSING_INTERVAL = int(get_env("REFUND_PROCESSING_INTERVAL", "3600"))
DEVS_DONATION_RATE = float(get_env("DEVS_DONATION_RATE", "0.021"))
NSEC = require_env("NSEC")
DEV_LN_ADDRESS = get_env("DEV_LN_ADDRESS", "routstr@minibits.cash")

# Database
DATABASE_URL = get_env("DATABASE_URL", "sqlite+aiosqlite:///keys.db")

# App info
NAME = get_env("NAME", "ARoutstrNode")
DESCRIPTION = get_env("DESCRIPTION", "A Routstr Node")
NPUB = get_env("NPUB", "")
HTTP_URL = get_env("HTTP_URL", "")
ONION_URL = get_env("ONION_URL", "")
CORS_ORIGINS = get_env("CORS_ORIGINS", "*")

# Pricing service
EXCHANGE_FEE = float(get_env("EXCHANGE_FEE", "1.005"))

# Admin
ADMIN_PASSWORD = get_env("ADMIN_PASSWORD", "")

# Discovery
TOR_PROXY_URL = get_env("TOR_PROXY_URL", "socks5://127.0.0.1:9050")
