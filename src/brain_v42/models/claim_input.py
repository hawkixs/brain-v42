"""Leaf-only structural validation for declared claim expectations."""

from __future__ import annotations

import re
from collections.abc import Sequence
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

_FACT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_OPS = frozenset({"eq", "ne", "lt", "lte", "gt", "gte", "in", "exists"})
_EXPECTED_FIELDS = frozenset({"path", "op", "value", "policy"})


class ClaimMutationError(ValueError):
    """A curated claim input or mutation refusal whose caller must roll back its write."""


def _storable(text: str) -> bool:
    """Canonical JSON and PostgreSQL refuse NUL and surrogates: refuse them at input."""
    return "\x00" not in text and not any(0xD800 <= ord(char) <= 0xDFFF for char in text)


class ClaimInput(BaseModel):
    """One bounded declared comparison, independent of facts and their catalogue."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    statement: str
    fact_name: str
    expected: dict[str, object]
    validity_seconds: int | None = None
    replaces: UUID | None = None
    measure: bool = False

    @field_validator("statement", mode="before")
    @classmethod
    def _normalize_statement(cls, value: object) -> str:
        """Store trimmed prose so whitespace cannot create a distinct empty claim."""
        if not isinstance(value, str):
            raise ClaimMutationError("statement rule requires a string")
        trimmed = value.strip()
        if not 1 <= len(trimmed) <= 500:
            raise ClaimMutationError("statement rule requires 1 to 500 characters after strip")
        if not _storable(trimmed):
            raise ClaimMutationError("statement rule refuses NUL characters and Unicode surrogates")
        return trimmed

    @field_validator("fact_name", mode="before")
    @classmethod
    def _validate_fact_name(cls, value: object) -> str:
        """Keep the copied catalogue vocabulary local to preserve the models leaf."""
        if not isinstance(value, str) or _FACT_NAME.fullmatch(value) is None:
            raise ClaimMutationError("fact_name rule requires ^[a-z][a-z0-9_]{0,63}$")
        return value

    @field_validator("validity_seconds", mode="before")
    @classmethod
    def _validate_validity_seconds(cls, value: object) -> object:
        """Refuse booleans and coercions because validity is a persisted integer contract."""
        if value is None:
            return value
        if type(value) is not int or not 60 <= value <= 31_536_000:
            raise ClaimMutationError(
                "validity_seconds rule requires an integer from 60 to 31536000"
            )
        return value

    @field_validator("measure", mode="before")
    @classmethod
    def _validate_measure(cls, value: object) -> bool:
        """Refuse coercions: a truthy non-bool must not silently opt into a write-time verdict."""
        if type(value) is not bool:
            raise ClaimMutationError("measure rule requires a bool")
        return value

    @field_validator("expected", mode="before")
    @classmethod
    def _validate_expected(cls, value: object) -> dict[str, object]:
        """Close comparison vocabulary before catalogue-dependent resolution later."""
        if not isinstance(value, dict):
            raise ClaimMutationError("expected structure rule requires an object")

        keys = frozenset(value)
        if not {"path", "op"} <= keys or keys - _EXPECTED_FIELDS:
            raise ClaimMutationError(
                "expected fields rule requires path and op only with value or policy"
            )
        op = value["op"]
        if not isinstance(op, str):
            raise ClaimMutationError("op rule requires a string operator")
        if op == "regex":
            raise ClaimMutationError("op rule: regex was removed")
        if op not in _OPS:
            raise ClaimMutationError("op rule requires a supported operator")

        has_value = "value" in value
        has_policy = "policy" in value
        if op == "exists":
            if has_value or has_policy:
                raise ClaimMutationError("exists rule takes neither value nor policy")
        elif has_value == has_policy:
            raise ClaimMutationError("expected fields rule requires exactly one of value or policy")

        cls._validate_pointer(value["path"])
        if has_value:
            cls._validate_value(value["value"])
        if op == "in" and (not has_value or not isinstance(value["value"], list)):
            raise ClaimMutationError("in rule requires value to be an array")
        if has_policy:
            policy = value["policy"]
            if not isinstance(policy, str) or not policy.strip():
                raise ClaimMutationError("policy rule requires a non-empty string")
        return dict(value)

    @staticmethod
    def _validate_pointer(value: object) -> None:
        """Validate RFC 6901 escapes while measuring decoded, not encoded, segments."""
        if not isinstance(value, str) or not value.startswith("/"):
            raise ClaimMutationError("path rule requires a JSON pointer starting with /")
        segments = value[1:].split("/")
        if len(segments) > 8:
            raise ClaimMutationError("path segments rule allows at most 8 segments")
        for segment in segments:
            decoded: list[str] = []
            index = 0
            while index < len(segment):
                character = segment[index]
                if character != "~":
                    decoded.append(character)
                    index += 1
                    continue
                if index + 1 == len(segment) or segment[index + 1] not in {"0", "1"}:
                    raise ClaimMutationError("path escape rule requires ~0 or ~1")
                decoded.append("~" if segment[index + 1] == "0" else "/")
                index += 2
            if len("".join(decoded)) > 64:
                raise ClaimMutationError("path segment length rule allows at most 64 characters")

    @staticmethod
    def _validate_value(value: object) -> None:
        """Align structural scalar vocabulary with canonical JSON before keying occurs."""
        values = value if isinstance(value, list) else [value]
        if isinstance(value, list) and len(value) > 32:
            raise ClaimMutationError("value rule allows at most 32 array items")
        if not all(item is None or type(item) in {str, int, bool} for item in values):
            raise ClaimMutationError("value rule requires scalars and refuses floats")
        if not all(_storable(item) for item in values if isinstance(item, str)):
            raise ClaimMutationError("value rule refuses NUL characters and Unicode surrogates")


#: Claims one entry write may carry, explicit and automatic together.
MAX_CLAIMS_PER_WRITE = 10


def validate_claim_inputs(inputs: Sequence[ClaimInput]) -> None:
    """Reject oversized or duplicate request input before any writer starts work."""
    if len(inputs) > MAX_CLAIMS_PER_WRITE:
        raise ClaimMutationError(f"input count rule allows at most {MAX_CLAIMS_PER_WRITE} inputs")
    seen: list[ClaimInput] = []
    for claim in inputs:
        if claim in seen:
            raise ClaimMutationError("duplicate input rule refuses identical validated inputs")
        seen.append(claim)
