# Deployment

Start with the [private quickstart](quickstart.md). This guide is for an operator who has rotated the bootstrap password, configured an upstream, and decided to publish the node. For agent-assisted setup use [llms.txt](../../llms.txt). The original `compose.yml` is a separate stack that exposes port 8000 on all interfaces and starts Tor; do not switch to it by running bare `docker compose up` during this workflow.

## Prepare a durable host

Use Docker with the Compose plugin, Python 3 and persistent disk. Review a release tag before checkout; don't assume that a hard-coded tag in documentation is current. Run `python3 scripts/node_setup.py start` from the checkout. It creates `.env` with mode 0600 if absent and starts `compose.node.yml`, which only publishes `127.0.0.1:8000`. Review `.env` before the first boot; never commit it. First build may take a few minutes.

For a remote host, administer over an SSH tunnel. The temporary admin password appears once in the container's stdout. Read it in the **operator's private terminal**, rotate it immediately in **Settings → Admin Settings**, and do not relay startup logs through an agent. Do not publish a node with its bootstrap password intact.

## Publish intentionally

Before exposure, configure and verify an upstream and its enabled models (`python3 scripts/node_setup.py check`), choose a public HTTPS domain, firewall the host, and run a reverse proxy on the same host. For example, after DNS and certificates are ready:

```caddyfile
node.example {
    reverse_proxy 127.0.0.1:8000
}
```

Keep port 8000 loopback-only; only 443 should be publicly routed. Restrict administrative endpoints to trusted clients at the proxy when possible. In the admin settings, set the HTTP URL to the actual public HTTPS origin. Configure a dedicated Nostr identity and approved relays in the dashboard only when you want discovery. `compose.node.yml` disables automatic identity generation. Analytics is also forced off by default: to opt in, create a local `compose.analytics.yml` containing `services: {routstr: {environment: {ENABLE_ANALYTICS_SHARING: "true"}}}`, then use `docker compose -f compose.node.yml -f compose.analytics.yml up -d` and the same pair of files for subsequent operations. Enable analytics sharing in the dashboard only with operator consent. Keep the override and the node's `nsec` out of Git.

From another machine, run `python3 scripts/node_setup.py check --public-url https://node.example` (the checkout must be present on that machine) and verify the expected node name and nonzero model count. Normal hostname and certificate verification are required. `/v1/info` is not a health or paid-inference test. Make a small, budget-approved Cashu-paid completion and streaming request before declaring production readiness; reconcile usage, upstream charges and refunds. Test payout separately.

## Persistence and recovery

This Compose file bind-mounts the checkout to `/app`. Back up the **whole** state with all writers stopped:

- `keys.db` and any `keys.db-wal` / `keys.db-shm` sidecars (or a consistent external database backup);
- `routstr_secret.key` (or the configured `ROUTSTR_SECRET_KEY_FILE` / secret-manager key);
- `.wallet/`, `.env`, the Compose file and the node's Nostr identity.

Keep encryption keys and their database together in an encrypted off-host backup. Verify restoration on an isolated host with no relay publishing or payout activity; never run two copies of the same wallet concurrently. For a lost password, run `docker compose -f compose.node.yml exec routstr /.venv/bin/python scripts/reset_admin_password.py --regenerate` privately. Never remove volumes or wallet state to fix an authentication problem.

Before updates: take and verify a consistent backup, record the current tag and image, review migrations/release notes, then rebuild with `docker compose -f compose.node.yml up -d --build`. Rollback across database migrations may require a matching database and wallet restore, not only an older image. Re-run local checks, external checks, and the approved paid-inference checks after an update.
