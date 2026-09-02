"""Bind address of the metrics sidecar — loopback by default.

Decision of 2026-07-04 (supersedes b68356c2): the historical 0.0.0.0 served the
GitLab webhook (LAN URL 192.168.1.12:9200), dead since 2026-06-24 (secret lost in
a unit regeneration, fails closed 401). Real consumers: red-monitor on loopback
only. A secure default; a METRICS_HOST env override stays possible should the
webhook ever be revived (docker gateway — NO loopback-only validator, unlike
mcp_http_host).
"""

from __future__ import annotations

import inspect

import pytest

from brain_v42.config import Settings
from brain_v42.metrics.server import MetricsServer


def test_settings_metrics_host_defaults_to_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    # Settings() requires POSTGRES_URL (learning 80b4e8a6) — in CI there is neither
    # a .env nor an env var on the unit job: we provide a dummy URL so that the test
    # stays hermetic and validates ONLY metrics_host's default.
    monkeypatch.setenv("POSTGRES_URL", "postgresql+asyncpg://t:t@localhost:5433/t")
    assert Settings().metrics_host == "127.0.0.1"


def test_metrics_server_host_defaults_to_loopback() -> None:
    default = inspect.signature(MetricsServer.__init__).parameters["host"].default
    assert default == "127.0.0.1"
