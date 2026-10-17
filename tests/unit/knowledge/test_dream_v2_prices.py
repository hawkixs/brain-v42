"""Standard P3 billing must not accidentally use Batch discounts."""

from decimal import Decimal

import pytest

from brain_v42.knowledge.dream_v2_prices import MODEL, Usage, message_bound, real_cost_usd
from brain_v42.knowledge.extraction import INPUT_PRICE, OUTPUT_PRICE


def test_standard_rates_and_all_four_usage_classes():
    assert real_cost_usd(MODEL, Usage(1000000, 1000000, 1000000, 1000000)) == Decimal("14.7")
    assert (INPUT_PRICE, OUTPUT_PRICE) == (1, 5)


@pytest.mark.parametrize(
    "usage,expected",
    [
        (Usage(input_tokens=1000000), "2"),
        (Usage(cache_write_tokens=1000000), "2.5"),
        (Usage(cache_read_tokens=1000000), "0.2"),
        (Usage(output_tokens=1000000), "10"),
    ],
)
def test_each_usage_class_has_its_own_standard_rate(usage, expected):
    assert real_cost_usd(MODEL, usage) == Decimal(expected)


def test_reservation_uses_cache_write_rate_and_clamped_output():
    assert message_bound(MODEL, 1000000, 8192) == Decimal("2.54096")
    assert message_bound(MODEL, 1, 1) == Decimal("0.0000125")


@pytest.mark.parametrize("model", ["sonnet", "claude-opus-5", "unknown"])
def test_unknown_model_is_refused(model):
    with pytest.raises(ValueError):
        message_bound(model, 1, 1)


@pytest.mark.parametrize("value", [-1, True, 1.1])
def test_invalid_token_counts_are_refused(value):
    with pytest.raises(ValueError):
        Usage(value, 0, 0, 0)


def test_usage_counts_cache_reads_and_writes_toward_soft_cap():
    assert Usage(1, 2, 3, 4).total_tokens == 10
