"""Shared logging setup routes each supported format to stderr."""

from __future__ import annotations

from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("log_format", ["json", "console"])
def test_configure_stderr_logging_selects_the_requested_renderer(
    monkeypatch: pytest.MonkeyPatch, log_format: str
) -> None:
    from brain_v42 import safe_logging

    processors = [Mock()]
    build = Mock(return_value=processors)
    json_configure = Mock()
    console_configure = Mock()
    monkeypatch.setattr(safe_logging, "build_logging_processors", build)
    monkeypatch.setattr(safe_logging, "configure_json_logging", json_configure)
    monkeypatch.setattr(safe_logging, "configure_console_logging", console_configure)

    safe_logging.configure_stderr_logging(log_format)  # type: ignore[attr-defined]

    build.assert_called_once_with(log_format, colors=False)
    if log_format == "json":
        json_configure.assert_called_once_with(processors)
        console_configure.assert_not_called()
    else:
        console_configure.assert_called_once_with(processors)
        json_configure.assert_not_called()
