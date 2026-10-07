# Run a provider node

A Routstr node connects to an upstream AI provider and accepts Cashu payments for
inference.

The default onboarding assumes you already have a **subdomain with HTTPS and a
reverse proxy** (nginx, Caddy, …) forwarding to `127.0.0.1:8000`. With that in
place the node publishes its Nostr listing as soon as it starts — one command and
it is live. If you do not have a public URL yet, start in **private mode** and
publish later.

A node that is running is not necessarily selling: public mode gets you
discoverable, but you still need an upstream with enabled models and a rotated
admin password. The phases below cover the whole path.

---

## Before you start

- A host with Docker and the Compose plugin, Python 3, and enough space to build
  the dashboard. Keep the checkout on persistent storage — it holds your
  database, key and wallet.
- An upstream API key (OpenAI, Anthropic, OpenRouter, …).
- **Public mode only**: a subdomain with a valid TLS certificate and a reverse
  proxy forwarding it to `127.0.0.1:8000`, **plus a rule restricting `/admin`**
  (IP allowlist or basic auth) — the bootstrap password is live from first boot.

Clone the repository and, for production, check out a reviewed release tag:

```bash
git clone https://github.com/Routstr/routstr-core.git
cd routstr-core
# git checkout <latest release tag>
```

!!! warning "Keep secrets out of chats"
    Don't paste admin passwords, API keys, wallet tokens or startup logs into an
    agent chat. If an agent is helping, point it at
    [llms.txt](https://github.com/Routstr/routstr-core/blob/main/llms.txt); it
    cannot complete login, funding or public exposure without you.

---

## The five phases

| Phase | What you do |
|-------|-------------|
| **0** | Prepare the host: DNS, TLS, reverse proxy → `127.0.0.1:8000`, restrict `/admin` |
| **1** | Start the node (`node_setup.py start`) — it generates secrets and publishes |
| **2** | **Back up** the master key, database and wallet *before* configuring |
| **3** | Configure via the dashboard: rotate password, add upstream, pricing |
| **4** | Verify and accept: external check, then a small paid request |

---

## Phase 0 — Prepare the host

**Public mode (default).** Before starting the node:

1. Point a subdomain at the host (e.g. `node.example`).
2. Get a TLS certificate for it.
3. Run a reverse proxy on the **same host** forwarding
   `https://node.example` → `http://127.0.0.1:8000`. Only `443` should be
   publicly routed.
4. Restrict `/admin` at the proxy (allowlist or basic auth).

**Private mode (opt-out).** Nothing to prepare; administer the node over an SSH
tunnel (see Phase 1).

---

## Phase 1 — Start the node

**Public mode (default):**

```bash
python3 scripts/node_setup.py start \
  --public-url https://node.example \
  --ln-address you@wallet.com
```

`start`:

- **pre-flights** the public origin over HTTPS — DNS, TLS and the proxy must
  answer before anything is published (a proxy `502` while the node boots is
  fine). Use `--private` if this fails;
- writes `.env` from `.env.example` with owner-only (`0600`) permissions,
  setting `HTTP_URL` and `RECEIVE_LN_ADDRESS`. An existing `.env` is never
  blindly overwritten: a conflicting `HTTP_URL` is an error, and `NSEC` /
  `ONION_URL` are refused (manage those elsewhere);
- builds and starts `compose.node.yml`: the API binds **`127.0.0.1` only**, there
  is no Tor service, and analytics sharing starts off (change it later in Settings);
- **auto-generates the node's secrets** — the master key `routstr_secret.key` and
  the Nostr `nsec` (stored encrypted, never printed; only its `npub` is logged);
- waits up to three minutes for the local API, then **publishes the kind `38421`
  listing on its first announce pass** and prints the remaining phases;
- mints a long-lived **CLI token** and writes it to `~/.routstr/config.json`
  (mode `0600`; left untouched, with no token minted, if it already points at
  another node) so the [Routstr CLI](https://github.com/routstr/routstr-cli) works
  immediately. The token is full node admin — see
  [Connect the Routstr CLI](#connect-the-routstr-cli). Opt out with
  `--no-cli-token`.

**Private mode (opt-out):**

```bash
python3 scripts/node_setup.py start --private --ln-address you@wallet.com
```

Same node, but `HTTP_URL` stays unset: it is loopback-only and **publishes
nothing**. Set a public URL in the dashboard later to go live.

**Options**

| Flag | Meaning |
|------|---------|
| `--port N` | loopback port (default `8000`) |
| `--ln-address ADDR` | payout Lightning address (`user@host`, `lnurl1…`, or an `https://` LNURL-pay URL) |
| `--min-payout-sat N` | minimum payout balance in sats (default `210`) |
| `--payout-interval N` | seconds between payout checks (default `900`) |
| `--cli-config PATH` | where to write the CLI config (default `~/.routstr/config.json`) |
| `--cli-token-name NAME` | label for the generated token (default `node_setup`) |
| `--cli-token-expires-in-days N` | expire the CLI token after `N` days (default: never) |
| `--no-cli-token` | skip creating the CLI token / CLI config |

The payout address is validated at setup (a `user@host` address is resolved to
`/.well-known/lnurlp/…`), but **saved is not tested** — verify an actual payout
in Phase 4.

The payout flags take effect on the node's **first** boot. The node saves its
settings then, and saved values win over `.env` afterwards, so re-running
`start` with different payout flags only updates `.env`. Change payout settings
on a running node under **Settings** in the dashboard.

!!! tip "Payouts are optional at boot"
    A node can accept payments with no payout address; the payout loop simply
    idles. Set one whenever you are ready to withdraw.

!!! warning "Port already in use? Don't stop the other service"
    Pick a free port instead and point the proxy at it:

    ```bash
    python3 scripts/node_setup.py start --public-url https://node.example --port 18080
    ```

!!! note "Every later Compose command uses the private stack"
    Use `docker compose -f compose.node.yml …` (e.g. `logs routstr`). Plain
    `docker compose up` starts the **different** public/Tor stack.

### Remote host

Forward the loopback port from your own computer and open the dashboard there:

```bash
ssh -L 8000:127.0.0.1:8000 user@your-server
# then browse http://127.0.0.1:8000/admin/
```

---

## Phase 2 — Back up before configuring

The master key and identity exist the moment the node boots, so capture them
**before** configuring anything. Stop nothing else, but ensure writers are
quiesced for a consistent copy, and copy:

- **`routstr_secret.key`** — encrypts node secrets at rest, including the `nsec`
  in `keys.db`. Lose it and the stored `nsec` cannot be decrypted;
- **`keys.db`** (plus any `keys.db-wal` / `keys.db-shm` sidecars);
- **`.wallet/`** — your Bitcoin;
- **`.env`** — node configuration.

Reveal the `nsec` to store beside the key:

```bash
docker compose -f compose.node.yml exec routstr \
  /.venv/bin/python scripts/reveal_nsec.py
```

Keep the database and its encryption key **together** in an encrypted off-host
backup, and verify an isolated restore. See
[Deployment → Backup and Restore](deployment.md#backup-and-restore).

---

## Phase 3 — Configure via the dashboard

Open the dashboard at `https://node.example/admin/` (public) or
`http://127.0.0.1:8000/admin/` (private/tunnel), and read the one-time bootstrap
password in a private terminal:

```bash
docker compose -f compose.node.yml logs routstr | grep -i admin
```

Don't capture those logs in automated output.

1. **Rotate the password** — **Settings → Admin Settings → Change Admin
   Password**.
2. **Add an upstream** — **Providers → Add Provider**: choose the type, enter the
   base URL and API key, review the **Fee**, and save. Confirm the provider is
   enabled and has enabled models. With public mode you are already listed; a
   zero-model catalog self-heals as soon as models are discovered.
3. **Pricing** — per-provider **Fee** (multiplier on upstream cost), or per-model
   prices, or a flat `FIXED_PRICING` rate. See [Pricing](pricing.md).
4. **Review** node name, payout address, mints and relays in
   **Settings → Admin Settings**.

Lost the password? Regenerate it in a private terminal and rotate it again:

```bash
docker compose -f compose.node.yml exec routstr \
  /.venv/bin/python scripts/reset_admin_password.py --regenerate
```

Never delete volumes or wallet state to fix a login problem.

### Connect the Routstr CLI

`start` already minted a long-lived token and wrote it to
`~/.routstr/config.json`, so the [Routstr CLI](https://github.com/routstr/routstr-cli)
works with no further setup:

```bash
git clone https://github.com/routstr/routstr-cli.git
cd routstr-cli
bun install

routstr instruct        # canonical agent guide for this node
routstr status
routstr providers list
routstr providers add openrouter --api-key sk-or-... --base-url https://openrouter.ai/api/v1
```

If the CLI runs on a **different machine** than the node, copy the token and
configure it there instead:

```bash
routstr init --node-url https://node.example --token <token>
```

The token is full node admin. Revoke it in **Settings → CLI Tokens** (or
`DELETE /admin/api/cli-tokens/{id}`) when you are done; give agents their own
named, expiring token rather than sharing yours. To mint another one directly:

```bash
docker compose -f compose.node.yml exec routstr \
  /.venv/bin/python scripts/create_cli_token.py --name agent
```

---

## Phase 4 — Verify and accept

From **another machine** with a checkout (public mode):

```bash
python3 scripts/node_setup.py check --public-url https://node.example
```

It reports the node name and public model count and exits `0` when reachable with
≥1 model, or `2` when the API is up but **not configured** (zero models). A
passing check proves **discovery only** — it does not test payments, streaming,
upstream requests, refunds or payout.

Before calling the node production-ready, make a small, budget-approved
Cashu-paid request — **non-streaming and streaming** — reconcile usage/upstream
charges/refunds, and **test a payout separately**. See
[Production Acceptance](deployment.md#production-acceptance).

---

## Monitoring and withdrawals

The dashboard shows:

- **Total Wallet**: all Bitcoin held by the node
- **User Balance**: funds belonging to active client sessions
- **Your Balance**: your profit (`Total - User Balances`)

Withdraw from **Balances → Withdraw**: select the mint/currency and amount,
generate a Cashu token, and redeem it to your Lightning wallet.

---

## Next Steps

- **[Deployment](deployment.md)**: public/private stacks, publishing, backups and updates
- **[Dashboard Guide](dashboard.md)**: full reference for all dashboard features
- **[Pricing](pricing.md)**: configure pricing strategies and per-model overrides
- **[Discovery](discovery.md)**: how the node announces itself on Nostr
- **[llms.txt](https://github.com/Routstr/routstr-core/blob/main/llms.txt)**: instructions for an agent helping you set up a node
