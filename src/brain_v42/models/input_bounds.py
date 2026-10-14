"""Character bounds with headroom over production measurements (2026-10-05).

Measured max / p99 characters: learning insight 17,047 / 3,997; ADR decision
14,010 / 1,817; runbook steps JSON 15,445 / 8,102 (one step 3,126);
ticket body 10,093 / 6,444; ticket reply 8,609 / 5,651; decision description
6,869 / 2,836; snippet code 6,025 / 5,232; feature description 4,479;
runbook description 3,112; ADR context 2,767; decision reasoning 2,775;
snippet gotchas 1,678; snippet intention 1,527; abandonment reason 1,912.
Short attributes: prerequisite 711; test strategy 609; alternative 581;
git workflow 423; trigger 410; source 403; blocker 312; tag 44 (p99.9 26).
List maxima: tags 34, alternatives 12, steps 16, prerequisites 7,
dependencies 7, related projects 8, explicit relations 9.

Knowledge text has 2.9x headroom over the longest measured body. Short text
has at least 2.8x headroom; lists at least 2.9x and relations 5.5x. These
bounds refuse writes without truncating, and deliberately leave reads unbounded.
"""

import json
from typing import Annotated

from pydantic import BeforeValidator, Field

KNOWLEDGE_TEXT_MAX_LENGTH = 50_000
SHORT_TEXT_MAX_LENGTH = 2_000
TAG_MAX_LENGTH = 100
LIST_MAX_ITEMS = 100
RELATIONS_MAX_ITEMS = 50
SEARCH_QUERY_MAX_LENGTH = 10_000

KnowledgeText = Annotated[str, Field(max_length=KNOWLEDGE_TEXT_MAX_LENGTH)]
ShortText = Annotated[str, Field(max_length=SHORT_TEXT_MAX_LENGTH)]
Tag = Annotated[str, Field(max_length=TAG_MAX_LENGTH)]


def _single_text_as_list(value: object) -> object:
    """Accept scalar text and JSON-encoded lists without hiding malformed input."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be an empty string")
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError("looks like a JSON list but is not valid JSON") from exc
        if isinstance(parsed, list):
            return parsed
        raise ValueError("looks like a JSON list but is not valid JSON")
    return [value]


_TEXT_LIST_DESCRIPTION = "A list of text; a single string is accepted as a one-element list."
TagList = Annotated[
    list[Tag],
    BeforeValidator(_single_text_as_list),
    Field(max_length=LIST_MAX_ITEMS, description=_TEXT_LIST_DESCRIPTION),
]
ShortTextList = Annotated[
    list[ShortText],
    BeforeValidator(_single_text_as_list),
    Field(max_length=LIST_MAX_ITEMS, description=_TEXT_LIST_DESCRIPTION),
]
StepList = Annotated[
    list[dict | ShortText],
    BeforeValidator(_single_text_as_list),
    Field(max_length=LIST_MAX_ITEMS, description="Each step is an object or a title string."),
]
RelationList = Annotated[list[dict], Field(max_length=RELATIONS_MAX_ITEMS)]
SearchQuery = Annotated[str, Field(max_length=SEARCH_QUERY_MAX_LENGTH)]
