"""Gateway entry points retain the project's safe stderr logging."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import structlog

from brain_v42.codex_gateway import __main__ as gateway_main
from brain_v42.codex_gateway import launcher


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    structlog_config = structlog.get_config()
    root = logging.getLogger()
    root_handlers = root.handlers[:]
    root_level = root.level
    loggers = [
        logging.getLogger(name)
        for name in ("fastmcp", "uvicorn", "uvicorn.error", "uvicorn.access")
    ]
    saved_loggers = [(logger.handlers[:], logger.propagate, logger.level) for logger in loggers]
    yield
    root.handlers[:] = root_handlers
    root.setLevel(root_level)
    for logger, (handlers, propagate, level) in zip(loggers, saved_loggers, strict=True):
        logger.handlers, logger.propagate, logger.level = handlers, propagate, level
    structlog.reset_defaults()
    structlog.configure(**structlog_config)


def _prepare_entrypoint(
    entrypoint: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, log_format: str
) -> list[tuple[str, object]]:
    order: list[tuple[str, object]] = []
    settings = SimpleNamespace(
        brain_log_format=log_format,
        brain_codex_gateway_host="127.0.0.1",
        brain_codex_gateway_port=8765,
    )
    monkeypatch.setattr(entrypoint, "get_settings", MagicMock(return_value=settings))
    monkeypatch.setattr(entrypoint, "create_production_app", lambda _: object())
    monkeypatch.setattr(
        entrypoint, "uvicorn", SimpleNamespace(run=lambda *a, **k: order.append(("serve", k)))
    )
    if entrypoint is launcher:
        token_file = tmp_path / "token.env"
        token_file.write_text("BRAIN_CODEX_GATEWAY_TOKEN=" + "t" * 48 + "\n")
        token_file.chmod(0o600)
        monkeypatch.setenv("BRAIN_CODEX_GATEWAY_TOKEN_FILE", str(token_file))
        monkeypatch.setattr(launcher, "use_engine_profile", lambda _: None)
    return order


@pytest.mark.parametrize("entrypoint", [gateway_main, launcher], ids=["main", "launcher"])
@pytest.mark.parametrize("log_format", ["console", "json"])
def test_run_configures_matching_logging_before_uvicorn(
    entrypoint: object,
    log_format: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from brain_v42.codex_gateway import log_setup as gateway_logging

    order = _prepare_entrypoint(entrypoint, monkeypatch, tmp_path, log_format)
    monkeypatch.setattr(
        gateway_logging,
        "configure_console_logging",
        lambda _: order.append(("configured", "console")),
    )
    monkeypatch.setattr(
        gateway_logging,
        "configure_json_logging",
        lambda _: order.append(("configured", "json")),
    )
    entrypoint.run()
    assert order == [("configured", log_format), ("serve", order[-1][1])]
    assert order[-1][1]["log_config"] is None


@pytest.mark.parametrize("entrypoint", [gateway_main, launcher], ids=["main", "launcher"])
@pytest.mark.parametrize("log_format", ["console", "json"])
def test_run_sanitizes_uvicorn_access_query(
    entrypoint: object,
    log_format: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _prepare_entrypoint(entrypoint, monkeypatch, tmp_path, log_format)

    def _run(app: object, **kw: object) -> None:
        import uvicorn

        uvicorn.Config(
            app,
            log_config=kw.get("log_config", uvicorn.config.LOGGING_CONFIG),
        ).configure_logging()

    monkeypatch.setattr(entrypoint.uvicorn, "run", _run)
    entrypoint.run()
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %s',
        "127.0.0.1:1",
        "GET",
        "/x?token=secret",
        "1.1",
        200,
    )
    out, err = capsys.readouterr()
    assert "token=secret" not in out + err
    assert "GET /x HTTP/1.1" in err
