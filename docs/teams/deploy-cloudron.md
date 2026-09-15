# Deploy on Cloudron

[Cloudron](https://www.cloudron.io/) is the supported deployment target for a team node. The packaged image already contains **both** processes — the `routstrd` daemon and the `routstrd-auth` proxy — supervised inside a single container, with `/app/data` handled as persistent storage and TLS terminated by the platform.

| | |
|---|---|
| **App ID** | `io.routstr.routstrd-auth` |
| **Public port** | `8008` (Cloudron proxies it over HTTPS on 443) |
| **Health check** | `GET /health` |
| **Memory limit** | 512 MB |
| **Minimum box version** | Cloudron 9.1.0 |

---

## Prerequisites

- A running Cloudron box with a domain that can get a certificate.
- The [`cloudron` CLI](https://docs.cloudron.io/cli/) installed and logged in, **only if** you are building the image yourself:

```bash
npm install -g cloudron
cloudron login my.example.com
```

## Install

### Option A — from the published version list

The app is published as a custom Cloudron app with a version list (`CloudronVersions.json`), currently at `0.1.26`. Once that app store entry is registered on your Cloudron instance, install it from the dashboard, or:

```bash
cloudron install --appstore-id io.routstr.routstrd-auth --location routstr.example.com
```

### Option B — build the image yourself

Use this when you want to run a local modification:

```bash
git clone https://github.com/routstr/routstrd-remote
cd routstrd-remote

cloudron build          # builds the Dockerfile and pushes it to your registry
cloudron install --image <registry>/routstrd-remote:<tag> --location routstr.example.com
```

!!! note "The Dockerfile is the Cloudron image"
    The repository's `Dockerfile` is built `FROM cloudron/base:5.0.0` and its `CMD` is `cloudron/start.sh`, which prepares `/app/data` and starts `supervisord`. It expects Cloudron's filesystem conventions and should not be confused with a generic Docker image. See [Deploy with Docker](deploy-docker.md) for what that means in practice.

---

## Bootstrap the first admin

The moment the app is healthy, the npub table is **empty**, and nothing except the public endpoints can be reached. Claim it before anything else — while the table is empty, `POST /npubs` is accepted without authentication, so this is the only window in which an unauthenticated registration succeeds.

On the machine of whoever will be the first admin:

```bash
bun i -g routstrd
routstrd remote https://routstr.example.com
routstrd npubs register --name "Alice"
```

`routstrd remote` generates a fresh Nostr identity if you do not have one, stores it in `~/.routstrd/config.json`, and prints your npub. `routstrd npubs register` then posts that npub and, because no npubs exist yet, receives `admin`.

!!! warning "Register immediately after install"
    Until the first admin registers, anyone who knows the URL can claim the node. Do this as part of the install, not later.

Verify:

```bash
routstrd npubs list
```

---

## Configuration

Cloudron defaults are set by `cloudron/start.sh` and the two supervisor programs. Everything below can be overridden through the Cloudron **Environment Variables** tab.

| Variable | Default | Purpose |
|---|---|---|
| `ROUTSTRD_AUTH_PORT` | `8008` | Public port served by the auth proxy. Must match the manifest's `httpPort`. |
| `ROUTSTRD_AUTH_HOST` | `0.0.0.0` | Bind address of the auth proxy. |
| `ROUTSTRD_UPSTREAM` | `http://localhost:8009` | Where the daemon listens. Keep this on loopback. |
| `ROUTSTRD_PORT` | `8009` | Port the daemon binds. |
| `ROUTSTRD_DIR` | `/app/data/routstrd` | Config directory shared by the daemon and the proxy. |
| `ROUTSTRD_DB_PATH` | `/app/data/routstrd/routstr.db` | Shared SQLite database. |
| `ROUTSTRD_CONFIG_FILE` | `$ROUTSTRD_DIR/config.json` | Daemon config file. |
| `ROUTSTRD_AUTH_MODEL_ALLOWLIST` | `false` | Set to `true` to restrict the team to the Routstr 21 model list. See [Usage and Model Policy](usage-and-policy.md). |
| `ROUTSTRD_AUTH_ADMIN_NPUBS` | *(unset)* | Optional bootstrap admins. See below. |

### Bootstrapping admins from the environment

Instead of the interactive `npubs register` step you can seed admins declaratively. Three variables are accepted and merged: `ROUTSTRD_AUTH_ADMIN_NPUBS`, `ROUTSTRD_AUTH_ADMIN_PUBKEYS`, and `ROUTSTRD_AUTH_BOOTSTRAP_NPUB`. Values are comma- or whitespace-separated and may be either `npub1...` or 64-character hex.

Rows created this way are tagged `source = 'env'`. At every startup the proxy **reconciles** them: an env-sourced row whose pubkey is no longer present in the environment is **deleted**. This means the environment variables are the source of truth for those rows — removing someone from the variable revokes their access on the next restart.

!!! tip "Prefer `npubs register` for the first admin"
    There is deliberately **no** hardcoded default admin npub in the image. An image with a baked-in admin pubkey would hand control of every deployment to the same key.

### Filesystem layout

| Path | Lifetime | Contents |
|---|---|---|
| `/app/code` | replaced on update | Auth proxy source and the `start.sh` / `run-auth.sh` scripts. |
| `/app/data` | persistent, backed up | `routstrd/config.json`, `routstrd/routstr.db`, `logs/`, and a `.initialized` marker. |
| `/run` | ephemeral | `supervisord` socket and pid file. |

The startup script writes `authUrl` into `routstrd/config.json` pointing at the local proxy, and generates the container's Nostr identity (`nsec`) on first boot if one is missing. That generated identity is what authorises the daemon's own calls back through the proxy.

---

## Backups

Cloudron's `localstorage` addon makes `/app/data` persistent and includes it in regular backups. The entire data directory is covered, which means the SQLite database at `/app/data/routstrd/routstr.db` travels with it.

To restore, restore the app from a Cloudron backup — the wallet configuration, npub table, and client records all come back together. Do not hand-copy files between hosts: the daemon's `config.json` holds the container's `nsec`, and losing it breaks the node's ability to authenticate to its own proxy.

!!! warning "Use `cloudron exec` carefully"
    The database uses SQLite WAL mode. If you need a manual snapshot, stop the app first (`cloudron stop`) or use `sqlite3 .backup` inside the container — copying the `.db` file while the daemon is writing can produce an inconsistent file.

---

## Updates

```bash
cloudron update --app routstr.example.com
```

Updates are the primary lifecycle event on Cloudron and are designed to preserve `/app/data`. After the app comes back, confirm health and that your admin npub is still recognised:

```bash
curl https://routstr.example.com/health
routstrd npubs list
```

## Day-to-day operations

```bash
cloudron logs -f --app routstr.example.com   # follow both processes' output
cloudron exec --app routstr.example.com      # shell into the container
cloudron stop  --app routstr.example.com
cloudron start --app routstr.example.com
cloudron debug --app routstr.example.com     # read-write filesystem, app paused
cloudron debug --disable --app routstr.example.com
```

Inside the container you are on the machine that holds the wallet and the shared database, so `routstrd` commands there operate on the node itself rather than as a remote member:

```bash
routstrd npubs list      # everyone with access, and their roles
routstrd clients list    # every client on the node, not just your own
routstrd top             # interactive usage TUI across all members
```

Both processes log to stdout/stderr and are collected by Cloudron; there are no log files to rotate inside `/app/data`.

### Failure handling

`supervisord` runs both programs with `autorestart=true` and a start priority that brings the **daemon up first** (priority 10) and the **proxy second** (priority 20). The proxy's launcher additionally waits — up to 120 seconds — for the database file to exist *and* for the daemon's `/health` to answer before it starts serving. A crash-looping proxy therefore usually means the daemon never became healthy.

---

## Next steps

- [Team Members](team-members.md) — invite the rest of your team.
- [Security Model](security.md) — what is exposed and what is not.
- [Troubleshooting](troubleshooting.md) — when the proxy will not start.
