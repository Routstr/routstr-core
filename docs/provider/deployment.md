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
| Analytics sharing | As configured | On by default; change it in Settings |
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

# Check out a release (v0.5.1 is current — see the releases page for the newest tag)
git checkout v0.5.1

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
git checkout v0.5.1   # or the reviewed release tag you are deploying

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

`compose.yml` brings up four services:

1. **ui** — builds the Next.js admin dashboard and copies the result into the
   shared `./ui_out` volume.
2. **postgres** — the node's database (PostgreSQL 16). Not published to the
   host; see [Database](#database-postgresql).
3. **routstr** — the Python node, serving the API and the dashboard built above.
4. **tor** — serves the node as a `.onion` hidden service, so no port forwarding
   is needed. See [Tor Support](tor.md) for how to read your `.onion` address.

`compose.node.yml` brings up only **ui**, **postgres** and **routstr**. There is
no Tor service, the node port is bound to `127.0.0.1`, `ENABLE_ANALYTICS_SHARING`
is enabled on first boot, and `AUTO_GENERATE_NSEC=true` creates the identity on
first boot — nothing is published until a public `HTTP_URL` (or onion endpoint)
is set.

### Database (PostgreSQL)

Both stacks keep the node's data in the bundled **postgres** service:

- The data lives in the `postgres-data` volume.
- The database password is generated on the first start and kept in the
  `postgres-secret` volume, which only the postgres and routstr containers
  mount. If `.env` sets `POSTGRES_PASSWORD`, that value is used instead, on that
  first start only.
- Nothing is published to the host; only the node reaches the database.
- A `DATABASE_URL` in `.env` always wins over the bundled database (for an
  external PostgreSQL, or to stay on SQLite).

Scripts run with `docker compose exec routstr …` use the same database as the
node.

!!! warning "Private stack: add `-f compose.node.yml`"
    The `docker compose` commands in this section and in
    [Backup and Restore](#backup-and-restore) are written for the standard stack.
    On the private stack, add `-f compose.node.yml` right after `docker compose`
    (or set `COMPOSE_FILE=compose.node.yml` in `.env`). A bare `docker compose up`
    there starts the standard stack instead, which publishes port 8000 on all
    interfaces and starts Tor.

!!! danger "Never run `docker compose down -v`"
    `-v` removes every volume of the stack: `postgres-data` and
    `postgres-secret`, which hold every balance, API key and setting, and on the
    standard stack also `tor-data`, which holds the onion keys, so the node's
    `.onion` address changes. Use plain `docker compose down` to stop the stack;
    back up with `pg_dump` (see [Backup and Restore](#backup-and-restore)). When
    a step below says to delete the database, use
    [Deleting only the database](#deleting-only-the-database).

#### Upgrading a node that runs on SQLite

Nothing to configure. Back up as described in [Updates](#updates), then update
as usual. On the first start the node copies `./keys.db` into PostgreSQL once:
balances, API keys, providers, settings and the Nostr identity come across
unchanged. Plan a minute of downtime.

Follow the log while it starts (add `-f compose.node.yml` on the private stack):

```bash
docker compose logs -f routstr
```

- `SQLite database imported into PostgreSQL`: done. `keys.db` (and its
  `-wal`/`-shm`) is renamed to `keys.db.imported-<date and time>`. Keep that
  file: it is your SQLite copy from the moment of the switch. Later starts skip
  the import.
- `No SQLite database to import; starting with a new database`: expected on a
  brand-new node. On a node that had data, stop it now
  (`docker compose stop routstr`) and see [troubleshooting](#database-troubleshooting).
- `SQLite import did not run; nothing was copied and the node will not start`:
  the lines above it give the reason. PostgreSQL keeps none of the rows and
  `keys.db` is untouched; to get back on SQLite while you look into it, see
  [Staying on SQLite](#staying-on-sqlite).

The import only happens when `.env` does not set `DATABASE_URL`. A node whose
`.env` still sets it (older `.env.example` files had
`DATABASE_URL=sqlite+aiosqlite:///keys.db` uncommented) stays on SQLite and logs
`DATABASE_URL keeps this node on SQLite instead of the bundled PostgreSQL`. To
move it, delete that line and start again. If it points to another file than
`keys.db`, also add that file's path *inside the container* as
`SQLITE_IMPORT_PATH` (the checkout is mounted at `/app`, so
`sqlite+aiosqlite:///data/keys.db` becomes `SQLITE_IMPORT_PATH=/app/data/keys.db`).
Unless `.env` sets `ROUTSTR_SECRET_KEY`, add
`ROUTSTR_SECRET_KEY_FILE=/app/data/routstr_secret.key` too: the generated master
key sits beside the old database file, and without that line the node looks for
it in `/app` and cannot decrypt its stored secrets.

Nodes that already moved to PostgreSQL with `compose.postgres.yml` (v0.5.1) need
nothing: the same `postgres-data` volume is used. Keep `POSTGRES_PASSWORD` in
`.env`: it seeds the password file on the first start after the update.
`COMPOSE_FILE` can stay or go; `-f compose.node.yml` alone is safe again.

#### Check your data

Log in to the dashboard and compare with the node before the update:

- the node's npub is the same (dashboard, or `/v1/info`),
- **Balances** shows the same total,
- your upstream providers and settings are all there.

With an API key at hand, check its balance; replace `sk-...` with the key and
`8000` with your `ROUTSTR_NODE_PORT` if you changed it:

```bash
curl -s -H "Authorization: Bearer sk-..." http://127.0.0.1:8000/v1/balance/info
```

If something doesn't match, stop the node (`docker compose stop routstr`)
before users spend on it, and [go back to SQLite](#staying-on-sqlite).

Keep the same `ROUTSTR_SECRET_KEY` (or `routstr_secret.key`) and `.wallet/`:
the encrypted secrets in PostgreSQL need that key, and the Cashu wallet stays on
local SQLite in `.wallet/`.

#### Staying on SQLite

To keep a node on SQLite, add this line to `.env` **before** updating:

```bash
echo "DATABASE_URL=sqlite+aiosqlite:///keys.db" >> .env
```

The postgres service still starts but stays unused.

To go back after an import, add the same line, then put the SQLite file back.
The imported file holds everything up to the switch; anything written to
PostgreSQL afterwards (new keys, top-ups, spending) is **not** in it, and no
tool copies PostgreSQL back into SQLite. Go back only before the node has served
users on PostgreSQL; after that, stay and restore from a `pg_dump` if needed.

```bash
docker compose stop routstr
ls -l keys.db.imported-*
archive='keys.db.imported-20261028T173919Z'   # replace with your file's name
stray=$(mktemp -d ../routstr-stray-sqlite.XXXXXX) && echo "moved aside to $stray"
for f in keys.db keys.db-wal keys.db-shm; do [ -f "$f" ] && mv "$f" "$stray/"; done
mv "$archive" keys.db
[ -f "$archive-wal" ] && mv "$archive-wal" keys.db-wal
[ -f "$archive-shm" ] && mv "$archive-shm" keys.db-shm
```

If the import **failed**, `keys.db` was never renamed; skip the file moves. Then
start the node with the command for your stack:

```bash
docker compose up -d --build                      # standard stack
docker compose -f compose.node.yml up -d --build  # private stack
```

#### Database troubleshooting

- **"keys.db changed after it was imported into PostgreSQL"**. `keys.db` was
  written after the import. First work out which database is the live one:
    - **The node went back to SQLite after moving to PostgreSQL**, for example
      it moved with `compose.postgres.yml` in v0.5.1 and later followed that
      release's "Switching back to SQLite" steps. Then `keys.db` holds every
      key, top-up and spend since, and PostgreSQL is out of date. Don't move
      `keys.db` aside. Either stay on SQLite (add
      `DATABASE_URL=sqlite+aiosqlite:///keys.db` to `.env`, see
      [Staying on SQLite](#staying-on-sqlite)), or back up `keys.db` with its
      `-wal`/`-shm` files and [delete only the database](#deleting-only-the-database),
      so the next start imports `keys.db` afresh.
    - **The node kept serving users from PostgreSQL** and a SQLite node (an
      older checkout, a `DATABASE_URL` set for a while, or a script run on the
      host) wrote to `keys.db` in between. PostgreSQL is the live database and
      those writes are not in it. Move the SQLite files aside with the `stray`
      lines above, start again, and refund or re-credit any top-up from that
      period by hand.
- **"refusing to merge into it"**. PostgreSQL already holds data that did not
  come from an import, for example after the node once started there with no
  `keys.db` to import. If that data matters, set `SQLITE_IMPORT_PATH=` (empty)
  in `.env` to keep using it. If it holds nothing you need,
  [delete only the database](#deleting-only-the-database) (**everything in
  PostgreSQL is gone**), and the next `docker compose up -d --build` imports
  `keys.db`.
- **The node started empty on a node that had data.** `keys.db` was not where
  the node looked (`/app/keys.db`). Stop the node before it takes traffic, then
  either [delete the empty database](#deleting-only-the-database) and fix the
  path (see `SQLITE_IMPORT_PATH` above), or go back to SQLite.
- **`password authentication failed`**. The password file no longer matches the
  database, e.g. the `postgres-secret` volume was removed without
  `postgres-data`. Set the database to the current file's password:

    ```bash
    docker compose exec -T postgres sh -c 'echo "ALTER ROLE CURRENT_USER PASSWORD :'"'"'pw'"'"';" | psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v pw="$(cat /run/routstr-postgres/password)"'
    docker compose restart routstr
    ```

#### Deleting only the database

This removes the `postgres-data` and `postgres-secret` volumes and nothing else
(the onion keys in `tor-data`, `.env`, `keys.db*` and `.wallet/` stay).
**Everything in PostgreSQL is gone**; take a `pg_dump` first if any of it
matters.

```bash
project=$(docker compose config | sed -n 's/^name: //p') &&
test -n "$project" &&
docker compose rm -sf routstr postgres &&
docker volume rm -f "${project}_postgres-data" "${project}_postgres-secret"
```

The next `docker compose up -d --build` creates an empty database with a new
password (or the one in `POSTGRES_PASSWORD`).

All import rules are in
[Configuration](configuration.md#postgresql-and-multiple-workers).

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

# Encrypts node secrets at rest. Optional — if unset, a key is generated in the
# checkout (routstr_secret.key) and its file is named once for backup. Set it
# explicitly to manage the key yourself.
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
`docker compose exec routstr /.venv/bin/python scripts/reveal_nsec.py` (add
`-f compose.node.yml` on the private stack). Run it inside the container: on the
host the script doesn't see the bundled PostgreSQL and creates an empty
`keys.db`, which stops the next start. Set `NSEC` only to import a
specific identity, or `AUTO_GENERATE_NSEC=false` to configure one from the
dashboard instead.

With `compose.node.yml` the identity is generated automatically too; a node stays
undiscoverable until a public `HTTP_URL` (or onion endpoint) is set. Configure
approved relays in the dashboard when you are ready to be listed.

!!! note "Secret key persistence"
    If you leave `ROUTSTR_SECRET_KEY` unset, the node generates one and stores it
    as `routstr_secret.key` **in the checkout**, so it persists across restarts —
    just include it in your backups. For stronger isolation (keeping the key off
    the host disk), set `ROUTSTR_SECRET_KEY` from a secrets manager instead.

### Analytics Sharing on the Guided Stack

`compose.node.yml` starts the node with analytics sharing on. The first boot
saves that value in the node's settings, and saved settings win over the
environment on every later boot, so editing `ENABLE_ANALYTICS_SHARING` in `.env`
or Compose has no effect afterwards. Turn it off (or back on) under **Settings → Share
Analytics** in the dashboard.

Analytics publishing does not depend on `HTTP_URL`: once enabled, a node with a
Nostr identity publishes usage snapshots to its relays even in private mode.

See [Configuration](configuration.md) for all available options.

---

## Persistence

The database lives in Docker volumes; everything else Routstr persists stays in
the directory you cloned, which is mounted into the container:

| Path | Contents |
|------|----------|
| `postgres-data` volume | PostgreSQL database (settings, API keys, balances, sessions) |
| `postgres-secret` volume | The database password |
| `keys.db.imported-*` | The SQLite database as it was when it was imported, if the node ran on SQLite before |
| `routstr_secret.key` | Auto-generated master key, written to the checkout when `ROUTSTR_SECRET_KEY` is unset (or wherever `ROUTSTR_SECRET_KEY_FILE` points) |
| `.wallet/` | Cashu wallet data (your Bitcoin!); created the first time the node handles ecash |
| `.env` | Node configuration and any seeded secrets |
| `logs/` | Node logs |

!!! warning "Back Up Your Data"
    Your cloned directory holds your wallet and your master key. Losing it means
    losing funds. Back it up regularly — and don't delete the checkout to
    "start fresh" without a `pg_dump`, `routstr_secret.key` and `.wallet/`
    first.

### Backup and Restore

- **Back up the database with `pg_dump`** (below), which is consistent while
  the node runs. On a node that stays on SQLite, stop it and copy `keys.db` with
  any `-wal`/`-shm` sidecars instead. Include
  `routstr_secret.key` (or your secret-manager key), `.wallet/`, `.env`, the
  Compose file(s) you use, and the node's Nostr identity.
- **Keep the database and its encryption key together** in an encrypted,
  off-host backup. Encrypted secrets can't be recovered without the original key.
- **Test restores on an isolated host** with no relay publishing or payout
  activity. Never run two copies of the same wallet at the same time.
- **Never delete volumes or wallet state to fix a login problem.** Reset the
  password instead (below).

Replace `routstr` with your `POSTGRES_USER`/`POSTGRES_DB` if you changed them,
and add `-f compose.node.yml` after `docker compose` on the private stack.

**Back up** (the node can keep running):

```bash
dump="routstr-$(date +%F-%H%M%S).sql"
[ ! -e "$dump" ] &&
docker compose exec -T postgres pg_dump -U routstr routstr > "$dump" &&
echo "backup OK: $dump" || echo "BACKUP FAILED: don't use $dump"
```

Every backup gets its own file name, so an earlier backup is never overwritten.
Only a run that prints `backup OK` produced a usable file. Keep the dump
together with `.env`, `routstr_secret.key` and `.wallet/`. The database password
is not needed for a restore: a restore creates a new database with a new one.

**Restore** replaces everything in PostgreSQL with the dump, so it is a separate
step. Set `dump` to the backup you want, then run the rest as one block:

```bash
dump='routstr-2026-10-28-153000.sql'   # replace with your backup's file name

test -s "$dump" &&
cp "$dump" "$dump.keep" &&
project=$(docker compose config | sed -n 's/^name: //p') &&
test -n "$project" &&
docker compose rm -sf routstr postgres &&
docker volume rm -f "${project}_postgres-data" "${project}_postgres-secret" &&
docker compose up -d --wait postgres &&
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U routstr -d routstr < "$dump" &&
docker compose up -d --build &&
echo "restore OK"
```

Each line runs only if the one before it succeeded. `test -s` stops before
anything is touched if the file is missing or empty; the next four lines delete
the current PostgreSQL data and nothing else (the onion keys in `tor-data`
stay), `--wait` holds until the new database is ready, and the
node starts only after the whole dump loaded. If you don't see `restore OK`, the
node stays stopped: fix the cause and run the block again (the spare copy
`$dump.keep` is still there). Afterwards,
[check your data](#check-your-data). The
restored database still records the original import, so the node doesn't
import `keys.db` again.

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
   `compose.yml`, firewall port `8000`). Optionally, restrict administrative
   endpoints to trusted clients at the proxy — the node's admin password is the
   required protection.
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

!!! warning "Before you change anything"
    Record the current tag (`git describe --tags`) and read the release notes for
    the version you are moving to. Then back up the database your node uses now:

    - **SQLite** (`keys.db` exists): stop the node and copy `keys.db` with its
      `-wal`/`-shm` files. Coming from SQLite, the update moves the node to
      PostgreSQL; see [Database](#database-postgresql).
    - **PostgreSQL:** run the **Back up** block in
      [Backup and Restore](#backup-and-restore). The `keys.db.imported-*` file
      from the migration does not contain anything written since.

    In both cases also keep `.env`, `routstr_secret.key` (or your
    `ROUTSTR_SECRET_KEY`) and `.wallet/` if it exists, and write down the node's
    npub, the total on **Balances** and your upstream providers so you can
    [check your data](#check-your-data) afterwards.

Then check out the new release and rebuild with the **one** command that matches
how your node runs:

```bash
git fetch --tags
git checkout v0.5.1   # or the tag you are moving to
```

- **Standard stack** (or `COMPOSE_FILE` set in `.env`):

    ```bash
    docker compose up -d --build
    ```

- **Private stack**:

    ```bash
    docker compose -f compose.node.yml up -d --build
    ```

`--build` is required: Compose reuses an existing image for a service unless you
ask it to rebuild.

Rolling back across a database migration may need the matching database and
wallet restored from backup, not just an older image. In particular, a release
from before PostgreSQL became the default starts on a new, empty SQLite
database once `keys.db` has been imported: add
`DATABASE_URL=sqlite+aiosqlite:///keys.db` to `.env` and put the
`keys.db.imported-*` file back first (see [Staying on SQLite](#staying-on-sqlite)). After an update, re-run the
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
