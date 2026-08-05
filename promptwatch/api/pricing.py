# Token costs in USD per 1M tokens (input, output)
# Updated 2025-06 — update periodically or fetch from provider APIs
PRICING = {
    "openai": {
        "gpt-4o":              (2.50, 10.00),
        "gpt-4o-mini":         (0.15, 0.60),
        "gpt-4-turbo":         (10.00, 30.00),
        "gpt-4":               (30.00, 60.00),
        "gpt-3.5-turbo":       (0.50, 1.50),
        "text-embedding-3-small": (0.02, 0.0),
        "text-embedding-3-large": (0.13, 0.0),
    },
    "anthropic": {
        "claude-sonnet-4-6":   (3.00, 15.00),
        "claude-opus-4-8":     (15.00, 75.00),
        "claude-haiku-4-5-20251001": (0.80, 4.00),
        "claude-3-5-sonnet-20241022": (3.00, 15.00),
        "claude-3-5-haiku-20241022":  (0.80, 4.00),
        "claude-3-opus-20240229":     (15.00, 75.00),
    },
    "google": {
        "gemini-2.0-flash":    (0.075, 0.30),
        "gemini-1.5-pro":      (1.25, 5.00),
        "gemini-1.5-flash":    (0.075, 0.30),
    },
    "mistral": {
        "mistral-large-latest": (2.00, 6.00),
        "mistral-small-latest": (0.20, 0.60),
        "open-mistral-7b":     (0.25, 0.25),
    },
}


def compute_cost(provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> float:
    provider_prices = PRICING.get(provider.lower(), {})
    # Try exact match, then prefix match
    prices = provider_prices.get(model)
    if not prices:
        for key in provider_prices:
            if model.startswith(key) or key.startswith(model):
                prices = provider_prices[key]
                break
    if not prices:
        return 0.0
    input_price, output_price = prices
    cost = (prompt_tokens * input_price + completion_tokens * output_price) / 1_000_000
    return round(cost, 8)
