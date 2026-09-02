"""Bounded decoding of Claude Code's OTLP/HTTP JSON logs.

The oracle for the attribute names is `tests/fixtures/claude_otlp_logs.json`, a
real capture from the spike (Claude Code 2.1.220, verdict in
`docs/upstream/2026-08-06-claude-otlp-session-join.md`). When the fixture and the
plan diverge, the fixture wins: it was measured, the plan assumed.

Three measured discrepancies these tests freeze:

1. `event.name` is NOT prefixed — the attribute is `user_prompt`, the
   `claude_code.` prefix lives in `body.stringValue`.
2. `input_tokens` does not measure the context (10 observed when
   `cache_read_tokens` was 12,973): the three input counters must be projected.
3. Every record carries `user.email` in the clear. Allowlist projection is the only
   barrier between that address and a registry exposed over HTTP — hence the file's
   first test.
"""

from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any

import pytest

from brain_v42.metrics.claude_telemetry import (
    _PROJECTED_KEYS,
    decode_claude_logs,
)
from brain_v42.metrics.claude_telemetry import (
    # The filter itself, aliased: `_attribute` below BUILDS an OTLP attribute,
    # `_attributes` PROJECTS a record — one character apart for two opposite roles.
    _attributes as _project_attributes,
)
from brain_v42.metrics.codex_telemetry import (
    MAX_REQUEST_BYTES,
    CodexTelemetryLimitError,
    CodexTelemetryMalformedError,
)

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "claude_otlp_logs.json"

FAKE_UUID = "12345678-1234-4abc-8def-1234567890ab"
# Distinct from the session: reusing FAKE_UUID would make the absence assertion
# uninterpretable, the legitimate session then carrying the same string.
ACCOUNT_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ORGANIZATION_UUID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

# Values that must NEVER come back out of the decoding, in any shape.
SENSITIVE_VALUES: dict[str, str] = {
    "user.email": "personne@exemple.test",
    "user.id": "e" * 64,
    "user.account_uuid": ACCOUNT_UUID,
    "user.account_id": "compte-en-clair-42",
    "organization.id": ORGANIZATION_UUID,
    "prompt": "phrase secrete de l operateur",
}

# Name fragments that betray a personal-data channel, whether in a returned field
# or in a key admitted upstream.
PERSONAL_DATA_MARKERS = ("email", "user", "account", "organization", "prompt")

# Allowlist values: their survival is the positive control for every absence
# assertion in this file.
PROJECTED_MODEL = "claude-opus-5"


def _attribute(key: str, value: dict[str, object]) -> dict[str, object]:
    return {"key": key, "value": value}


