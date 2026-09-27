import json
import subprocess
from pathlib import Path

import pytest

from headless_agents import model_live
from headless_agents.model_live import live_models, parse_agy, parse_opencode, parse_openrouter

FIXTURES = Path(__file__).parent / "fixtures" / "model_lists"


def test_recorded_opencode_list_has_models() -> None:
    raw = (FIXTURES / "opencode_models.txt").read_text()
    models = parse_opencode(raw)
    assert models
    assert all("/" in model for model in models)


def test_recorded_agy_list_has_models() -> None:
    raw = (FIXTURES / "agy_models.txt").read_text()
    assert parse_agy(raw)


def test_agy_diagnostic_line_on_a_zero_exit_is_rejected() -> None:
    """A zero-exit ``agy models`` that answers a diagnostic, not a model list
    (review finding #1), must not be read as a one-model catalogue. The real
    format is ``<id>\\t<Display Name>`` (measured on the operator's machine,
    agy on PATH; the progress banner goes to stderr, never stdout): a
    lowercase, tab-free diagnostic like ``unsupported models command`` has no
    tab at all, so it must raise rather than being read as one model named
    after its first word."""
    with pytest.raises(ValueError, match="tab"):
        parse_agy("unsupported models command\n")


def test_agy_line_with_no_display_name_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_agy("gemini-3.8-flash-high\t\n")


def test_agy_line_with_an_extra_tab_is_rejected() -> None:
    with pytest.raises(ValueError, match="tab"):
        parse_agy("gemini-3.8-flash-high\tGemini 3.8 Flash\t(High)\n")


def test_live_models_reports_unreadable_for_agy_diagnostic_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        model_live.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 0, b"unsupported models command\n", b""
        ),
    )
    result = live_models("agy", environ={}, home=tmp_path)
    assert result.status == "unreadable"
    assert result.models == ()


def test_recorded_openrouter_response_uses_exact_ids() -> None:
    body = json.loads((FIXTURES / "openrouter_models.json").read_text())
    assert parse_openrouter(body) == tuple(item["id"] for item in body["data"])


def test_timeout_is_reported_without_raising(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def timed_out(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired("agy models", 5)

    monkeypatch.setattr(model_live.subprocess, "run", timed_out)
    result = live_models("agy", environ={}, home=tmp_path)
    assert result.status == "unavailable"
    assert result.models == ()
    assert "timeout" in result.detail


def test_unreadable_output_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        model_live.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, b"\xff", b""),
    )
    result = live_models("agy", environ={}, home=tmp_path)
    assert result.status == "unreadable"
    assert result.models == ()


def test_openrouter_error_cannot_echo_key_or_body(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret = "fixture-secret"
    monkeypatch.setattr(
        model_live,
        "preset_key",
        lambda *args, **kwargs: model_live.PresetKey(secret, "environment"),
    )

    def failed(*args: object, **kwargs: object) -> object:
        raise RuntimeError(f"response body contains {secret}")

    monkeypatch.setattr(model_live.urllib.request, "urlopen", failed)
    result = live_models("openrouter", environ={}, home=tmp_path)
    assert result.status == "unavailable"
    assert secret not in result.detail
