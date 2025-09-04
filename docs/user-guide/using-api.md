# Using the API

This guide shows how to integrate Routstr with your applications using various programming languages and tools.

## API Compatibility

Routstr maintains full compatibility with the OpenAI API, meaning:

- Existing OpenAI client libraries work without modification
- Only the base URL and API key need to change
- All parameters and responses match OpenAI's format

## Basic Setup

### Python

Using the official OpenAI Python library:

```python
from openai import OpenAI

# Initialize client with Routstr endpoint
client = OpenAI(
    api_key="sk-...",
    base_url="https://api.routstr.com/v1"
)

# Use exactly like OpenAI
response = client.chat.completions.create(
    model="gpt-3.5-turbo",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello!"}
    ]
)

print(response.choices[0].message.content)
```

### JavaScript/TypeScript

Using the official OpenAI Node.js library:

```javascript
import OpenAI from 'openai';

// Initialize client
const openai = new OpenAI({
    apiKey: 'sk-...',
    baseURL: 'https://api.routstr.com/v1'
});

// Make a request
async function main() {
    const completion = await openai.chat.completions.create({
        model: 'gpt-3.5-turbo',
        messages: [
            { role: 'system', content: 'You are a helpful assistant.' },
            { role: 'user', content: 'Hello!' }
        ]
    });
    
    console.log(completion.choices[0].message.content);
}

main();
```

### cURL

Direct HTTP requests:

```bash
curl https://api.routstr.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-..." \
  -d '{
    "model": "gpt-3.5-turbo",
    "messages": [
      {"role": "user", "content": "Hello!"}
    ]
  }'
```

## Common Use Cases

### Chat Completions

Standard chat with conversation history:

```python
messages = []

def chat(user_input):
    # Add user message
    messages.append({"role": "user", "content": user_input})
    
    # Get AI response
    response = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=messages,
        temperature=0.7,
        max_tokens=150
    )
    
    # Add AI response to history
    ai_message = response.choices[0].message
    messages.append({"role": "assistant", "content": ai_message.content})
    
    return ai_message.content

# Usage
print(chat("What's the weather like?"))
print(chat("How should I dress?"))  # Maintains context
```

### Streaming Responses

For real-time output:

```python
stream = client.chat.completions.create(
    model="gpt-3.5-turbo",
    messages=[{"role": "user", "content": "Write a short story"}],
    stream=True
)

for chunk in stream:
    if chunk.choices[0].delta.content is not None:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

### Function Calling

Using OpenAI's function calling feature:

```python
tools = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get current weather",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {"type": "string"},
                "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]}
            },
            "required": ["location"]
        }
    }
}]

response = client.chat.completions.create(
    model="gpt-4",
    messages=[{"role": "user", "content": "What's the weather in Tokyo?"}],
    tools=tools,
    tool_choice="auto"
)

# Check if function was called
if response.choices[0].message.tool_calls:
    tool_call = response.choices[0].message.tool_calls[0]
    print(f"Function: {tool_call.function.name}")
    print(f"Arguments: {tool_call.function.arguments}")
```

### Embeddings

Generate text embeddings:

```python
response = client.embeddings.create(
    model="text-embedding-3-small",
    input="The quick brown fox jumps over the lazy dog"
)

embedding = response.data[0].embedding
print(f"Embedding dimension: {len(embedding)}")
```

### Image Generation

Create images with DALL-E:

```python
response = client.images.generate(
    model="dall-e-3",
    prompt="A futuristic city with flying cars",
    size="1024x1024",
    quality="standard",
    n=1
)

image_url = response.data[0].url
print(f"Image URL: {image_url}")
```

### Audio Transcription

Convert speech to text:

```python
with open("audio.mp3", "rb") as audio_file:
    response = client.audio.transcriptions.create(
        model="whisper-1",
        file=audio_file,
        response_format="text"
    )
    
print(response.text)
```

## Error Handling

### Balance Errors

Handle insufficient balance gracefully:

```python
try:
    response = client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": "Hello"}]
    )
except Exception as e:
    if "insufficient_balance" in str(e):
        print("Low balance! Please top up your API key.")
        # Implement top-up logic
    else:
        raise
```

### Rate Limiting

Implement exponential backoff:

```python
import time
from typing import Optional

def make_request_with_retry(
    func, 
    max_retries: int = 3,
    initial_delay: float = 1.0
) -> Optional[any]:
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            if "rate_limit" in str(e) and attempt < max_retries - 1:
                delay = initial_delay * (2 ** attempt)
                print(f"Rate limited. Waiting {delay}s...")
                time.sleep(delay)
            else:
                raise
    return None
```

### Connection Errors

Handle network issues:

```python
import httpx

# Configure timeout and retries
client = OpenAI(
    api_key="sk-...",
    base_url="https://your-node.com/v1",
    timeout=httpx.Timeout(60.0, connect=5.0),
    max_retries=2
)
```

## Advanced Features

### Using Tor

Route requests through Tor for privacy:

```python
import httpx

# Configure Tor proxy
proxies = {
    "http://": "socks5://127.0.0.1:9050",
    "https://": "socks5://127.0.0.1:9050"
}

http_client = httpx.Client(proxies=proxies)

