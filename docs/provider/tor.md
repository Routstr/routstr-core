# Tor Support

Running Routstr as a **Tor Hidden Service** allows you to offer API access anonymously and bypass NAT/firewalls without port forwarding.

## Automatic Setup (Docker)

The standard `compose.yml` includes a Tor container pre-configured to serve your node.

1.  **Start the stack**: `docker compose up -d`
2.  **Wait**: Tor takes about 30 seconds to generate keys and bootstrap.
3.  **Find your address**:
    ```bash
    docker exec tor cat /var/lib/tor/hidden_service/hostname
    ```
    Output: `v2xyz...longaddress.onion`

Routstr will automatically detect this address (via the `discover_onion_url_from_tor` logic) and include it in:
- The `/v1/info` endpoint.
- Nostr announcements (RIP-02).

## Manual Setup

If you are running outside Docker or managing Tor yourself:

1.  **Install Tor**: `sudo apt install tor`
2.  **Edit `torrc`**:
    ```
    HiddenServiceDir /var/lib/tor/routstr/
    HiddenServicePort 80 127.0.0.1:8000
    ```
3.  **Restart Tor**: `sudo systemctl restart tor`
4.  **Get Address**: `sudo cat /var/lib/tor/routstr/hostname`
5.  **Configure Routstr**:
    Set `ONION_URL=http://youraddress.onion` in your `.env` file so the node knows its own address.

## Client Usage

Clients connecting to your `.onion` address must route traffic through SOCKS5.

**Python Example:**
```python
import httpx
from openai import OpenAI

client = OpenAI(
    base_url="http://youraddress.onion/v1",
    api_key="sk-...",
    http_client=httpx.Client(proxy="socks5://127.0.0.1:9050")
)
```
