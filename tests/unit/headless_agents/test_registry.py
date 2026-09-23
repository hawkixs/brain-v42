"""The facade: providers by name, a zero-quota probe, per-provider prompt limits."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from headless_agents import registry
from headless_agents.protocol import AgentProvider
from headless_agents.providers import agy, opencode


def _script(directory: Path, body: str, name: str = "fake-cli") -> Path:
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class TestGetProvider:
    @pytest.mark.parametrize("name", ["claude", "codex", "agy", "opencode"])
    def test_every_cli_rail_is_reached_by_its_name(self, name: str) -> None:
        provider = registry.get_provider(name)
        assert isinstance(provider, AgentProvider)
        assert provider.name == name

    def test_the_names_are_the_four_cli_rails(self) -> None:
        assert registry.PROVIDER_NAMES == ("claude", "codex", "agy", "opencode")

    def test_each_call_returns_a_fresh_instance(self) -> None:
        assert registry.get_provider("codex") is not registry.get_provider("codex")

    def test_an_unknown_name_raises_a_message_listing_the_valid_ones(self) -> None:
        with pytest.raises(registry.UnknownProvider) as caught:
            registry.get_provider("gpt")
        message = str(caught.value)
        assert "'gpt'" in message
        for name in registry.PROVIDER_NAMES:
            assert name in message
        assert isinstance(caught.value, ValueError)
        assert caught.value.name == "gpt"


class TestMaxPromptBytes:
    def test_the_stdin_rails_have_no_limit(self) -> None:
        assert registry.max_prompt_bytes("claude") is None
        assert registry.max_prompt_bytes("codex") is None

    def test_the_argv_rails_expose_their_own_limit(self) -> None:
        assert registry.max_prompt_bytes("agy") == agy.MAX_PROMPT_BYTES
        assert registry.max_prompt_bytes("opencode") == opencode.MAX_PROMPT_BYTES

    def test_an_unknown_name_is_refused(self) -> None:
        with pytest.raises(registry.UnknownProvider):
            registry.max_prompt_bytes("gpt")


class TestProbe:
    def test_an_answering_executable_is_available_with_its_version(self, tmp_path: Path) -> None:
        cli = _script(tmp_path, 'echo "2.1.280 (Claude Code)"')
        found = registry.probe("claude", executable=str(cli))
        assert found.available is True
        assert found.version == "2.1.280 (Claude Code)"
        assert found.detail == str(cli)

    def test_an_absent_executable_is_unavailable(self, tmp_path: Path) -> None:
        found = registry.probe("codex", executable=str(tmp_path / "missing"))
        assert found.available is False
        assert found.version is None
        assert "not found" in found.detail

    def test_a_failing_version_is_unavailable_with_its_exit_code(self, tmp_path: Path) -> None:
        cli = _script(tmp_path, "echo broken >&2; exit 3")
        found = registry.probe("agy", executable=str(cli))
        assert found.available is False
        assert "exited 3" in found.detail

    def test_a_hanging_version_is_bounded_by_the_timeout(self, tmp_path: Path) -> None:
        cli = _script(tmp_path, "sleep 5")
        found = registry.probe("opencode", executable=str(cli), timeout_seconds=0.2)
        assert found.available is False
        assert "no answer" in found.detail

    def test_the_default_executable_is_found_on_path_by_the_rails_name(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _script(tmp_path, 'echo "codex-cli 0.156.0"', name="codex")
        monkeypatch.setenv("PATH", str(tmp_path))
        found = registry.probe("codex")
        assert found.available is True
        assert found.version == "codex-cli 0.156.0"

    def test_an_unknown_name_is_refused_even_with_an_executable(self) -> None:
        with pytest.raises(registry.UnknownProvider):
            registry.probe("gpt")
        with pytest.raises(registry.UnknownProvider):
            registry.probe("gpt", executable="/bin/true")
