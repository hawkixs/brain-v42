"""Refusals have one bounded observation path and a closed vocabulary."""

import inspect
from collections.abc import Iterator

import pytest
from structlog.testing import capture_logs

from brain_v42.credentials.reasons import (
    emit_refusal,
    refusal_counts,
    reset_refusal_counts,
)

PAIRS = [
    ("missing_token", 401),
    ("unknown_token", 401),
    ("revoked_token", 401),
    ("expired_token", 401),
    ("agent_mismatch", 403),
    ("foreign_client_attach", 403),
    ("rate_limited", 429),
    ("registry_unavailable", 503),
    ("family_denied", 403),
    ("issuer_not_allowed", 403),
]


@pytest.fixture(autouse=True)
def clean_counts() -> Iterator[None]:
    reset_refusal_counts()
    yield
    reset_refusal_counts()


@pytest.mark.parametrize(("reason", "status"), PAIRS)
def test_each_refusal_emits_exactly_one_warning(reason: str, status: int) -> None:
    with capture_logs() as logs:
        emit_refusal(reason, status=status, requesting_client_id="client")
    assert logs == [
        {
            "event": "mcp_auth.refused",
            "log_level": "warning",
            "reason": reason,
            "status": status,
            "requesting_client_id": "client",
        }
    ]


@pytest.mark.parametrize(("reason", "status"), PAIRS)
def test_wrong_status_has_no_observation(reason: str, status: int) -> None:
    with capture_logs() as logs, pytest.raises(ValueError):
        emit_refusal(reason, status=status + 1, requesting_client_id="client")
    assert logs == []
    assert refusal_counts() == {}


def test_unknown_reason_is_rejected_without_echoing_input() -> None:
    token = "tok-" + "x" * 40
    with pytest.raises(ValueError) as refused:
        emit_refusal(token, status=401)
    assert token not in str(refused.value)
    assert refusal_counts() == {}


def test_counts_start_empty_and_count_one_per_call() -> None:
    assert refusal_counts() == {}
    emit_refusal("missing_token", status=401)
    emit_refusal("missing_token", status=401)
    emit_refusal("registry_unavailable", status=503)
    assert refusal_counts() == {"missing_token": 2, "registry_unavailable": 1}


def test_counts_are_an_independent_copy() -> None:
    emit_refusal("missing_token", status=401)
    refusal_counts().clear()
    assert refusal_counts() == {"missing_token": 1}


def test_registry_unavailable_preserves_peer() -> None:
    with capture_logs() as logs:
        emit_refusal("registry_unavailable", status=503, peer="loopback")
    assert logs[0]["status"] == 503
    assert logs[0]["peer"] == "loopback"
    assert "client_id" not in logs[0]


def test_foreign_attach_preserves_watcher_fields() -> None:
    with capture_logs() as logs:
        emit_refusal(
            "foreign_client_attach",
            status=403,
            requesting_client_id="requester",
            owner_client_id="owner",
            session_id="session",
            peer="loopback",
        )
    for field, value in {
        "requesting_client_id": "requester",
        "owner_client_id": "owner",
        "session_id": "session",
        "peer": "loopback",
    }.items():
        assert logs[0][field] == value


def test_foreign_attach_requires_requesting_client() -> None:
    with pytest.raises(ValueError):
        emit_refusal("foreign_client_attach", status=403)
    assert refusal_counts() == {}


def test_attacker_controlled_text_is_bounded() -> None:
    with capture_logs() as logs:
        emit_refusal(
            "agent_mismatch",
            status=403,
            declared_agent="A" * 80,
            path="/" * 200,
            tool="t" * 200,
        )
    assert logs[0]["declared_agent"] == "_" * 64
    assert logs[0]["path"] == "/" * 128
    assert logs[0]["tool"] == "t" * 128


def test_token_arguments_cannot_enter_the_observation_path() -> None:
    assert all("token" not in name for name in inspect.signature(emit_refusal).parameters)


@pytest.mark.parametrize(
    "field",
    ["client_id", "requesting_client_id", "owner_client_id", "session_id", "peer"],
)
def test_identity_and_peer_fields_are_bounded(field: str) -> None:
    with capture_logs() as logs:
        emit_refusal("unknown_token", status=401, **{field: "x" * 300})
    assert logs[0][field] == "x" * 128
