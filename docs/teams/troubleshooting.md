# Troubleshooting

Most team-node problems are one of five things: the daemon never came up, the wrong database, a credential problem, a clock or proxy-header problem, or a networking/timeout issue around streaming.

---

## First triage

Run these in order. Together they answer "is the node up, does it have people, and is the proxy seeing the right database".

```bash
# 1. Is the public surface alive?
curl -sS https://team.example.com/health

# 2. Do both processes run, and what did they log on boot?
cloudron logs --app routstr.example.com | tail -50     # or: docker logs routstr-remote

# 3. Can the proxy see the database, and does it know your people?
cloudron exec --app routstr.example.com
routstrd-auth validate
```

Step 3 is the most informative. Its output ends with a line like `✅ DB accessible. 3 npub(s) registered (1 admin, 2 user).` — if that count is wrong, or the path is wrong, you have found your problem.

On startup the proxy also logs a summary, and warns loudly if the node is unclaimed:

```text
routstrd-auth proxy listening on http://0.0.0.0:8008
  Upstream: http://localhost:8009
  DB path:  /app/data/routstrd/routstr.db
  Registered npubs: 0
  Model allowlist: disabled
  Warning: no registered npub/pubkey. The first admin can be registered without auth using POST /npubs.
```

!!! warning "`Registered npubs: 0` on an established node is an emergency"
    Either the database was lost (usually a missing volume mount) or the proxy is pointed at the wrong file. While the table is empty the node is claimable by anyone who reaches it. Fix it before anything else — see [Lost identity or empty npub list](#lost-identity-or-empty-npub-list).

---

## Error reference

### Startup

| Symptom | Cause | Fix |
|---|---|---|
| `Timed out waiting for routstrd to become ready.` | The proxy's launcher waited 120 seconds for the database file to exist **and** for `http://localhost:8009/health` to answer. The daemon is unhealthy or crashing. | Read the daemon's log lines above this one. It is a daemon problem, not a proxy problem. |
| `Database not found at /app/data/routstrd/routstr.db. Make sure routstrd has been initialized (routstrd onboard).` | The proxy shares the daemon's database and never creates the schema. It ran before the daemon had ever initialised. | Start the daemon first (`routstrd start`, or let `supervisord` do it — priority 10 before 20). |
| `Invalid admin Nostr pubkey(s): <value>. Use npub or 64-char hex pubkeys.` | A bootstrap admin variable contains something unparseable. | Fix `ROUTSTRD_AUTH_ADMIN_NPUBS` / `_PUBKEYS` / `_BOOTSTRAP_NPUB`. Use `npub1...` or 64-char hex. |
| Container exits with `Illegal instruction` / `SIGILL` | Bun's default x64 build needs AVX/AVX2, which some hosts and VMs do not expose. | The shipped image already uses the **baseline** build for this reason. If you build your own, keep `BUN_TARGET=bun-linux-x64-baseline`. |
| Proxy crash-loops immediately after a config change | Validation fails, so `start` exits non-zero and `supervisord` restarts it. | Run `routstrd-auth validate` to see the message. |

### Authentication

| Response | Meaning | Fix |
|---|---|---|
| `401 Missing Authorization header. Use 'Authorization: Bearer sk-...' or 'Authorization: Nostr <base64-event>'.` | No credential sent, and the path is not public. Remember public paths are `GET`/`HEAD` only. | Send a credential, or use a read method on a public path. |
| `401 Invalid API key.` | The key is not in the client records. | Recover it with `routstrd clients add --name "<existing name>"`, which prints the existing key. |
| `403 API keys cannot access this endpoint. Use NIP-98 auth from a registered npub/pubkey.` | You used an `sk-...` key on a wallet, node-control, or management endpoint. | Use the CLI, which signs with NIP-98 automatically. |
| `403 Admin access required. Only admin npubs can perform this action.` | The npub is registered but holds the `user` role. | An admin promotes them: `routstrd npubs update <npub> --role admin`. |
| `403 This endpoint requires a registered npub/pubkey, but none is configured. Register the first admin with 'routstrd npubs register'.` | The npub table is empty. | Bootstrap the first admin. |
| `403 This endpoint requires NIP-98 auth from a registered npub/pubkey.` | The signature was valid but the pubkey is not in the table. | An admin adds it: `routstrd npubs add <npub>`. |
| `401 Invalid Authorization format. Expected 'Bearer sk-...' or 'Nostr <base64-event>'.` | The header used a different scheme or a typo'd prefix. | Fix the prefix. |

A useful diagnostic: the token **type** determines the error. A `403` naming a specific capability means you authenticated successfully and were then refused by policy. A `401` means you did not authenticate at all.

### NIP-98 signature rejections

These all return `401` with a precise reason.

| Message | Cause | Fix |
|---|---|---|
| `NIP-98 URL tag does not match this request.` | The most common one. The `u` tag holds the URL the client signed, and the proxy reconstructs the public URL from `X-Forwarded-Proto` / `X-Forwarded-Host` (falling back to `Host`). Behind TLS termination, a missing forwarded header makes the reconstructed URL `http://...` while the client signed `https://...`. | Configure the reverse proxy to set both headers. See [Deploy with Docker](deploy-docker.md#put-tls-in-front). |
| `NIP-98 event timestamp is outside the allowed window.` | Events must be within **±60 seconds**. Clock skew. | Sync the client's clock (`timedatectl`, NTP). If only one machine fails, it is that machine. |
| `NIP-98 payload tag is required for requests with a body.` \| `NIP-98 payload tag does not match the request body hash.` | The signed SHA-256 does not match the body that arrived — something rewrote the body in transit. | Check for a middleware, WAF, or forward proxy that re-encodes request bodies. |
| `NIP-98 method tag does not match this request.` | The event was signed for a different method (often a `GET` signature reused on a `POST`). | Sign per request; do not reuse events. |
| `Invalid NIP-98 event signature.` \| `Invalid NIP-98 event kind.` | Corrupted token, or not a kind `27235` event. | Regenerate the request with the CLI. |
| `Invalid NIP-98 token encoding.` \| `Invalid NIP-98 event JSON.` | The base64 payload is truncated — common when a long `Authorization` header is split or truncated by a client. | Check for a header-size limit in the client or proxy. |

### Client-side CLI messages

| Message | Meaning | Fix |
|---|---|---|
| `The daemon at <url> rejected this account.` then `Register/authorize this npub on the remote daemon first: <npub>` | The node does not recognise your npub. | Send the printed npub to an admin. |
| `No remote node is set up.` | No `daemonUrl` in `~/.routstrd/config.json`. | `routstrd remote https://team.example.com`. |
| `Your npub is not in the npub list. Ask an admin to add your npub:` | `npubs list` worked (so you *are* registered) but shows you as absent — typically a stale local identity after a config reset. | Re-run `routstrd remote` to display your current npub, and confirm with an admin which one is registered. |
| `Daemon is not running` | The local daemon is unreachable. | Only relevant when running from source; `routstrd start`. |

---

## Common scenarios

### Streaming responses get cut off mid-answer

The symptom is a reply that stops abruptly — often after exactly the same number of seconds — with no error from the model.

This is **not** a node problem. It is response buffering or an idle timeout in a proxy in front of the node. Model turns can be silent for a long time while reasoning or waiting on tools, and intermediaries treat that silence as a dead connection.

Fix, in order of what actually bites:

1. **Disable response buffering** where the proxy talks to `8008`. The node already sends `X-Accel-Buffering: no`, but the fronting proxy must also be told (`proxy_buffering off` in nginx).
2. **Raise read timeouts** well above the default (`proxy_read_timeout 3600s`). nginx's 60-second default is the usual culprit.
3. **Do not work around it by exposing the daemon.** Publishing `8009` removes authentication entirely.

The proxy itself disables Bun's per-request idle timeout (`server.timeout(req, 0)`) precisely because valid streams can be quiet for a long time; anything still timing out is outside the node.

### Lost identity or empty npub list

If `routstrd npubs list` is empty after a restart, or `routstrd-auth validate` reports `0 npub(s)`, the node lost its data directory. In Docker this is almost always an **unmounted or changed volume**: `/app/data` is the only persistent path.

The damage is two-fold:

- The `nsec` in `routstrd/config.json` is gone, so the node has a **new** identity.
- The npub table is empty, so `POST /npubs` is **unauthenticated again** — anyone who reaches the URL can claim the node.

Recover in this order:

1. **Stop the app** so nobody claims it.
2. Restore `/app/data` from a backup, or re-mount the correct volume and restart.
3. If data is unrecoverable, accept the new identity and re-register the first admin — then have everyone whose npub was lost send theirs again, and recreate their clients (keys do not survive either).

### Model requests return `403` unexpectedly

Either the sender is genuinely outside the allowlist, or the allowlist is enforcing a **stale** list.

```bash
cloudron exec --app routstr.example.com
sqlite3 /app/data/routstrd/routstr.db \
  "SELECT substr(value,1,120) FROM sdk_storage WHERE key = 'routstr21Models';"
```

- **No output:** the daemon never bootstrapped the list, so the proxy is failing **open** and allowing everything. The `403` is coming from somewhere else.
- **Output present but missing the model you want:** the list is stale. Refresh it with `routstrd clients --manual-refresh`, and check the scheduled job has not been disabled with `--disable-automatic-refresh`.

Remember the check applies to `POST`/`PUT`/`PATCH` bodies containing a `model` field only, and comparisons are case-sensitive.

### A member cannot authenticate at all

Work down this list:

1. **Are they registered?** `routstrd npubs list` as an admin.
2. **Is their npub the one you registered?** They run `routstrd remote` with no arguments to print the identity actually in their config. A machine with an old or regenerated `nsec` presents a different npub.
3. **Is their clock correct?** Off by more than 60 seconds means every NIP-98 event is rejected.
4. **Are forwarded headers set?** If *everyone* is failing with a URL tag mismatch, it is the reverse proxy, not the people.
5. **Do they have admin-requiring needs?** A `403 Admin access required` is a role problem, not an authentication problem.

### The node works but nobody can see usage

Usage is attributed per client, and `/usage` is scoped to the caller's npub. A member with no clients has no usage to show. Aggregate figures require running the CLI **on the node**, where the daemon is unauthenticated and unfiltered:

```bash
cloudron exec --app routstr.example.com
routstrd top
```

---

## Next steps

- [Security Model](security.md) — the rules behind the `401`s and `403`s.
- [Usage and Model Policy](usage-and-policy.md) — allowlist behaviour and its fail-open case.
