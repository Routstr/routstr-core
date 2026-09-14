# Deploy with Docker

The team node is a single container running two supervised processes. You can run that same image on any Docker host and terminate TLS with your own reverse proxy.

!!! note "Read this first"
    The image is built `FROM cloudron/base:5.0.0` and its entrypoint is the Cloudron startup script. It works outside Cloudron — it only requires a writable `/app/data` — but its filesystem conventions are Cloudron's, and the repository's `docker-compose.yml` predates this image. The commands below are the ones that match the current image.

---

## Build

```bash
git clone https://github.com/routstr/routstrd-remote
cd routstrd-remote
docker build -t routstr-remote:0.1.26 .
```

The Dockerfile installs Bun (the x64 **baseline** build, chosen because some hosts do not expose AVX/AVX2 and the default binary crashes with `SIGILL`), installs the `routstrd` daemon globally, installs the proxy's dependencies, and copies in the supervisor configuration for both processes.

## Run

```bash
mkdir -p "$HOME/routstr-remote-data"

docker run -d \
  --name routstr-remote \
  --restart unless-stopped \
  -p 127.0.0.1:8008:8008 \
  -v "$HOME/routstr-remote-data:/app/data" \
  --memory 1g \
  routstr-remote:0.1.26
```

**Why each flag matters:**

| Flag | Reason |
|---|---|
| `-v ...:/app/data` | The only persistent path. It holds `routstrd/config.json` (including the container's `nsec`), `routstrd/routstr.db`, and `logs/`. **Without it the node loses its identity and every npub on restart.** |
| `-p 127.0.0.1:8008:8008` | Publish on loopback only and let a TLS reverse proxy in front of it expose the service. Binding `0.0.0.0:8008` on a public host sends API keys over plaintext HTTP. |
| `--memory 1g` | Two Bun processes plus the daemon's model and usage state. The Cloudron manifest requests 512 MB; give a bare Docker host at least that, and prefer more. |

The container listens on exactly two ports: `8008` (public auth proxy) and `8009` (daemon, bound to loopback **inside** the container and deliberately not published).

Check it came up:

```bash
curl http://127.0.0.1:8008/health
docker logs -f routstr-remote
```

### Startup ordering

The entrypoint starts `supervisord`, which brings up the daemon first and the proxy second. The proxy's launcher then waits for the database file to appear and for `http://localhost:8009/health` to answer, retrying up to 120 times at one-second intervals. If that window expires, the proxy exits with `Timed out waiting for routstrd to become ready.` and restarts.

## Put TLS in front

Anything that terminates TLS and forwards to `127.0.0.1:8008` works. Two properties are worth configuring explicitly:

- **Disable response buffering.** The proxy already sends `X-Accel-Buffering: no` upstream, but your own proxy should also be configured not to buffer, otherwise streamed LLM responses appear to truncate.
- **Raise idle timeouts.** Model responses can be silent for a long time while reasoning or waiting on tools. The proxy disables Bun's per-request idle timeout for exactly this reason, but nginx's default 60-second `proxy_read_timeout` will still cut streams. Set it to something generous.

```nginx
location / {
    proxy_pass http://127.0.0.1:8008;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    proxy_buffering off;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
```

## Bootstrap the first admin

Identical to Cloudron — run this from the admin's own machine:

```bash
bun i -g routstrd
routstrd remote https://routstr.example.com
routstrd npubs register --name "Alice"
```

While the npub table is empty, `POST /npubs` needs no authentication, so the first registration wins. See [Team Members](team-members.md).

## Configuration

Override any of the variables from [Deploy on Cloudron](deploy-cloudron.md#configuration) with `-e`, for example:

```bash
docker run -d \
  --name routstr-remote \
  --restart unless-stopped \
  -p 127.0.0.1:8008:8008 \
  -v "$HOME/routstr-remote-data:/app/data" \
  -e ROUTSTRD_AUTH_MODEL_ALLOWLIST=true \
  routstr-remote:0.1.26
```

Inside the container the defaults are `ROUTSTRD_DIR=/app/data/routstrd`, `ROUTSTRD_DB_PATH=/app/data/routstrd/routstr.db`, `ROUTSTRD_UPSTREAM=http://localhost:8009`, `ROUTSTRD_AUTH_HOST=0.0.0.0`, `ROUTSTRD_AUTH_PORT=8008`.

!!! warning "Use a bind mount, not an anonymous volume"
    If you recreate the container (`docker rm` then `docker run`), an unnamed volume is orphaned and the node comes back with a **new** Nostr identity and an **empty** npub table — meaning the next person to hit `POST /npubs` becomes admin. Always mount a known host directory.

## Backups

The database is SQLite in WAL mode, so stop the container before copying the data directory, or take a proper snapshot from inside:

```bash
docker stop routstr-remote
tar czf routstr-remote-$(date +%F).tar.gz -C "$HOME" routstr-remote-data
docker start routstr-remote
```

Or, without downtime:

```bash
docker exec routstr-remote sqlite3 /app/data/routstrd/routstr.db ".backup /app/data/routstrd/backup.db"
docker cp routstr-remote:/app/data/routstrd/backup.db .
```

## Updates

```bash
docker stop routstr-remote && docker rm routstr-remote
git pull
docker build -t routstr-remote:new .
docker run -d ... routstr-remote:new     # same -v and -p flags as before
```

Because `/app/data` is a bind mount, the node's identity, npub table, and client records survive the swap. That is the entire reason the mount is not optional.

---

## Running from source (evaluation and development)

You do not need Docker to try the proxy. Point it at any routstrd daemon's database:

```bash
bun install
bun run src/index.ts validate          # checks config and opens the DB
bun run src/index.ts start             # binds 0.0.0.0:8008
```

Useful flags: `--port`, `--host`, `--upstream`, `--db-path`. The `validate` subcommand prints the effective configuration and reports how many npubs are registered, split by role — it is the fastest way to confirm the proxy can see the right database:

```text
Configuration:
  Port:     8008
  Host:     0.0.0.0
  Upstream: http://localhost:8009
  DB path:  /app/data/routstrd/routstr.db
  Bootstrap admin npubs/pubkeys from env: 0
  Model allowlist: disabled

✅ DB accessible. 3 npub(s) registered (1 admin, 2 user).
```

!!! warning "`validate` fails before the daemon has run once"
    If the database does not exist, validation stops with `Database not found at ... Make sure routstrd has been initialized`. The proxy shares the daemon's database; it never creates the schema itself.

---

## Next steps

- [Team Members](team-members.md) — invite the rest of your team.
- [Security Model](security.md) — which endpoints require what.
- [Troubleshooting](troubleshooting.md) — startup and streaming failures.