def _record(
    *,
    event_name: str = "user_prompt",
    session_id: str | None = FAKE_UUID,
    timestamp: str = "1786039576188000000",
    extra: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    attributes = [_attribute("event.name", {"stringValue": event_name})]
    if session_id is not None:
        attributes.append(_attribute("session.id", {"stringValue": session_id}))
    attributes.extend(extra or [])
    return {"timeUnixNano": timestamp, "attributes": attributes}


def _envelope(records: list[dict[str, object]]) -> bytes:
    return json.dumps(
        {"resourceLogs": [{"scopeLogs": [{"logRecords": records}]}]},
        separators=(",", ":"),
    ).encode()


def _sensitive_attributes() -> list[dict[str, object]]:
    return [_attribute(key, {"stringValue": value}) for key, value in SENSITIVE_VALUES.items()]


def _readable_state(record: object) -> dict[str, Any]:
    """Every readable value of a decoded record, name by name.

    Does not assume the dataclass: a decoder storing the raw attributes in some
    other object must be seen by this test.
    """
    if is_dataclass(record) and not isinstance(record, type):
        names = [field.name for field in fields(record)]
    else:
        names = [
            name
            for name in dir(record)
            if not name.startswith("__") and not callable(getattr(record, name))
        ]
    return {name: getattr(record, name) for name in names}


class TestPersonalDataNeverSurvives:
    """The allowlist is the only barrier before a registry exposed over HTTP.

    Three witnesses, because a barrier breaks in three ways: the output (a personal
    field in the decoded record), the LIST (a personal key admitted) and the FILTER
    (the list no longer consulted at all). The first two see nothing of the third.
    """

    def test_account_identifiers_never_survive_the_projection(self) -> None:
        payload = _envelope(
            [
                _record(
                    event_name="api_request",
                    extra=[
                        _attribute("model", {"stringValue": PROJECTED_MODEL}),
                        _attribute("input_tokens", {"intValue": 10}),
                        *_sensitive_attributes(),
                    ],
                ),
                # Measured: hook events ALSO carry the email in the clear.
                _record(event_name="hook_execution_start", extra=_sensitive_attributes()),
            ]
        )

        # Input control: the sentinels are indeed in the submitted payload.
        for key, value in SENSITIVE_VALUES.items():
            assert value.encode() in payload, f"sentinelle {key} absente de la charge de test"

        records = decode_claude_logs(payload)

        assert len(records) == 1, "l'événement de hook ne doit produire aucun enregistrement"
        state = _readable_state(records[0])
        rendered_by_name = {name: repr(value) for name, value in state.items()}

        # Positive control: the read channel does show the projected values. Without
        # it, "no sentinel found" would go green on an empty object.
        assert any(PROJECTED_MODEL in rendered for rendered in rendered_by_name.values()), (
            f"le modèle projeté n'apparaît dans aucun champ lu : {rendered_by_name}"
        )
        assert any(FAKE_UUID in rendered for rendered in rendered_by_name.values()), (
            f"la session projetée n'apparaît dans aucun champ lu : {rendered_by_name}"
        )

        for name, rendered in rendered_by_name.items():
            for key, value in SENSITIVE_VALUES.items():
                assert value not in rendered, f"{key} a survécu dans le champ {name}"

        # No field must even offer a named channel for this data.
        for name in rendered_by_name:
            lowered = name.lower()
            for marker in PERSONAL_DATA_MARKERS:
                assert marker not in lowered, f"le champ {name} ouvre un canal « {marker} »"

        # And nothing must leak through a side structure of the result.
        whole = repr(records)
        for key, value in SENSITIVE_VALUES.items():
            assert value not in whole, f"{key} a survécu dans le résultat complet"

    def test_the_whitelist_admits_no_personal_data_key(self) -> None:
        """The upstream barrier, tested on its own terms.

        The test above only reads the fields of the returned record: it therefore
        sees ONLY the second barrier, the frozen list of fields. Measured by
        mutation — adding ``user.email`` to ``_PROJECTED_KEYS`` without touching the
        dataclass let it through all twelve tests. Such a widening leaks nothing the
        day it is made; it arms the leak for the next field added. The allowlist
        being described as "the only barrier", it must bite on its own.
        """
        # Positive control: without it, an emptied or renamed allowlist would make
        # the two assertions below true for nothing.
        assert {"session.id", "event.name", "model"} <= _PROJECTED_KEYS

        admitted = SENSITIVE_VALUES.keys() & _PROJECTED_KEYS
        assert not admitted, f"clés personnelles mesurées admises : {sorted(admitted)}"

        for key in sorted(_PROJECTED_KEYS):
            lowered = key.lower()
            for marker in PERSONAL_DATA_MARKERS:
                assert marker not in lowered, f"la liste blanche admet {key} (canal « {marker} »)"

    def test_the_projection_drops_every_key_outside_the_whitelist(self) -> None:
        """The FILTER, exercised on what it retains — not on what the list contains.

        The two tests above cannot see this third defect. Measured on 2026-08-07:
        removing ``if key not in _PROJECTED_KEYS: continue`` entirely leaves
        ``decode_claude_logs`` returning an IDENTICAL ``ClaudeRecord``, field for
        field, on a payload carrying the five personal identifiers. The dataclass is
        frozen: an unprojected attribute has nowhere to land, so no assertion on the
        returned object can bite. An allowlist that is never consulted is
        nevertheless a dead barrier, and the next field added to the dataclass would
        cross it without breaking anything.

        The only place the barrier is observable is therefore the projection itself:
        four keys retained out of the ten submitted here. That is what this test
        measures, at the exact place the filter applies.
        """
        record = _record(
            event_name="api_request",
            extra=[
                _attribute("model", {"stringValue": PROJECTED_MODEL}),
                _attribute("input_tokens", {"intValue": 10}),
                *_sensitive_attributes(),
            ],
        )

        retained = _project_attributes(record)

        leaked = SENSITIVE_VALUES.keys() & retained.keys()
        assert not leaked, f"clés personnelles retenues par la projection : {sorted(leaked)}"

        # Exhaustive: it also bites on a personal key we have not measured. Positive
        # control included — an empty projection would fail here, which forbids the
        # absence assertion above from passing for nothing.
        assert set(retained) == {"event.name", "session.id", "model", "input_tokens"}


class TestEventFiltering:
    def test_bare_event_name_is_recognized_and_prefixed_one_is_not(self) -> None:
        """The `claude_code.` prefix is in the BODY, not in `event.name`.

        Measured on 2026-08-06. The two records are submitted together: a decoder
        filtering on `claude_code.*` would return nothing, a decoder filtering
        nothing would return two.
        """
        payload = _envelope(
            [
                _record(event_name="claude_code.user_prompt"),
                _record(event_name="user_prompt"),
            ]
        )

        records = decode_claude_logs(payload)

        assert [record.event_name for record in records] == ["user_prompt"]

    def test_unknown_event_is_ignored_while_known_event_survives(self) -> None:
        payload = _envelope(
            [
                _record(event_name="tool_decision"),
                _record(event_name="plugin_loaded"),
                _record(event_name="api_request"),
            ]
        )

        records = decode_claude_logs(payload)

        assert [record.event_name for record in records] == ["api_request"]


class TestCounters:
    def test_the_three_input_counters_are_projected(self) -> None:
        """`input_tokens` alone would under-estimate the context by three orders."""
        payload = _envelope(
            [
                _record(
                    event_name="api_request",
                    extra=[
                        _attribute("input_tokens", {"intValue": 10}),
                        _attribute("cache_read_tokens", {"intValue": 11_776}),
                        _attribute("cache_creation_tokens", {"intValue": 6_804}),
                        _attribute("output_tokens", {"intValue": 43}),
                    ],
                )
            ]
        )

        record = decode_claude_logs(payload)[0]

        assert record.input_tokens == 10
        assert record.cache_read_tokens == 11_776
        assert record.cache_creation_tokens == 6_804
        assert record.output_tokens == 43

    def test_negative_cost_is_dropped_and_positive_cost_survives(self) -> None:
        payload = _envelope(
            [
                _record(
                    event_name="api_request",
                    extra=[_attribute("cost_usd", {"doubleValue": -1.0})],
                ),
                _record(
                    event_name="api_request",
                    extra=[_attribute("cost_usd", {"doubleValue": 0.0125})],
                ),
            ]
        )

        negative, positive = decode_claude_logs(payload)

        assert negative.cost_usd is None
        # Positive control: without it, a decoder losing ALL cost would pass.
        assert positive.cost_usd == pytest.approx(0.0125)

    def test_absent_model_falls_back_to_unknown_and_present_model_is_kept(self) -> None:
        payload = _envelope(
            [
                _record(event_name="api_request"),
                _record(
                    event_name="api_request",
                    extra=[_attribute("model", {"stringValue": PROJECTED_MODEL})],
                ),
            ]
        )

        absent, present = decode_claude_logs(payload)

        assert absent.model == "unknown"
        assert present.model == PROJECTED_MODEL


class TestMalformedSession:
    def test_non_uuid_session_is_malformed(self) -> None:
        with pytest.raises(CodexTelemetryMalformedError):
            decode_claude_logs(_envelope([_record(session_id="not-a-uuid")]))

    def test_missing_session_is_malformed(self) -> None:
        with pytest.raises(CodexTelemetryMalformedError):
            decode_claude_logs(_envelope([_record(session_id=None)]))

    def test_canonical_session_is_accepted(self) -> None:
        """Positive control for the two rejections above.

        Without it, a decoder raising on ANY payload would make them pass.
        """
        record = decode_claude_logs(_envelope([_record(session_id=FAKE_UUID)]))[0]

        assert record.session_id == FAKE_UUID


class TestPayloadBounds:
    def test_payload_over_the_shared_limit_is_rejected(self) -> None:
        oversized = b"{" + b" " * MAX_REQUEST_BYTES + b"}"

        assert len(oversized) > MAX_REQUEST_BYTES
        with pytest.raises(CodexTelemetryLimitError):
            decode_claude_logs(oversized)

    def test_payload_at_the_shared_limit_still_decodes(self) -> None:
        """Positive control: the rejection above is about the size, not the rest."""
        envelope = _envelope([_record()])
        padded = envelope + b" " * (MAX_REQUEST_BYTES - len(envelope))

        assert len(padded) == MAX_REQUEST_BYTES
        assert len(decode_claude_logs(padded)) == 1


class TestRealCapture:
    """The spike's capture is the oracle: if this test fails, it is the code."""

    def test_recorded_capture_decodes_with_its_measured_counters(self) -> None:
        records = decode_claude_logs(FIXTURE_PATH.read_bytes())

        assert [record.event_name for record in records] == ["user_prompt", "api_request"]
        assert {record.session_id for record in records} == {FAKE_UUID}

        prompt, request = records
        assert prompt.timestamp == 1786039576188000000
        assert request.model == "claude-haiku-4-5-20251001"
        assert request.input_tokens == 10
        assert request.cache_read_tokens == 12_973
        assert request.cache_creation_tokens == 5_606
        assert request.output_tokens == 43
        assert request.cost_usd == pytest.approx(0.0127343)
