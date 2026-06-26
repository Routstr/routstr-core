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

If you are a node runner, start a Routstr Core instance using Docker Compose:

1. **Prepare your `.env`**:
   ```bash
   # Required: encrypts secrets at rest. The node won't start without it.
   ROUTSTR_SECRET_KEY=<generated-key>
   NAME="My AI Node"
   DESCRIPTION="Fast access to models"
   NSEC=yournsec
   RECEIVE_LN_ADDRESS=yourname@wallet.com
   ```

   Generate `ROUTSTR_SECRET_KEY` once and keep it stable — changing it makes
   previously encrypted secrets unreadable:
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

2. **Start the services**:
   ```bash
   docker compose up -d
   ```

3. **Get your admin password**:
   On first start the node generates an admin password and logs it once with the
   `/admin` URL. Read it from the logs:
   ```bash
   docker compose logs routstr | grep -i admin
   ```
   (Lost it? Reset with `python scripts/reset_admin_password.py --regenerate`.)

4. **Configure**:
   Open [http://localhost:8000/admin/](http://localhost:8000/admin/) to connect your AI providers and set pricing.

For full instructions, see the **[Provider Quick Start Guide](https://docs.routstr.com/provider/quickstart/)**.

## Development

```bash
make setup
cp .env.example .env
fastapi run routstr
```
