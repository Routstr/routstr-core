# Discovery

Routstr uses **Nostr** as a decentralized directory for service discovery. Your node announces its presence, models, and pricing on Nostr relays, allowing clients to find you without a central server.

---

## How It Works

1. **Provider Advertisement (Kind 38421)**: Your node periodically publishes an event with its URL, models, and pricing
2. **Client Discovery**: Clients query relays for these events to find suitable providers

### When announcements are published

Your node publishes an advertisement as soon as it has both a **Nsec** and at least one
reachable endpoint (a public `HTTP_URL`, or an `.onion` address). Saving the Nsec in the
dashboard is enough — the announcement follows within a minute, and **no restart is
required**. After the first publish it re-announces every 24 hours, and immediately
whenever the Nsec, endpoints, mints or relays change.

A node that has no Nsec yet simply waits, and starts announcing the moment one is
configured. Note that `HTTP_URL` defaults to `http://localhost:8000`, which is not a
reachable endpoint: a node with the default value and no onion address has nothing to
advertise and will not publish until one is set.

---

## Configuration

Configure discovery in **Dashboard** → **Settings** → **Nostr**.

### Required Settings

| Field | Description |
|-------|-------------|
| **Npub** | Your node's public identity (clients use this to verify your node) |
| **Nsec** | Your node's private key (used to sign advertisements) |
| **Relays** | Where to publish your announcements |

### Default Relays

If not configured, Routstr publishes to:

- `wss://relay.damus.io`
- `wss://relay.nostr.band`
- `wss://nos.lol`

---

## Advertisement Format

Your node publishes events like:

```json
{
  "kind": 38421,
  "content": {
    "name": "My Routstr Node",
    "description": "Fast GPT-4 access via Lightning",
    "endpoints": {
      "http": "https://api.mynode.com",
      "onion": "http://xyz...onion"
    },
    "models": ["gpt-4", "claude-3-opus"],
    "pricing": { ... }
  },
  "tags": [
    ["d", "routstr-provider"],
    ["g", "US"]
  ]
}
```

---

## Tor Integration

If you're running with Tor (see [Tor Support](tor.md)), your `.onion` address is automatically included in announcements. This allows clients to connect anonymously.

---

## Verify Your Announcements

Check if your node is broadcasting:

1. Copy your `Npub`
2. Search on [Nostr.band](https://nostr.band) or [Primal](https://primal.net)
3. Look for Kind 38421 events

---

## Generating Keys

If you don't have a Nostr identity:

1. Use any Nostr client (e.g., [Primal](https://primal.net), [Damus](https://damus.io))
2. Create an account
3. Export your keys (npub and nsec)
4. Enter them in the dashboard

Or generate keys programmatically:

```python
from nostr_sdk import Keys

keys = Keys.generate()
print(f"npub: {keys.public_key().to_bech32()}")
print(f"nsec: {keys.secret_key().to_bech32()}")
```
