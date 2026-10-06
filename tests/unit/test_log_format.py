"""The container watcher consumes a strict, typed logging wire format."""

from __future__ import annotations

import ast
import asyncio
import io
import json
import logging
import os
import re
import sys
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog
from pydantic import ValidationError

from brain_v42.config import Settings, get_settings
from brain_v42.mcp import server as mcp_server
from brain_v42.metrics import __main__ as metrics_entrypoint
from brain_v42.metrics.runtime import build_sidecar_structlog_processors


@pytest.fixture(autouse=True)
def _restore_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    saved = structlog.get_config()
    get_settings.cache_clear()
    monkeypatch.setenv("POSTGRES_URL", "postgresql+asyncpg://test@localhost/log_test")
    monkeypatch.setattr(logging, "basicConfig", lambda **_: None)
    yield
    structlog.reset_defaults()
    structlog.configure(**saved)
    get_settings.cache_clear()


def test_log_format_defaults_to_console(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRAIN_LOG_FORMAT", raising=False)
    settings = Settings(postgres_url="postgresql+asyncpg://test@localhost/log_test", _env_file=None)
    assert settings.brain_log_format == "console"


@pytest.mark.parametrize("renderer", ["console", "json"])
def test_log_format_loads_from_environment(monkeypatch: pytest.MonkeyPatch, renderer: str) -> None:
    monkeypatch.setenv("BRAIN_LOG_FORMAT", renderer)
    settings = Settings(postgres_url="postgresql+asyncpg://test@localhost/log_test", _env_file=None)
    assert settings.brain_log_format == renderer


@pytest.mark.parametrize("renderer", ["yaml", "JSON", "", " json", "console "])
def test_unknown_log_format_refuses_settings_load(
    monkeypatch: pytest.MonkeyPatch, renderer: str
) -> None:
    monkeypatch.setenv("BRAIN_LOG_FORMAT", renderer)
    with pytest.raises(ValidationError, match="BRAIN_LOG_FORMAT must be 'console' or 'json'"):
        Settings(postgres_url="postgresql+asyncpg://test@localhost/log_test", _env_file=None)


@pytest.mark.parametrize("service", ["mcp", "metrics"])
def test_json_renders_one_typed_line(monkeypatch: pytest.MonkeyPatch, service: str) -> None:
    buffer = io.StringIO()
    monkeypatch.setenv("BRAIN_LOG_FORMAT", "json")
    if service == "mcp":
        monkeypatch.setattr(sys, "stderr", buffer)
        mcp_server._configure_stdio_logging()
    else:
        structlog.configure(
            processors=build_sidecar_structlog_processors(MagicMock(), log_format="json"),
            logger_factory=structlog.PrintLoggerFactory(file=buffer),
        )
    buffer.seek(0)
    buffer.truncate()

    structlog.get_logger().info("typed.event", items=["first", "second"], count=42, optional=None)

    lines = buffer.getvalue().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "typed.event"
    assert payload["level"] == "info"
    assert datetime.fromisoformat(payload["timestamp"]).utcoffset() == timedelta(0)
    assert payload["items"] == ["first", "second"]
    assert type(payload["count"]) is int
    assert payload["count"] == 42
    assert payload["optional"] is None


@pytest.mark.parametrize("renderer", ["console", "json"])
@pytest.mark.parametrize("service", ["mcp", "metrics"])
def test_configure_emits_exactly_one_startup_marker(
    monkeypatch: pytest.MonkeyPatch, renderer: str, service: str
) -> None:
    output = io.StringIO()
    other_stream = io.StringIO()
    monkeypatch.setenv("BRAIN_LOG_FORMAT", renderer)
    monkeypatch.setattr(sys, "stderr", output if service == "mcp" else other_stream)
    monkeypatch.setattr(sys, "stdout", output if service == "metrics" else other_stream)

    if service == "mcp":
        mcp_server._configure_stdio_logging()
    else:
        metrics_entrypoint._configure_logging(MagicMock())

    assert other_stream.getvalue() == ""
    lines = output.getvalue().splitlines()
    assert len(lines) == 1
    if renderer == "json":
        payload = json.loads(lines[0])
        assert payload["event"] == "logging.configured"
        assert payload["level"] == "info"
        assert payload["renderer"] == renderer
        assert payload["service"] == f"brain-v42-{service}"
        assert payload["pid"] == os.getpid()
        assert type(payload["pid"]) is int
    else:
        # The sidecar's existing console renderer uses ANSI colors.
        lines[0] = re.sub(r"\x1b\[[0-9;]*m", "", lines[0])
        assert lines[0].count("logging.configured") == 1
        assert "[info" in lines[0]
        assert "renderer=console" in lines[0]
        assert f"service=brain-v42-{service}" in lines[0]
        assert f"pid={os.getpid()}" in lines[0]


@pytest.mark.asyncio
async def test_metrics_main_configures_selected_format_before_running(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BRAIN_LOG_FORMAT", "json")
    runtime = MagicMock()
    runtime.run = AsyncMock(return_value=0)
    monkeypatch.setattr(metrics_entrypoint, "build_metrics_runtime", lambda: runtime)
    stop = asyncio.Event()

    assert await metrics_entrypoint.main(stop_event=stop) == 0

    runtime.run.assert_awaited_once_with(stop)
    output = capsys.readouterr()
    assert output.err == ""
    lines = output.out.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "logging.configured"


def test_mcp_entrypoint_selects_transport_before_logging_loads_settings() -> None:
    tree = ast.parse(Path(mcp_server.__file__).read_text(encoding="utf-8"))
    entrypoint = next(node for node in tree.body if isinstance(node, ast.If))
    calls = [
        node.value.func.id
        for node in entrypoint.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
    ]
    assert calls.index("_apply_http_server_arg") < calls.index("_configure_stdio_logging")
