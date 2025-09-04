# Custom Pricing

This guide covers advanced pricing strategies and customization options for Routstr Core.

## Pricing Models Overview

Routstr supports three pricing models:

1. **Fixed Pricing** - Simple per-request fee
2. **Token-Based Pricing** - Charge per input/output token
3. **Model-Based Pricing** - Dynamic pricing from models.json

## Model-Based Pricing

### Configuration

Enable model-based pricing:

```bash
# .env
MODEL_BASED_PRICING=true
MODELS_PATH=/app/config/models.json
EXCHANGE_FEE=1.005         # 0.5% exchange fee
UPSTREAM_PROVIDER_FEE=1.05  # 5% provider margin
```

### Custom Models File

Create a `models.json` with your pricing:

```json
{
  "models": [
    {
      "id": "gpt-4",
      "name": "GPT-4",
      "description": "Advanced reasoning model",
      "context_length": 8192,
      "pricing": {
        "prompt": "0.03",        // USD per 1K tokens
        "completion": "0.06",    // USD per 1K tokens
        "request": "0.0001",     // Fixed per-request fee
        "image": "0",            // For multimodal models
        "web_search": "0.005",   // Additional features
        "internal_reasoning": "0.01"
      },
      "supported_features": [
        "function_calling",
        "vision",
        "json_mode"
      ],
      "deprecation_date": null,
      "replacement_model": null
    },
    {
      "id": "custom-model",
      "name": "Custom Fine-tuned Model",
      "pricing": {
        "prompt": "0.001",
        "completion": "0.002",
        "request": "0.00005"
      },
      "minimum_charge": "0.0001"  // Minimum charge per request
    }
  ],
  "default_pricing": {
    "prompt": "0.002",
    "completion": "0.002",
    "request": "0"
  }
}
```

### Dynamic Price Updates

Automatically fetch prices from providers:

```python
# scripts/update_prices.py
import asyncio
import httpx
import json

async def fetch_openrouter_models():
    """Fetch current model pricing from OpenRouter."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://openrouter.ai/api/v1/models"
        )
        return response.json()

async def update_models_json():
    """Update local models.json with latest prices."""
    data = await fetch_openrouter_models()
    
    models = []
    for model in data['data']:
        models.append({
            "id": model['id'],
            "name": model['name'],
            "pricing": {
                "prompt": model['pricing']['prompt'],
                "completion": model['pricing']['completion'],
                "request": model['pricing'].get('request', '0')
            },
            "context_length": model.get('context_length', 4096)
        })
    
    with open('models.json', 'w') as f:
        json.dump({"models": models}, f, indent=2)

# Run periodically
if __name__ == "__main__":
    asyncio.run(update_models_json())
```

## Token-Based Pricing

### Configuration

Set up token-based pricing:

```bash
# .env
MODEL_BASED_PRICING=false
COST_PER_REQUEST=1              # 1 sat base fee
COST_PER_1K_INPUT_TOKENS=5      # 5 sats per 1K input
COST_PER_1K_OUTPUT_TOKENS=15    # 15 sats per 1K output
```

### Custom Token Counting

Override default token counting:

```python
from tiktoken import encoding_for_model

class CustomTokenCounter:
    def __init__(self):
        self.encodings = {}
    
    def count_tokens(
        self, 
        text: str, 
        model: str
    ) -> int:
        """Custom token counting logic."""
        # Cache encodings
        if model not in self.encodings:
            try:
                self.encodings[model] = encoding_for_model(model)
            except:
                # Fallback encoding
                self.encodings[model] = encoding_for_model("gpt-3.5-turbo")
        
        encoding = self.encodings[model]
        
        # Special handling for certain content
        if text.startswith("```"):
            # Code blocks might need special handling
            tokens = encoding.encode(text)
            return len(tokens) * 1.1  # 10% markup for code
        
        return len(encoding.encode(text))
```

## Advanced Pricing Strategies

### Time-Based Pricing

Implement peak/off-peak pricing:

```python
from datetime import datetime
import pytz

