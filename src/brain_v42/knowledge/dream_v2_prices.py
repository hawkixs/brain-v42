"""Versioned standard Messages prices, deliberately separate from Batch prices.

These are the D1 reference rates, not a live provider-price measurement. The
caller must attest price verification before arming a paid run.
"""

from dataclasses import dataclass
from decimal import Decimal

MODEL = "claude-sonnet-5-5"
PRICING_VERSION = "sonnet-5.5-standard-v1"
INPUT_USD_PER_MILLION = Decimal("2")
OUTPUT_USD_PER_MILLION = Decimal("10")
CACHE_WRITE_MULTIPLIER = Decimal("1.25")
CACHE_READ_MULTIPLIER = Decimal("0.1")
MAX_OUTPUT_TOKENS = 4096


def _tokens(value: int) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("invalid_token_count")
    return value


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        for value in (
            self.input_tokens,
            self.cache_write_tokens,
            self.cache_read_tokens,
            self.output_tokens,
        ):
            _tokens(value)

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_write_tokens
            + self.cache_read_tokens
            + self.output_tokens
        )


def real_cost_usd(model: str, usage: Usage) -> Decimal:
    if model != MODEL:
        raise ValueError("unknown_model_price")
    return (
        INPUT_USD_PER_MILLION
        * (
            usage.input_tokens
            + usage.cache_write_tokens * CACHE_WRITE_MULTIPLIER
            + usage.cache_read_tokens * CACHE_READ_MULTIPLIER
        )
        + OUTPUT_USD_PER_MILLION * usage.output_tokens
    ) / Decimal(1000000)


def message_bound(model: str, input_tokens: int, output_tokens: int = MAX_OUTPUT_TOKENS) -> Decimal:
    """Charge every counted input token at the highest permitted cache rate."""
    _tokens(input_tokens)
    _tokens(output_tokens)
    if output_tokens == 0:
        raise ValueError("invalid_output_limit")
    return real_cost_usd(
        model,
        Usage(cache_write_tokens=input_tokens, output_tokens=min(output_tokens, MAX_OUTPUT_TOKENS)),
    )
