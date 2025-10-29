# Quick Start

Get Routstr Core up and running in minutes with Docker or local development setup.

## Prerequisites

- Docker and Docker Compose (for production)
- Python 3.11+ (for development)
- A Cashu-compatible wallet (optional for testing)

## Option 1: Docker (Recommended)

### Quick Run

The fastest way to start Routstr Core:

```bash
docker run -d \
  --name routstr-proxy \
  -p 8000:8000 \
  -e UPSTREAM_BASE_URL=https://api.openai.com/v1 \
  -e UPSTREAM_API_KEY=your-openai-api-key \
  ghcr.io/routstr/proxy:latest
```

### Docker Compose

For a full setup with Tor support:

1. Clone the repository:

```bash
git clone https://github.com/routstr/routstr-core.git
cd routstr-core
```

2. Create environment file:

```bash
cp .env.example .env
# Edit .env with your settings
```

3. Start the services:

```bash
docker compose up -d
```

This will start:

- Routstr proxy on port 8000
- Tor hidden service (optional)
- Automatic database migrations

### Verify Installation

Check that Routstr is running:

```bash
curl http://localhost:8000/v1/info
```

You should see:

```json
{
  "name": "ARoutstrNode",
  "description": "A Routstr Node",
  "version": "0.1.4",
  "npub": "",
  "mints": ["https://mint.minibits.cash/Bitcoin"],
  "models": {...}
}
```

## Option 2: Local Development

### Install Dependencies

1. Install [uv](https://github.com/astral-sh/uv) package manager:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

2. Clone and setup:

```bash
git clone https://github.com/routstr/routstr-core.git
cd routstr-core
uv sync
```

3. Configure environment:

```bash
cp .env.example .env
# Edit .env with your settings
```

### Run the Server

```bash
fastapi run routstr --host 0.0.0.0 --port 8000
```

## First API Call

### 1. Get an eCash Token

You'll need a Cashu token to pay for API calls. Options:

- Use a [Cashu wallet](https://cashu.space) to create tokens
- Get test tokens from a testnet mint
- Use the example token (for testing only)

### 2. Create an API Key

Send your eCash token to create an API key:

```bash
curl -X POST http://localhost:8000/v1/wallet/create \
  -H "Content-Type: application/json" \
  -d '{
    "cashu_token": "cashuAeyJ0b2..."
  }'
```

Response:

```json
{
  "api_key": "rUvK7...",
  "balance": 10000
}
```

### 3. Make an API Call

Use your API key like a normal OpenAI key:

```python
import openai

client = openai.OpenAI(
    api_key="rUvK7...",  # Your Routstr API key
    base_url="http://localhost:8000/v1"
)

response = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "Hello!"}]
)

print(response.choices[0].message.content)
```

## Example Client

Run the included example:

```bash
CASHU_TOKEN="your-token" python example.py
```

This demonstrates:

- Creating an API key from a token
- Making streaming chat requests
- Automatic balance deduction

## Testing the Setup

### Check Available Models

```bash
curl http://localhost:8000/v1/models
```

### View Admin Dashboard

Open <http://localhost:8000/admin/> in your browser.

Default password is set in `ADMIN_PASSWORD` environment variable.

### Monitor Logs

Docker:

```bash
docker compose logs -f routstr
```

Local:

```bash
# Logs are in ./logs/ directory
tail -f logs/routstr.log
```

## Common Issues

### Connection Refused

- Ensure the service is running: `docker ps`
- Check firewall settings
- Verify port 8000 is not in use

### Invalid API Key

- Ensure you've created an API key with sufficient balance
- Check the token was valid and had value
- Verify the mint URL is accessible

### Upstream Errors

- Check `UPSTREAM_BASE_URL` is correct
- Verify `UPSTREAM_API_KEY` if required
- Test upstream service directly

## Next Steps

- [Configuration Guide](configuration.md) - Customize settings
- [Docker Setup](docker.md) - Production deployment
- [User Guide](../user-guide/introduction.md) - Detailed usage
