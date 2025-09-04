# Admin Dashboard

The Routstr admin dashboard provides a web interface for managing your node, viewing balances, and handling withdrawals.

## Accessing the Dashboard

### URL Format

The admin dashboard is available at:

```
https://api.routstr.com/admin/
```

> **Important**: Always include the trailing slash (`/`) in the URL.

### Authentication

The dashboard is protected by a password set in the `ADMIN_PASSWORD` environment variable.

1. Navigate to `/admin/`
2. Enter the admin password
3. Click "Login"

The password is stored as a secure cookie for the session.

## Dashboard Overview

### Main Interface

The dashboard displays:

- **Node Information**
  - Node name and description
  - Version number
  - Public URLs (HTTP and Onion)
  - Supported Cashu mints

- **Statistics**
  - Total API keys
  - Active keys
  - Total balance across all keys
  - Recent activity

- **API Key List**
  - All keys with balances
  - Usage statistics
  - Management options

## Features

### Viewing API Keys

The main table shows all API keys with:

| Column | Description |
|--------|-------------|
| API Key | Masked key (first/last 4 chars) |
| Balance | Current balance in sats |
| Created | Creation timestamp |
| Last Used | Most recent API call |
| Total Spent | Lifetime usage |
| Status | Active/Expired/Disabled |

### Searching and Filtering

- **Search**: Find keys by partial match
- **Sort**: Click column headers to sort
- **Filter**: Show only active/expired keys
- **Export**: Download data as CSV

### Key Details

Click on any key to view:

- Full API key (masked by default)
- Complete transaction history
- Usage graphs
- Metadata (name, expiry, refund address)

## Balance Management

### Viewing Balances

Balances are displayed in multiple units:

- **Sats**: Standard satoshi units
- **mSats**: Millisatoshis (internal precision)
- **BTC**: Bitcoin decimal format
- **USD**: Approximate USD value

### Balance History

View balance changes over time:

```
Time         | Type      | Amount  | Balance | Description
-------------|-----------|---------|---------|-------------
12:34:56     | Deposit   | +10,000 | 10,000  | Token redemption
12:35:12     | Usage     | -154    | 9,846   | gpt-3.5-turbo call
12:36:45     | Usage     | -210    | 9,636   | gpt-4 call
```

## Withdrawals

### Manual Withdrawal

To withdraw funds from an API key:

1. Click "Withdraw" next to the key
2. Optionally specify amount (default: full balance)
3. Select target Cashu mint
4. Click "Generate Token"
5. Copy the eCash token
6. Redeem in your Cashu wallet

### Bulk Operations

For multiple withdrawals:

1. Select keys using checkboxes
2. Click "Bulk Actions" → "Withdraw"
3. Tokens are generated for each key
4. Download all tokens as text file

### Automatic Withdrawals

If configured with `RECEIVE_LN_ADDRESS`:

- Balances above threshold auto-convert to Lightning
- Sent to configured Lightning address
- View payout history in dashboard

## Node Configuration

### Viewing Settings

Current node configuration is displayed:

- Upstream provider URL
- Enabled features
- Pricing model
- Fee structure

### Models and Pricing

View supported models and their pricing:

| Model | Input $/1K | Output $/1K | Sats/1K |
|-------|------------|-------------|---------|
| gpt-3.5-turbo | $0.0015 | $0.002 | 3/4 |
| gpt-4 | $0.03 | $0.06 | 60/120 |
| dall-e-3 | - | - | 1000/image |

### Updating Configuration

> **Note**: Configuration changes require node restart.

To update settings:

1. Modify environment variables
2. Restart the node
3. Verify changes in dashboard

## Analytics

### Usage Statistics

View comprehensive usage data:

- **Requests per Day**: Line graph
- **Token Usage**: Stacked bar chart
- **Model Distribution**: Pie chart
- **Cost Analysis**: Breakdown by model

### Performance Metrics

Monitor node performance:

- Average response time
- Request success rate
- Upstream API latency
- Cache hit ratio

### Export Data

Export analytics data:

1. Select date range
2. Choose metrics
3. Click "Export"
4. Download as CSV/JSON

## Security Features

### Access Control

- Password protection
- Session timeout (configurable)
- IP allowlisting (optional)
- Audit logging

### Security Log

View security events:

```
2024-01-15 12:34:56 | Login Success | IP: 192.168.1.1
2024-01-15 12:35:12 | Withdrawal | Key: sk-****abcd | Amount: 5000
2024-01-15 12:40:00 | Session Timeout | IP: 192.168.1.1
```

### Best Practices

1. **Strong Password**: Use a long, random password
2. **HTTPS Only**: Always access via HTTPS
3. **Regular Monitoring**: Check logs frequently
4. **Limited Access**: Restrict dashboard access

## Troubleshooting

### Cannot Access Dashboard

**Issue**: 404 Not Found

- Ensure trailing slash: `/admin/`
- Check if admin routes are enabled

**Issue**: Unauthorized

- Verify `ADMIN_PASSWORD` is set
- Clear browser cookies
- Try incognito/private mode

### Display Issues

**Issue**: Broken Layout

- Clear browser cache
- Disable ad blockers
- Try different browser

**Issue**: Missing Data

- Check database connectivity
- Verify node is running
- Review error logs

### Withdrawal Problems

**Issue**: Token Generation Fails

- Check mint connectivity
- Verify sufficient balance
- Try different mint

**Issue**: Invalid Token

- Ensure complete token copy
- Check token hasn't expired
- Verify mint compatibility

## Advanced Features

### Custom Branding

Customize dashboard appearance:

```bash
# Environment variables
ADMIN_LOGO_URL=https://example.com/logo.png
ADMIN_THEME_COLOR=#FF6B00
ADMIN_CUSTOM_CSS=/path/to/custom.css
```

### API Access

Access admin functions programmatically:

```bash
# Get node stats
curl -X GET https://your-node.com/admin/api/stats \
  -H "X-Admin-Password: your-password"

# Export key data
curl -X GET https://your-node.com/admin/api/keys \
  -H "X-Admin-Password: your-password" \
  -H "Accept: application/json"
```

### Webhooks

Configure notifications:

```bash
ADMIN_WEBHOOK_URL=https://example.com/webhook
ADMIN_WEBHOOK_EVENTS=withdrawal,low_balance,error
```

## Dashboard Shortcuts

### Keyboard Navigation

- `Ctrl+K`: Quick search
- `Ctrl+R`: Refresh data
- `Ctrl+E`: Export current view
- `Escape`: Close modals

### Quick Actions

- Double-click to copy API key
- Right-click for context menu
- Drag to reorder columns
- Shift-click to select multiple

## Mobile Access

The dashboard is mobile-responsive:

- Touch-optimized controls
- Swipe navigation
- Compact view mode
- Offline capability

## Next Steps

- [Models & Pricing](models-pricing.md) - Configure pricing
- [API Reference](../api/overview.md) - Admin API endpoints
- [Advanced Configuration](../advanced/custom-pricing.md) - Advanced settings
