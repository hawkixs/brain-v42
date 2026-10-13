"""Exception diagnostics retain provenance without exposing driver messages."""

from __future__ import annotations

import io
import json
import logging
import sys
from collections.abc import Iterator
from typing import Literal

import pytest
import structlog

from brain_v42.safe_logging import (
    build_logging_processors,
    configure_json_logging,
    safe_console_renderer,
)

SECRET_MESSAGE = "connect failed for postgresql://brain:s3cret@db:5433/brain?sslmode=require"


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    saved = structlog.get_config()
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    logs = [
        logging.getLogger(name)
        for name in ("fastmcp", "uvicorn", "uvicorn.error", "uvicorn.access")
    ]
    states = [(log.handlers[:], log.propagate, log.level) for log in logs]
    yield
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers, root.level = handlers, level
    for log, state in zip(logs, states, strict=True):
        log.handlers, log.propagate, log.level = state
    structlog.reset_defaults()
    structlog.configure(**saved)


def assert_safe(rendered: str, function: str) -> None:
    assert "ConnectionError" in rendered
    assert "test_safe_logging.py" in rendered
    assert function in rendered
    assert "<message redacted>" in rendered
    for forbidden in (SECRET_MESSAGE, "s3cret", "brain:s3cret", "sslmode", "postgresql://"):
        assert forbidden not in rendered


def make_logger(log_format: Literal["console", "json"], buffer: io.StringIO) -> None:
    structlog.configure(
        processors=build_logging_processors(log_format, colors=False),
        logger_factory=structlog.PrintLoggerFactory(file=buffer),
        wrapper_class=structlog.make_filtering_bound_logger(0),
        cache_logger_on_first_use=False,
    )


@pytest.mark.parametrize("log_format", ["console", "json"])
def test_exception_message_is_redacted_with_frames_and_event_fields(
    log_format: Literal["console", "json"],
) -> None:
    buffer = io.StringIO()
    make_logger(log_format, buffer)
    try:
        raise ConnectionError(SECRET_MESSAGE)
    except ConnectionError:
        structlog.get_logger().exception("db.failure", correlation_id="correlation-canary")

    rendered = buffer.getvalue()
    assert_safe(rendered, "test_exception_message_is_redacted_with_frames_and_event_fields")
    assert "correlation-canary" in rendered
    if log_format == "json":
        exception = json.loads(rendered)["exception"][0]
        assert exception["exc_value"] == "<message redacted>"
        assert exception["frames"][0]["lineno"] > 0
        assert not exception["frames"][0].get("locals")


@pytest.mark.parametrize("log_format", ["console", "json"])
@pytest.mark.parametrize("explicit_cause", [True, False])
def test_chained_exception_messages_are_redacted(
    log_format: Literal["console", "json"], explicit_cause: bool
) -> None:
    buffer = io.StringIO()
    make_logger(log_format, buffer)
    try:
        try:
            raise ConnectionError(SECRET_MESSAGE)
        except ConnectionError as cause:
            if explicit_cause:
                raise RuntimeError("outer-secret-canary") from cause
            raise RuntimeError("outer-secret-canary")  # noqa: B904 — exercise implicit context
    except RuntimeError:
        structlog.get_logger().exception("db.failure")

    rendered = buffer.getvalue()
    assert_safe(rendered, "test_chained_exception_messages_are_redacted")
    assert "RuntimeError" in rendered
    assert "outer-secret-canary" not in rendered
    if log_format == "json":
        exceptions = json.loads(rendered)["exception"]
        assert len(exceptions) == 2
        assert all(entry["exc_value"] == "<message redacted>" for entry in exceptions)
        assert exceptions[1]["is_cause"] is explicit_cause
    else:
        assert ("direct cause" if explicit_cause else "During handling") in rendered


def test_standalone_console_renderer_redacts_exception_messages() -> None:
    try:
        raise ConnectionError(SECRET_MESSAGE)
    except ConnectionError:
        rendered = safe_console_renderer(colors=False)(
            None, "error", {"event": "db.failure", "exc_info": sys.exc_info()}
        )
    assert_safe(rendered, "test_standalone_console_renderer_redacts_exception_messages")


