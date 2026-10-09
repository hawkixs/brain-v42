"""Only a named Compose peer may extend the loopback activity egress boundary."""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from brain_v42.config import Settings


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in os.environ:
        if name.startswith(("BRAIN_", "CLIENT_ACTIVITY_", "MCP_", "GRAPH_", "NEO4J_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("POSTGRES_URL", "postgresql+asyncpg://brain_app@postgres:5432/brain_test")


def test_activity_allowed_host_is_empty_by_default() -> None:
    assert Settings(_env_file=None).client_activity_allowed_host == ""


@pytest.mark.parametrize(
    "alias", ["BRAIN_CLIENT_ACTIVITY_ALLOWED_HOST", "CLIENT_ACTIVITY_ALLOWED_HOST"]
)
def test_named_peer_is_accepted_through_environment_aliases(
    monkeypatch: pytest.MonkeyPatch, alias: str
) -> None:
    monkeypatch.setenv(alias, "metrics")
    monkeypatch.setenv("BRAIN_CLIENT_ACTIVITY_URL", "http://metrics:9200/v1/client-activity")
    settings = Settings(_env_file=None)
    assert settings.client_activity_allowed_host == "metrics"
    assert settings.client_activity_url == "http://metrics:9200/v1/client-activity"


@pytest.mark.parametrize("host", ["other", "metrics.example.com", "notmetrics", "metrics-other"])
def test_named_peer_does_not_authorize_any_other_host(host: str) -> None:
    with pytest.raises(ValidationError, match="client_activity_url must be loopback"):
        Settings(
            _env_file=None,
            client_activity_allowed_host="metrics",
            client_activity_url=f"http://{host}:9200/v1/client-activity",
        )


@pytest.mark.parametrize(
    "host",
    [
        "http://metrics",
        "metrics:9200",
        "metrics/path",
        "user@metrics",
        "metrics other",
        " metrics",
        "metrics ",
        "metrics\n",
        "metrics\t",
        "metrics,other",
        "*",
        "metrics\\other",
        "-metrics",
        "metrics-",
    ],
)
def test_allowed_host_refuses_non_host_values_even_with_a_loopback_url(host: str) -> None:
    with pytest.raises(
        ValidationError, match="client_activity_allowed_host must be a bare host name"
    ):
        Settings(_env_file=None, client_activity_allowed_host=host)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
def test_named_peer_preserves_loopback_targets(host: str) -> None:
    url = f"http://{host}:9200/v1/client-activity"
    assert (
        Settings(
            _env_file=None, client_activity_allowed_host="metrics", client_activity_url=url
        ).client_activity_url
        == url
    )


@pytest.mark.parametrize(
    "host",
    [
        "10.0.0.5",
        "127.0.0.1",
        "0",
        "8.8.8.8",
        "metrics.123",
        "0xAABBCCDD",
        "0x7f.1",
        "017700000001",
    ],
)
def test_allowed_host_refuses_ip_literals_and_numeric_top_labels(host: str) -> None:
    # A name is the contract: an IP literal would bypass the Compose service
    # name and could point anywhere on the network.
    with pytest.raises(
        ValidationError, match="client_activity_allowed_host must be a bare host name"
    ):
        Settings(_env_file=None, client_activity_allowed_host=host)


@pytest.mark.parametrize("host", ["metrics", "brain-v42-metrics", "metrics2", "metrics.internal"])
def test_allowed_host_accepts_compose_service_names(host: str) -> None:
    assert (
        Settings(_env_file=None, client_activity_allowed_host=host).client_activity_allowed_host
        == host
    )
