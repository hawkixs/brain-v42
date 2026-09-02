"""Bounded, privacy-preserving decoding of Codex OTLP/HTTP JSON logs."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from brain_v42.metrics.codex_telemetry import (
    MAX_ATTRIBUTES_PER_RECORD,
    MAX_JSON_CONTAINERS,
    MAX_LOG_RECORDS,
    MAX_REQUEST_BYTES,
    MAX_TOKEN_COUNT,
    CodexConversationRegistry,
    CodexTelemetryLimitError,
    CodexTelemetryMalformedError,
)

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "codex_otlp_logs.json"
FAKE_UUID = "12345678-1234-4abc-8def-1234567890ab"
OTHER_UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def _attribute(key: str, value: dict[str, object]) -> dict[str, object]:
    return {"key": key, "value": value}


def _record(
    *,
    event_name: str = "codex.sse_event",
    conversation_id: str = FAKE_UUID,
    timestamp: str | None = "100",
    event_kind: str | None = "response.completed",
    token_value: dict[str, object] | None = None,
    model: str | None = "gpt-5.4",
    extra_attributes: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    attributes = [
        _attribute("event.name", {"stringValue": event_name}),
        _attribute("conversation.id", {"stringValue": conversation_id}),
    ]
    if event_kind is not None:
        attributes.append(_attribute("event.kind", {"stringValue": event_kind}))
    if token_value is not None:
        attributes.append(_attribute("input_token_count", token_value))
    if model is not None:
        attributes.append(_attribute("model", {"stringValue": model}))
    attributes.extend(extra_attributes or [])
    record: dict[str, object] = {"attributes": attributes}
    if timestamp is not None:
        record["timeUnixNano"] = timestamp
    return record


def _payload(*records: dict[str, object], unknown: object | None = None) -> bytes:
    envelope: dict[str, object] = {"resourceLogs": [{"scopeLogs": [{"logRecords": list(records)}]}]}
    if unknown is not None:
        envelope["futureField"] = unknown
    return json.dumps(envelope, separators=(",", ":")).encode()


def _snapshot_for(*records: dict[str, object]) -> dict[str, object]:
    registry = CodexConversationRegistry(secret=b"x" * 32)
    registry.ingest_otlp_json(_payload(*records))
    return registry.snapshot()


def test_synthetic_probe_shape_projects_only_allowlisted_fields() -> None:
    payload = FIXTURE_PATH.read_bytes()
    registry = CodexConversationRegistry(secret=b"x" * 32)

    registry.ingest_otlp_json(payload)

    snapshot = registry.snapshot()
    assert snapshot["active_convs"] == 1
    assert snapshot["ctx_tokens"] == 1234
    assert snapshot["activeConvs"] == [
        {
            "id": "codex-74f165fb349c73a6b26c68bf9504b070",
            "topic": "[redacted]",
            "agent": "codex",
            "started": snapshot["activeConvs"][0]["started"],
            "turns": 1,
            "tokens": 1234,
            "model": "gpt-5.4",
            "cost": None,
        }
    ]
    serialized = json.dumps(snapshot, sort_keys=True)
    assert FAKE_UUID not in serialized
    for sentinel in (
        "SENSITIVE_RESOURCE_SENTINEL",
        "SENSITIVE_BODY_SENTINEL",
        "SENSITIVE_EMAIL_SENTINEL",
        "SENSITIVE_ACCOUNT_SENTINEL",
        "SENSITIVE_PROMPT_SENTINEL",
        "SENSITIVE_MCP_SENTINEL",
    ):
        assert sentinel not in serialized


@pytest.mark.parametrize(
    ("any_value", "expected"),
    [
        ({"stringValue": "17"}, 17),
        ({"intValue": "18"}, 18),
        ({"intValue": 19}, 19),
    ],
)
def test_token_count_accepts_observed_anyvalue_variants(
    any_value: dict[str, object], expected: int
) -> None:
    snapshot = _snapshot_for(_record(token_value=any_value))

    assert snapshot["ctx_tokens"] == expected


def test_resource_scope_body_headers_and_unknown_fields_never_participate() -> None:
    record = _record(
        event_name="future.codex_event",
        conversation_id=OTHER_UUID,
        token_value={"intValue": 999},
    )
    envelope = {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        _attribute("conversation.id", {"stringValue": FAKE_UUID}),
                        _attribute("event.name", {"stringValue": "codex.user_prompt"}),
                    ]
                },
                "scopeLogs": [
                    {
                        "scope": {"attributes": {"prompt": "SENSITIVE_SCOPE"}},
                        "logRecords": [{**record, "body": {"stringValue": "SENSITIVE_BODY"}}],
                    }
                ],
            }
        ],
        "headers": {"authorization": "SENSITIVE_HEADER"},
        "future": {"deep": ["SENSITIVE_UNKNOWN"]},
    }
    registry = CodexConversationRegistry(secret=b"x" * 32)

    registry.ingest_otlp_json(json.dumps(envelope).encode())

    assert registry.snapshot() == {
        "active_convs": 0,
        "ctx_tokens": 0,
        "activeConvs": [],
        "clients": [],
    }


def test_duplicate_projected_attribute_rejects_the_whole_batch_statically() -> None:
    duplicate = _record(
        token_value={"intValue": 1},
        extra_attributes=[_attribute("conversation.id", {"stringValue": OTHER_UUID})],
    )
    registry = CodexConversationRegistry(secret=b"x" * 32)

    with pytest.raises(CodexTelemetryMalformedError) as error:
        registry.ingest_otlp_json(_payload(duplicate))

    assert str(error.value) == "invalid OTLP JSON payload"
    assert FAKE_UUID not in str(error.value)
    assert OTHER_UUID not in str(error.value)


@pytest.mark.parametrize(
    "conversation_id",
    [
        "1234567812344abc8def1234567890ab",
        "12345678-1234-4ABC-8DEF-1234567890AB",
        "not-a-uuid",
        "12345678-1234-4abc-8def-1234567890ab-extra",
    ],
)
def test_recognized_events_require_a_canonical_uuid(conversation_id: str) -> None:
    with pytest.raises(CodexTelemetryMalformedError, match="^invalid OTLP JSON payload$"):
        _snapshot_for(_record(conversation_id=conversation_id, token_value={"intValue": 1}))


@pytest.mark.parametrize("model", ["bad model", "modèle", "x" * 129, "", "/leading"])
def test_invalid_model_slugs_are_replaced_with_unknown(model: str) -> None:
    snapshot = _snapshot_for(_record(model=model, token_value={"intValue": 1}))

    assert snapshot["activeConvs"][0]["model"] == "unknown"


def test_maximum_length_model_slug_is_preserved() -> None:
    model = "m" + "x" * 127

    snapshot = _snapshot_for(_record(model=model, token_value={"intValue": 1}))

    assert snapshot["activeConvs"][0]["model"] == model


@pytest.mark.parametrize(
    "token_value",
    [
        {"intValue": -1},
        {"intValue": True},
        {"intValue": MAX_TOKEN_COUNT + 1},
        {"stringValue": "-1"},
        {"stringValue": "not-a-count"},
        {"doubleValue": 1.5},
    ],
)
def test_invalid_completion_token_counts_do_not_update_tokens(
    token_value: dict[str, object],
) -> None:
    snapshot = _snapshot_for(_record(token_value=token_value))

    assert snapshot["active_convs"] == 1
    assert snapshot["ctx_tokens"] == 0
    assert snapshot["activeConvs"][0]["tokens"] == 0


def test_request_size_limit_has_a_static_error() -> None:
    registry = CodexConversationRegistry(secret=b"x" * 32)

    with pytest.raises(CodexTelemetryLimitError) as error:
        registry.ingest_otlp_json(b" " * (MAX_REQUEST_BYTES + 1))

    assert str(error.value) == "OTLP JSON payload exceeds receiver limits"


def test_oversized_unknown_json_integer_has_a_static_error() -> None:
    payload = b'{"resourceLogs":[],"future":' + (b"9" * 4_301) + b"}"
    assert len(payload) < MAX_REQUEST_BYTES

    with pytest.raises((CodexTelemetryMalformedError, CodexTelemetryLimitError)) as error:
        CodexConversationRegistry(secret=b"x" * 32).ingest_otlp_json(payload)

    assert str(error.value) in {
        "invalid OTLP JSON payload",
        "OTLP JSON payload exceeds receiver limits",
    }


def test_log_record_limit_is_global_across_resource_logs() -> None:
    records = [{} for _ in range(MAX_LOG_RECORDS)]
    envelope = {
        "resourceLogs": [
            {"scopeLogs": [{"logRecords": records[:128]}]},
            {"scopeLogs": [{"logRecords": records[128:] + [{}]}]},
        ]
    }

    with pytest.raises(CodexTelemetryLimitError, match="^OTLP JSON payload exceeds"):
        CodexConversationRegistry(secret=b"x" * 32).ingest_otlp_json(json.dumps(envelope).encode())


def test_attribute_limit_applies_to_every_record() -> None:
    record = {
        "attributes": [
            _attribute(f"future.{index}", {"stringValue": "x"})
            for index in range(MAX_ATTRIBUTES_PER_RECORD + 1)
        ]
    }

    with pytest.raises(CodexTelemetryLimitError, match="^OTLP JSON payload exceeds"):
        CodexConversationRegistry(secret=b"x" * 32).ingest_otlp_json(_payload(record))


def test_json_depth_limit_counts_unknown_fields() -> None:
    unknown: object = "leaf"
    for _ in range(13):
        unknown = [unknown]

    with pytest.raises(CodexTelemetryLimitError, match="^OTLP JSON payload exceeds"):
        CodexConversationRegistry(secret=b"x" * 32).ingest_otlp_json(
            json.dumps({"resourceLogs": [], "future": unknown}).encode()
        )


def test_json_container_limit_counts_unknown_fields() -> None:
    unknown = [[] for _ in range(MAX_JSON_CONTAINERS)]

    with pytest.raises(CodexTelemetryLimitError, match="^OTLP JSON payload exceeds"):
        CodexConversationRegistry(secret=b"x" * 32).ingest_otlp_json(
            json.dumps({"resourceLogs": [], "future": unknown}).encode()
        )


@pytest.mark.parametrize(
    "envelope",
    [
        b"not-json",
        b'{"resourceLogs":[],"future":NaN}',
        b"[]",
        b"{}",
        b'{"resourceLogs":"wrong"}',
        b'{"resourceLogs":[{"scopeLogs":"wrong"}]}',
        b'{"resourceLogs":[{"scopeLogs":[{"logRecords":"wrong"}]}]}',
        b'{"resourceLogs":[{"scopeLogs":[{"logRecords":[{"attributes":"wrong"}]}]}]}',
    ],
)
def test_malformed_json_and_otlp_envelopes_have_one_static_error(envelope: bytes) -> None:
    with pytest.raises(CodexTelemetryMalformedError) as error:
        CodexConversationRegistry(secret=b"x" * 32).ingest_otlp_json(envelope)

    assert str(error.value) == "invalid OTLP JSON payload"


def test_empty_and_irrelevant_batches_are_valid_noops() -> None:
    registry = CodexConversationRegistry(secret=b"x" * 32)

    registry.ingest_otlp_json(b'{"resourceLogs":[]}')
    registry.ingest_otlp_json(_payload(_record(event_name="codex.future_event")))

    assert registry.snapshot() == {
        "active_convs": 0,
        "ctx_tokens": 0,
        "activeConvs": [],
        "clients": [],
    }


def test_decoder_does_not_mutate_the_caller_owned_json_shape() -> None:
    envelope = json.loads(_payload(_record(token_value={"intValue": 7})))
    original = copy.deepcopy(envelope)

    CodexConversationRegistry(secret=b"x" * 32).ingest_otlp_json(json.dumps(envelope).encode())

    assert envelope == original


# ---------------------------------------------------------------------------
# The confidentiality barrier, tested for its own sake
#
# Every Codex OTLP record carries user.email, user.id, user.account_uuid,
# user.account_id and organization.id in clear — measured at the spike of
# 2026-08-06. The allowlist is what stops them entering a registry exposed over
# HTTP. It had NO witness: removing it left the suite entirely green (measured
# 2026-08-10, zero delta over 7416 tests).
# ---------------------------------------------------------------------------

SENSITIVE_VALUES = {
    "user.email": "personne@exemple.test",
    "user.id": "user-0123456789",
    "user.account_uuid": "99999999-8888-4777-8666-555555555555",
    "user.account_id": "account-0123456789",
    "organization.id": "org-0123456789",
    "prompt": "le texte que l'opérateur a tapé",
    "mcp_servers": "brain-v42,red-data",
}

# Identical to the Claude twin's (tests/unit/test_claude_telemetry.py:64), and
# deliberately so: the two barriers have the same shape and must be judged alike.
# Do NOT add "token" to it — it would accuse input_token_count, which is a COUNT
# of tokens and not an authentication token.
PERSONAL_DATA_MARKERS = ("email", "user", "account", "organization", "prompt")


def _sensitive_attributes() -> list[dict[str, object]]:
    return [_attribute(key, {"stringValue": value}) for key, value in SENSITIVE_VALUES.items()]


def test_the_codex_whitelist_admits_no_personal_data_key() -> None:
    """The LIST, tested alone — it had no witness on the Codex side either.

    Widening it leaks nothing on the day it is done; it arms the leak for the next
    field. Measured: adding ``user.email`` to the list, filter intact, leaves the
    414 metrics tests green in 1.93 s.
    """
    from brain_v42.metrics.codex_telemetry import _PROJECTED_ATTRIBUTE_KEYS

    # Positive control: without it, an emptied or renamed list would make the
    # two assertions below true for nothing.
    assert {"conversation.id", "event.name", "model"} <= _PROJECTED_ATTRIBUTE_KEYS

    admitted = SENSITIVE_VALUES.keys() & _PROJECTED_ATTRIBUTE_KEYS
    assert not admitted, f"clés personnelles mesurées admises : {sorted(admitted)}"

    for key in sorted(_PROJECTED_ATTRIBUTE_KEYS):
        lowered = key.lower()
        for marker in PERSONAL_DATA_MARKERS:
            assert marker not in lowered, f"la liste blanche admet {key} (canal « {marker} »)"


def test_the_codex_projection_drops_every_key_outside_the_whitelist() -> None:
    """The FILTER, tested where it is OBSERVABLE: the projection itself.

    No assertion on the RETURNED record can bite. Measured, identical payload with
    and without the filter: the ``_ProjectedRecord`` is the same field for field.
    The dataclass is ``frozen=True, slots=True`` and the projection reads only
    allowlisted names through ``.get()`` — an unprojected attribute has nowhere to
    land. That is why this test hangs onto a private symbol: it is the price of a
    barrier invisible from the public API. If anyone inlines the projection, this
    test dies at import, hence loudly.
    """
    from brain_v42.metrics.codex_telemetry import _attributes as _project_attributes

    record = _record(
        token_value={"intValue": 10},  # otherwise the key is missing and exhaustiveness lies
        extra_attributes=_sensitive_attributes(),
    )

    retained = _project_attributes(record)

    leaked = SENSITIVE_VALUES.keys() & retained.keys()
    assert not leaked, f"clés personnelles retenues par la projection : {sorted(leaked)}"

    # Exhaustive, and a positive control at the same time: an empty or shrunken
    # projection fails here, which stops the absence assertion above from passing
    # for nothing. Two positive probes do not prove a guard exists.
    assert set(retained) == {
        "conversation.id",
        "event.kind",
        "event.name",
        "input_token_count",
        "model",
    }
