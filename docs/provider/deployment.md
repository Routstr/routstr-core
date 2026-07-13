# Deployment

Production deployment guide for Routstr Provider nodes.

## All-in-One Docker Image (Preferred)

The easiest way to deploy Routstr is using the all-in-one Docker image from Docker Hub, which includes both the FastAPI backend and the Next.js admin dashboard in a single container.

### Quick Start

```bash
docker run -d \
  --name routstr \
  -p 8000:8000 \
  -v routstr-data:/app/data \
  -e DATABASE_URL="sqlite:////app/data/routstr.db" \
  9qeklajc/routstr:latest
```

Access your node:
- **API & Admin Dashboard**: http://localhost:8000

### Docker Compose Setup

Create `docker-compose.yml`:

```yaml
version: '3.8'

services:
  routstr:
    image: 9qeklajc/routstr:latest
    container_name: routstr
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - routstr-data:/app/data
    environment:
      DATABASE_URL: "sqlite:////app/data/routstr.db"
      ADMIN_KEY: "your-secure-admin-key"
      LOG_LEVEL: "info"

volumes:
  routstr-data:
```

Start it:

```bash
docker compose up -d
```

---

## Docker Compose (Recommended)

For production, use Docker Compose with persistent storage and optional Tor support.

Use the included `compose.yml` for a flexible setup that handles both the UI and the node execution. This is useful for development or when you want to manage Tor as a separate service.

```bash
docker compose up -d
```

This will:
1.  **Build the UI**: Compiles the frontend and copies it to a shared volume.
2.  **Start Routstr**: Runs the Python node, mounting the built UI.
3.  **Start Tor**: Provides anonymous access via a `.onion` address.

---

## With Tor (Anonymous Access)

Add Tor to serve your node as a hidden service—no port forwarding needed.

```yaml
services:
  routstr:
    image: ghcr.io/routstr/proxy:latest
    container_name: routstr
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
    environment:
      - TOR_PROXY_URL=socks5://tor:9050
    depends_on:
      - tor

  tor:
    image: ghcr.io/hundehausen/tor-hidden-service:latest
    container_name: tor
    restart: unless-stopped
    volumes:
      - ./tor-data:/var/lib/tor
    environment:
      - HS_ROUTER=routstr:8000:80
```

After starting, find your `.onion` address:

```bash
docker exec tor cat /var/lib/tor/hidden_service/hostname
```

See [Tor Support](tor.md) for details.

---

## Pre-Configuration (Optional)

While everything can be configured via the dashboard, you can pre-configure settings with environment variables for automated deployments.

### Using Environment Variables

```yaml
services:
  routstr:
    image: ghcr.io/routstr/proxy:latest
    environment:
      # Pre-configure upstream (optional)
      - UPSTREAM_BASE_URL=https://api.openai.com/v1
      - UPSTREAM_API_KEY=sk-proj-...
      
      # Secure the dashboard (recommended)
      - ADMIN_PASSWORD=your-secure-password
      
      # Node identity
      - NAME=My Provider Node
      - DESCRIPTION=Fast GPT-4 access via Lightning
      
      # Lightning withdrawals
      - RECEIVE_LN_ADDRESS=me@walletofsatoshi.com
    volumes:
      - ./data:/app/data
```

### Using an .env File

```yaml
services:
  routstr:
    image: ghcr.io/routstr/proxy:latest
    env_file:
      - .env
    volumes:
      - ./data:/app/data
```

Example `.env`:

```bash
UPSTREAM_BASE_URL=https://api.openai.com/v1
UPSTREAM_API_KEY=sk-proj-...
ADMIN_PASSWORD=change-me
# Encrypts node secrets at rest. Optional — if unset, a key is generated next to
# your database (on the same volume) and printed once. Set it explicitly to
# manage the key yourself.
ROUTSTR_SECRET_KEY=
NAME=My Provider Node
RECEIVE_LN_ADDRESS=me@walletofsatoshi.com
```

!!! note "Secret key persistence"
    If you leave `ROUTSTR_SECRET_KEY` unset, the node generates one and stores it
    as `routstr_secret.key` **next to your database**, so it persists on the same
    volume as your data — just include that volume in your backups. For stronger
    isolation (keeping the key off the data volume), set `ROUTSTR_SECRET_KEY` from
    a secrets manager instead.

See [Configuration](configuration.md) for all available options.

---

## Persistence

Routstr stores all data in `/app/data`:

| Path | Contents |
|------|----------|
| `keys.db` | SQLite database (settings, API keys, sessions) |
| `.wallet/` | Cashu wallet data (your Bitcoin!) |

!!! warning "Back Up Your Data"
    The `./data` volume contains your wallet. Losing it means losing funds. Back up regularly.

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

Pull the latest image and restart:

```bash
docker compose pull
docker compose up -d
```

---

## Building from Source

### Using Docker Compose
The easiest way to build everything from source:

```bash
docker compose build
```

### Individual Components
If you prefer building the node only (requires manual UI build first):

```bash
docker build -t routstr-node .
```
