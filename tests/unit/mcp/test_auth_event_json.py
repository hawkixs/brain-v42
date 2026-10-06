"""Freeze the actual JSON lines a container log watcher receives."""

import json
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from uuid import UUID

import pytest
import structlog

from brain_v42.credentials.audit import render_event
from brain_v42.credentials.reasons import emit_refusal, reset_refusal_counts
from brain_v42.repositories.pg_client_credentials import AuditRow
from brain_v42.safe_logging import build_logging_processors

GOLDEN = Path(__file__).parent / "golden" / "auth_events.jsonl"
NOW = datetime(2026, 1, 1, tzinfo=UTC)
CREDENTIAL_ID = "00000000-0000-0000-0000-000000000001"
ELEVATION_ID = "00000000-0000-0000-0000-000000000002"
SESSION_ID = "00000000-0000-0000-0000-000000000003"
ENVELOPE = {"event", "level", "timestamp"}
ELEVATION_KEYS = {
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


def representative_rows() -> list[AuditRow]:
    credential = {
        "credential_id": CREDENTIAL_ID,
        "client_id": "red-rail-reviewer",
        "families": ["read"],
        "author": "operator",
    }
    elevation = {
        "elevation_id": ELEVATION_ID,
        "session_id": SESSION_ID,
        "session_label": None,
        "expires_at": "2026-01-01T01:00:00+00:00",
        "ttl_seconds": 3600,
        "reason": "maintenance",
        "via": "hook",
        "client_id": "workstation-claude",
        "connection_count": 2,
        "excluded_client_ids": ["red-rail", "auto-discord", "red-rail"],
        "excluded_connection_count": 3,
    }
    return [
        AuditRow(1, "credentials.issued", None, credential, NOW, None),
        AuditRow(2, "credentials.revoked", None, {**credential, "reason": "rotated"}, NOW, None),
        *[
            AuditRow(index, event, UUID(ELEVATION_ID), elevation, NOW, None)
            for index, event in enumerate(
                ("credentials.elevated", "credentials.unelevated", "credentials.elevation_expired"),
                3,
            )
        ],
    ]


def test_rendered_auth_and_audit_lines_match_the_native_json_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.config import Settings

    monkeypatch.setenv("BRAIN_LOG_FORMAT", "json")
    settings = Settings(postgres_url="postgresql+asyncpg://localhost/unused", _env_file=None)
    previous = structlog.get_config().copy()
    output = StringIO()
    reset_refusal_counts()
    try:
        structlog.configure(
            processors=build_logging_processors(settings.brain_log_format),
            logger_factory=structlog.PrintLoggerFactory(file=output),
            wrapper_class=structlog.make_filtering_bound_logger(0),
            cache_logger_on_first_use=False,
        )
        emit_refusal("unknown_token", status=401, peer="127.0.0.1", path="/mcp")
        emit_refusal(
            "agent_mismatch",
            status=403,
            client_id="red-rail-reviewer",
            declared_agent="red-rail",
            peer="127.0.0.1",
            path="/mcp",
        )
        emit_refusal(
            "foreign_client_attach",
            status=403,
            requesting_client_id="red-rail-reviewer",
            session_id=SESSION_ID,
            owner_client_id="workstation-claude",
            peer="127.0.0.1",
        )
        emit_refusal("registry_unavailable", status=503, peer="127.0.0.1", path="/mcp")
        for row in representative_rows():
            fields = render_event(row)
            structlog.get_logger().warning(fields.pop("event"), **fields)
    finally:
        structlog.configure(**previous)
        reset_refusal_counts()
    lines = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(lines) == 9
    for line in lines:
        assert line["level"] == "warning"
        assert line["timestamp"].endswith("Z")
        assert datetime.fromisoformat(line["timestamp"]).utcoffset().total_seconds() == 0
    refusal_keys = {"reason", "status", "peer", "path"}
    assert lines[0].keys() == ENVELOPE | refusal_keys
    assert lines[1].keys() == ENVELOPE | refusal_keys | {"client_id", "declared_agent"}
    assert lines[2].keys() == ENVELOPE | {
        "reason",
        "status",
        "peer",
        "requesting_client_id",
        "session_id",
        "owner_client_id",
    }
    assert lines[3].keys() == ENVELOPE | refusal_keys
    assert all(type(line["status"]) is int for line in lines[:4])
    credential_keys = {"credential_id", "client_id", "families", "author"}
    assert lines[4].keys() == ENVELOPE | credential_keys
    assert lines[5].keys() == ENVELOPE | credential_keys | {"reason"}
    assert lines[4]["families"] == lines[5]["families"] == ["read"]
    for line in lines[6:]:
        assert line.keys() == ENVELOPE | ELEVATION_KEYS
        assert line["excluded_client_ids"] == ["auto-discord", "red-rail"]
        assert line["session_label"] is None
        assert all(
            type(line[key]) is int
            for key in (
                "connection_count",
                "excluded_connection_count",
                "ttl_seconds",
            )
        )
    expected = [json.loads(line) for line in GOLDEN.read_text().splitlines()]
    # Every field except the wall-clock timestamp must match, including key sets.
    assert [{k: v for k, v in line.items() if k != "timestamp"} for line in lines] == [
        {k: v for k, v in line.items() if k != "timestamp"} for line in expected
    ]
