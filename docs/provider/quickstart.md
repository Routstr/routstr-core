# Quick Start

Start earning Bitcoin by selling AI access in under 5 minutes.

## What You'll Build

A **Routstr Provider Node** acts as a gateway that:

1. **Connects** to upstream AI providers (OpenAI, Anthropic, OpenRouter, etc.)
2. **Accepts** Bitcoin payments via Cashu eCash
3. **Serves** AI requests to clients on the network

You bring the API keys, Routstr handles the billing, payments, and client management.

A node that is running is not necessarily ready to sell: it needs an upstream
with enabled models, a rotated admin password and, if you want public clients, a
deliberately published HTTPS endpoint. This guide walks through each step.

!!! tip "Future: Node-to-Node Routing"
In future versions, you'll be able to run a node that connects to other Routstr nodes—eliminating the need to configure upstream providers yourself. For now, you'll need your own API credentials.

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) installed, with the Compose plugin
- API credentials from at least one AI provider (OpenAI, Anthropic, OpenRouter, etc.)
- Python 3, if you use the private first run (`scripts/node_setup.py`)
- Persistent storage for the checkout — it holds your database, key and wallet

Clone the repository first:

```bash
git clone https://github.com/Routstr/routstr-core.git
cd routstr-core
git checkout v0.4.7   # current release — see https://github.com/Routstr/routstr-core/releases/latest
```