client = OpenAI(
    api_key="sk-...",
    base_url="http://your-onion-address.onion/v1",
    http_client=http_client
)
```

### Custom Headers

Add custom headers if needed:

```python
import httpx

class CustomClient(httpx.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.headers["X-Custom-Header"] = "value"

client = OpenAI(
    api_key="sk-...",
    base_url="https://your-node.com/v1",
    http_client=CustomClient()
)
```

### Azure OpenAI compatibility

To use Azure OpenAI through Routstr with minimal changes:

- Set `UPSTREAM_BASE_URL` to your Azure deployments URL, for example: `https://<resource>.openai.azure.com/openai/deployments/<deployment>`
- Set `CHAT_COMPLETIONS_API_VERSION=2024-05-01-preview`

When this env var is set, Routstr automatically appends `api-version=2024-05-01-preview` to all upstream `/chat/completions` requests.

### Async Operations

For high-performance applications:

```python
import asyncio
from openai import AsyncOpenAI

async_client = AsyncOpenAI(
    api_key="sk-...",
    base_url="https://your-node.com/v1"
)

async def process_messages(messages):
    tasks = []
    for msg in messages:
        task = async_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": msg}]
        )
        tasks.append(task)
    
    responses = await asyncio.gather(*tasks)
    return [r.choices[0].message.content for r in responses]

# Run async
messages = ["Hello", "How are you?", "What's 2+2?"]
results = asyncio.run(process_messages(messages))
```

## Best Practices

### 1. Environment Variables

Never hardcode API keys:

```python
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.getenv("ROUTSTR_API_KEY"),
    base_url=os.getenv("ROUTSTR_BASE_URL", "https://api.routstr.com/v1")
)
```

### 2. Error Logging

Implement comprehensive logging:

```python
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    response = client.chat.completions.create(...)
    logger.info(f"Request successful. Tokens used: {response.usage.total_tokens}")
except Exception as e:
    logger.error(f"API request failed: {e}")
    raise
```

### 3. Cost Tracking

Monitor your usage:

```python
class UsageTracker:
    def __init__(self):
        self.total_tokens = 0
        self.total_requests = 0
    
    def track(self, response):
        self.total_tokens += response.usage.total_tokens
        self.total_requests += 1
        
        # Estimate cost (example rates)
        cost_per_1k = 0.002  # $0.002 per 1K tokens
        estimated_cost = (self.total_tokens / 1000) * cost_per_1k
        
        logger.info(f"Total usage: {self.total_tokens} tokens, "
                   f"${estimated_cost:.4f} (~{estimated_cost * 50000:.0f} sats)")

tracker = UsageTracker()
response = client.chat.completions.create(...)
tracker.track(response)
```

### 4. Caching Responses

Reduce costs with intelligent caching:

```python
import hashlib
import json
from functools import lru_cache

@lru_cache(maxsize=100)
def cached_completion(prompt: str, model: str = "gpt-3.5-turbo"):
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0  # Deterministic for caching
    )
    return response.choices[0].message.content

# Repeated calls with same prompt use cache
result1 = cached_completion("What is 2+2?")
result2 = cached_completion("What is 2+2?")  # From cache, no API call
```

## Testing

### Mock Responses

For development without spending sats:

```python
class MockOpenAI:
    class Completions:
        def create(self, **kwargs):
            return type('Response', (), {
                'choices': [type('Choice', (), {
                    'message': type('Message', (), {
                        'content': 'Mock response'
                    })()
                })],
                'usage': type('Usage', (), {
                    'total_tokens': 10
                })()
            })()
    
    def __init__(self):
        self.chat = type('Chat', (), {
            'completions': self.Completions()
        })()

# Use mock in tests
if os.getenv('TESTING'):
    client = MockOpenAI()
else:
    client = OpenAI(...)
```

### Integration Tests

Test your Routstr integration:

```python
def test_routstr_connection():
    try:
        # Test models endpoint
        models = client.models.list()
        assert len(models.data) > 0
        
        # Test simple completion
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": "test"}],
            max_tokens=5
        )
        assert response.choices[0].message.content
        
        print("✅ Routstr integration working!")
        return True
    except Exception as e:
        print(f"❌ Integration test failed: {e}")
        return False
```

## Troubleshooting

### Common Issues

**SSL Certificate Errors**

```python
# For development only - not for production!
import ssl
import httpx

client = OpenAI(
    api_key="sk-...",
    base_url="https://localhost:8000/v1",
    http_client=httpx.Client(verify=False)
)
```

**Timeout Issues**

```python
# Increase timeout for slow connections
client = OpenAI(
    api_key="sk-...",
    base_url="https://your-node.com/v1",
    timeout=httpx.Timeout(120.0)  # 2 minutes
)
```

**Debugging Requests**

```python
import logging
import httpx

# Enable debug logging
logging.basicConfig(level=logging.DEBUG)
httpx_logger = logging.getLogger("httpx")
httpx_logger.setLevel(logging.DEBUG)
```

## Next Steps

- [Admin Dashboard](admin-dashboard.md) - Manage your account
- [Models & Pricing](models-pricing.md) - Understanding costs
- [API Reference](../api/overview.md) - Technical details
