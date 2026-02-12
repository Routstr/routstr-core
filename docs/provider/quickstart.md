# Quick Start

Start earning Bitcoin by selling AI access in under 5 minutes.

## What You'll Build

A **Routstr Provider Node** acts as a gateway that:

1. **Connects** to upstream AI providers (OpenAI, Anthropic, OpenRouter, etc.)
2. **Accepts** Bitcoin payments via Cashu eCash
3. **Serves** AI requests to clients on the network

You bring the API keys, Routstr handles the billing, payments, and client management.

!!! tip "Future: Node-to-Node Routing"
    In future versions, you'll be able to run a node that connects to other Routstr nodes—eliminating the need to configure upstream providers yourself. For now, you'll need your own API credentials.

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) installed
- API credentials from at least one AI provider (OpenAI, Anthropic, OpenRouter, etc.)

---

## 1. Start the Node

You can run the pre-built image directly:

```bash
docker run -d \
  --name routstr \
  -p 8000:8000 \
  -v routstr-data:/app/data \
  ghcr.io/routstr/proxy:latest
```

### Build from Source (Recommended)
If you want to build the node and UI yourself from source, use the unified Dockerfile:

```bash
git clone https://github.com/routstr/routstr-core.git
cd routstr-core
docker build -f Dockerfile.full -t routstr-local .
docker run -d -p 8000:8000 --name routstr routstr-local
```

Verify it's running:

```bash
curl http://localhost:8000/v1/info
```

---

## 2. Configure via Dashboard

Open the **Admin Dashboard** at [http://localhost:8000/admin/](http://localhost:8000/admin/).

!!! note "Default Access"
    The dashboard has no password by default. Set one immediately in Settings for production use.

### Connect Your AI Providers

1. Navigate to **Settings** → **Upstream**
2. Enter your upstream URL (e.g., `https://api.openai.com/v1`)
3. Enter your API key
4. Save

### Set Your Profit Margin

1. Go to **Settings** → **Pricing**
2. Configure your markup (default is 10%)
3. Optionally set a fixed price per request instead

### Secure the Dashboard

1. Go to **Settings** → **Admin**
2. Set a strong password
3. Save and re-login

---

## 3. Start Earning

Once configured, your node is live. Clients pay you in Bitcoin (via Cashu tokens) for every AI request.

### Monitor Your Earnings

The dashboard shows:

- **Total Wallet**: All Bitcoin held by your node
- **User Balances**: Funds belonging to active client sessions
- **Your Balance**: Your profit (`Total - User Balances`)

### Withdraw Profits

1. Go to **Withdraw** in the dashboard
2. Select amount and mint
3. Generate a Cashu token
4. Redeem to your Lightning wallet

---

## Next Steps

- **[Deployment](deployment.md)**: Production setup with Docker Compose and Tor
- **[Dashboard Guide](dashboard.md)**: Full reference for all dashboard features
- **[Pricing](pricing.md)**: Configure pricing strategies and per-model overrides
- **[Discovery](discovery.md)**: Announce your node on Nostr for clients to find you
