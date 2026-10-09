# Deployment

Production deployment guide for Routstr Provider nodes.

Routstr ships two Compose stacks. Pick one and use it for **every** Compose
command on that node — they are separate stacks, so switching mid-setup starts a
different configuration against the same data.

| | `compose.yml` (standard) | `compose.node.yml` (guided, loopback) |
|---|---|---|
| Port binding | `8000` on **all interfaces** | `127.0.0.1` only (`ROUTSTR_NODE_PORT`, default `8000`) |
| Tor hidden service | Yes | No |
| Nostr identity | Auto-generated (`AUTO_GENERATE_NSEC=true`) | Auto-generated (`AUTO_GENERATE_NSEC=true`); nothing is published until a public `HTTP_URL`/onion endpoint is set |
| Analytics sharing | As configured | Off on first boot; change it in Settings |
| Compose command | `docker compose …` | `docker compose -f compose.node.yml …` |

`compose.node.yml` sits behind a reverse proxy and is **public by default**: pass
a public URL to the setup script and the node publishes itself on first boot. Use
`--private` instead when you want nothing reachable from outside the host until
the admin password is rotated and an upstream is configured — for example on a
public VPS, or when an agent is doing the setup (agents: read
[llms.txt](https://github.com/Routstr/routstr-core/blob/main/llms.txt) first).
Running bare `docker compose up` always selects the standard stack.

## Quick Start (Standard Stack)

For a new node the [guided first run](#guided-first-run) is the recommended path:
it sets up the loopback stack behind your reverse proxy and publishes on first
boot. The standard stack below is an alternative that bundles a Tor hidden
service and can be started with plain `docker compose up`.

The standard stack clones the repository at the **latest release** and starts it
with Docker Compose. Compose builds both the node and the admin dashboard from
source, so there is no image to pull and no dashboard build to keep in sync with
the node.

```bash
git clone https://github.com/Routstr/routstr-core.git
cd routstr-core

# Check out a release (v0.4.7 is current — see the releases page for the newest tag)
git checkout v0.4.7

# Compose reads its configuration from .env
cp .env.example .env

docker compose up -d
```

Then open your node:
- **API & Admin Dashboard**: <http://localhost:8000>
- **Admin login**: the password is generated and logged once on first start

```bash
docker compose logs routstr | grep -i admin
```

!!! note "The first start takes a few minutes"
    `docker compose up` builds both images locally, and the Next.js dashboard
    build is the slow part. Later starts reuse the built images.

!!! tip "Always tracking the newest release"
    To check out whatever `releases/latest` currently points at, use:

    ```bash
    git clone https://github.com/Routstr/routstr-core.git
    cd routstr-core
    git checkout "$(curl -sSL -o /dev/null -w '%{url_effective}' \
      https://github.com/Routstr/routstr-core/releases/latest | sed 's|.*/tag/||')"
    ```

    Omitting the `git checkout` entirely leaves you on `main` — newer, but not a
    tested release.

!!! warning "Port 8000 is public with the standard stack"
    `compose.yml` publishes port 8000 on every interface. On a host with a public
    IP, firewall it (or use the [guided first run](#guided-first-run)) until you
    have rotated the admin password.

---

## Guided First Run

`scripts/node_setup.py` starts `compose.node.yml` and checks that it came up. It
needs Docker with the Compose plugin and Python 3 on the host. The setup is
**public by default** and expects DNS, TLS and a reverse proxy to already forward
an HTTPS origin to `127.0.0.1:8000`; `--private` opts out.

```bash
git clone https://github.com/Routstr/routstr-core.git
cd routstr-core
git checkout v0.4.7   # or the reviewed release tag you are deploying

# Public (default): pre-flights DNS/TLS/proxy, then publishes on first boot
python3 scripts/node_setup.py start \
  --public-url https://node.example \
  --ln-address you@wallet.com

# Private: loopback only, nothing published until a public URL is set later
python3 scripts/node_setup.py start --private
```

`start`:

- **pre-flights** the public origin over HTTPS before starting, so the node never
  advertises a dead endpoint;
- creates `.env` from `.env.example` with owner-only (`0600`) permissions when it
  does not exist, and only writes the keys it manages (`HTTP_URL`,
  `RECEIVE_LN_ADDRESS`, `MIN_PAYOUT_SAT`, `PAYOUT_INTERVAL_SECONDS`). The
  template's upstream pair is left commented, so a new node has no upstream until
  one is added in the dashboard. A conflicting `HTTP_URL` is an error and
  `NSEC`/`ONION_URL` are refused, so an operator edit is never silently clobbered.
  Review it and never commit it;
- validates `--ln-address` (a `user@host` address is resolved to its LNURL-pay
  endpoint) — a saved address is still not a *tested* payout;
- builds and starts `compose.node.yml`, generates the master key and Nostr
  identity, then waits up to three minutes for `/v1/info` and `/v1/models` on the
  loopback port (the first build itself can take longer). Public mode publishes
  the listing; private mode publishes nothing.
- mints a long-lived **CLI token** and writes `~/.routstr/config.json` (0600;
  left untouched if it already points at another node) so
  the [Routstr CLI](https://github.com/routstr/routstr-cli) can operate the node;
  `--no-cli-token` skips it. The token is full node admin — revoke it in
  **Settings → CLI Tokens** when you are done.

If port 8000 already belongs to another service, leave that service alone and
pick a free port:

```bash
python3 scripts/node_setup.py start --private --port 18080
python3 scripts/node_setup.py check --port 18080
ROUTSTR_NODE_PORT=18080 docker compose -f compose.node.yml logs routstr
```

The dashboard is then at `http://127.0.0.1:18080/admin`. In public mode, point
the reverse proxy at that port instead of `8000`.

### Administer a Remote Host over SSH

The private stack is only reachable from the host itself. From your own computer,
forward the port and open <http://127.0.0.1:8000/admin> in your local browser:

```bash
ssh -L 8000:127.0.0.1:8000 user@your-server
```

### Rotate the Bootstrap Password

The admin password is generated and printed once to the container's stdout. Read
it yourself in a private terminal, sign in, and change it straight away in
**Settings → Admin Settings → Change Admin Password**:

```bash
docker compose -f compose.node.yml logs routstr | grep -i admin
```

Don't relay those logs through an agent or paste them into a chat, and don't
publish a node that still uses its bootstrap password.

### Check Readiness

After adding an upstream under **Providers** (see
[Phase 3 of the Quick Start](quickstart.md)):

```bash
python3 scripts/node_setup.py check
```

It prints the node name and public model count, and exits `2` when there are zero
models — the API is up but setup is incomplete. A passing check proves discovery
only: it does not test payments, streaming, upstream requests, refunds or payout.
See [Production Acceptance](#production-acceptance).

---

## What Docker Compose Starts

`compose.yml` brings up three services:

1. **ui** — builds the Next.js admin dashboard and copies the result into the
   shared `./ui_out` volume.
2. **routstr** — the Python node, serving the API and the dashboard built above.
3. **tor** — serves the node as a `.onion` hidden service, so no port forwarding
   is needed. See [Tor Support](tor.md) for how to read your `.onion` address.

`compose.node.yml` brings up only **ui** and **routstr**. There is no Tor
service, the node port is bound to `127.0.0.1`, `ENABLE_ANALYTICS_SHARING` is
off on first boot, and `AUTO_GENERATE_NSEC=true` creates the identity on first boot —
nothing is published until a public `HTTP_URL` (or onion endpoint) is set.

---

## Pre-Configuration (Optional)

Everything can be configured from the dashboard after first start, but you can
pre-configure a deployment by editing the `.env` file you created above:

```bash
# Upstream (optional — prefer the dashboard, where providers are managed).
# Uncommenting this pair seeds one enabled "custom" provider on the node's first
# boot; the values are saved in Settings afterwards, so dashboard changes and
# later `.env` edits win over it.
# UPSTREAM_BASE_URL=https://api.openai.com/v1
# UPSTREAM_API_KEY=sk-proj-...

# Encrypts node secrets at rest. Optional — if unset, a key is generated next to
# your database (on the same volume) and its file is named once for backup. Set
# it explicitly to manage the key yourself.
ROUTSTR_SECRET_KEY=

# Node identity
NAME=My Provider Node
DESCRIPTION=Fast GPT-4 access via Lightning

# Lightning withdrawals
RECEIVE_LN_ADDRESS=me@walletofsatoshi.com
```

The admin password is generated and logged once on first start; set
`ADMIN_PASSWORD` only as a legacy seed for an existing deployment.

The Nostr identity is also automatic: `compose.yml` sets
`AUTO_GENERATE_NSEC=true`, so the node creates an `nsec` on first boot and stores
it encrypted. Only its `npub` is logged; retrieve the `nsec` with
`docker compose exec routstr /.venv/bin/python scripts/reveal_nsec.py` (or
`python scripts/reveal_nsec.py` from a source checkout; it requires
`ROUTSTR_SECRET_KEY` or the persisted key file). Set `NSEC` only to import a
specific identity, or `AUTO_GENERATE_NSEC=false` to configure one from the
dashboard instead.

With `compose.node.yml` the identity is generated automatically too; a node stays
undiscoverable until a public `HTTP_URL` (or onion endpoint) is set. Configure
approved relays in the dashboard when you are ready to be listed.

!!! note "Secret key persistence"
    If you leave `ROUTSTR_SECRET_KEY` unset, the node generates one and stores it
    as `routstr_secret.key` **next to your database**, so it persists alongside
    your data — just include that in your backups. For stronger isolation
    (keeping the key off the data volume), set `ROUTSTR_SECRET_KEY` from a
    secrets manager instead.

### Analytics Sharing on the Guided Stack

`compose.node.yml` starts the node with analytics sharing off. The first boot
saves that value in the node's settings, and saved settings win over the
environment on every later boot, so editing `ENABLE_ANALYTICS_SHARING` in `.env`
or Compose has no effect afterwards. Turn it on or off under **Settings → Share
Analytics** in the dashboard, and only with the operator's consent.

Analytics publishing does not depend on `HTTP_URL`: once enabled, a node with a
Nostr identity publishes usage snapshots to its relays even in private mode.

See [Configuration](configuration.md) for all available options.

---

## Persistence

With either Compose file the repository directory is mounted into the container,
so everything Routstr persists stays in the directory you cloned:

| Path | Contents |
|------|----------|
| `keys.db` | SQLite database (settings, API keys, sessions) |
| `keys.db-wal`, `keys.db-shm` | SQLite sidecar files, when present — part of the database |
| `routstr_secret.key` | Auto-generated master key, written beside the database when `ROUTSTR_SECRET_KEY` is unset (or wherever `ROUTSTR_SECRET_KEY_FILE` points) |
| `.wallet/` | Cashu wallet data (your Bitcoin!); created the first time the node handles ecash |
| `.env` | Node configuration and any seeded secrets |
| `logs/` | Node logs |

!!! warning "Back Up Your Data"
    Your cloned directory holds your wallet and your master key. Losing it means
    losing funds. Back it up regularly — and don't delete the checkout to
    "start fresh" without copying `keys.db`, `routstr_secret.key` and `.wallet/`
    first.

### Backup and Restore

- **Back up with all writers stopped** (`docker compose stop`, or
  `docker compose -f compose.node.yml stop`), or take a consistent backup of an
  external database. Include `keys.db` with any `-wal`/`-shm` sidecars,
  `routstr_secret.key` (or your secret-manager key), `.wallet/`, `.env`, the
  Compose file(s) you use, and the node's Nostr identity.
- **Keep the database and its encryption key together** in an encrypted,
  off-host backup. Encrypted secrets can't be recovered without the original key.
- **Test restores on an isolated host** with no relay publishing or payout
  activity. Never run two copies of the same wallet at the same time.
- **Never delete volumes or wallet state to fix a login problem.** Reset the
  password instead (below).

### Admin Password Recovery

Lost the admin password? Clear it from a private terminal and restart the node;
the restart generates and logs a new one-time password (the API is briefly
unavailable). Then rotate it again in **Settings → Admin Settings**:

```bash
docker compose exec routstr /.venv/bin/python scripts/reset_admin_password.py --regenerate
docker compose restart routstr
# private stack:
docker compose -f compose.node.yml exec routstr /.venv/bin/python scripts/reset_admin_password.py --regenerate
docker compose -f compose.node.yml restart routstr
```

---

## Reverse Proxy (Optional)

For custom domains and SSL, use a reverse proxy like Caddy or nginx.

### Caddy Example

```
api.yournode.com {
    reverse_proxy localhost:8000
}
```

### nginx Example

```nginx
server {
    listen 443 ssl;
    server_name api.yournode.com;
    
    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;
    
    location / {
        proxy_pass http://localhost:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

If you started with `--port`, proxy to that port instead of `8000`.

### Publishing a Node

Before you expose a node, promote it deliberately. In **public mode**
(`node_setup.py start --public-url …`) the endpoint and listing already exist, so
only steps 1–2 remain; when you started in **private mode** (or with
`compose.yml`), work through all of them:

1. Rotate the bootstrap admin password.
2. Configure an upstream with enabled models, and confirm with
   `python3 scripts/node_setup.py check`.
3. Choose a public HTTPS domain, set up DNS and certificates, firewall the host,
   and run the reverse proxy **on the same host**. Only `443` should be publicly
   routed — keep the node port on loopback (`compose.node.yml` does this; with
   `compose.yml`, firewall port `8000`). Restrict administrative endpoints to
   trusted clients at the proxy where you can.
4. Decide explicitly whether the node should be discoverable on Nostr (see
   [Discovery](discovery.md)) and whether to share analytics. Setting the URL in
   the next step publishes the listing immediately.
5. In **Settings → Admin Settings**, set **HTTP URL** to the real public HTTPS
   origin — the node publishes its listing as soon as it is set.

From **another machine** with a checkout of the repository, verify the public
endpoint (normal hostname and certificate checks apply):

```bash
python3 scripts/node_setup.py check --public-url https://api.yournode.com
```

Confirm it reports the expected node name and a nonzero model count.

### Production Acceptance

`/v1/info` and `/v1/models` are not a health or paid-inference test. Before
calling a node production-ready:

- make a small, budget-approved Cashu-paid completion request, both
  non-streaming and streaming, using a funded client credential (not an admin
  token);
- reconcile usage, upstream charges and refunds;
- test payout separately;
- verify a backup restore as described above.

---

## Updates

Check out the new release and rebuild:

```bash
git fetch --tags
git checkout v0.4.7   # or the tag you are moving to
docker compose up -d --build
# private stack:
docker compose -f compose.node.yml up -d --build
```

`--build` is required: Compose reuses an existing image for a service unless you
ask it to rebuild.

!!! warning "Back up first"
    Copy `keys.db`, `routstr_secret.key` and `.wallet/` before updating, and read
    the release notes for the version you are moving to.

Before updating, also record the current tag and image so you can return to them.
Rolling back across a database migration may need the matching database and
wallet restored from backup, not just an older image. After an update, re-run the
local and public `node_setup.py check` and your approved paid-inference checks.

---

## Building Without Starting

`docker compose up -d` already builds from source. To build the images
explicitly without starting them:

```bash
docker compose build
# private stack:
docker compose -f compose.node.yml build
```

To build only the node image (the dashboard must already be built into
`./ui_out`):

```bash
docker build -t routstr-node .
```
