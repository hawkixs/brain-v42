"""Accepted shapes for bounded list inputs."""

import pytest
from pydantic import TypeAdapter, ValidationError

from brain_v42.models.input_bounds import ShortTextList, StepList, TagList


@pytest.mark.parametrize("annotation", [TagList, ShortTextList])
def test_text_list_accepts_one_string(annotation: object) -> None:
    assert TypeAdapter(annotation).validate_python("hello") == ["hello"]


@pytest.mark.parametrize("annotation", [TagList, ShortTextList, StepList])
def test_list_accepts_json_encoded_list(annotation: object) -> None:
    assert TypeAdapter(annotation).validate_python('["x","y"]') == ["x", "y"]


@pytest.mark.parametrize("annotation", [TagList, ShortTextList, StepList])
@pytest.mark.parametrize("value", ["[broken", "", "  "])
def test_list_rejects_malformed_json_or_empty_string(annotation: object, value: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(annotation).validate_python(value)


@pytest.mark.parametrize("annotation", [TagList, ShortTextList])
def test_text_list_rejects_integer(annotation: object) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(annotation).validate_python(3)


def test_text_list_item_bound_applies_after_string_coercion() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(TagList).validate_python("x" * 101)
