"""Console entrypoints must sanitize foreign exceptions before writing to stderr."""

from __future__ import annotations

import io
import logging
import sys
from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
import structlog

from brain_v42.config import get_settings
from brain_v42.mcp import server as mcp_server
from brain_v42.metrics import __main__ as metrics_entrypoint

_SECRET = "postgresql://u:SECRET@h/db"
_FOREIGN_LOGGERS = ("fastmcp", "uvicorn", "uvicorn.error", "uvicorn.access")


@pytest.fixture(autouse=True)
def restore_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate global logging state so each entrypoint owns its handlers."""
    saved = structlog.get_config()
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    logs = [logging.getLogger(name) for name in ("test.console_stdlib", *_FOREIGN_LOGGERS)]
    states = [(log.handlers[:], log.propagate, log.level, log.disabled) for log in logs]
    root.handlers = []
    for log in logs:
        log.handlers = []
        log.propagate = True
        log.setLevel(logging.NOTSET)
        log.disabled = False
    structlog.reset_defaults()
    monkeypatch.setenv("POSTGRES_URL", "postgresql+asyncpg://test@localhost/log_test")
    monkeypatch.setenv("BRAIN_LOG_FORMAT", "console")
    get_settings.cache_clear()
    yield
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers, root.level = handlers, level
    for log, state in zip(logs, states, strict=True):
        log.handlers, log.propagate, log.level, log.disabled = state
    structlog.reset_defaults()
    structlog.configure(**saved)
    get_settings.cache_clear()


@pytest.mark.parametrize("service", ["mcp", "metrics"])
@pytest.mark.parametrize("logger_name", ["test.console_stdlib", *_FOREIGN_LOGGERS])
def test_console_stdlib_exception_redacts_message_and_keeps_frames(
    monkeypatch: pytest.MonkeyPatch, service: str, logger_name: str
) -> None:
    stderr, stdout = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(sys, "stdout", stdout)
    log = logging.getLogger(logger_name)
    if logger_name in _FOREIGN_LOGGERS:
        # Servers may install private handlers before the application configures logging.
        log.handlers = [logging.StreamHandler(stdout)]
        log.propagate = False
        log.setLevel(logging.CRITICAL)

    if service == "mcp":
        mcp_server._configure_stdio_logging()
    else:
        metrics_entrypoint._configure_logging(MagicMock())
    stderr.seek(0)
    stderr.truncate()
    stdout.seek(0)
    stdout.truncate()

    try:
        raise ConnectionError(_SECRET)
    except ConnectionError:
        log.exception("db.failure")

    rendered = stderr.getvalue()
    assert "SECRET" not in rendered
    assert _SECRET not in rendered
    assert "Traceback" in rendered
    assert "ConnectionError" in rendered
    assert "test_console_stdlib_logging.py" in rendered
    assert "test_console_stdlib_exception_redacts_message_and_keeps_frames" in rendered
    assert "<message redacted>" in rendered
    # The access sanitizer replaces uvicorn.access events, as in JSON mode.
    assert ("http.access" if logger_name == "uvicorn.access" else "db.failure") in rendered
    assert stdout.getvalue() == ""
