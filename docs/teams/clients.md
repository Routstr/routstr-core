# Connecting Clients

A **client** is one agent or application talking to the node. Each client gets its own ID and its own API key (`sk-...`). Clients are how a team node attributes usage: every request is billed against the client that made it.

Members create and manage **their own** clients. They cannot see or touch anyone else's.

---

## Two credentials, two jobs

The node accepts two entirely different kinds of credential, and confusing them is the most common source of `403`s.

| Credential | Header | Purpose | Can do |
|---|---|---|---|
| **API key** | `Authorization: Bearer sk-...` | Inference | Send chat/completion requests. Cannot touch wallets, clients, or npubs. |
| **NIP-98** | `Authorization: Nostr <base64-event>` | Management | Manage clients, npubs, wallet, node control. Signed per-request by the member's `nsec`. |

Your agents use the **API key**. The `routstrd` CLI uses **NIP-98** automatically, which is why it needs your `nsec` in `~/.routstrd/config.json`.

---

## Add a client

From the member's own machine:

```bash
# Name it explicitly
routstrd clients add --name "My Laptop"

# Or use a one-shot integration setup
routstrd clients add --claude-code
routstrd clients add --pi-agent
routstrd clients add --opencode
routstrd clients add --openclaw
routstrd clients add --hermes
```

The integration flags configure the agent's own config file as well as registering the client, so you do not have to hand-edit anything. Several can be combined in one call.

On success the CLI prints the credentials and the endpoint to point at:

```text
Client created.

  ID:      my-laptop
  Name:    My Laptop
  API Key: sk-9f2a...

  Access Routstr at: https://team.example.com/v1
```

!!! warning "The API key is a secret"
    Treat `sk-...` like a password. It bills inference to the team wallet. Do not commit it, and do not paste it into a chat — unlike an npub, it is not safe to share.

### Adding is idempotent

Running `clients add` with a name that already exists does not create a duplicate. It looks the client up and prints the existing record — including its API key — so re-running is a safe way to recover a key you lost:

```text
Client 'my-laptop' already exists.

  ID:      my-laptop
  Name:    My Laptop
  API Key: sk-9f2a...
```

### List and delete

```bash
routstrd clients list
routstrd clients delete my-laptop
```

`clients list` shows **only your own** clients.

---

## Going beyond the CLI

The agent integrations cover the common tools, but any OpenAI-compatible client works — point it at the node and use the API key:

```bash
curl https://team.example.com/v1/chat/completions \
  -H "Authorization: Bearer sk-9f2a..." \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "hello"}]
  }'
```

The base URL is always the node host plus `/v1`. Discover available models without any credential at all:

```bash
curl https://team.example.com/v1/models
```

---

## How client ownership works

This is the part that explains the odd-looking IDs on the node.

### IDs are derived from the name

A client's ID is the name lowercased with internal whitespace collapsed to hyphens, then stripped of anything that is not alphanumeric or a hyphen. `"My Laptop!"` becomes `my-laptop`.

### The node appends an owner suffix

So that two members can both have a client called `my-laptop` without colliding, the auth proxy appends the **last 7 characters of the owner's npub** to the ID before it reaches the daemon:

| Where you look | Client ID |
|---|---|
| Member's `routstrd clients list` | `my-laptop` |
| On the node (`cloudron exec`, then `routstrd clients list`) | `my-laptop-4f2x9k7` |

The suffix is stripped again on the way back, so members always see the clean ID. On the node you deliberately see the suffixed form — **the trailing characters are what tell you which member owns a client.**

### Ownership is recorded explicitly

Newly created clients store the owner's npub in an `ownerNpub` field, and the proxy authorises against that field. Clients created before that field existed fall back to matching the ID suffix, so older installs keep working until those clients are recreated.

### Consequence: admins are not automatically superusers here

`/clients`, `/clients/add`, and `/clients/delete` are owner-scoped by the calling npub. Even an `admin` cannot list or delete a colleague's clients through these endpoints. Cross-member visibility comes from running the CLI **on the node itself**, where the daemon is unauthenticated on loopback:

```bash
cloudron exec --app routstr.example.com
routstrd clients list     # all clients, all owners, suffixed IDs
```

That is also the only practical way to clean up a departing member's keys — see [Team Members](team-members.md#what-revocation-does-and-does-not-do).

---

## Refreshing models and integrations

The `clients` command carries options for the daemon's scheduled refresh job, which updates the Routstr 21 model list and re-syncs client integrations:

```bash
routstrd clients --manual-refresh                # refresh now, once
routstrd clients --disable-automatic-refresh     # stop the scheduled job
routstrd clients --enable-automatic-refresh      # start it again
```

The model list matters because it is also what the [model allowlist](usage-and-policy.md#model-allowlist) is enforced against.

---

## Next steps

- [Usage and Model Policy](usage-and-policy.md) — watch what those clients are spending.
- [Security Model](security.md) — the exact rules applied to each credential.
