"""Unit tests for scripts.dream._render_prompt.

The function used to be a sed pipeline inside dream.sh; moved to Python
because PROMOTE's candidate pool JSON contains `|` which broke sed. These
tests lock in the safe-substitution contract.
"""

from __future__ import annotations

import pytest
from scripts.dream._render_prompt import render

_FULL_VALUES = {
    "PROJECT_KEY": "brain-v42",
    "DATE": "2026-04-19",
    "DRY_RUN": "true",
    "CANDIDATE_POOL_JSON": "[]",
    "RECENT_PROMOTIONS_JSON": "[]",
}


def test_substitutes_all_five_placeholders() -> None:
    tpl = (
        "pk={{PROJECT_KEY}} date={{DATE}} dry={{DRY_RUN}} "
        "pool={{CANDIDATE_POOL_JSON}} recent={{RECENT_PROMOTIONS_JSON}}"
    )
    assert render(tpl, _FULL_VALUES) == ("pk=brain-v42 date=2026-04-19 dry=true pool=[] recent=[]")


def test_pipe_in_json_does_not_break_substitution() -> None:
    """The bug that prompted this refactor: sed broke on `|` in the JSON."""
    values = {**_FULL_VALUES, "CANDIDATE_POOL_JSON": '[{"topic":"a|b|c"}]'}
    assert render("{{CANDIDATE_POOL_JSON}}", values) == '[{"topic":"a|b|c"}]'


def test_ampersand_in_json_substituted_literally() -> None:
    """`&` is special in sed replacement (means `the whole match`). Literal here."""
    values = {**_FULL_VALUES, "CANDIDATE_POOL_JSON": '[{"topic":"a & b"}]'}
    assert render("{{CANDIDATE_POOL_JSON}}", values) == '[{"topic":"a & b"}]'


def test_backslash_in_json_substituted_literally() -> None:
    """`\\` is special in sed replacement. Literal here."""
    values = {**_FULL_VALUES, "CANDIDATE_POOL_JSON": r'[{"path":"C:\Users"}]'}
    assert render("{{CANDIDATE_POOL_JSON}}", values) == r'[{"path":"C:\Users"}]'


def test_placeholder_repeated_is_substituted_everywhere() -> None:
    tpl = "{{PROJECT_KEY}} on {{DATE}} for {{PROJECT_KEY}}"
    assert render(tpl, _FULL_VALUES) == "brain-v42 on 2026-04-19 for brain-v42"


def test_unknown_placeholder_left_untouched() -> None:
    """Template can contain `{{OTHER}}` markers; only known keys are replaced."""
    tpl = "{{PROJECT_KEY}} and {{UNKNOWN}} still there"
    assert render(tpl, _FULL_VALUES) == "brain-v42 and {{UNKNOWN}} still there"


def test_missing_key_raises() -> None:
    """Every placeholder key must be present in values — guard against drift."""
    with pytest.raises(KeyError):
        render("{{PROJECT_KEY}}", {})
