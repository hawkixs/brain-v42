"""The provider chain: advance on the fallback code only, and say so."""

from __future__ import annotations

import pytest

from headless_agents.capability import PROVIDER_FALLBACK_EXIT_CODE
from headless_agents.chain import ChainResult, run_chain


def _runner(codes: dict[str, int]) -> tuple[list[str], object]:
    calls: list[str] = []

    def run_one(provider: str) -> int:
        calls.append(provider)
        return codes[provider]

    return calls, run_one


def test_stops_at_the_first_link_that_does_not_ask_for_a_fallback() -> None:
    calls, run_one = _runner({"a": 0, "b": 0})
    result = run_chain(["a", "b"], run_one=run_one)
    assert result == ChainResult(provider="a", rc=0, fallbacks=())
    assert calls == ["a"]


def test_an_ordinary_failure_and_a_timeout_stop_where_they_fell() -> None:
    for code in (1, 2):
        calls, run_one = _runner({"a": code, "b": 0})
        result = run_chain(["a", "b"], run_one=run_one)
        assert result.rc == code and result.provider == "a" and calls == ["a"]


def test_the_fallback_code_advances_and_is_announced() -> None:
    calls, run_one = _runner({"a": PROVIDER_FALLBACK_EXIT_CODE, "b": 0})
    announced: list[tuple[str, str]] = []
    result = run_chain(
        ["a", "b"], run_one=run_one, on_fallback=lambda p, n: announced.append((p, n))
    )
    assert result == ChainResult(provider="b", rc=0, fallbacks=("a",))
    assert calls == ["a", "b"] and announced == [("a", "b")]


def test_an_exhausted_chain_ends_as_an_ordinary_failure() -> None:
    calls, run_one = _runner({"a": PROVIDER_FALLBACK_EXIT_CODE, "b": PROVIDER_FALLBACK_EXIT_CODE})
    exhausted: list[str] = []
    result = run_chain(["a", "b"], run_one=run_one, on_exhausted=exhausted.append)
    assert result == ChainResult(provider="b", rc=1, fallbacks=("a",))
    assert calls == ["a", "b"] and exhausted == ["b"]


def test_an_empty_chain_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one"):
        run_chain([], run_one=lambda provider: 0)
