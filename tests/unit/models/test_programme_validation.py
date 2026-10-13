"""Write-side validation of programmes/v1 (contract docs/contracts/programmes_v1.md §2, §5).

The contract promises consumers that a declared text is one clean line and that a
key matches its grammar: brain refuses at write, it never cuts. Every refusal
carries a stable code a session can act on.
"""

from __future__ import annotations

import pytest

from brain_v42.models.programme import (
    ProgrammeError,
    acyclic_or_refuse,
    gate_key,
    line,
    programme_key,
    progress,
    weight,
)


@pytest.mark.parametrize("value", ["G7", "brain-move-red-base", "lot-0.7.0", "a", "x" * 64])
def test_programme_keys_follow_the_contract_grammar(value: str) -> None:
    assert programme_key(value) == value


@pytest.mark.parametrize(
    "value", ["", "-lead", ".lead", "has space", "x" * 65, "G7\n", "é", "a/b", "a:b"]
)
def test_programme_keys_outside_the_grammar_are_refused(value: str) -> None:
    with pytest.raises(ProgrammeError) as refused:
        programme_key(value)
    assert refused.value.code == "invalid_key"


def test_gate_keys_stop_at_32_characters() -> None:
    assert gate_key("G" * 32) == "G" * 32
    with pytest.raises(ProgrammeError) as refused:
        gate_key("G" * 33)
    assert refused.value.code == "invalid_key"


@pytest.mark.parametrize(
    "value",
    [
        "two\nlines",
        "carriage\rreturn",
        "nul\u0000here",
        "del\u007fhere",
        "c1\u0085here",
        "zero\u200bwidth",
        "line\u2028separator",
        "paragraph\u2029separator",
        "bidi\u202eflip",
        "isolate\u2066here",
        "bom\ufeffhere",
    ],
)
def test_a_forbidden_character_is_refused_not_stripped(value: str) -> None:
    with pytest.raises(ProgrammeError) as refused:
        line(value, field="note", limit=200)
    assert refused.value.code == "invalid_text"
    assert "note" in refused.value.message


@pytest.mark.parametrize("value", ["", "   ", " lead", "trail ", "x" * 201])
def test_blank_padded_or_long_text_is_refused(value: str) -> None:
    with pytest.raises(ProgrammeError) as refused:
        line(value, field="ask", limit=200)
    assert refused.value.code == "invalid_text"


def test_a_clean_line_passes_unchanged_at_its_limit() -> None:
    base = "validate the export-accounts spec (G7) — red-backup, 2 h ⏳ "
    text = base + "x" * (200 - len(base))
    assert len(text) == 200
    assert line(text, field="ask", limit=200) == text


@pytest.mark.parametrize("value", [1, 4, 100])
def test_weights_from_1_to_100(value: int) -> None:
    assert weight(value) == value


@pytest.mark.parametrize("value", [0, 101, -1, True])
def test_weights_outside_1_to_100_are_refused(value: int) -> None:
    with pytest.raises(ProgrammeError) as refused:
        weight(value)
    assert refused.value.code == "invalid_weight"


def test_progress_is_bounded_and_carries_a_unit() -> None:
    assert progress(6, 15, "tasks") == {"done": 6, "total": 15, "unit": "tasks"}
    for done, total, unit in ((16, 15, "tasks"), (-1, 15, "t"), (0, 0, "t"), (0, 100001, "t")):
        with pytest.raises(ProgrammeError) as refused:
            progress(done, total, unit)
        assert refused.value.code == "invalid_progress"
    with pytest.raises(ProgrammeError) as refused:
        progress(1, 2, "u" * 25)
    assert refused.value.code == "invalid_text"


def test_a_dependency_graph_with_a_cycle_is_refused_and_named() -> None:
    acyclic_or_refuse({"G1": [], "G2": ["G1"], "G3": ["G2", "G1"]})
    with pytest.raises(ProgrammeError) as refused:
        acyclic_or_refuse({"G1": ["G3"], "G2": ["G1"], "G3": ["G2"]})
    assert refused.value.code == "dependency_cycle"
    assert "G1" in refused.value.message
