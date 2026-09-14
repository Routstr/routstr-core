# Routstr Core Documentation

**Routstr** is a decentralized protocol for permissionless AI inference. It enables an open marketplace where anyone can buy and sell compute using **Bitcoin eCash (Cashu)**.

---

## 🐣 For Clients (Users & Builders)

If you want to use AI models in your application without accounts or KYC.

- **[Introduction](client/introduction.md)**: How the ecosystem works.
- **[Payment Flow](client/payments.md)**: Funding sessions, topping up, and refunds.
- **[Integration Guide](client/integration.md)**: Code examples for Python, JS, and cURL.

## 🦁 For Providers (Node Operators)

If you want to run a node, resell API access, or monetize hardware.

- **[Quick Start](provider/quickstart.md)**: Deploy a node in 5 minutes.
- **[Deployment](provider/deployment.md)**: Production Docker setup.
- **[Configuration](provider/configuration.md)**: Environment variables and settings.
- **[Dashboard](provider/dashboard.md)**: Managing your node visually.
- **[Pricing Strategy](provider/pricing.md)**: Setting margins and fees.
- **[Discovery](provider/discovery.md)**: Announcing your node on Nostr.
- **[Tor Support](provider/tor.md)**: Running an anonymous hidden service.

---

## 👥 For Teams (Remote Nodes)

If you want one shared Routstr endpoint for a whole team, with per-member identities and per-member usage tracking.

- **[Overview](teams/index.md)**: What a remote node is, and when to use one.
- **[Deploy on Cloudron](teams/deploy-cloudron.md)**: The packaged, supported deployment.
- **[Deploy with Docker](teams/deploy-docker.md)**: Run it on any host behind your own TLS.
- **[Team Members](teams/team-members.md)**: Bootstrap the first admin and invite people.
- **[Connecting Clients](teams/clients.md)**: Wire up Claude Code, Pi, OpenCode, and API keys.
- **[Usage and Model Policy](teams/usage-and-policy.md)**: Per-member spend and model allowlists.
- **[Security Model](teams/security.md)**: Auth rules and endpoint scoping.

---

## 🔌 API Reference

- **[Overview](api/overview.md)**: Base URL, headers, and standards.
- **[Endpoints](api/endpoints.md)**: Full list of REST endpoints.
- **[Authentication](api/authentication.md)**: Handling API keys and tokens.
- **[Errors](api/errors.md)**: Status codes and debugging.

## 🛠️ Contributing

- **[Architecture](contributing/architecture.md)**: System design.
- **[Setup](contributing/setup.md)**: Development environment.
- **[Testing](contributing/testing.md)**: Running tests.

---

*Powered by [Cashu](https://cashu.space) and [Nostr](https://nostr.com).*
