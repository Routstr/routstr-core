# Models & Pricing

Understanding how Routstr calculates costs is essential for managing your API usage efficiently. This guide explains the pricing models and how to configure them.

## Pricing Models

Routstr supports three pricing models:

### 1. Fixed Pricing

Simple per-request charging:

```bash
MODEL_BASED_PRICING=false
COST_PER_REQUEST=10  # 10 sats per request
```

**Best for:**

- Uniform API usage
- Simple applications
- Predictable costs

### 2. Token-Based Pricing

Charge based on actual token usage:

```bash
MODEL_BASED_PRICING=false
COST_PER_REQUEST=1              # 1 sat base fee
COST_PER_1K_INPUT_TOKENS=5      # 5 sats per 1K input
COST_PER_1K_OUTPUT_TOKENS=15    # 15 sats per 1K output
```

**Best for:**

- Varied request sizes
- Fair usage billing
- Cost optimization

### 3. Model-Based Pricing

Dynamic pricing based on model costs:

```bash
MODEL_BASED_PRICING=true
EXCHANGE_FEE=1.005      # 0.5% exchange fee
UPSTREAM_PROVIDER_FEE=1.05  # 5% provider fee
```

**Best for:**

- Multiple models
- Market-based pricing
- Automatic updates

## Model Configuration

### Default Models

Routstr includes pricing for popular models:

| Model | Input ($/1K) | Output ($/1K) | Context | Notes |
|-------|--------------|---------------|---------|-------|
| gpt-3.5-turbo | $0.0015 | $0.002 | 16K | Fast, economical |
| gpt-4 | $0.03 | $0.06 | 8K | Advanced reasoning |
| gpt-4-turbo | $0.01 | $0.03 | 128K | Large context |
| claude-3-opus | $0.015 | $0.075 | 200K | Best quality |
| claude-3-sonnet | $0.003 | $0.015 | 200K | Balanced |
| llama-2-70b | $0.0007 | $0.0009 | 4K | Open source |

### Custom Models File

Create `models.json` to override defaults:

```json
{
  "models": [
    {
      "id": "gpt-4-vision",
      "name": "GPT-4 Vision",
      "pricing": {
        "prompt": "0.00003",
        "completion": "0.00006",
        "request": "0",
        "image": "0.00255"
      },
      "context_length": 128000,
      "supports_vision": true
    },
    {
      "id": "custom-model",
      "name": "My Custom Model",
      "pricing": {
        "prompt": "0.001",
        "completion": "0.002",
        "request": "0.0001"
      },
      "context_length": 8192
    }
  ]
}
```

### Auto-updating Models

Fetch latest models from OpenRouter:

```bash
# Update models from API
python scripts/models_meta.py

# Or manually
curl https://openrouter.ai/api/v1/models > models.json
```

## Cost Calculation

### Understanding the Formula

```
Base Cost = (Input Tokens × Input Rate) + (Output Tokens × Output Rate) + Request Fee

Bitcoin Price = Current BTC/USD rate (e.g., $50,000)
Sats Cost = (Base Cost / Bitcoin Price) × 100,000,000

Final Cost = Sats Cost × Exchange Fee × Provider Fee
```

### Example Calculations

**Example 1: Simple Chat (gpt-3.5-turbo)**

```
Input: 50 tokens
Output: 150 tokens
Model rates: $0.0015/1K input, $0.002/1K output

USD Cost = (50/1000 × 0.0015) + (150/1000 × 0.002)
         = $0.000075 + $0.0003
         = $0.000375

At $50,000/BTC: 0.75 sats
With 5.5% total fees: 0.79 sats
```

**Example 2: Large Context (gpt-4)**

```
Input: 2,000 tokens
Output: 500 tokens
Model rates: $0.03/1K input, $0.06/1K output

USD Cost = (2000/1000 × 0.03) + (500/1000 × 0.06)
         = $0.06 + $0.03
         = $0.09

At $50,000/BTC: 180 sats
With 5.5% total fees: 190 sats
```

**Example 3: Image Generation (dall-e-3)**

```
Model: dall-e-3
Size: 1024x1024
Quality: standard
Cost: $0.04 per image

At $50,000/BTC: 80 sats
With 5.5% fees: 84 sats
```

## Fee Structure

### Exchange Fee

Covers Bitcoin/USD conversion costs:

```bash
EXCHANGE_FEE=1.005  # 0.5% default
```

Factors:

- Exchange rate volatility
- Conversion costs
- Price update frequency

### Provider Fee

Node operator's margin:

```bash
UPSTREAM_PROVIDER_FEE=1.05  # 5% default
```

Covers:

- Infrastructure costs
- Maintenance
- Support
- Profit margin

### Calculating Total Fees

```
Total Multiplier = EXCHANGE_FEE × UPSTREAM_PROVIDER_FEE
Example: 1.005 × 1.05 = 1.05525 (5.525% total)
```

## Special Pricing

### Image Models

Image generation uses per-image pricing:

| Model | Size | Quality | Price |
|-------|------|---------|-------|
| dall-e-2 | 256x256 | - | $0.016 |
| dall-e-2 | 512x512 | - | $0.018 |
| dall-e-2 | 1024x1024 | - | $0.02 |
| dall-e-3 | 1024x1024 | standard | $0.04 |
| dall-e-3 | 1024x1024 | hd | $0.08 |
| dall-e-3 | 1024x1792 | standard | $0.08 |
| dall-e-3 | 1024x1792 | hd | $0.12 |

