"""Tests for Code Mode opt-in flag."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from brain_v42.mcp.server import maybe_apply_code_mode


def test_code_mode_disabled_does_not_wrap() -> None:
    """When brain_code_mode=False, maybe_apply_code_mode returns mcp unchanged."""
    mock_settings = MagicMock()
    mock_settings.brain_code_mode = False

    mcp_instance = MagicMock()
    result = maybe_apply_code_mode(mcp_instance, mock_settings)

    assert result is mcp_instance


def test_code_mode_enabled_wraps_mcp() -> None:
    """When brain_code_mode=True, maybe_apply_code_mode wraps mcp with CodeMode."""
    mock_settings = MagicMock()
    mock_settings.brain_code_mode = True

    mcp_instance = MagicMock()

    with patch("fastmcp.experimental.transforms.code_mode.CodeMode") as mock_code_mode:
        mock_code_mode.return_value = MagicMock(name="wrapped_mcp")
        result = maybe_apply_code_mode(mcp_instance, mock_settings)
        mock_code_mode.assert_called_once_with(mcp_instance)
        assert result is mock_code_mode.return_value


def test_code_mode_enabled_graceful_on_import_error() -> None:
    """When CodeMode import fails, maybe_apply_code_mode returns mcp unchanged."""
    mock_settings = MagicMock()
    mock_settings.brain_code_mode = True

    mcp_instance = MagicMock()

    with patch.dict("sys.modules", {"fastmcp.experimental.transforms.code_mode": None}):
        result = maybe_apply_code_mode(mcp_instance, mock_settings)
        assert result is mcp_instance
