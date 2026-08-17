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
# La barrière de confidentialité, éprouvée pour elle-même
#
# Chaque enregistrement OTLP de Codex porte en clair user.email, user.id,
# user.account_uuid, user.account_id et organization.id — mesuré au spike du
# 2026-08-06. La liste blanche est ce qui les empêche d'entrer dans un registre
# exposé par HTTP. Elle n'avait AUCUN témoin : la retirer laissait la suite
# entièrement verte (mesuré 2026-08-10, delta zéro sur 7416 tests).
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

# Identiques à ceux du jumeau Claude (tests/unit/test_claude_telemetry.py:64), et
# volontairement : les deux barrières ont la même forme et doivent se juger pareil.
# Ne PAS y ajouter « token » — il accuserait input_token_count, qui est un COMPTE
# de tokens et non un jeton d'authentification.
PERSONAL_DATA_MARKERS = ("email", "user", "account", "organization", "prompt")


def _sensitive_attributes() -> list[dict[str, object]]:
    return [_attribute(key, {"stringValue": value}) for key, value in SENSITIVE_VALUES.items()]


def test_the_codex_whitelist_admits_no_personal_data_key() -> None:
    """La LISTE, testée seule — elle n'avait pas non plus de témoin côté Codex.

    L'élargir ne fuit rien le jour où on le fait ; ça arme la fuite pour le champ
    suivant. Mesuré : ajouter ``user.email`` à la liste, filtre intact, laisse les
    414 tests métriques verts en 1,93 s.
    """
    from brain_v42.metrics.codex_telemetry import _PROJECTED_ATTRIBUTE_KEYS

    # Contrôle positif : sans lui, une liste vidée ou renommée rendrait les
    # deux assertions suivantes vraies pour rien.
    assert {"conversation.id", "event.name", "model"} <= _PROJECTED_ATTRIBUTE_KEYS

    admitted = SENSITIVE_VALUES.keys() & _PROJECTED_ATTRIBUTE_KEYS
    assert not admitted, f"clés personnelles mesurées admises : {sorted(admitted)}"

    for key in sorted(_PROJECTED_ATTRIBUTE_KEYS):
        lowered = key.lower()
        for marker in PERSONAL_DATA_MARKERS:
            assert marker not in lowered, f"la liste blanche admet {key} (canal « {marker} »)"


def test_the_codex_projection_drops_every_key_outside_the_whitelist() -> None:
    """Le FILTRE, éprouvé là où il est OBSERVABLE : la projection elle-même.

    Aucune assertion sur l'enregistrement RENDU ne peut mordre. Mesuré, charge
    identique avec et sans filtre : le ``_ProjectedRecord`` est le même champ pour
    champ. La dataclass est ``frozen=True, slots=True`` et la projection ne lit que
    des noms whitelistés via ``.get()`` — un attribut non projeté n'a nulle part où
    atterrir. C'est pourquoi ce test s'accroche à un symbole privé : c'est le prix
    d'une barrière invisible depuis l'API publique. Si quelqu'un inline la
    projection, ce test meurt à l'import, donc bruyamment.
    """
    from brain_v42.metrics.codex_telemetry import _attributes as _project_attributes

    record = _record(
        token_value={"intValue": 10},  # sinon la clé manque et l'exhaustivité ment
        extra_attributes=_sensitive_attributes(),
    )

    retained = _project_attributes(record)

    leaked = SENSITIVE_VALUES.keys() & retained.keys()
    assert not leaked, f"clés personnelles retenues par la projection : {sorted(leaked)}"

    # Exhaustif, et contrôle positif du même coup : une projection vide ou
    # rétrécie échoue ici, ce qui interdit à l'assertion d'absence ci-dessus de
    # passer pour rien. Deux sondes positives ne prouvent pas qu'une garde existe.
    assert set(retained) == {
        "conversation.id",
        "event.kind",
        "event.name",
        "input_token_count",
        "model",
    }
