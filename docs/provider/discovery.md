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

### Automatic identity (no manual nsec)

Setting `AUTO_GENERATE_NSEC=true` makes the node create its own Nostr identity on
first boot when none is configured — no dashboard step required. Both bundled
stacks enable this by default (`compose.yml` and `compose.node.yml`), so a fresh
node starts announcing as soon as it has a reachable endpoint: the Tor service
supplies one for `compose.yml`, and a public `HTTP_URL` (behind your reverse
proxy) supplies one for `compose.node.yml`. With neither, the generated identity
simply waits — nothing is published.

Only the generated `npub` is logged at startup; the `nsec` is stored encrypted at
rest and never printed. Retrieve it when you need to back it up with
`python scripts/reveal_nsec.py` (requires `ROUTSTR_SECRET_KEY`). It is the node's
identity: back up the database and the master key together. A node that already has
an identity — a provided `NSEC` seed or an nsec saved in the dashboard — is never
regenerated, and an identity you intentionally clear in the dashboard stays cleared.

---

## Configuration

Configure discovery in **Dashboard** → **Settings** → **Nostr**.

### Required Settings

| Field | Description |
|-------|-------------|
| **Npub** | Your node's public identity (clients use this to verify your node) |
| **Nsec** | Your node's private key (used to sign advertisements) |
| **Relays** | Where to publish your announcements |

These can be set by hand, or skipped entirely by letting the node generate its own
identity with `AUTO_GENERATE_NSEC=true` (see above).

### Default Relays

If not configured, Routstr publishes to and reads from:

- `wss://relay.damus.io`
- `wss://relay.routstr.com`
- `wss://nos.lol`

A relay belongs in this list only while it completes reads: one relay that
never sends `EOSE` makes provider-id lookup fail and pauses stats publication.

---

## Enclava deployment handoff (optional)

An Enclava node can add an **origin-specific release claim** to its existing
38421. A release claim advertises that origin as a `u`, too. It does not change
node identity, mark a review positive, prove the process is attested, or alter
other HTTP/onion endpoints' trust. See [the release-team 8428 format](enclava-release.md).

```json
["attestation", "enclava-tee", "https://api.example.com", "<64 lowercase hex event ID>"]
```

Deployment tooling should take `IMAGE` (the full `ghcr.io/…@sha256:…`
reference), `BUILD_ID` and `EVENT_ID` **from the same successful trusted release
job**, plus the operator's HTTPS `ORIGIN`. Never take these from an unverified
relay search. From the authenticated Enclava project containing `enclava.toml`,
with those variables and `STORAGE_PASSWORD_FILE` set:

```sh
set -eu
: "${IMAGE:?}" "${BUILD_ID:?}" "${EVENT_ID:?}" "${ORIGIN:?}" "${STORAGE_PASSWORD_FILE:?}"
listing=$(mktemp)
trap 'rm -f "$listing"' EXIT
jq -cn --arg build "$BUILD_ID" --arg origin "$ORIGIN" --arg id "$EVENT_ID" \
  '{build_id:$build, origins:{($origin):$id}}' > "$listing"
enclava deploy --no-input --image "$IMAGE" \
  --storage-password-file "$STORAGE_PASSWORD_FILE" \
  --set-file "ROUTSTR_ENCLAVA_LISTING=$listing"
```

`origins` maps each canonical public DNS HTTPS origin (no credentials, path,
trailing slash, or default port) to its own 8428 ID. Include only origins you
intend to advertise. The Enclava image auto-generates a node identity on first
boot if needed; no admin or `HTTP_URL` edit is necessary for these origins.

The node reads only the public `ROUTSTR_ENCLAVA_LISTING` config file under
`/state/.enclava/config`, not the whole directory and not a shell environment
file. The listing worker polls for config delivered after boot. A missing or
invalid value produces no new attestation claims. Config storage can survive an
upgrade: the file must match `ROUTSTR_ENCLAVA_BUILD_ID` baked into the image by
CI. This rejects stale config from other builds; the build ID is a configuration
guard, **not** a digest or cryptographic proof. Always supply the intended
mapping on deploy, including when reusing the same image. To stop claiming TEE
for a node that will still be advertised, deploy `origins:{}` with a regular
HTTP/onion endpoint configured. As with any ordinary node, if no endpoints
remain the publisher idles; it does not erase previously cached relay events.

Only the node signs 38421. Clients must independently verify the release
signature, approved policy and fresh channel-bound evidence before using a
route that requires TEE. Ordinary nodes need none of this configuration.

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
