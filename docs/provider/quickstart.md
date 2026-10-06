# Run a provider node

A Routstr node connects to an upstream AI provider and accepts Cashu payments for inference. First boot is **private**: a working API is not yet a configured or publicly reachable provider.

## Before you start

You need a host with Docker and the Compose plugin, Python 3, enough space to build the dashboard, and an upstream API key. Keep the checkout on persistent storage. For a remote host, use an SSH tunnel to reach its private dashboard. Do not paste passwords, API keys, wallet proofs or startup logs into an agent chat.

## 1. Start privately

```bash
git clone https://github.com/Routstr/routstr-core.git
cd routstr-core
# For a production node, check out a reviewed release tag before the next step.
python3 scripts/node_setup.py start
```

The command copies `.env.example` to `.env` (0600) only if absent, builds the UI and API, and waits up to three minutes for the local API. The first build itself may take longer. `compose.node.yml` binds only `127.0.0.1:8000`, starts no Tor service, and disables automatic Nostr identity generation and analytics publication. Use this file for all subsequent Compose operations (`docker compose -f compose.node.yml ...`); plain `docker compose up` selects the **different public/Tor stack**.

On a remote server, forward port 8000 from your own computer:

```bash
ssh -L 8000:127.0.0.1:8000 user@your-server
```

Open <http://127.0.0.1:8000/admin/> in that computer's browser.

## 2. Secure the admin login

The server generates a bootstrap password once. **The operator** reads it privately in a trusted terminal using `docker compose -f compose.node.yml logs routstr`, enters it in the dashboard, then immediately changes it in **Settings → Admin Settings → Change Admin Password**. Do not capture those logs in automated output. If the password is lost, run `docker compose -f compose.node.yml exec routstr /.venv/bin/python scripts/reset_admin_password.py --regenerate` privately and rotate it again.

## 3. Configure service

In **Providers**, choose **Add Provider**, select the upstream type, enter its correct base URL and API key, review the provider fee, and save. Check that the provider is enabled and its model list has enabled models. In **Settings → Admin Settings**, review node name, payout Lightning address and pricing before accepting paid traffic. A saved payout address is not a tested payout.

```bash
python3 scripts/node_setup.py check
```

A successful check reports a nonzero **public model count**. Zero models means the API is running but setup is incomplete. This check proves discovery only: it does not test payments, streaming, actual upstream requests, refunds or payout.

## 4. Publish deliberately

Before making the node public, back up `keys.db` (including any SQLite sidecars), `routstr_secret.key`, `.wallet/` and `.env` while all writers are stopped. Verify a restore offline; the database and encryption key must remain together. Rotate the admin password first, put HTTPS and a firewall/reverse proxy in front of the loopback port, set the public URL, and decide explicitly whether to enable Nostr discovery and analytics. See [deployment](deployment.md). From **outside** the server, run `python3 scripts/node_setup.py check --public-url https://node.example` to verify certificate, info and models.

A production acceptance test also needs an operator-approved, small paid non-streaming and streaming inference request, accounting/reconciliation, and a tested backup/restore. Do not mark the node production-ready based only on `/v1/info` or `/v1/models`.

Agents should follow the separate [operator instructions](../../llms.txt); an agent cannot complete login, funding, or public exposure without the operator's approval.
