# Deployment

Production deployment guide for Routstr Provider nodes.

## Quick Start (Recommended)

The recommended way to run a provider node is to clone the repository at the
**latest release** and start the stack with Docker Compose. Compose builds both
the node and the admin dashboard from source, so there is no image to pull and no
dashboard build to keep in sync with the node.

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

---

## What Docker Compose Starts

`compose.yml` brings up three services:

1. **ui** — builds the Next.js admin dashboard and copies the result into the
   shared `./ui_out` volume.
2. **routstr** — the Python node, serving the API and the dashboard built above.
3. **tor** — serves the node as a `.onion` hidden service, so no port forwarding
   is needed. See [Tor Support](tor.md) for how to read your `.onion` address.

---

## Pre-Configuration (Optional)

Everything can be configured from the dashboard after first start, but you can
pre-configure a deployment by editing the `.env` file you created above:

```bash
# Upstream (optional — can also be set from the dashboard)
UPSTREAM_BASE_URL=https://api.openai.com/v1
UPSTREAM_API_KEY=sk-proj-...

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

!!! note "Secret key persistence"
    If you leave `ROUTSTR_SECRET_KEY` unset, the node generates one and stores it
    as `routstr_secret.key` **next to your database**, so it persists alongside
    your data — just include that in your backups. For stronger isolation
    (keeping the key off the data volume), set `ROUTSTR_SECRET_KEY` from a
    secrets manager instead.

See [Configuration](configuration.md) for all available options.

---

## Persistence

With the default `compose.yml` the repository directory is mounted into the
container, so everything Routstr persists stays in the directory you cloned:

| Path | Contents |
|------|----------|
| `keys.db` | SQLite database (settings, API keys, sessions) |
| `routstr_secret.key` | Auto-generated master key, written beside the database when `ROUTSTR_SECRET_KEY` is unset |
| `.wallet/` | Cashu wallet data (your Bitcoin!) |
| `logs/` | Node logs |

!!! warning "Back Up Your Data"
    Your cloned directory holds your wallet and your master key. Losing it means
    losing funds. Back it up regularly — and don't delete the checkout to
    "start fresh" without copying `keys.db`, `routstr_secret.key` and `.wallet/`
    first.

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

---

## Updates

Check out the new release and rebuild:

```bash
git fetch --tags
git checkout v0.4.7   # or the tag you are moving to
docker compose up -d --build
```

`--build` is required: Compose reuses an existing image for a service unless you
ask it to rebuild.

!!! warning "Back up first"
    Copy `keys.db`, `routstr_secret.key` and `.wallet/` before updating, and read
    the release notes for the version you are moving to.

---

## Building Without Starting

`docker compose up -d` already builds from source. To build the images
explicitly without starting them:

```bash
docker compose build
```

To build only the node image (the dashboard must already be built into
`./ui_out`):

```bash
docker build -t routstr-node .
```