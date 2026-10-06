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

## Quick Start (Docker)

Start a private node with Docker Compose, then follow the
[provider quickstart](docs/provider/quickstart.md). Agents: read [llms.txt](llms.txt)
first. Rotate the initial admin password before deliberately publishing a node.

```bash
git clone https://github.com/Routstr/routstr-core.git
cd routstr-core
python3 scripts/node_setup.py start
```

This binds the API to `127.0.0.1:8000` and disables discovery during setup.
See the [deployment guide](docs/provider/deployment.md) for production exposure
and backups.

## Development

```bash
make setup
cp .env.example .env
fastapi run routstr
```
