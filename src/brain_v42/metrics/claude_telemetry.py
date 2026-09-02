"""Bounded projection of Claude Code's OTLP/HTTP JSON logs.

A schema distinct from Codex's: the identifier is ``session.id`` and not
``conversation.id``, event names are bare (``user_prompt``, ``api_request`` —
the ``claude_code.`` prefix lives in the body, not in the attribute), and the
input counters are split in three: new tokens, cache read and cache creation.
The bounds are shared with the existing receiver, ``codex_telemetry``.

The allowlist projection is the part that matters: measured on 2026-08-06 on
Claude Code 2.1.220, the source also sends ``user.email``, ``user.id``,
``user.account_uuid`` and ``organization.id`` in clear on *every* record, hook
and plugin events included. None of that must reach the registry exposed over
HTTP.

The oracle for attribute names is ``tests/fixtures/claude_otlp_logs.json``; the
full survey is in ``docs/upstream/2026-08-06-claude-otlp-session-join.md``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from brain_v42.metrics.codex_telemetry import (
    MAX_ATTRIBUTES_PER_RECORD,
    MAX_LOG_RECORDS,
    CodexTelemetryLimitError,
    _canonical_uuid,
    _load_json,
    _model_value,
    _only_any_value,
    _raise_malformed,
    _string_value,
    _timestamp_value,
    _token_value,
)

# STRICT allowlist. Anything not here is dropped before it is even read: this
# is the only barrier between the operator's email address and the registry.
_PROJECTED_KEYS = frozenset(
    {
        "session.id",
        "event.name",
        "model",
        "input_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "output_tokens",
        "cost_usd",
    }
)
# Unprefixed: `event.name` is `user_prompt`, the body is
# `claude_code.user_prompt`. Measured on Claude Code 2.1.220.
_KNOWN_EVENTS = frozenset({"user_prompt", "api_request"})
_MAX_COST_USD = 1_000_000.0


@dataclass(frozen=True, slots=True)
class ClaudeRecord:
    """The little we keep from a Claude Code OTLP record.

    ``None`` — never ``0`` — for any counter absent from the source: a cosmetic
    zero would be indistinguishable from a measured zero.
    """

    session_id: str
    event_name: str
    model: str
    input_tokens: int | None
    cache_read_tokens: int | None
    cache_creation_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    timestamp: int | None


def _cost_value(value: object) -> float | None:
    raw = _only_any_value(value, "doubleValue")
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    cost = float(raw)
    if math.isnan(cost) or cost < 0.0 or cost > _MAX_COST_USD:
        return None
    return cost


def _attributes(record: dict[str, object]) -> dict[str, object]:
    raw = record.get("attributes", [])
    if not isinstance(raw, list):
        _raise_malformed()
    if len(raw) > MAX_ATTRIBUTES_PER_RECORD:
        raise CodexTelemetryLimitError
    projected: dict[str, object] = {}
    for item in raw:
        if not isinstance(item, dict):
            _raise_malformed()
        key, value = item.get("key"), item.get("value")
        if not isinstance(key, str) or not isinstance(value, dict):
            _raise_malformed()
        if key not in _PROJECTED_KEYS:
            continue
        if key in projected:
            _raise_malformed()
        projected[key] = value
    return projected


def _project_record(record: dict[str, object]) -> ClaudeRecord | None:
    attributes = _attributes(record)
    name_value = attributes.get("event.name")
    if name_value is None:
        return None
    event_name = _string_value(name_value)
    if event_name is None:
        _raise_malformed()
    if event_name not in _KNOWN_EVENTS:
        return None
    session_id = _canonical_uuid(attributes.get("session.id"))
    if session_id is None:
        _raise_malformed()
    return ClaudeRecord(
        session_id=session_id,
        event_name=event_name,
        model=_model_value(attributes.get("model")),
        input_tokens=_token_value(attributes.get("input_tokens")),
        cache_read_tokens=_token_value(attributes.get("cache_read_tokens")),
        cache_creation_tokens=_token_value(attributes.get("cache_creation_tokens")),
        output_tokens=_token_value(attributes.get("output_tokens")),
        cost_usd=_cost_value(attributes.get("cost_usd")),
        timestamp=_timestamp_value(record.get("timeUnixNano")),
    )


def decode_claude_logs(payload: bytes) -> tuple[ClaudeRecord, ...]:
    """Validate a complete OTLP payload and extract the safe projection from it."""
    root = _load_json(payload)
    resource_logs = root.get("resourceLogs")
    if not isinstance(resource_logs, list):
        _raise_malformed()

    decoded: list[ClaudeRecord] = []
    seen = 0
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
            seen += len(log_records)
            if seen > MAX_LOG_RECORDS:
                raise CodexTelemetryLimitError
            for record in log_records:
                if not isinstance(record, dict):
                    _raise_malformed()
                item = _project_record(record)
                if item is not None:
                    decoded.append(item)
    return tuple(decoded)
