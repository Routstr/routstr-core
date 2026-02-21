# Deployment

Production deployment guide for Routstr Provider nodes.

## Docker Compose (Recommended)

For production, use Docker Compose with persistent storage and optional Tor support.

### Unified Setup (All-in-one)
To build and run the node with the UI integrated in a single container using the multi-stage build:

```bash
docker build -f Dockerfile.full -t routstr-full .
docker run -d -p 8000:8000 --env-file .env routstr-full
```

### Advanced Setup (Separated UI & Node)
Use the included `compose.yml` for a more flexible setup that separates the UI build process from the node execution. This is useful for development or when you want to manage Tor as a separate service.

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
NAME=My Provider Node
RECEIVE_LN_ADDRESS=me@walletofsatoshi.com
```

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

### Unified Image (UI + Node)
The easiest way to build everything from source into a single production-ready image:

```bash
docker build -f Dockerfile.full -t routstr-full .
```

### Individual Components
If you prefer building them separately or using Docker Compose:

```bash
# Build using compose
docker compose build

# Or build the node only (requires manual UI build first)
docker build -t routstr-node .
```
