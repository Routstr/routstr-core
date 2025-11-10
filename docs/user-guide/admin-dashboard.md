# Admin Dashboard

The Routstr admin dashboard is a modern web interface for managing your node, monitoring wallet balances, configuring AI models and providers, and handling Bitcoin Lightning payments through Cashu eCash.

## Accessing the Dashboard

### Authentication

The dashboard is protected by password authentication:

1. Navigate to `/admin/` in your browser
2. Enter the admin password
3. Optional: Configure custom base URL if not pre-configured
4. Click "Login"

The interface supports both environment-configured URLs and manual URL entry for deployment flexibility.

## Dashboard Overview

The main dashboard consists of four primary sections accessible through a collapsible sidebar:

- **Dashboard** - Wallet balance monitoring and fund management
- **Models** - AI model management and testing
- **Providers** - Upstream provider configuration
- **Settings** - Node configuration and admin preferences

### Navigation

## Dashboard Page

### Wallet Balance Management

#### Balance Display Options

Switch between display units using the toggle buttons:

- **msat** - Millisatoshis (highest precision)
- **sat** - Satoshis (standard Bitcoin unit)
- **usd** - US Dollar equivalent (when exchange rate available)

#### Balance Overview

The dashboard displays three key metrics:

- **Your Balance (Total)** - Available funds for node operator
- **Total Wallet** - Combined balance across all Cashu mints
- **User Balance** - Funds held for API key holders

#### Detailed Balance Breakdown

View balances by mint with the following information:

| Column      | Description                           |
| ----------- | ------------------------------------- |
| Mint / Unit | Cashu mint URL and currency unit      |
| Wallet      | Total funds in this mint              |
| Users       | Funds belonging to API key holders    |
| Owner       | Your available funds (Wallet - Users) |

### Temporary Balances

Monitor API key activity with:

- **Summary Cards** - Total balance, total spent, total requests
- **Search Functionality** - Filter by key hash or refund address
- **Detailed Table** - Individual key balances with expiry times
- **Auto-refresh** - Updates every 60 seconds

### Fund Management

#### Withdrawing Funds

To withdraw your available balance:

1. Click the **Withdraw** button
2. Select which mint to withdraw from
3. Specify the amount (or withdraw full balance)
4. Click **Generate Token**
5. Copy the generated eCash token
6. Import the token into your Cashu wallet

#### Real-time Updates

- Balances refresh automatically every 30 seconds
- Manual refresh option available
- Live Bitcoin/USD exchange rate integration
- Error handling for mint connectivity issues

## Models Management Page

### Model Organization

Models are organized by provider groups with tabs:

- **All Models** - Combined view of all available models
- **Provider-specific tabs** - Individual providers (OpenRouter, Azure, etc.)
- Badge indicators showing active/total model counts

### Model Management Features

#### Individual Model Operations

For each model you can:

- **Toggle Enable/Disable** - Control model availability
- **View Details** - Context length, pricing, description
- **Edit Configuration** - Model-specific settings
- **Status Indicators** - Green badges for enabled, gray for disabled

#### Bulk Operations

- **Select All/Deselect All** - Quick selection controls
- **Bulk Enable/Disable** - Mass model management
- **Bulk Delete** - Remove model overrides
- **Provider-level Actions** - Apply settings to all models in a provider

#### Model Information Display

- **Model Types** - Text, embedding, image, audio, multimodal indicators
- **Pricing Information** - Per-million-token costs for input/output
- **Context Length** - Maximum tokens supported
- **API Key Status** - Whether credentials are configured
- **Free Model Indicators** - No-cost models clearly marked

## Providers Management Page

### Upstream Provider Configuration

Manage AI provider connections and credentials:

#### Provider Types Supported

- **OpenRouter** - Multi-model aggregator
- **Azure OpenAI** - Microsoft's OpenAI service
- **OpenAI** - Direct OpenAI integration
- **Custom Providers** - Any OpenAI-compatible API

#### Adding New Providers

1. Click **Add Provider**
2. Select **Provider Type** from dropdown
3. Enter **Base URL** (auto-populated for known providers)
4. Add **API Key** for authentication
5. Set **API Version** (required for Azure)
6. Toggle **Enabled** status
7. Click **Create**

#### Provider Management

**Provider Cards Display:**

- Provider type and status (Enabled/Disabled)
- Base URL configuration
- Action buttons (Models, Edit, Delete)

**Available Actions:**

- **Edit** - Modify provider configuration
- **Delete** - Remove provider (with confirmation)
- **View Models** - Expand model discovery interface
- **Enable/Disable** - Toggle provider availability

#### Model Discovery

Each provider shows two types of models:

**Provided Models Tab:**

- Auto-discovered from provider's catalog
- Read-only model information
- Real-time availability updates

**Custom Models Tab:**

- Manually configured model overrides
- Extend or override provider catalog
- Individual enable/disable controls

## Settings Page

### Node Configuration

Configure core node settings and preferences:

#### Basic Information

- **Node Name** - Identifier for your node
- **Node Description** - Descriptive text for your service
- **HTTP URL** - Public HTTP endpoint
- **Onion URL** - Tor hidden service address

#### Nostr Integration

- **Public Key (npub)** - Your Nostr public identity
- **Private Key (nsec)** - Nostr private key with show/hide toggle
- **Nostr Relays** - Configure relays for provider announcements

#### Security Settings

- **Upstream API Key** - Primary API key for upstream providers
- **Admin Password** - Change dashboard access password
- **API Key Visibility** - Control credential display in interface

#### Cashu Mint Management

- **Add Mint URLs** - Configure multiple Cashu mint endpoints
- **Remove Mints** - Delete unused mint configurations
- **Mint Validation** - Verify mint endpoint connectivity

#### Settings Features

- **Real-time Save** - Changes apply immediately
- **Validation** - Form validation with error feedback
- **Secure Fields** - Password masking with reveal toggles
- **Reload Functionality** - Refresh configuration from server

## Next Steps

- [Payment Flow](payment-flow.md) - Understanding Bitcoin payment processing
- [Using the API](using-api.md) - Making API requests to your node
- [Models & Pricing](models-pricing.md) - Configuring model pricing and fees
- [API Reference](../api/overview.md) - Complete API documentation
