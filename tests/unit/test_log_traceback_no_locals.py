"""No ConsoleRenderer in src/ may print frame locals in a traceback.

structlog's ConsoleRenderer renders ``exc_info`` through rich when it is
installed, and rich shows every frame's local variables. During a Postgres
outage the asyncpg connect frame holds ``password=...``, so the journal received
the database password in clear (ticket 1bffd79a). Each test below logs an
exception raised under a local named like a secret and asserts that the value
never reaches the rendered output while the traceback itself still does.
"""

from __future__ import annotations

import ast
import importlib
import io
import json
import logging
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import structlog

from brain_v42.config import get_settings
from brain_v42.mcp import server as mcp_server
from brain_v42.metrics import __main__ as metrics_entrypoint
from brain_v42.metrics.runtime import build_sidecar_structlog_processors

_SECRET = f"s3cr3t-{uuid.uuid4().hex}"
_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src" / "brain_v42"
_SCRIPTS = _ROOT / "scripts"


def _connect() -> None:
    """Raise with the secret held in a local, as asyncpg's connect frame does."""
    password = _SECRET
    raise ConnectionRefusedError(f"postgres down (len {len(password)})")


def _log_failure() -> None:
    log = structlog.get_logger("test.no_locals")
    try:
        _connect()
    except ConnectionRefusedError:
        log.error("db.connect_failed", exc_info=True)


def _assert_traceback_without_locals(rendered: str) -> None:
    assert "Traceback" in rendered, rendered
    assert "ConnectionRefusedError" in rendered, rendered
    assert "postgres down" in rendered, rendered
    assert _SECRET not in rendered, "frame local leaked into the rendered traceback"


@pytest.fixture(autouse=True)
def _restore_structlog() -> Iterator[None]:
    saved = structlog.get_config()
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    uvicorn_loggers = [
        logging.getLogger(name)
        for name in ("fastmcp", "uvicorn", "uvicorn.error", "uvicorn.access")
    ]
    saved_uvicorn = [(log.handlers[:], log.propagate, log.level) for log in uvicorn_loggers]
    yield
    root.handlers, root.level = saved_handlers, saved_level
    for log, (handlers, propagate, level) in zip(uvicorn_loggers, saved_uvicorn, strict=True):
        log.handlers, log.propagate, log.level = handlers, propagate, level
    structlog.reset_defaults()
    structlog.configure(**saved)


def test_sidecar_chain_never_renders_frame_locals() -> None:
    buffer = io.StringIO()
    structlog.configure(
        processors=build_sidecar_structlog_processors(MagicMock()),
        wrapper_class=structlog.BoundLogger,
        logger_factory=structlog.PrintLoggerFactory(file=buffer),
    )

    _log_failure()

    _assert_traceback_without_locals(buffer.getvalue())


def test_sidecar_exception_path_never_renders_frame_locals() -> None:
    buffer = io.StringIO()
    structlog.configure(
        processors=build_sidecar_structlog_processors(MagicMock()),
        # The sidecar entrypoint's wrapper: only it turns ``exception()`` into
        # ``exc_info=True``; the generic ``structlog.BoundLogger`` does not.
        wrapper_class=structlog.make_filtering_bound_logger(0),
        logger_factory=structlog.PrintLoggerFactory(file=buffer),
    )
    log = structlog.get_logger("test.no_locals")
    try:
        _connect()
    except ConnectionRefusedError:
        log.exception("sidecar.failure")
    _assert_traceback_without_locals(buffer.getvalue())


def test_mcp_logging_never_renders_frame_locals(monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buffer)
    monkeypatch.setattr(logging, "basicConfig", lambda **_: None)
    # Pin the console renderer: the configure call reads settings, which another test
    # may have cached in json mode.
    monkeypatch.setenv("BRAIN_LOG_FORMAT", "console")
    monkeypatch.setenv("POSTGRES_URL", "postgresql+asyncpg://test@localhost/log_test")
    get_settings.cache_clear()
    try:
        mcp_server._configure_stdio_logging()
    finally:
        get_settings.cache_clear()

    _log_failure()

    _assert_traceback_without_locals(buffer.getvalue())


