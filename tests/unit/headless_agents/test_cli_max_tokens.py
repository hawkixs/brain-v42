"""The run grammar accepts an explicit HTTP answer bound."""

from __future__ import annotations

import pytest

from headless_agents import cli


def test_the_run_parser_takes_max_tokens() -> None:
    args = cli._parser().parse_args(["run", "openrouter", "task", "--max-tokens", "64"])
    assert args.max_tokens == 64


def test_a_non_integer_max_tokens_is_refused() -> None:
    with pytest.raises(SystemExit) as refused:
        cli._parser().parse_args(["run", "openrouter", "task", "--max-tokens", "many"])
    assert refused.value.code == 2