def test_stdlib_json_exception_redacts_message(monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buffer)
    configure_json_logging(build_logging_processors("json"))
    try:
        raise ConnectionError(SECRET_MESSAGE)
    except ConnectionError:
        logging.getLogger("test.foreign_exception").exception("db.failure")

    rendered = buffer.getvalue()
    assert_safe(rendered, "test_stdlib_json_exception_redacts_message")
    assert len(rendered.splitlines()) == 1
    assert json.loads(rendered)["event"] == "db.failure"


@pytest.mark.parametrize("log_format", ["console", "json"])
def test_internal_error_keeps_correlation_id_and_frames(
    log_format: Literal["console", "json"],
) -> None:
    buffer = io.StringIO()
    make_logger(log_format, buffer)
    try:
        raise ConnectionError(SECRET_MESSAGE)
    except ConnectionError:
        structlog.get_logger().error(
            "mcp.tool_internal_error", correlation_id="internal-error-canary", exc_info=True
        )

    rendered = buffer.getvalue()
    assert_safe(rendered, "test_internal_error_keeps_correlation_id_and_frames")
    assert "mcp.tool_internal_error" in rendered
    assert "internal-error-canary" in rendered


@pytest.mark.parametrize("log_format", ["console", "json"])
def test_exception_group_messages_and_notes_are_redacted(
    log_format: Literal["console", "json"],
) -> None:
    buffer = io.StringIO()
    make_logger(log_format, buffer)
    try:
        try:
            raise ConnectionError(SECRET_MESSAGE)
        except ConnectionError as child:
            child.add_note("note-secret-canary")
            raise ExceptionGroup("group-secret-canary", [child]) from None
    except ExceptionGroup:
        structlog.get_logger().exception("db.failure")

    rendered = buffer.getvalue()
    assert_safe(rendered, "test_exception_group_messages_and_notes_are_redacted")
    assert "ExceptionGroup" in rendered
    assert "group-secret-canary" not in rendered
    assert "note-secret-canary" not in rendered


@pytest.mark.parametrize("log_format", ["console", "json"])
def test_syntax_error_messages_and_source_are_redacted(
    log_format: Literal["console", "json"],
) -> None:
    buffer = io.StringIO()
    make_logger(log_format, buffer)
    try:
        raise SyntaxError("syntax-secret-canary", ("synthetic.py", 1, 1, "source-secret-canary"))
    except SyntaxError:
        structlog.get_logger().exception("syntax.failure")

    rendered = buffer.getvalue()
    assert "SyntaxError" in rendered
    assert "test_safe_logging.py" in rendered
    assert "<message redacted>" in rendered
    assert "syntax-secret-canary" not in rendered
    assert "source-secret-canary" not in rendered


@pytest.mark.parametrize("log_format", ["console", "json"])
def test_exc_info_outside_an_except_block_renders_no_traceback(
    log_format: Literal["console", "json"],
) -> None:
    """`exc_info=True` with no active exception must neither crash nor invent a trace."""
    buffer = io.StringIO()
    make_logger(log_format, buffer)
    structlog.get_logger().error("no.active.exception", exc_info=True)

    rendered = buffer.getvalue()
    assert "no.active.exception" in rendered
    assert "Traceback" not in rendered


def test_sidecar_safe_format_exc_info_redacts_without_crashing() -> None:
    """The sidecar's pre-formatting step shares the sanitizer, also with no exception."""
    from brain_v42.safe_logging import safe_format_exc_info  # noqa: PLC0415

    try:
        raise ConnectionError(SECRET_MESSAGE)
    except ConnectionError:
        event = safe_format_exc_info(None, "error", {"event": "x", "exc_info": True})
    assert "<message redacted>" in event["exception"]
    assert "s3cret" not in event["exception"]
    assert "exception" not in safe_format_exc_info(None, "error", {"event": "x", "exc_info": True})
