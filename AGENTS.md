# Routstr Core - Agent Documentation

This folder contains detailed documentation about the Routstr project, designed for AI agents and developers to understand how the system works.

## Quick Navigation

| Document                                                         | Description                                                |
| ---------------------------------------------------------------- | ---------------------------------------------------------- |
| **[01-project-overview.md](.agents/01-project-overview.md)**     | High-level architecture, concepts, and directory structure |
| **[02-api-endpoints.md](.agents/02-api-endpoints.md)**           | Complete API reference with examples                       |
| **[03-payment-flow.md](.agents/03-payment-flow.md)**             | How Cashu payments work end-to-end                         |
| **[04-upstream-providers.md](.agents/04-upstream-providers.md)** | Provider architecture and routing                          |
| **[05-nostr-integration.md](.agents/05-nostr-integration.md)**   | NIP-91 discovery and announcements                         |
| **[06-database-models.md](.agents/06-database-models.md)**       | Database schema and SQLModel classes                       |
| **[07-child-keys.md](.agents/07-child-keys.md)**                 | Sub-account system with spending limits                    |
| **[08-docker-deployment.md](.agents/08-docker-deployment.md)**   | Docker and production deployment                           |
| **[09-ui-architecture.md](.agents/09-ui-architecture.md)**       | Admin dashboard structure                                  |

## System Summary

**Routstr** is a decentralized AI inference marketplace:

- **Anyone can sell** AI inference for Bitcoin (sats)
- **Anyone can buy** using privacy-preserving eCash (Cashu)
- **No accounts, no KYC**, no central authority

### Key Technologies

- **FastAPI**: OpenAI-compatible proxy API
- **Cashu**: Private eCash payments on Bitcoin
- **Nostr**: Censorship-resistant discovery (NIP-91)
- **Next.js**: Admin dashboard
- **SQLite**: Data persistence

### Request Flow

```
1. Client pays with Cashu token (Bearer or x-cashu header)
2. Proxy validates token, reserves cost
3. Request forwarded to upstream AI (OpenAI, Anthropic, etc.)
4. Token usage calculated, balance adjusted
5. Response returned with cost info
```

### Pricing

- Fixed: flat fee per request
- Token-based: per-1K input/output tokens
- Provider fee: markup on upstream costs
- Exchange fee: BTC/USD conversion

## Quick Code Reference

### Entry Point

```python
# routstr/core/main.py
app = FastAPI()
app.include_router(proxy_router)
app.include_router(balance_router)
app.include_router(models_router)
```

### Adding a Provider

```python
# routstr/upstream/myprovider.py
class MyProvider(BaseUpstreamProvider):
    provider_type = "myprovider"
    def transform_model_name(self, model_id): return model_id
```

### Key Files

| File                         | Purpose           |
| ---------------------------- | ----------------- |
| `routstr/core/main.py`       | FastAPI app setup |
| `routstr/proxy.py`           | Request routing   |
| `routstr/auth.py`            | Auth & payment    |
| `routstr/wallet.py`          | Cashu operations  |
| `routstr/payment/price.py`   | BTC pricing       |
| `routstr/nostr/listing.py`   | NIP-91 publish    |
| `routstr/nostr/discovery.py` | Provider search   |

## Version

Current: **0.4.1**
