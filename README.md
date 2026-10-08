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

Start a provider node with the guided setup. The default assumes you already have
a subdomain with HTTPS and a reverse proxy forwarding to `127.0.0.1:8000`, so the
node publishes itself on Nostr as soon as it starts. Without a public URL yet, use
`--private` and publish later. Agents: read [llms.txt](llms.txt) first, and rotate
the bootstrap admin password before publishing.

```bash
git clone https://github.com/Routstr/routstr-core.git
cd routstr-core
# production: check out a reviewed release tag

# Phase 0: DNS + TLS + reverse proxy -> 127.0.0.1:8000, and restrict /admin

# Phase 1: start (public is the default)
python3 scripts/node_setup.py start \
  --public-url https://node.example \
  --ln-address you@wallet.com

# Phase 2: back up before configuring
#   routstr_secret.key, keys.db (+ -wal/-shm), .wallet/ and .env
docker compose -f compose.node.yml exec routstr \
  /.venv/bin/python scripts/reveal_nsec.py

# Phase 3: dashboard - rotate the admin password, add an upstream, set pricing
docker compose -f compose.node.yml logs routstr | grep -i admin

# Phase 4: verify
python3 scripts/node_setup.py check --public-url https://node.example
```

- `compose.node.yml` binds the API to `127.0.0.1` only, starts no Tor service, and
  starts with analytics off. Your reverse proxy fronts it.
- Private mode (`python3 scripts/node_setup.py start --private`) keeps the node
  loopback-only and publishes nothing until you set a public URL later.
- Port 8000 taken? Pass `--port 18080` and point the proxy at it.
- Use `docker compose -f compose.node.yml ...` for every later Compose command.
  Plain `docker compose up` selects the **different** public/Tor stack.
- `start` also mints a long-lived CLI token and writes `~/.routstr/config.json`
  (mode `0600`; an existing config for another node is left untouched) so the
  [Routstr CLI](https://github.com/routstr/routstr-cli) can operate the node
  (`routstr instruct`, `routstr providers list`, ...). The token is full node
  admin; revoke it in **Settings → CLI Tokens** when done (`--no-cli-token`
  skips creation).

See the [provider quickstart](docs/provider/quickstart.md) and the
[deployment guide](docs/provider/deployment.md) for publishing, backups and
updates.

## Development

```bash
make setup
cp .env.example .env
fastapi run routstr
```
