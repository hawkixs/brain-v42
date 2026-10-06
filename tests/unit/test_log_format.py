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
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    uvicorn_loggers = [
        logging.getLogger(name)
        for name in ("fastmcp", "uvicorn", "uvicorn.error", "uvicorn.access")
    ]
    saved_uvicorn = [(log.handlers[:], log.propagate, log.level) for log in uvicorn_loggers]
    get_settings.cache_clear()
    monkeypatch.setenv("POSTGRES_URL", "postgresql+asyncpg://test@localhost/log_test")
    yield
    root.handlers, root.level = saved_handlers, saved_level
    for log, (handlers, propagate, level) in zip(uvicorn_loggers, saved_uvicorn, strict=True):
        log.handlers, log.propagate, log.level = handlers, propagate, level
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


@pytest.mark.parametrize(("renderer", "utc"), [("console", False), ("json", True)])
def test_only_json_timestamps_are_utc(renderer: str, utc: bool) -> None:
    """The console keeps its local timestamps: only the container's JSON lines are UTC."""
    from brain_v42.safe_logging import build_logging_processors

    stamper = build_logging_processors(renderer)[0]  # type: ignore[arg-type]
    assert isinstance(stamper, structlog.processors.TimeStamper)
    assert stamper.utc is utc


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
    uses_stderr = service == "mcp" or renderer == "json"
    monkeypatch.setattr(sys, "stderr", output if uses_stderr else other_stream)
    monkeypatch.setattr(sys, "stdout", other_stream if uses_stderr else output)

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
    assert output.out == ""
    lines = output.err.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "logging.configured"


@pytest.mark.parametrize("service", ["mcp", "metrics"])
@pytest.mark.parametrize(
    "logger_name",
    [
        "test.foreign",
        "fastmcp.server.server",
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "aiohttp.access",
    ],
)
def test_json_stdlib_records_use_one_stderr_handler(
    monkeypatch: pytest.MonkeyPatch, service: str, logger_name: str
) -> None:
    output = io.StringIO()
    monkeypatch.setenv("BRAIN_LOG_FORMAT", "json")
    monkeypatch.setattr(sys, "stderr", output)
    # Uvicorn may have installed handlers before this entrypoint is configured.
    log = logging.getLogger(logger_name)
    if logger_name.startswith("uvicorn"):
        log.handlers = [logging.StreamHandler(output)]
        log.propagate = False
    if service == "mcp":
        mcp_server._configure_stdio_logging()
    else:
        metrics_entrypoint._configure_logging(MagicMock())
    output.seek(0)
    output.truncate()

    if logger_name == "uvicorn.access":
        log.info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:1234",
            "GET",
            "/mcp?token=synthetic-query-canary",
            "1.1",
            200,
            extra={"Authorization": "Bearer synthetic-header-canary"},
        )
        expected = '127.0.0.1:1234 - "GET /mcp HTTP/1.1" 200'
    elif logger_name == "aiohttp.access":
        log.info(
            '127.0.0.1 - [date] "GET /metrics?token=synthetic-query-canary HTTP/1.1" 200 123 "Bearer synthetic-header-canary" "synthetic-agent-canary"'
        )
        expected = '"GET /metrics HTTP/1.1" 200'
    else:
        log.info("foreign %s\nmessage", "formatted")
        expected = "foreign formatted\nmessage"

    lines = output.getvalue().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == expected
    assert payload["logger"] == logger_name
    assert payload["level"] == "info"
    assert datetime.fromisoformat(payload["timestamp"]).utcoffset() == timedelta(0)
    assert "synthetic-query-canary" not in lines[0]
    assert "synthetic-header-canary" not in lines[0]
    assert "synthetic-agent-canary" not in lines[0]
    assert "positional_args" not in payload
    assert len(logging.getLogger().handlers) == 1
    assert isinstance(
        logging.getLogger().handlers[0].formatter, structlog.stdlib.ProcessorFormatter
    )
    assert "_record" not in payload
    assert "_from_structlog" not in payload


def test_json_unknown_access_format_drops_unparsed_request(monkeypatch: pytest.MonkeyPatch) -> None:
    output = io.StringIO()
    monkeypatch.setenv("BRAIN_LOG_FORMAT", "json")
    monkeypatch.setattr(sys, "stderr", output)
    mcp_server._configure_stdio_logging()
    output.seek(0)
    output.truncate()

    logging.getLogger("uvicorn.access").info("Authorization: Bearer synthetic-header-canary")

    assert json.loads(output.getvalue())["event"] == "http.access"
    assert "synthetic-header-canary" not in output.getvalue()


def test_json_foreign_access_never_leaks_into_sidecar_recent_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_LOG_FORMAT", "json")
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    collector = MagicMock()
    metrics_entrypoint._configure_logging(collector)
    collector.reset_mock()

    record = logging.LogRecord(
        "aiohttp.access",
        logging.INFO,
        "<access>",
        1,
        '127.0.0.1 - [date] "GET /metrics?token=synthetic-query-canary HTTP/1.1" 200 123 "-" "-"',
        (),
        None,
    )
    logging.getLogger("aiohttp.access").handle(record)

    assert "synthetic-query-canary" not in str(collector.mock_calls)
    assert "_record" not in str(collector.mock_calls)


@pytest.mark.parametrize("service", ["mcp", "metrics"])
@pytest.mark.parametrize("positional", [False, True])
def test_json_structlog_includes_logger_and_sidecar_recent_log(
    monkeypatch: pytest.MonkeyPatch, service: str, positional: bool
) -> None:
    output = io.StringIO()
    monkeypatch.setenv("BRAIN_LOG_FORMAT", "json")
    monkeypatch.setattr(sys, "stderr", output)
    collector = MagicMock()
    if service == "mcp":
        mcp_server._configure_stdio_logging()
    else:
        metrics_entrypoint._configure_logging(collector)
    output.seek(0)
    output.truncate()
    collector.reset_mock()

    log = structlog.get_logger("test.structured")
    if positional:
        log.info("structured.%s", "event", count=42)
    else:
        log.info("structured.event", count=42)

    payload = json.loads(output.getvalue())
    assert payload["logger"] == "test.structured"
    assert payload["event"] == "structured.event"
    assert payload["count"] == 42
    if service == "metrics":
        collector.push_recent_log.assert_called_once_with("info", "structured.event count=42")


def test_console_mcp_keeps_plain_stdlib_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    output = io.StringIO()
    monkeypatch.setenv("BRAIN_LOG_FORMAT", "console")
    monkeypatch.setattr(sys, "stderr", output)
    mcp_server._configure_stdio_logging()
    output.seek(0)
    output.truncate()

    logging.getLogger("test.console").info("plain message")

    assert output.getvalue() == "INFO:test.console:plain message\n"


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
