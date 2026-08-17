"""Bind address du sidecar metrics — loopback par défaut.

Décision 2026-07-04 (supersède b68356c2) : le 0.0.0.0 historique servait le
webhook GitLab (URL LAN 192.168.1.12:9200), mort depuis le 2026-06-24 (secret
perdu à une régénération d'unit, fails closed 401). Consommateurs réels :
red-monitor en loopback uniquement. Default sécurisé ; un override env
METRICS_HOST reste possible si le webhook est un jour ressuscité (gateway
docker — PAS de validator loopback-only, contrairement à mcp_http_host).
"""

from __future__ import annotations

import inspect

import pytest

from brain_v42.config import Settings
from brain_v42.metrics.server import MetricsServer


def test_settings_metrics_host_defaults_to_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    # Settings() exige POSTGRES_URL (learning 80b4e8a6) — en CI il n'y a ni
    # .env ni var d'env sur le job unit : on fournit une URL factice pour que
    # le test reste hermétique et ne valide QUE le default de metrics_host.
    monkeypatch.setenv("POSTGRES_URL", "postgresql+asyncpg://t:t@localhost:5433/t")
    assert Settings().metrics_host == "127.0.0.1"


def test_metrics_server_host_defaults_to_loopback() -> None:
    default = inspect.signature(MetricsServer.__init__).parameters["host"].default
    assert default == "127.0.0.1"
