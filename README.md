# Routstr Payment Proxy

[![License](https://img.shields.io/github/license/routstr/routstr-core?style=flat-square)](LICENSE)
[![Stars](https://img.shields.io/github/stars/routstr/routstr-core?style=flat-square)](https://github.com/routstr/routstr-core/stargazers)
[![Issues](https://img.shields.io/github/issues/routstr/routstr-core?style=flat-square)](https://github.com/routstr/routstr-core/issues)
[![Release](https://img.shields.io/github/v/release/routstr/routstr-core?style=flat-square)](https://github.com/routstr/routstr-core/releases)

Routstr is a decentralized protocol for permissionless, private, and censorship-resistant AI inference. It combines Nostr for discovery and Cashu for private Bitcoin micropayments.

This repo contains Routstr Core: a FastAPI-based reverse proxy that sits in front of OpenAI-compatible APIs and handles pay-per-request billing.

## Start Here

- **Overview**: <https://docs.routstr.com/overview/>
- **Provider Guide**: <https://docs.routstr.com/provider/quickstart/>
- **User Guide**: <https://docs.routstr.com/user-guide/introduction/>

## Basic Usage

If you are a user/developer, you just point an OpenAI-compatible SDK at a Routstr node and pay with a Cashu token.

### OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://api.routstr.com/v1",
    api_key="cashuBo2FteCJodHRwczovL21...",
)

response = client.chat.completions.create(
    model="gpt-5-nano",
    messages=[{"role": "user", "content": "hello"}],
)

print(response.choices[0].message.content)
```

### cURL

```bash
curl https://api.routstr.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "x-cashu: cashuBo2FteCJodHRwczovL21..." \
  -d '{
    "model": "gpt-5-nano",
    "messages": [{"role": "user", "content": "hello"}]
  }'
```

## Quick Start (Run a Node)

Start earning Bitcoin by selling AI access in under 5 minutes.

### 1. Prepare Configuration

Create a `.env` file:

```bash
# Initial Admin Password
ADMIN_PASSWORD=mysecretpassword

# Node Identity
NAME="My AI Node"
DESCRIPTION="Fast access to models"

# Lightning Payouts
RECEIVE_LN_ADDRESS=yourname@wallet.com
```

### 2. Start the Node

```bash
docker run -d \
  --name routstr \
  -p 8000:8000 \
  --env-file .env \
  -v routstr-data:/app/data \
  ghcr.io/routstr/proxy:latest
```

Verify it's running:

```bash
curl http://localhost:8000/v1/info
```

### 3. Configure via Dashboard

1. Open **Admin Dashboard** at http://localhost:8000/admin/
2. Login with your `ADMIN_PASSWORD`
3. Go to **Settings** → **Upstream** - Add your AI provider (OpenAI, Anthropic, OpenRouter, etc.)
4. Go to **Settings** → **Pricing** - Set your profit margin (default 10%)
5. Go to **Settings** → **Admin** - Set a strong password

### 4. Start Earning

Clients pay you in Bitcoin (via Cashu) for every AI request. Monitor earnings and withdraw profits from the dashboard.

## Development

```bash
make setup
cp .env.example .env
fastapi run routstr
```
