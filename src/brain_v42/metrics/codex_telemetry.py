"""Bounded, in-memory projection of Codex OTLP/HTTP JSON log records.

This is intentionally not a general OpenTelemetry decoder.  It accepts only
the small log-record projection needed by the local cockpit and never retains
raw conversation identifiers or arbitrary input values.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Never

MAX_REQUEST_BYTES = 262_144
MAX_IN_FLIGHT_REQUESTS = 4
MAX_LOG_RECORDS = 256
MAX_ATTRIBUTES_PER_RECORD = 64
MAX_JSON_DEPTH = 12
MAX_JSON_CONTAINERS = 4_096
MAX_MODEL_BYTES = 128
MAX_TOKEN_COUNT = 2_147_483_647

_MALFORMED_MESSAGE = "invalid OTLP JSON payload"
_LIMIT_MESSAGE = "OTLP JSON payload exceeds receiver limits"
_PROJECTED_ATTRIBUTE_KEYS = frozenset(
    {
        "conversation.id",
        "event.kind",
        "event.name",
        "input_token_count",
        "model",
    }
)
_ANY_VALUE_KEYS = frozenset(
    {
        "arrayValue",
        "boolValue",
        "bytesValue",
        "doubleValue",
        "intValue",
        "kvlistValue",
        "stringValue",
    }
)
_DIRECT_EVENTS = frozenset({"codex.conversation_starts", "codex.user_prompt"})
_COMPLETION_EVENTS = frozenset({"codex.sse_event", "codex.websocket_event"})
_MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}\Z")
_DECIMAL_PATTERN = re.compile(r"[0-9]+\Z")
_MAX_JSON_INTEGER_DIGITS = 20


class CodexTelemetryError(ValueError):
    """Base class for safe, input-independent receiver failures."""


class CodexTelemetryMalformedError(CodexTelemetryError):
    """The request is not a supported OTLP/HTTP JSON log envelope."""

    def __init__(self) -> None:
        super().__init__(_MALFORMED_MESSAGE)


class CodexTelemetryLimitError(CodexTelemetryError):
    """The request exceeds one of the receiver's fixed resource limits."""

    def __init__(self) -> None:
        super().__init__(_LIMIT_MESSAGE)


@dataclass(frozen=True, slots=True)
class _ProjectedRecord:
    conversation_id: str
    event_name: str
    event_kind: str | None
    model: str
    input_tokens: int | None
    timestamp: int | None


def _raise_malformed() -> Never:
    raise CodexTelemetryMalformedError


def _validate_json_bounds(root: object) -> None:
    containers = 0
    stack: list[tuple[object, int]] = [(root, 0)]
    while stack:
        value, depth = stack.pop()
        if not isinstance(value, dict | list):
            continue
        containers += 1
        if containers > MAX_JSON_CONTAINERS or depth > MAX_JSON_DEPTH:
            raise CodexTelemetryLimitError
        if isinstance(value, dict):
            stack.extend((child, depth + 1) for child in value.values())
        else:
            stack.extend((child, depth + 1) for child in value)


def _reject_json_constant(_value: str) -> Never:
    raise CodexTelemetryMalformedError


def _bounded_json_int(value: str) -> int:
    unsigned = value.removeprefix("-")
    normalized = unsigned.lstrip("0") or "0"
    if len(normalized) > _MAX_JSON_INTEGER_DIGITS:
        raise CodexTelemetryLimitError
    return int(value)


def _load_json(payload: bytes) -> dict[str, object]:
    if not isinstance(payload, bytes):
        _raise_malformed()
    if len(payload) > MAX_REQUEST_BYTES:
        raise CodexTelemetryLimitError
    try:
        decoded: object = json.loads(
            payload,
            parse_constant=_reject_json_constant,
            parse_int=_bounded_json_int,
        )
    except RecursionError:
        raise CodexTelemetryLimitError from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CodexTelemetryMalformedError from None
    if not isinstance(decoded, dict):
        _raise_malformed()
    _validate_json_bounds(decoded)
    return decoded


def _only_any_value(value: object, key: str) -> object | None:
    if not isinstance(value, dict):
        return None
    populated = _ANY_VALUE_KEYS.intersection(value)
    if populated != {key}:
        return None
    return value.get(key)


