# Tor Support

Routstr Core includes built-in support for Tor hidden services, enabling anonymous access to your API and enhanced privacy for users.

## Overview

Tor support provides:

- **Anonymous Access**: Hidden service (.onion) address
- **Enhanced Privacy**: No IP address logging
- **Censorship Resistance**: Accessible from restricted networks
- **Optional Usage**: Regular HTTP/HTTPS access remains available

## Docker Setup

### Using Docker Compose

The included `compose.yml` automatically sets up Tor:

```yaml
version: '3.8'

services:
  routstr:
    build: .
    environment:
      - TOR_PROXY_URL=socks5://tor:9050
    ports:
      - 8000:8000

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

Start with:

```bash
docker compose up -d
```

### Getting Your Onion Address

After starting, retrieve your hidden service address:

```bash
# View Tor logs
docker compose logs tor

# Or directly from the hostname file
docker exec tor cat /var/lib/tor/hidden_service/hostname
```

Your onion address will look like:

```
roustrjfsdgfiueghsklchg.onion
```

## Manual Tor Setup

### Install Tor

```bash
# Ubuntu/Debian
sudo apt-get install tor

# macOS
brew install tor

# Start Tor
sudo systemctl start tor
```

### Configure Hidden Service

Edit `/etc/tor/torrc`:

```bash
# Hidden service configuration
HiddenServiceDir /var/lib/tor/routstr/
HiddenServicePort 80 127.0.0.1:8000

# Optional: Restrict to v3 addresses
HiddenServiceVersion 3
```

Restart Tor:

```bash
sudo systemctl restart tor
```

Get onion address:

```bash
sudo cat /var/lib/tor/routstr/hostname
```

## Client Configuration

### Using Tor with Python

```python
import httpx
from openai import OpenAI

# Configure SOCKS proxy
proxies = {
    "http://": "socks5://127.0.0.1:9050",
    "https://": "socks5://127.0.0.1:9050"
}

# Create client with Tor
http_client = httpx.Client(proxies=proxies)

client = OpenAI(
    api_key="sk-...",
    base_url="http://roustrjfsdgfiueghsklchg.onion/v1",
    http_client=http_client
)

# Use normally
response = client.chat.completions.create(
    model="gpt-3.5-turbo",
    messages=[{"role": "user", "content": "Hello via Tor!"}]
)
```

### Using Tor with cURL

```bash
# Install torify
sudo apt-get install torsocks

# Make request through Tor
torify curl http://roustrjfsdgfiueghsklchg.onion/v1/models

# Or with explicit proxy
curl --socks5 127.0.0.1:9050 http://roustrjfsdgfiueghsklchg.onion/v1/models
```

### JavaScript/Node.js

```javascript
import { SocksProxyAgent } from 'socks-proxy-agent';
import OpenAI from 'openai';

// Create SOCKS agent
const agent = new SocksProxyAgent('socks5://127.0.0.1:9050');

// Configure OpenAI client
const openai = new OpenAI({
    apiKey: 'sk-...',
    baseURL: 'http://roustrjfsdgfiueghsklchg.onion/v1',
    httpAgent: agent
});

// Use normally
const response = await openai.chat.completions.create({
    model: 'gpt-3.5-turbo',
    messages: [{ role: 'user', content: 'Hello via Tor!' }]
});
```

## Configuration

### Environment Variables

Configure Tor proxy for outgoing connections:

```bash
# .env
TOR_PROXY_URL=socks5://tor:9050  # Docker
# or
TOR_PROXY_URL=socks5://127.0.0.1:9050  # Local
```

### Publishing Onion Address

Make your onion address discoverable:

```bash
# .env
ONION_URL=http://roustrjfsdgfiueghsklchg.onion
```

This will be included in:

- `/v1/info` endpoint
- Nostr announcements
- Admin dashboard

## Security Considerations

### Hidden Service Security

1. **Keep Private Key Secure**

   ```bash
   # Backup hidden service keys
   sudo tar -czf tor-keys-backup.tar.gz /var/lib/tor/routstr/
   
   # Restore to maintain same address
   sudo tar -xzf tor-keys-backup.tar.gz -C /
   ```

2. **Access Control**

   ```bash
   # Restrict to authenticated clients
   HiddenServiceAuthorizeClient stealth client1,client2
   ```

3. **Rate Limiting**

   ```bash
   # In torrc
   HiddenServiceMaxStreams 100
   HiddenServiceMaxStreamsCloseCircuit 1
   ```

### Operational Security

1. **Separate Tor Instance**

   ```yaml
   # Use dedicated Tor container
   tor:
     image: ghcr.io/hundehausen/tor-hidden-service:latest
     restart: always
     networks:
       - tor_network
   ```

2. **Monitor Tor Health**

   ```python
   async def check_tor_connection():
       """Verify Tor connectivity."""
       try:
           async with httpx.AsyncClient(
               proxies={"all://": TOR_PROXY_URL}
           ) as client:
               response = await client.get(
                   "https://check.torproject.org/api/ip"
               )
               data = response.json()
               return data.get("IsTor", False)
       except Exception:
           return False
   ```

3. **Logging Considerations**

   ```python
   # Don't log .onion addresses with IPs
   def sanitize_logs(message: str) -> str:
       # Remove IP addresses when .onion is present
       if ".onion" in message:
           message = re.sub(r'\d+\.\d+\.\d+\.\d+', '[IP]', message)
       return message
   ```

## Performance Optimization

### Connection Pooling

```python
# Reuse Tor circuits
class TorConnectionPool:
    def __init__(self, proxy_url: str):
        self.proxy_url = proxy_url
        self._clients = []
    
    async def get_client(self) -> httpx.AsyncClient:
        if not self._clients:
            client = httpx.AsyncClient(
                proxies={"all://": self.proxy_url},
                timeout=httpx.Timeout(30.0),
                limits=httpx.Limits(
                    max_keepalive_connections=5,
                    max_connections=10
                )
            )
            self._clients.append(client)
        return self._clients[0]
