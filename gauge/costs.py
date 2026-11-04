"""Model prices and cost math.

Prices are US dollars per 1,000 tokens for on demand use in us-east-1.
Prices checked: 2026-11-03 from the AWS Bedrock pricing page. Check them again before
quoting cost numbers anywhere, since AWS changes them.
"""

PRICES_PER_1K = {
    # model key: (input, output)
    "nova-micro": (0.000035, 0.00014),
    "nova-lite": (0.00006, 0.00024),
    "nova-pro": (0.0008, 0.0032),
    "claude-haiku": (0.001, 0.005),
    "llama-3.1-8b": (0.00022, 0.00022),
    "mistral-small": (0.001, 0.003),
    # Local models run on your own machine, so there is no per token price.
    "ollama-llama3.1": (0.0, 0.0),
    "ollama-qwen2.5": (0.0, 0.0),
    # SageMaker is billed by instance hour. The provider prices each call from the hourly rate, so this is unused.
    "sagemaker-qwen2.5-7b": (0.0, 0.0),
}


def cost_usd(model_key: str, input_tokens: int, output_tokens: int) -> float:
    if model_key not in PRICES_PER_1K:
        raise KeyError(f"No price for model '{model_key}'. Add it to PRICES_PER_1K.")
    price_in, price_out = PRICES_PER_1K[model_key]
    return input_tokens / 1000 * price_in + output_tokens / 1000 * price_out
