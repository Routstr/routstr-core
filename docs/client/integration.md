# Using the API

Routstr is **OpenAI-compatible**. Almost any AI application, SDK, or tool that supports custom endpoints will work out of the box. Just change two things:

```
BASE_URL  →  https://api.routstr.com/v1
API_KEY   →  sk-... or cashuA...
```

**Both work as API keys:**
- `sk-7f8e9d...` — Session key (from Lightning invoice or Cashu import)
- `cashuA3s8j...` — Raw Cashu token (use directly from your wallet)

If the app lets you set a base URL and API key, you're good to go.

---

## Quick Setup Examples

### OpenAI SDK (Python/JS)

```python
client = OpenAI(base_url="https://api.routstr.com/v1", api_key="sk-...")  # or any provider's URL
```

### Claude Code

```bash
export ANTHROPIC_BASE_URL=https://api.routstr.com/v1
export ANTHROPIC_AUTH_TOKEN=sk-...
```

### Any OpenAI-compatible app

Look for "Custom API endpoint", "Base URL", or "OpenAI-compatible" in settings. Paste the URL and key.

---

## Detailed Examples

### Python (Official SDK)

```python
from openai import OpenAI

# 1. Initialize with Routstr URL and your funded key
client = OpenAI(
    base_url="https://api.routstr.com/v1",
    api_key="sk-7f8e9d..." 
)

# 2. Call the API normally
response = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Hello!"}],
    stream=True
)

for chunk in response:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")
```

### Node.js

```javascript
import OpenAI from 'openai';

// You can use a session key OR a raw Cashu token directly
const openai = new OpenAI({
  baseURL: 'https://api.routstr.com/v1',
  apiKey: 'cashuA3s8jKx9...', // or 'sk-7f8e9d...'
});

async function main() {
  const completion = await openai.chat.completions.create({
    messages: [{ role: 'user', content: 'Say this is a test' }],
    model: 'gpt-3.5-turbo',
  });

  console.log(completion.choices[0]);
}

main();
```

### cURL

```bash
# Works with session key or raw Cashu token
curl https://api.routstr.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer cashuA3s8jKx9..." \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

---

## Error Handling

### Insufficient Balance (402 Payment Required)
If your session runs out of funds, the API will return a `402` error.

```json
{
  "error": {
    "message": "Insufficient balance. Current: 1000 msat, Required: 5000 msat",
    "type": "insufficient_balance",
    "code": 402
  }
}
```

**Action**: Top up your key using the `/lightning/invoice` (topup purpose) or `/v1/balance/topup` endpoints.

### Rate Limiting
Routstr passes through rate limits from the upstream provider. Handle `429 Too Many Requests` with standard exponential backoff.

---

## Advanced: Tor Access

If the node is running as a hidden service, use a SOCKS5 proxy (like `127.0.0.1:9050`).

**Python:**
```python
import httpx
from openai import OpenAI

proxy_mounts = {
    "http://": httpx.HTTPTransport(proxy="socks5://127.0.0.1:9050"),
    "https://": httpx.HTTPTransport(proxy="socks5://127.0.0.1:9050"),
}

client = OpenAI(
    base_url="http://verylongonionaddress.onion/v1",
    api_key="sk-...",
    http_client=httpx.Client(mounts=proxy_mounts),
)
```