class TimeBased PricingStrategy:
    def __init__(self):
        self.timezone = pytz.timezone('US/Eastern')
        self.peak_hours = [(9, 17)]  # 9 AM - 5 PM
        self.peak_multiplier = 1.5
        self.weekend_discount = 0.8
    
    def get_price_multiplier(self) -> float:
        """Calculate price multiplier based on time."""
        now = datetime.now(self.timezone)
        
        # Weekend discount
        if now.weekday() >= 5:  # Saturday or Sunday
            return self.weekend_discount
        
        # Peak hours surcharge
        hour = now.hour
        for start, end in self.peak_hours:
            if start <= hour < end:
                return self.peak_multiplier
        
        # Off-peak standard pricing
        return 1.0
    
    def apply_to_cost(self, base_cost: int) -> int:
        """Apply time-based pricing to cost."""
        multiplier = self.get_price_multiplier()
        return int(base_cost * multiplier)
```

### Model-Specific Surcharges

Add custom fees for specific models:

```python
class ModelSurchargeStrategy:
    def __init__(self):
        self.surcharges = {
            "gpt-4-turbo": 1.1,      # 10% premium
            "claude-3-opus": 1.15,    # 15% premium
            "dall-e-3-hd": 1.25,      # 25% premium for HD
        }
        
        self.discounts = {
            "gpt-3.5-turbo": 0.95,    # 5% discount
            "deprecated-model": 0.8,   # 20% discount
        }
    
    def get_model_multiplier(self, model: str) -> float:
        """Get price multiplier for model."""
        if model in self.surcharges:
            return self.surcharges[model]
        elif model in self.discounts:
            return self.discounts[model]
        return 1.0
```

### Geographic Pricing

Adjust pricing based on client location:

```python
import geoip2.database

class GeographicPricingStrategy:
    def __init__(self):
        self.reader = geoip2.database.Reader('GeoLite2-Country.mmdb')
        self.country_multipliers = {
            'US': 1.0,
            'GB': 1.0,
            'DE': 1.0,
            'IN': 0.7,  # 30% discount
            'BR': 0.8,  # 20% discount
            'NG': 0.6,  # 40% discount
        }
        self.default_multiplier = 0.9
    
    def get_country_multiplier(self, ip_address: str) -> float:
        """Get price multiplier based on country."""
        try:
            response = self.reader.country(ip_address)
            country_code = response.country.iso_code
            return self.country_multipliers.get(
                country_code, 
                self.default_multiplier
            )
        except:
            return 1.0  # Default pricing if lookup fails
```

## Cost Calculation Pipeline

### Implementing Custom Calculator

```python
from abc import ABC, abstractmethod

class CostCalculator(ABC):
    @abstractmethod
    async def calculate(
        self,
        request_data: dict,
        usage_data: dict,
        context: dict
    ) -> CostResult:
        pass

class CompositeCostCalculator(CostCalculator):
    """Combine multiple pricing strategies."""
    
    def __init__(self):
        self.strategies = [
            BaseCostCalculator(),
            TimeBasedPricingStrategy(),
            ModelSurchargeStrategy(),
            GeographicPricingStrategy()
        ]
    
    async def calculate(
        self,
        request_data: dict,
        usage_data: dict,
        context: dict
    ) -> CostResult:
        # Start with base cost
        base_cost = await self.strategies[0].calculate(
            request_data, usage_data, context
        )
        
        # Apply each strategy
        final_cost = base_cost.total_msats
        breakdown = {"base": base_cost.total_msats}
        
        for strategy in self.strategies[1:]:
            multiplier = await strategy.get_multiplier(context)
            adjustment = final_cost * (multiplier - 1)
            final_cost += adjustment
            breakdown[strategy.__class__.__name__] = adjustment
        
        return CostResult(
            total_msats=int(final_cost),
            breakdown=breakdown
        )
```

### Integration with Routstr

```python
# In routstr/payment/cost_calculation.py
async def calculate_request_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    request_type: str,
    context: dict
) -> CostData:
    """Enhanced cost calculation with custom strategies."""
    
    # Use custom calculator if configured
    if os.getenv("USE_CUSTOM_PRICING", "false").lower() == "true":
        calculator = CompositeCostCalculator()
        result = await calculator.calculate(
            request_data={
                "model": model,
                "type": request_type
            },
            usage_data={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens
            },
            context=context
        )
        return result
    
    # Fall back to standard calculation
    return standard_calculate_cost(...)
```

## Monitoring Pricing

### Price Analytics

Track pricing effectiveness:

```python
class PricingAnalytics:
    async def analyze_pricing(
        self,
        start_date: datetime,
        end_date: datetime
    ):
        """Analyze pricing performance."""
        # Average cost per request by model
        model_costs = await self.get_average_costs_by_model(
            start_date, end_date
        )
        
        # Revenue by pricing strategy
        strategy_revenue = await self.get_revenue_by_strategy(
            start_date, end_date
        )
        
        # Price elasticity
        elasticity = await self.calculate_price_elasticity()
        
        return {
            "model_costs": model_costs,
            "strategy_revenue": strategy_revenue,
            "price_elasticity": elasticity,
            "recommendations": self.generate_recommendations(
                model_costs, elasticity
            )
        }