def _string_value(value: object) -> str | None:
    raw = _only_any_value(value, "stringValue")
    return raw if isinstance(raw, str) else None


def _token_value(value: object) -> int | None:
    raw: object | None
    if isinstance(value, dict) and "intValue" in value:
        raw = _only_any_value(value, "intValue")
    else:
        raw = _only_any_value(value, "stringValue")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        parsed = raw
    elif isinstance(raw, str) and len(raw) <= 10 and _DECIMAL_PATTERN.fullmatch(raw):
        parsed = int(raw)
    else:
        return None
    return parsed if 0 <= parsed <= MAX_TOKEN_COUNT else None


def _timestamp_value(value: object) -> int | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 20:
        return None
    if _DECIMAL_PATTERN.fullmatch(value) is None:
        return None
    return int(value)


def _model_value(value: object) -> str:
    model = _string_value(value)
    if model is None or len(model.encode("ascii", errors="ignore")) != len(model):
        return "unknown"
    if len(model) > MAX_MODEL_BYTES or _MODEL_PATTERN.fullmatch(model) is None:
        return "unknown"
    return model


def _canonical_uuid(value: object) -> str | None:
    candidate = _string_value(value)
    if candidate is None or len(candidate) > 36:
        return None
    try:
        parsed = uuid.UUID(candidate)
    except (ValueError, AttributeError):
        return None
    return candidate if str(parsed) == candidate else None


def _attributes(record: dict[str, object]) -> dict[str, object]:
    raw_attributes = record.get("attributes", [])
    if not isinstance(raw_attributes, list):
        _raise_malformed()
    if len(raw_attributes) > MAX_ATTRIBUTES_PER_RECORD:
        raise CodexTelemetryLimitError

    projected: dict[str, object] = {}
    for raw_attribute in raw_attributes:
        if not isinstance(raw_attribute, dict):
            _raise_malformed()
        key = raw_attribute.get("key")
        value = raw_attribute.get("value")
        if not isinstance(key, str) or not isinstance(value, dict):
            _raise_malformed()
        if key not in _PROJECTED_ATTRIBUTE_KEYS:
            continue
        if key in projected:
            _raise_malformed()
        projected[key] = value
    return projected


def _project_record(record: dict[str, object]) -> _ProjectedRecord | None:
    attributes = _attributes(record)
    event_name_value = attributes.get("event.name")
    if event_name_value is None:
        return None
    event_name = _string_value(event_name_value)
    if event_name is None:
        _raise_malformed()

    event_kind: str | None = None
    if event_name in _COMPLETION_EVENTS:
        event_kind = _string_value(attributes.get("event.kind"))
        if event_kind != "response.completed":
            return None
    elif event_name not in _DIRECT_EVENTS:
        return None

    conversation_id = _canonical_uuid(attributes.get("conversation.id"))
    if conversation_id is None:
        _raise_malformed()
    model = _model_value(attributes.get("model"))
    input_tokens = _token_value(attributes.get("input_token_count"))
    return _ProjectedRecord(
        conversation_id=conversation_id,
        event_name=event_name,
        event_kind=event_kind,
        model=model,
        input_tokens=input_tokens,
        timestamp=_timestamp_value(record.get("timeUnixNano")),
    )


def _decode(payload: bytes) -> tuple[_ProjectedRecord, ...]:
    root = _load_json(payload)
    if "resourceLogs" not in root:
        _raise_malformed()
    resource_logs = root["resourceLogs"]
    if not isinstance(resource_logs, list):
        _raise_malformed()

    projected: list[_ProjectedRecord] = []
    record_count = 0
    for resource_log in resource_logs:
        if not isinstance(resource_log, dict):
            _raise_malformed()
        scope_logs = resource_log.get("scopeLogs", [])
        if not isinstance(scope_logs, list):
            _raise_malformed()
        for scope_log in scope_logs:
            if not isinstance(scope_log, dict):
                _raise_malformed()
            log_records = scope_log.get("logRecords", [])
            if not isinstance(log_records, list):
                _raise_malformed()
            record_count += len(log_records)
            if record_count > MAX_LOG_RECORDS:
                raise CodexTelemetryLimitError
            for record in log_records:
                if not isinstance(record, dict):
                    _raise_malformed()
                item = _project_record(record)
                if item is not None:
                    projected.append(item)
    return tuple(projected)