@pytest.mark.parametrize("service", ["mcp", "metrics"])
def test_json_exception_is_one_line_without_frame_locals(
    monkeypatch: pytest.MonkeyPatch, service: str
) -> None:
    buffer = io.StringIO()
    monkeypatch.setenv("BRAIN_LOG_FORMAT", "json")
    monkeypatch.setenv("POSTGRES_URL", "postgresql+asyncpg://test@localhost/log_test")
    monkeypatch.setattr(sys, "stderr", buffer)
    get_settings.cache_clear()
    try:
        if service == "mcp":
            mcp_server._configure_stdio_logging()
        else:
            metrics_entrypoint._configure_logging(MagicMock())
        buffer.seek(0)
        buffer.truncate()

        _log_failure()

        rendered = buffer.getvalue()
        assert len(rendered.splitlines()) == 1
        payload = json.loads(rendered)
        assert payload["event"] == "db.connect_failed"
        assert payload["logger"] == "test.no_locals"
        assert payload["level"] == "error"
        assert "ConnectionRefusedError" in json.dumps(payload["exception"])
        assert "postgres down" in json.dumps(payload["exception"])
        assert _SECRET not in rendered, "frame local leaked into the rendered traceback"
        assert "exc_info" not in payload
        for exception in payload["exception"]:
            for frame in exception["frames"]:
                assert not frame.get("locals")
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("service", ["mcp", "metrics"])
def test_json_stdlib_exception_is_one_line_without_frame_locals(
    monkeypatch: pytest.MonkeyPatch, service: str
) -> None:
    buffer = io.StringIO()
    monkeypatch.setenv("BRAIN_LOG_FORMAT", "json")
    monkeypatch.setenv("POSTGRES_URL", "postgresql+asyncpg://test@localhost/log_test")
    monkeypatch.setattr(sys, "stderr", buffer)
    get_settings.cache_clear()
    try:
        if service == "mcp":
            mcp_server._configure_stdio_logging()
        else:
            metrics_entrypoint._configure_logging(MagicMock())
        buffer.seek(0)
        buffer.truncate()

        try:
            _connect()
        except ConnectionRefusedError:
            logging.getLogger("test.foreign_exception").exception("db.connect_failed")

        rendered = buffer.getvalue()
        assert len(rendered.splitlines()) == 1
        payload = json.loads(rendered)
        assert payload["event"] == "db.connect_failed"
        assert payload["logger"] == "test.foreign_exception"
        assert payload["level"] == "error"
        assert "ConnectionRefusedError" in json.dumps(payload["exception"])
        assert _SECRET not in rendered
        assert "exc_info" not in payload
        for exception in payload["exception"]:
            for frame in exception["frames"]:
                assert not frame.get("locals")
    finally:
        get_settings.cache_clear()


def test_migration_script_never_renders_frame_locals(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = importlib.import_module("brain_v42.scripts.migrate_neo4j_to_pg")
    importlib.reload(module)  # the script configures structlog at import time

    _log_failure()

    _assert_traceback_without_locals(capsys.readouterr().out)


def _console_renderer_calls(source: str) -> list[int]:
    """Line numbers of ConsoleRenderer constructions, however the name is reached.

    An attribute call (``structlog.dev.ConsoleRenderer()``, ``d.ConsoleRenderer()``)
    is matched on the attribute; a bare-name call is matched only when the name
    was imported from ``structlog.dev`` in this module, aliased or not.
    """
    tree = ast.parse(source)
    imported_as = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "structlog.dev"
        for alias in node.names
        if alias.name == "ConsoleRenderer"
    }
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr == "ConsoleRenderer")
            or (isinstance(node.func, ast.Name) and node.func.id in imported_as)
        )
    ]


@pytest.mark.parametrize(
    "source",
    [
        "import structlog\nstructlog.dev.ConsoleRenderer()",
        "import structlog\nstructlog.dev.ConsoleRenderer(exception_formatter=f)",
        "import structlog.dev as d\nd.ConsoleRenderer()",
        "from structlog import dev\ndev.ConsoleRenderer()",
        "from structlog import dev as d\nd.ConsoleRenderer()",
        "from structlog.dev import ConsoleRenderer\nConsoleRenderer()",
        "from structlog.dev import ConsoleRenderer as CR\nCR()",
    ],
)
def test_guard_flags_every_way_of_building_a_console_renderer(source: str) -> None:
    assert _console_renderer_calls(source) == [2]


@pytest.mark.parametrize(
    "source",
    [
        "from brain_v42.safe_logging import safe_console_renderer\nsafe_console_renderer()",
        "from other import ConsoleRenderer\nConsoleRenderer()",
        "from structlog.dev import ConsoleRenderer\nclass Sub(ConsoleRenderer): ...",
        "import structlog\nstructlog.dev.plain_traceback",
    ],
)
def test_guard_ignores_code_that_builds_no_structlog_console_renderer(
    source: str,
) -> None:
    assert _console_renderer_calls(source) == []


def test_src_builds_no_console_renderer_outside_the_safe_helper() -> None:
    offenders = [
        f"{path.relative_to(_SRC)}:{lineno}"
        for path in sorted(_SRC.rglob("*.py"))
        if path.name != "safe_logging.py"
        for lineno in _console_renderer_calls(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_scripts_build_no_console_renderer() -> None:
    offenders = [
        f"{path.relative_to(_ROOT)}:{lineno}"
        for path in sorted(_SCRIPTS.rglob("*.py"))
        for lineno in _console_renderer_calls(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
