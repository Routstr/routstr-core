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

If you are a node runner, the recommended way to start Routstr Core is to clone
the repository at the latest release and run it with Docker Compose:

1. **Clone the latest release**:
   ```bash
   git clone https://github.com/Routstr/routstr-core.git
   cd routstr-core
   git checkout v0.4.7   # current release — see https://github.com/Routstr/routstr-core/releases/latest
   ```

   Docker Compose builds the node and the admin dashboard from source, so there
   is no image to pull.

2. **Prepare your `.env`**:
   ```bash
   cp .env.example .env
   ```

   Then edit it with your details:
   ```bash
   # Optional: encrypts node secrets at rest. If unset, the node generates a key
   # on first start, writes it to routstr_secret.key, and prints it once — back
   # up that file. Set it explicitly to manage the key yourself (recommended in
   # production).
   ROUTSTR_SECRET_KEY=<generated-key>
   NAME="My AI Node"
   DESCRIPTION="Fast access to models"
   RECEIVE_LN_ADDRESS=yourname@wallet.com
   ```

   Your Nostr identity (`nsec`) is handled automatically: `compose.yml` sets
   `AUTO_GENERATE_NSEC=true`, so on first start the node creates one and stores
   it encrypted in the database. Only its `npub` is logged; retrieve the `nsec`
   later with `docker compose exec routstr /.venv/bin/python scripts/reveal_nsec.py`
   (needs `ROUTSTR_SECRET_KEY` or the persisted key file). Set
   `NSEC` in `.env` only to import a specific identity (it's read once as a
   legacy seed and always wins over auto-generation).

   If you don't set one, a key is generated and printed on first start — save it
   somewhere safe (losing it makes previously encrypted secrets unreadable). To
   supply your own, generate it once and keep it stable:
   ```bash
   uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

3. **Start the services**:
   ```bash
   docker compose up -d
   ```

   The first start builds both images (the dashboard build takes a few minutes).

4. **Get your admin password**:
   On first start the node generates an admin password and logs it once with the
   `/admin` URL. Read it from the logs:
   ```bash
   docker compose logs routstr | grep -i admin
   ```
   (Lost it? Reset with `docker compose exec routstr /.venv/bin/python scripts/reset_admin_password.py --regenerate`.)

5. **Configure**:
   Open [http://localhost:8000/admin/](http://localhost:8000/admin/) to connect your AI providers and set pricing.

For full instructions, see the **[Provider Quick Start Guide](https://docs.routstr.com/provider/quickstart/)**.

> **Port 8000 is public with this stack.** `compose.yml` publishes port 8000 on
> all interfaces and starts a Tor hidden service. On a host with a public IP,
> firewall it or use the private first run below until you've rotated the admin
> password.

### Private first run

To keep the node reachable only from the host while you set it up (agents: read
[llms.txt](llms.txt) first):

```bash
git clone https://github.com/Routstr/routstr-core.git
cd routstr-core
git checkout v0.4.7   # or the reviewed release tag you are deploying
python3 scripts/node_setup.py start
```

This creates `.env` from `.env.example` only if it doesn't exist, then starts
`compose.node.yml`: the API is bound to `127.0.0.1:8000` (`--port` picks another
port), there's no Tor service, and automatic Nostr identity and analytics sharing
stay off. Use `-f compose.node.yml` for every later Compose command. Rotate the
admin password, add a provider, then run `python3 scripts/node_setup.py check`.
See the [provider quickstart](docs/provider/quickstart.md) and the
[deployment guide](docs/provider/deployment.md) for publishing and backups.

## Development

```bash
make setup
cp .env.example .env
fastapi run routstr
```