!!! warning "Keep secrets out of chats"
    Don't paste admin passwords, API keys, wallet tokens or startup logs into an
    agent chat. If an agent is helping, point it at
    [llms.txt](https://github.com/Routstr/routstr-core/blob/main/llms.txt); it
    can't complete login, funding or public exposure without you.

---

## 1. Prepare Configuration

Create a `.env` file in the root of the project to store your secrets
(`cp .env.example .env` gives you a template with every option):

```bash
# Encrypts node secrets at rest. Optional — if unset, the node generates a key on
# first start and prints it once (back it up).
ROUTSTR_SECRET_KEY=

# Node Identity
NAME="My AI Node"
DESCRIPTION="Fast access to models"

# Lightning Payouts
RECEIVE_LN_ADDRESS=yourname@wallet.com

```

The admin password is generated and logged once on first start (read it from the
logs to sign in). Your Nostr identity (`nsec`) is handled for you: the bundled
compose stack sets `AUTO_GENERATE_NSEC=true`, so the node creates an identity on
first boot and announces itself — no dashboard step needed. The `nsec` is stored
encrypted in the database and never printed; only its `npub` is logged. Retrieve
it when you need to back it up with
`docker compose exec routstr /.venv/bin/python scripts/reveal_nsec.py` (or
`python scripts/reveal_nsec.py` from a source checkout; requires
`ROUTSTR_SECRET_KEY` or the persisted key file). (`ADMIN_PASSWORD` / `NSEC` are
still read once as a legacy seed for existing deployments, and a provided `NSEC`
always wins over auto-generation.)

If you use the private first run below, you can skip this step:
`node_setup.py` creates `.env` from `.env.example` (owner-only permissions) when
it doesn't exist, and never overwrites an existing one.

## 2. Start the Node

The recommended way to run Routstr is using Docker Compose, which handles the node, the UI, and optional services like Tor.

```bash
docker compose up -d
```

Verify it's running:

```bash
curl http://localhost:8000/v1/info
```

This standard stack (`compose.yml`) publishes port 8000 on all interfaces and
starts a Tor hidden service. On a machine with a public IP, firewall port 8000 or
use the private first run until the admin password is rotated.

### Private First Run (Optional)

To keep the node reachable only from the host while you set it up, use the
private stack instead:

```bash
python3 scripts/node_setup.py start
```

This builds and starts `compose.node.yml`, which binds only `127.0.0.1:8000`,
starts no Tor service, and keeps automatic Nostr identity generation and
analytics sharing off. It waits up to three minutes for the local API (the first
build itself can take longer) and prints the dashboard URL.

- **Port 8000 already taken?** Don't stop the other service; pick a free port
  with `python3 scripts/node_setup.py start --port 18080`, then use
  `check --port 18080`, `http://127.0.0.1:18080/admin/`, and
  `ROUTSTR_NODE_PORT=18080 docker compose -f compose.node.yml ...` afterwards.
- **Remote server?** Forward the port from your own computer, then open
  <http://127.0.0.1:8000/admin/> in your local browser:

  ```bash
  ssh -L 8000:127.0.0.1:8000 user@your-server
  ```

- **Every later Compose command** needs `-f compose.node.yml`, e.g.
  `docker compose -f compose.node.yml logs routstr`. Plain `docker compose up`
  starts the **different** public/Tor stack.

See [Deployment](deployment.md#private-first-run) for details.

### Build from Source (Optional)

If you've cloned the repository and want to build the images yourself:

```bash
docker compose build
docker compose up -d
```

---

## 3. Configure via Dashboard

Open the **Admin Dashboard** at [http://localhost:8000/admin/](http://localhost:8000/admin/).

!!! note "Login"
On first start the node generates an admin password and logs it once — read it from the container logs to sign in (`docker compose logs routstr | grep -i admin`, or `docker compose -f compose.node.yml logs routstr | grep -i admin` for the private stack). Read it yourself in a private terminal; don't capture it in automated output.

### Connect Your AI Providers

1. Navigate to **Providers** → **Add Provider**
2. Select the upstream type and enter its base URL (e.g., `https://api.openai.com/v1`)
3. Enter your API key
4. Review the provider **Fee** and save
5. Check that the provider is enabled and has enabled models

### Set Your Profit Margin

1. Each provider's **Fee** on the **Providers** page is a multiplier on upstream
   cost (default `1.01`, or `1.06` for OpenRouter)
2. Node-wide fees can also be set with `EXCHANGE_FEE` and `UPSTREAM_PROVIDER_FEE` in `.env`
3. Optionally set a fixed price for individual models on the **Model** page, or a
   flat price per request instead (`FIXED_PRICING=true` with
   `FIXED_COST_PER_REQUEST` in sats)

See [Pricing](pricing.md) for how these combine.

### Review Node Settings

In **Settings** → **Admin Settings**, review the node name, description,
Lightning payout settings, mints and relays before accepting paid traffic. A
saved payout address is not a tested payout.

### Secure the Dashboard

1. Go to **Settings** → **Admin Settings** → **Change Admin Password**
2. Set a strong password, replacing the generated one
3. Save and re-login

Lost the password? Run
`docker compose exec routstr /.venv/bin/python scripts/reset_admin_password.py --regenerate`
(add `-f compose.node.yml` for the private stack) in a private terminal and
rotate it again. Never delete volumes or wallet state to fix a login problem.

### Check Readiness

```bash
python3 scripts/node_setup.py check
```

A successful check reports a nonzero **public model count**; zero models (exit
code `2`) means the API is running but setup is incomplete. This proves
discovery only — it does not test payments, streaming, upstream requests,
refunds or payout.

---

## 4. Start Earning

Once configured, your node is live. Clients pay you in Bitcoin (via Cashu tokens) for every AI request.

### Before Going Public

If you started privately, or want public clients over HTTPS:

1. Back up `keys.db` (and any `keys.db-wal`/`keys.db-shm`), `routstr_secret.key`,
   `.wallet/` and `.env` while the node is stopped, and verify a restore
   offline — the database and its encryption key must stay together.
2. Rotate the admin password (above).
3. Put HTTPS, a firewall and a reverse proxy in front of the node, and set the
   public **HTTP URL** in **Settings** → **Admin Settings**.
4. Decide explicitly whether to enable Nostr discovery and analytics sharing.
5. From **another machine**, run
   `python3 scripts/node_setup.py check --public-url https://node.example`.

Full steps: [Deployment](deployment.md#publishing-a-node). Before calling the
node production-ready, also make a small paid non-streaming and streaming request
and reconcile the accounting — see
[Production Acceptance](deployment.md#production-acceptance).

### Monitor Your Earnings

The dashboard shows:

- **Total Wallet**: All Bitcoin held by your node
- **User Balance**: Funds belonging to active client sessions
- **Your Balance**: Your profit (`Total - User Balances`)

### Withdraw Profits

1. Go to **Balances** in the dashboard and choose **Withdraw**
2. Select the mint/currency and amount
3. Generate a Cashu token (copy or download it, and keep it safe)
4. Redeem to your Lightning wallet

---

## Next Steps

- **[Deployment](deployment.md)**: Production setup with Docker Compose and Tor, the private stack, backups and updates
- **[Dashboard Guide](dashboard.md)**: Full reference for all dashboard features
- **[Pricing](pricing.md)**: Configure pricing strategies and per-model overrides
- **[Discovery](discovery.md)**: Announce your node on Nostr for clients to find you
- **[llms.txt](https://github.com/Routstr/routstr-core/blob/main/llms.txt)**: Instructions for an agent helping you set up a node