```

### A/B Testing Prices

Test different pricing strategies:

```python
class PricingExperiment:
    def __init__(self):
        self.experiments = {
            "exp_001": {
                "name": "10% discount test",
                "group_a": {"multiplier": 1.0},
                "group_b": {"multiplier": 0.9},
                "allocation": 0.5  # 50/50 split
            }
        }
    
    def assign_group(self, api_key_id: int) -> str:
        """Assign API key to experiment group."""
        # Consistent assignment based on key ID
        import hashlib
        hash_value = int(hashlib.md5(
            str(api_key_id).encode()
        ).hexdigest()[:8], 16)
        
        return "group_b" if (hash_value % 100) < 50 else "group_a"
    
    def get_experiment_multiplier(
        self, 
        api_key_id: int,
        experiment_id: str
    ) -> float:
        """Get price multiplier for experiment."""
        experiment = self.experiments.get(experiment_id)
        if not experiment:
            return 1.0
        
        group = self.assign_group(api_key_id)
        return experiment[group]["multiplier"]
```

## Configuration Examples

### Enterprise Pricing

```json
{
  "models": [
    {
      "id": "gpt-4-enterprise",
      "name": "GPT-4 Enterprise",
      "pricing": {
        "prompt": "0.02",
        "completion": "0.04"
      },
      "minimum_commitment": "1000",  // $1000/month minimum
      "sla": {
        "uptime": "99.9%",
        "support_response": "1 hour",
        "dedicated_capacity": true
      }
    }
  ],
  "enterprise_features": {
    "priority_queue": true,
    "custom_models": true,
    "audit_logs": true,
    "sso": true
  }
}
```

### Budget-Friendly Options

```json
{
  "models": [
    {
      "id": "gpt-3.5-turbo-budget",
      "name": "GPT-3.5 Turbo Budget",
      "pricing": {
        "prompt": "0.0005",
        "completion": "0.001"
      },
      "restrictions": {
        "max_tokens_per_request": 1000,
        "requests_per_minute": 10,
        "peak_hours_blocked": true
      }
    }
  ],
  "prepaid_packages": [
    {
      "name": "Starter Pack",
      "price_usd": 10,
      "tokens_included": 10000000,
      "expires_days": 30
    }
  ]
}
```

## Troubleshooting

### Price Calculation Issues

```python
# Debug pricing
async def debug_price_calculation(
    model: str,
    tokens: dict,
    api_key_id: int
):
    """Debug price calculation step by step."""
    print(f"Model: {model}")
    print(f"Tokens: {tokens}")
    
    # Base price
    base_price = get_model_price(model)
    print(f"Base price: {base_price}")
    
    # Token cost
    token_cost = calculate_token_cost(base_price, tokens)
    print(f"Token cost: {token_cost}")
    
    # Strategies
    strategies = get_active_strategies()
    for strategy in strategies:
        multiplier = await strategy.get_multiplier(api_key_id)
        print(f"{strategy.name}: {multiplier}x")
    
    # Final cost
    final_cost = apply_all_strategies(token_cost, api_key_id)
    print(f"Final cost: {final_cost} msats")
    
    return final_cost
```

### Common Issues

1. **Prices Not Updating**
   - Check `MODELS_PATH` is correct
   - Verify file permissions
   - Check background task logs

2. **Wrong Currency Conversion**
   - Verify BTC/USD rate source
   - Check `EXCHANGE_FEE` setting
   - Monitor rate update frequency

3. **Discounts Not Applied**
   - Verify strategy configuration
   - Check API key metadata
   - Review transaction history

## Best Practices

1. **Transparent Pricing**
   - Publish pricing clearly
   - Show cost breakdowns
   - Notify of price changes

2. **Fair Pricing**
   - Regular competitive analysis
   - Consider user feedback
   - Offer budget options

3. **Performance**
   - Cache price calculations
   - Optimize database queries
   - Monitor calculation time

## Next Steps

- [Migrations](migrations.md) - Database migration guide
- [API Endpoints](../api/endpoints.md) - Pricing endpoints
- [Monitoring](../user-guide/admin-dashboard.md) - Track pricing metrics
