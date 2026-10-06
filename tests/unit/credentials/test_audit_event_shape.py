"""Pin the watcher contract independently of the payload kept by the outbox."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from structlog.testing import capture_logs

from brain_v42.repositories.pg_client_credentials import AuditRow

NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)
ELEVATION_EVENTS = (
    "credentials.elevated",
    "credentials.unelevated",
    "credentials.elevation_expired",
)
EXPECTED_KEYS = frozenset(
    {
        "elevation_id",
        "session_id",
        "session_label",
        "expires_at",
        "ttl_seconds",
        "reason",
        "via",
        "client_id",
        "connection_count",
        "excluded_client_ids",
        "excluded_connection_count",
    }
)


def elevation_row(event_name: str = "credentials.elevated", **payload: Any) -> AuditRow:
    elevation_id = uuid4()
    return AuditRow(
        id=1,
        event=event_name,
        elevation_id=elevation_id,
        payload={
            "elevation_id": str(elevation_id),
            "session_id": str(uuid4()),
            "session_label": "operator:slot-1",
            "expires_at": NOW.isoformat(),
            "ttl_seconds": 3600,
            "reason": "maintenance",
            "author": "operator",
            "via": "hook",
            "client_id": "workstation-elevate",
            "connection_count": 2,
            "excluded_client_ids": [],
            "excluded_connection_count": 0,
            **payload,
        },
        created_at=NOW,
        emitted_at=None,
    )


@pytest.mark.parametrize("event", ELEVATION_EVENTS)
def test_elevation_events_have_exactly_the_frozen_keys(event: str) -> None:
    from brain_v42.credentials.audit import ELEVATION_EVENT_KEYS, render_event

    assert ELEVATION_EVENT_KEYS == EXPECTED_KEYS
    assert isinstance(ELEVATION_EVENT_KEYS, frozenset)
    row = elevation_row(event)
    rendered = render_event(row)
    assert rendered.keys() == EXPECTED_KEYS | {"event"}
    assert rendered["event"] == event
    assert {key: rendered[key] for key in EXPECTED_KEYS} == {
        key: row.payload[key] for key in EXPECTED_KEYS
    }


@pytest.mark.parametrize("excluded", [[], ["red-rail", "auto-discord", "red-rail"]])
def test_excluded_clients_are_sorted_and_deduplicated(excluded: list[str]) -> None:
    from brain_v42.credentials.audit import render_event

    row = elevation_row(excluded_client_ids=excluded)
    assert render_event(row)["excluded_client_ids"] == sorted(set(excluded))
    assert row.payload["excluded_client_ids"] == excluded


def test_missing_excluded_clients_render_as_an_empty_list() -> None:
    from brain_v42.credentials.audit import render_event

    row = elevation_row()
    del row.payload["excluded_client_ids"]
    assert render_event(row)["excluded_client_ids"] == []


def test_session_label_is_cut_at_64_and_every_disallowed_character_is_replaced() -> None:
    from brain_v42.credentials.audit import render_event

    label = "az09.:_*-AZ /é\n" + "x" * 100
    assert render_event(elevation_row(session_label=label))["session_label"] == (
        "az09.:_*-" + "_" * 6 + "x" * 49
    )


@pytest.mark.parametrize("event", (*ELEVATION_EVENTS, "credentials.revoked"))
def test_reason_replaces_all_cc_characters_and_is_cut_at_200(event: str) -> None:
    from brain_v42.credentials.audit import render_event

    reason = "fix\n\r\t\x00\x7f\x85" + "x" * 250
    row = elevation_row(event, reason=reason, credential_id=str(uuid4()), families=["read"])
    assert render_event(row)["reason"] == "fix" + " " * 6 + "x" * 191


def test_reason_preserves_characters_outside_the_cc_category() -> None:
    from brain_v42.credentials.audit import render_event

    reason = "rotate café\u200d\u2028"
    assert render_event(elevation_row(reason=reason))["reason"] == reason


@pytest.mark.parametrize(
    ("event", "keys"),
    [
        ("credentials.issued", {"credential_id", "client_id", "families", "author", "reason"}),
        (
            "credentials.revoked",
            {"credential_id", "client_id", "families", "author", "reason"},
        ),
    ],
)
def test_credential_events_pass_through_only_the_repository_fields(
    event: str, keys: set[str]
) -> None:
    from brain_v42.credentials.audit import render_event

    payload = {
        "credential_id": str(uuid4()),
        "client_id": "auto-discord",
        "families": ["read", "write"],
        "author": "operator",
        "reason": "rotated",
        "token": "tok-" + "x" * 40,
        "token_sha256": "a" * 64,
        "connection_id": "conn-" + uuid4().hex,
        "event": "stray.event",
    }
    row = AuditRow(1, event, None, payload, NOW, None)
    assert render_event(row) == {"event": event, **{key: payload[key] for key in keys}}


def test_legacy_issued_event_without_reason_keeps_its_original_shape() -> None:
    from brain_v42.credentials.audit import render_event

    payload = {
        "credential_id": str(uuid4()),
        "client_id": "auto-discord",
        "families": ["read"],
        "author": "operator",
    }
    assert render_event(AuditRow(1, "credentials.issued", None, payload, NOW, None)) == {
        "event": "credentials.issued",
        **payload,
    }


def test_issued_reason_is_sanitized_for_the_watcher() -> None:
    from brain_v42.credentials.audit import render_event

    row = elevation_row(
        "credentials.issued",
        credential_id=str(uuid4()),
        families=["read"],
        reason="setup\n\x00" + "x" * 250,
    )
    assert render_event(row)["reason"] == "setup  " + "x" * 193


@pytest.mark.parametrize("event", (*ELEVATION_EVENTS, "credentials.issued", "credentials.revoked"))
async def test_drainer_emits_events_at_warning(event: str) -> None:
    from brain_v42.credentials.audit import AuditDrainer
    from tests.unit.credentials.test_audit_drainer import FakeRepo

    row = elevation_row(event, credential_id=str(uuid4()), families=["read"])
    with capture_logs() as logs:
        assert await AuditDrainer(FakeRepo([row]), clock=lambda: NOW).drain_once() == 1
    assert logs[0]["event"] == event
    assert logs[0]["log_level"] == "warning"


async def test_emitted_fields_never_include_stray_connection_ids_or_secrets() -> None:
    from brain_v42.credentials.audit import AuditDrainer
    from tests.unit.credentials.test_audit_drainer import FakeRepo

    connection_id = "conn-" + uuid4().hex
    token = "tok-" + "x" * 40
    digest = "a" * 64
    row = elevation_row(
        connection_id=connection_id,
        connection_ids=[connection_id],
        token=token,
        token_sha256=digest,
        event="stray.event",
    )
    with capture_logs() as logs:
        await AuditDrainer(FakeRepo([row]), clock=lambda: NOW).drain_once()
    assert logs[0].keys() == EXPECTED_KEYS | {"event", "log_level"}
    dump = json.dumps(logs)
    assert connection_id not in dump
    assert token not in dump
    assert digest not in dump