### Audio Models

Audio pricing by duration:

| Model | Type | Price |
|-------|------|-------|
| whisper-1 | Transcription | $0.006/minute |
| whisper-1 | Translation | $0.006/minute |
| tts-1 | Text-to-speech | $0.015/1K chars |
| tts-1-hd | HD speech | $0.03/1K chars |

### Embedding Models

Lower costs for embeddings:

| Model | Price/1K tokens |
|-------|-----------------|
| text-embedding-3-small | $0.00002 |
| text-embedding-3-large | $0.00013 |
| text-embedding-ada-002 | $0.0001 |

## Monitoring Costs

### Per-Request Tracking

Each API response includes usage data:

```json
{
  "usage": {
    "prompt_tokens": 50,
    "completion_tokens": 150,
    "total_tokens": 200
  },
  "x-routstr-cost": {
    "sats": 79,
    "usd": 0.000375,
    "breakdown": {
      "prompt_cost": 15,
      "completion_cost": 60,
      "fees": 4
    }
  }
}
```

### Daily Summaries

View in admin dashboard:

- Total requests
- Token usage by model
- Cost distribution
- Trending patterns

### Cost Alerts

Set up notifications:

```python
# Example monitoring script
def check_daily_spend(api_key):
    balance_start = get_balance(api_key, "00:00")
    balance_now = get_balance(api_key)
    spent = balance_start - balance_now
    
    if spent > DAILY_LIMIT:
        send_alert(f"Daily spend exceeded: {spent} sats")
```

## Optimization Strategies

### Model Selection

Choose the right model for each task:

| Task | Recommended Model | Why |
|------|-------------------|-----|
| Simple Q&A | gpt-3.5-turbo | Fast, cheap, sufficient |
| Code generation | gpt-4 | Better reasoning |
| Summarization | claude-3-haiku | Good balance |
| Creative writing | claude-3-opus | Best quality |
| Embeddings | text-embedding-3-small | Optimized for vectors |

### Prompt Engineering

Reduce costs with efficient prompts:

```python
# Expensive
prompt = """
You are an AI assistant. Your task is to help users.
Please provide detailed, comprehensive answers.
Now, answer this question: What is 2+2?
"""

# Economical
prompt = "Calculate: 2+2"
```

### Caching Strategies

Implement smart caching:

```python
# Cache embedding results
@lru_cache(maxsize=1000)
def get_embedding(text):
    return client.embeddings.create(
        model="text-embedding-3-small",
        input=text
    )

# Cache common responses
COMMON_RESPONSES = {
    "greeting": "Hello! How can I help you?",
    "goodbye": "Goodbye! Have a great day!"
}
```

### Batch Processing

Process multiple items efficiently:

```python
# Instead of multiple calls
for item in items:
    response = client.chat.completions.create(...)

# Use single call with formatted prompt
prompt = "\n".join([f"{i+1}. {item}" for i, item in enumerate(items)])
response = client.chat.completions.create(
    messages=[{"role": "user", "content": f"Process these items:\n{prompt}"}]
)
```

## Custom Pricing Rules

### Time-Based Pricing

Implement off-peak discounts:

```python
def calculate_multiplier():
    hour = datetime.now().hour
    if 2 <= hour <= 6:  # 2 AM - 6 AM
        return 0.8  # 20% discount
    elif 18 <= hour <= 22:  # 6 PM - 10 PM
        return 1.2  # 20% premium
    return 1.0
```

### Model-Specific Rules

Custom pricing logic:

```python
def adjust_model_price(model, base_price):
    # Premium for latest models
    if "turbo" in model or "latest" in model:
        return base_price * 1.1
    
    # Discount for older models
    if "legacy" in model:
        return base_price * 0.8
    
    return base_price
```

## Pricing Transparency

### Public Pricing Page

Display current rates:

```html
<!-- Available at /pricing -->
<table>
  <tr>
    <th>Model</th>
    <th>Input (sats/1K)</th>
    <th>Output (sats/1K)</th>
  </tr>
  <!-- Dynamically generated from models.json -->
</table>
```

### Cost Estimation API

Provide cost estimates:

```bash
POST /v1/estimate
{
  "model": "gpt-4",
  "prompt_tokens": 500,
  "max_tokens": 200
}

Response:
{
  "estimated_cost_sats": 45,
  "breakdown": {
    "prompt": 30,
    "completion": 12,
    "fees": 3
  }
}
```

## Troubleshooting

### Pricing Mismatches

**Issue**: Costs don't match expectations

- Check current BTC/USD rate
- Verify fee settings
- Review model configuration

**Issue**: Models not found

- Update models.json
- Check model ID spelling
- Verify upstream support

### Fee Calculations

**Issue**: Fees seem too high

- Review EXCHANGE_FEE setting
- Check UPSTREAM_PROVIDER_FEE
- Calculate total multiplier

## Next Steps

- [API Reference](../api/overview.md) - Technical details
- [Custom Pricing](../advanced/custom-pricing.md) - Advanced configuration
- [Contributing](../contributing/setup.md) - Help improve Routstr
