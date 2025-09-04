# Docker Setup

This guide covers deploying Routstr Core using Docker for production environments.

## Docker Images

Official images are available on GitHub Container Registry:

```bash
ghcr.io/routstr/proxy:latest
```

## Basic Docker Run

### Minimal Setup

```bash
docker run -d \
  --name routstr \
  -p 8000:8000 \
  -e UPSTREAM_BASE_URL=https://api.openai.com/v1 \
  -e UPSTREAM_API_KEY=sk-... \
  -e ADMIN_PASSWORD=secure-password \
  ghcr.io/routstr/proxy:latest
```

### With Persistent Storage

```bash
docker run -d \
  --name routstr \
  -p 8000:8000 \
  -v routstr-data:/app/data \
  -v routstr-logs:/app/logs \
  -e UPSTREAM_BASE_URL=https://api.openai.com/v1 \
  -e UPSTREAM_API_KEY=sk-... \
  -e DATABASE_URL=sqlite+aiosqlite:///data/keys.db \
  ghcr.io/routstr/proxy:latest
```

## Docker Compose Setup

### Basic Configuration

Create `compose.yml`:

```yaml
version: '3.8'

services:
  routstr:
    image: ghcr.io/routstr/proxy:latest
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
    env_file:
      - .env
    restart: unless-stopped
```

### With Tor Hidden Service

The included `compose.yml` provides Tor support:

```yaml
version: '3.8'

services:
  routstr:
    build: .  # Or use image: ghcr.io/routstr/proxy:latest
    volumes:
      - .:/app
      - ./logs:/app/logs
    env_file:
      - .env
    environment:
      - TOR_PROXY_URL=socks5://tor:9050
    ports:
      - 8000:8000
    extra_hosts:
      - "host.docker.internal:host-gateway"

  tor:
    image: ghcr.io/hundehausen/tor-hidden-service:latest
    volumes:
      - tor-data:/var/lib/tor
    environment:
      - HS_ROUTER=routstr:8000:80
    depends_on:
      - routstr

volumes:
  tor-data:
```

### Environment File

Create `.env` file:

```bash
# Required
UPSTREAM_BASE_URL=https://api.openai.com/v1
UPSTREAM_API_KEY=your-api-key
ADMIN_PASSWORD=secure-admin-password

# Cashu Configuration
CASHU_MINTS=https://mint.minibits.cash/Bitcoin

# Optional
NAME=My Routstr Node
DESCRIPTION=Pay-per-use AI API proxy
NPUB=npub1...
HTTP_URL=https://api.mynode.com
ONION_URL=http://mynode.onion

# Pricing
MODEL_BASED_PRICING=true
EXCHANGE_FEE=1.005
UPSTREAM_PROVIDER_FEE=1.05
```

## Building Custom Image

### Dockerfile Overview

The provided Dockerfile:

- Uses Alpine Linux for small size
- Installs required dependencies for secp256k1
- Runs as non-root user
- Exposes port 8000

### Build Locally

```bash
# Clone repository
git clone https://github.com/routstr/routstr-core.git
cd routstr-core

# Build image
docker build -t my-routstr:latest .

# Run custom image
docker run -d \
  --name routstr \
  -p 8000:8000 \
  --env-file .env \
  my-routstr:latest
```

## Deployment Considerations

### Resource Requirements

- **CPU**: 1-2 cores recommended
- **Memory**: 512MB-1GB
- **Storage**: 1GB + database growth
- **Network**: Low latency to upstream provider

### Health Checks

Add health check to compose.yml:

```yaml
services:
  routstr:
    # ... other config ...
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/v1/info"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s
```

### Reverse Proxy Setup

#### Nginx Example

```nginx
server {
    listen 443 ssl http2;
    server_name api.yournode.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # For streaming responses
        proxy_buffering off;
        proxy_cache off;
        proxy_set_header Connection '';
        proxy_http_version 1.1;
        chunked_transfer_encoding off;
    }
}
```

#### Caddy Example

```caddy
api.yournode.com {
    reverse_proxy localhost:8000 {
        flush_interval -1
    }
}
```

## Monitoring

### Log Management

View logs:

```bash
# Docker
docker logs -f routstr

# Docker Compose
docker compose logs -f routstr

# Log files
tail -f ./logs/routstr.log
```

### Metrics

Monitor key metrics:

- Request count and latency
- Token validation success rate
- Upstream API errors
- Database size growth

## Backup and Recovery

### Database Backup

```bash
# Backup SQLite database
docker exec routstr sqlite3 /app/data/keys.db ".backup /app/data/backup.db"

# Copy backup locally
docker cp routstr:/app/data/backup.db ./backup-$(date +%Y%m%d).db
```

### Restore from Backup

```bash
# Stop service
docker compose down

# Restore database
docker cp ./backup.db routstr:/app/data/keys.db

# Restart service
docker compose up -d
```

## Security Considerations

### Environment Variables

- Never commit `.env` files
- Use Docker secrets for sensitive data
- Rotate API keys regularly
- Use strong admin passwords

### Network Security

- Use HTTPS/TLS termination
- Restrict admin interface access
- Enable firewall rules
- Monitor for suspicious activity

### Container Security

- Run as non-root user
- Use read-only filesystem where possible
- Limit container capabilities
- Keep base image updated

## Troubleshooting

### Container Won't Start

```bash
# Check logs
docker logs routstr

# Verify environment
docker exec routstr env | grep -E "(UPSTREAM|CASHU|ADMIN)"

# Test database connection
docker exec routstr sqlite3 /app/data/keys.db ".tables"
```

### Permission Issues

```bash
# Fix volume permissions
sudo chown -R 1000:1000 ./data ./logs
```

### Network Issues

```bash
# Test upstream connectivity
docker exec routstr curl -I https://api.openai.com

# Check DNS resolution
docker exec routstr nslookup api.openai.com
```

## Production Checklist

- [ ] Set strong `ADMIN_PASSWORD`
- [ ] Configure proper `UPSTREAM_BASE_URL` and `UPSTREAM_API_KEY`
- [ ] Set up persistent volumes for data and logs
- [ ] Configure reverse proxy with TLS
- [ ] Set up monitoring and alerting
- [ ] Implement backup strategy
- [ ] Test disaster recovery
- [ ] Document deployment process

## Next Steps

- [Configuration Guide](configuration.md) - All environment variables
- [Admin Dashboard](../user-guide/admin-dashboard.md) - Manage your node