```

### Circuit Management

```python
# Rotate Tor circuits periodically
async def rotate_tor_circuit():
    """Signal Tor to create new circuit."""
    async with httpx.AsyncClient() as client:
        # Tor control port (requires configuration)
        response = await client.post(
            "http://localhost:9051",
            data="AUTHENTICATE\r\nSIGNAL NEWNYM\r\n"
        )
```

### Caching Strategies

```python
# Cache responses for Tor users
@lru_cache(maxsize=1000)
def get_cached_response(
    endpoint: str,
    params_hash: str
) -> Optional[dict]:
    """Cache frequently accessed data."""
    # Longer cache for Tor users due to latency
    return cache.get(f"tor:{endpoint}:{params_hash}")
```

## Monitoring

### Tor Metrics

Track Tor-specific metrics:

```python
class TorMetrics:
    def __init__(self):
        self.tor_requests = 0
        self.tor_errors = 0
        self.circuit_builds = 0
        self.average_latency = 0
    
    async def record_request(
        self,
        duration: float,
        success: bool
    ):
        self.tor_requests += 1
        if not success:
            self.tor_errors += 1
        
        # Update average latency
        self.average_latency = (
            (self.average_latency * (self.tor_requests - 1) + duration)
            / self.tor_requests
        )
```

### Health Checks

```python
@router.get("/health/tor")
async def tor_health():
    """Check Tor service health."""
    checks = {
        "tor_proxy": await check_tor_proxy(),
        "hidden_service": await check_hidden_service(),
        "circuit_established": await check_circuit()
    }
    
    status = "healthy" if all(checks.values()) else "unhealthy"
    
    return {
        "status": status,
        "checks": checks,
        "metrics": {
            "tor_requests_total": metrics.tor_requests,
            "tor_error_rate": metrics.tor_errors / max(metrics.tor_requests, 1),
            "average_latency_ms": metrics.average_latency * 1000
        }
    }
```

## Troubleshooting

### Common Issues

**Hidden Service Not Accessible**

```bash
# Check Tor logs
docker compose logs tor
# or
sudo journalctl -u tor

# Verify service is running
sudo systemctl status tor

# Test locally
curl --socks5 127.0.0.1:9050 http://your-onion.onion/v1/info
```

**Slow Connection**

- Tor adds 3+ hops of latency
- Use connection pooling
- Implement aggressive caching
- Consider increasing timeouts

**Connection Errors**

```python
# Implement Tor-specific retry logic
async def tor_retry(func, max_retries=5):
    for attempt in range(max_retries):
        try:
            return await func()
        except httpx.ProxyError:
            if attempt < max_retries - 1:
                # Exponential backoff for circuit building
                await asyncio.sleep(2 ** attempt)
            else:
                raise
```

### Debugging

Enable Tor debug logging:

```bash
# In torrc
Log debug file /var/log/tor/debug.log

# Monitor in real-time
tail -f /var/log/tor/debug.log
```

## Best Practices

### For Operators

1. **Backup Hidden Service Keys**
   - Store securely offline
   - Enables service recovery
   - Maintains same .onion address

2. **Monitor Tor Health**
   - Check circuit establishment
   - Track request latency
   - Alert on failures

3. **Separate Concerns**
   - Run Tor in separate container
   - Isolate from main application
   - Use internal networks

### For Users

1. **Verify Onion Addresses**
   - Check against multiple sources
   - Bookmark verified addresses
   - Watch for phishing

2. **Handle Higher Latency**
   - Increase client timeouts
   - Implement retries
   - Use connection pooling

3. **Enhance Privacy**
   - Use Tor Browser for web access
   - Avoid mixing Tor/clearnet
   - Don't include identifying info

## Advanced Configuration

### Multi-Hop Onion Services

For extra security, chain multiple Tor instances:

```yaml
# compose.yml
services:
  tor-entry:
    image: tor:latest
    command: tor -f /etc/tor/torrc.entry
    
  tor-middle:
    image: tor:latest
    command: tor -f /etc/tor/torrc.middle
    
  tor-exit:
    image: tor:latest
    command: tor -f /etc/tor/torrc.exit
```

### Onion Service Authentication

Require client authorization:

```bash
# Generate client auth
openssl rand -base64 32 > client_auth_key

# In torrc
HiddenServiceDir /var/lib/tor/routstr/
HiddenServicePort 80 127.0.0.1:8000
HiddenServiceAuthorizeClient stealth payments
```

### Load Balancing

Distribute load across multiple instances:

```nginx
# Onion service nginx config
upstream routstr_backends {
    server routstr1:8000;
    server routstr2:8000;
    server routstr3:8000;
}

server {
    listen 80;
    location / {
        proxy_pass http://routstr_backends;
    }
}
```

## Next Steps

- [Nostr Discovery](nostr.md) - Announce your onion service
- [Docker Setup](../getting-started/docker.md) - Container configuration
