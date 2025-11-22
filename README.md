# Routstr Payment Proxy

Routstr is a FastAPI-based reverse proxy that sits in front of any OpenAI-compatible API. It handles pay-per-request billing using the [Cashu](https://cashu.space/) eCash protocol on Bitcoin and tracks usage in a local SQL database.

The server exposes the same endpoints as the upstream API and deducts sats from user accounts for each call. Pricing can be static or model-specific by loading `models.json` (falls back to `models.example.json`).

## How It Works

The proxy implements a seamless eCash payment flow that maintains compatibility with existing OpenAI clients while enabling Bitcoin micropayments:

```mermaid
sequenceDiagram
    participant Client
    participant Proxy as Routstr Proxy
    participant DB as Database
    participant Upstream as OpenAI API
    participant Wallet as Cashu Wallet

    Client->>Proxy: API Request + eCash Token
    Proxy->>Wallet: Validate & Redeem Token
    Wallet-->>Proxy: Token Value (sats)
    Proxy->>DB: Store/Update Balance
    Proxy->>Upstream: Forward API Request
    Upstream-->>Proxy: API Response + Usage Data
    Proxy->>DB: Deduct Actual Request Cost
    Proxy->>DB: Update Final Balance
    Proxy-->>Client: API Response
```

## Features

- **Cashu Wallet Integration** – Accept Lightning payments and redeem eCash tokens before forwarding requests
- **API Key Management** – Hashed keys stored in SQLite with balance tracking and optional expiry/refund address
- **Model-Based Pricing** – Convert USD prices in `models.json` to sats using live BTC/USD rates
- **Admin Dashboard** – Simple HTML interface at `/admin/` to view balances and API keys
- **Discovery** – Fetch available providers from Nostr relays using NIP-91 protocol
- **NIP-91 Auto-Announcement** – Automatically announce this provider to Nostr relays when NSEC is provided
- **Docker Support** – Provided `Dockerfile` and `compose.yml` for running with an optional Tor hidden service

## Getting Started

### Running the proxy using Docker

```bash
docker run -d \
--name routstr-proxy \
-p 8000:8000 \
-e UPSTREAM_BASE_URL=https://api.openai.com/v1 \
-e UPSTREAM_API_KEY=your-openai-api-key \
ghcr.io/routstr/proxy:latest
```

### Development Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) package manager (used in development)

### Installation

```bash
uv sync  # install dependencies
```

Create a `.env` file based on `.env.example` and fill in the required values:

```bash
cp .env.example .env
```

### Running Locally

```bash
fastapi run routstr --host 0.0.0.0 --port 8000
```

The service forwards requests to `UPSTREAM_BASE_URL`. Supply the upstream API key via the `UPSTREAM_API_KEY` environment variable if required.

### Docker

```bash
docker compose up --build
```

This builds the image and also starts a Tor container exposing the API as a hidden service.

## Environment Variables

The most common settings are shown below. See `.env.example` for the full list.

### Core Settings

- `UPSTREAM_BASE_URL` – URL of the OpenAI-compatible service
- `UPSTREAM_API_KEY` – API key for the upstream service (optional)
- `FIXED_PRICING` – Set to `true` to use a fixed per-request price; `false` (default) uses model pricing from `models.json`
- `ADMIN_PASSWORD` – Password for the `/admin/` dashboard
- `CASHU_MINTS` – Comma-separated list of Cashu mint URLs
- `NAME` – Name of the proxy
- `DESCRIPTION` – Description of the proxy
- `NPUB` – Nostr public key of the proxy
- `HTTP_URL` – Public-facing URL of the proxy
- `ONION_URL` – Tor hidden service URL of the proxy
- `NEXT_PUBLIC_API_URL` - UI Configuration for Next.js frontend (proxy URL, default: 'http://127.0.0.1:8000' )

## Database Migrations

The application uses Alembic for database schema management and **automatically runs migrations on startup**. This ensures your database is always up-to-date when deploying new versions.

### Automatic Migrations in Production

When the FastAPI application starts, it automatically:

1. Runs all pending database migrations
2. Updates the schema to the latest version
3. Logs the migration status

This means you don't need to manually run migrations when deploying - just restart the application and migrations will be applied automatically.

### Manual Migration Commands

For development or troubleshooting, you can use these Makefile commands:

```bash
make db-upgrade         # Apply all pending migrations
make db-downgrade       # Downgrade one migration
make db-current         # Show current migration revision
make db-history         # Show migration history
make db-migrate         # Auto-generate new migration from model changes
make db-revision        # Create empty migration file
make db-heads           # Show current migration heads
make db-clean           # Clean migration cache files
```

### Creating New Migrations

When you modify SQLModel models:

```bash
# Auto-generate a migration from model changes
make db-migrate
# Enter a descriptive message when prompted

# Review the generated migration file in migrations/versions/
# Edit if needed, then test with:
make db-upgrade
```

## Admin UI

Routstr includes a modern Next.js admin dashboard that's served directly from the Python backend as static files - no separate Node.js server required.

### Building the UI

```bash
make ui-build
```

This compiles the Next.js application into static HTML, CSS, and JavaScript files in `ui/out/`.

### Accessing the Dashboard

Once built, the UI is automatically served by the FastAPI backend:

- **Dashboard**: `http://localhost:8000/`
- **Login**: `http://localhost:8000/login`
- **Models Management**: `http://localhost:8000/model
- **Providers Management**: `http://localhost:8000/providers`
- **Settings**: `http://localhost:8000/settings`

The dashboard provides:

- Real-time wallet balance monitoring
- Model pricing configuration
- Upstream provider management
- Transaction history
- System settings

**Authentication**: Use the `ADMIN_PASSWORD` environment variable to access the dashboard.

## Withdrawing Balance

Go to the admin dashboard at `http://localhost:8000/` and login with your `ADMIN_PASSWORD` to withdraw your balance as a Cashu token.

## Example Client

`example.py` shows how to use the proxy with the official OpenAI client:

```bash
CASHU_TOKEN=<redeemable token> python example.py
```

The script sends streaming chat completions and pays for each request using the provided token.

## Running Tests

```bash
uv run pytest
```

The tests create a temporary SQLite database and mock the Cashu wallet. See `tests/README.md` for more details.

## Future Features

### Nut-24 Header Support (Coming Soon)

We're implementing support for the Cashu Nut-24 specification, which will enable per-request token exchange with automatic change handling:

```mermaid
graph TD
    A["Client Request<br/>x-cashu: token"] --> B[Proxy Validates Token]
    B --> C{Token ≥ Minimum Amount?}
    C -->|No| F[Return 402 Payment Required]
    C -->|Yes| D[Calculate Request Cost]
    D --> E[Process Request]
    E --> G[Forward to Upstream API]
    G --> H[Receive API Response]
    H --> I[Calculate Change]
    I --> J["Return Response<br/>x-cashu: change_token"]
    F --> K[End]
    J --> K
```

**Key Benefits:**

- **Per-Request Payments** – Send exact tokens for each API call
- **Automatic Change** – Receive change tokens in response headers
- **No Pre-funding** – No need to maintain account balances
- **Precise Billing** – Pay only for actual usage with msat-level precision
- **Minimum Amount Protection** – Proxy enforces minimum token value to prevent dust attacks

**Header Format:**

- **Request**: `x-cashu: <ecash_token>` – Token to spend for this request (must meet minimum amount)
- **Response**: `x-cashu: <change_token>` – Change token if payment exceeds cost

**Implementation Note:**
The proxy should implement either a dedicated endpoint to communicate minimum eCash requirements per request, or extend the existing `models.json` to include minimum token amounts per model. This allows clients to autonomously determine the appropriate token amount to send with each request.

**Compatible Clients:**

To use this feature, you'll need a client that handles both OpenAI API calls and eCash header management. The following clients provide seamless integration:

- **[routstr-chat](https://github.com/routstr/routstr-chat)** – chat app for the routstr network
- **[otrta-client](https://github.com/routstr/otrta-client)** – rust web app for the routstr network

clients automatically:

- **Handle eCash Headers** – Add `x-cashu` tokens to requests and process change tokens
- **Manage Wallets** – Maintain your Cashu wallet
- **Configure Proxy** – Set Routstr proxy endpoints
- **Top-up Balances** – Automatically request ecash when tokens run low and redeem ecash tokens

This approach eliminates the need for account management while maintaining the security and privacy benefits of eCash payments.

## License

This project is licensed under the terms of the GPLv3. See the `LICENSE` file for the full license text.
