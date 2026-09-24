"""The facade: providers by name, a zero-quota probe, per-provider prompt limits."""

from __future__ import annotations

import os
import stat
import subprocess
import threading
import time
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


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class TestGetProvider:
    @pytest.mark.parametrize("name", ["claude", "codex", "agy", "opencode"])
    def test_every_cli_rail_is_reached_by_its_name(self, name: str) -> None:
        provider = registry.get_provider(name)
        assert isinstance(provider, AgentProvider)
        assert provider.name == name

    def test_the_names_are_the_specs_eight(self) -> None:
        # Spec 3.1: the four CLI rails, the three HTTP presets, the generic one.
        assert registry.PROVIDER_NAMES == (
            "claude",
            "codex",
            "agy",
            "opencode",
            "openrouter",
            "mistral",
            "nvidia",
            "openai-compat",
        )

    @pytest.mark.parametrize("name", ["openrouter", "mistral", "nvidia", "openai-compat"])
    def test_every_http_provider_is_reached_by_its_name(self, name: str) -> None:
        provider = registry.get_provider(name)
        assert isinstance(provider, AgentProvider)
        assert provider.name == name

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

    @pytest.mark.parametrize("name", ["openrouter", "mistral", "nvidia", "openai-compat"])
    def test_the_http_providers_have_no_argv_limit(self, name: str) -> None:
        assert registry.max_prompt_bytes(name) is None


class TestProbeHttp:
    """Spec 3.1: for an HTTP provider the probe is the key variable's presence."""

    def test_a_preset_is_available_when_its_key_is_set(self) -> None:
        found = registry.probe("mistral", environ={"MISTRAL_API_KEY": "sk-x"})
        assert found.available is True
        assert found.detail == "MISTRAL_API_KEY is set"
        assert found.version is None

    def test_a_preset_is_unavailable_without_its_key(self) -> None:
        found = registry.probe("openrouter", environ={"MISTRAL_API_KEY": "sk-x"})
        assert found.available is False
        assert "OPENROUTER_API_KEY" in found.detail

    def test_an_empty_key_is_unavailable(self) -> None:
        assert registry.probe("nvidia", environ={"NVIDIA_API_KEY": ""}).available is False

    def test_the_probe_never_reveals_the_key(self) -> None:
        found = registry.probe("mistral", environ={"MISTRAL_API_KEY": "sk-SECRET"})
        assert "sk-SECRET" not in found.detail

    def test_the_generic_provider_is_configured_per_run(self) -> None:
        found = registry.probe("openai-compat", environ={})
        assert found.available is True
        assert "base_url" in found.detail


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

    def test_output_that_is_not_utf8_never_raises(self, tmp_path: Path) -> None:
        cli = _script(tmp_path, "printf 'v1 \\377\\n'; printf '\\377' >&2")
        found = registry.probe("codex", executable=str(cli))
        assert found.available is True
        assert found.version == "v1 �"

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

    def test_a_version_printed_only_on_stderr_is_still_read(self, tmp_path: Path) -> None:
        cli = _script(tmp_path, 'echo "v9.9.9" >&2')
        found = registry.probe("claude", executable=str(cli))
        assert found.available is True
        assert found.version == "v9.9.9"

    def test_a_grandchild_holding_the_pipe_does_not_survive_the_timeout(
        self, tmp_path: Path
    ) -> None:
        # A wrapper that forks (a shim, a node launcher) leaves a grandchild
        # sharing its stdout/stderr pipe. ``process.kill()`` on a timeout only
        # reaches the direct child: the grandchild keeps the pipe open and, left
        # unkilled, keeps running forever. The probe must return promptly AND the
        # grandchild must not outlive it.
        pidfile = tmp_path / "grandchild.pid"
        cli = _script(
            tmp_path,
            f'sleep 30 &\necho $! > "{pidfile}"\nsleep 30',
        )
        outcome: dict[str, registry.Probe] = {}

        def _run() -> None:
            # 1 s, not less: the pidfile is written only once the shell has
            # started and forked, and a group kill landing earlier on a loaded
            # host would fail this test with a misleading FileNotFoundError.
            outcome["probe"] = registry.probe("agy", executable=str(cli), timeout_seconds=1.0)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(timeout=5.0)
        assert not thread.is_alive(), "probe() did not return within the guard: it hung"

        found = outcome["probe"]
        assert found.available is False
        assert "no answer" in found.detail

        grandchild_pid = int(pidfile.read_text().strip())
        try:
            deadline = time.monotonic() + 1.0
            while _is_alive(grandchild_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            assert not _is_alive(grandchild_pid), (
                "the grandchild outlived the probe: its process group was not killed"
            )
        finally:
            if _is_alive(grandchild_pid):
                os.kill(grandchild_pid, 9)

    def test_an_interrupted_probe_leaves_no_process_behind(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``start_new_session=True`` takes the probed CLI out of the terminal's
        # foreground group, so a Ctrl-C no longer reaches it: an interrupt
        # raised while the probe waits must kill the group itself, then
        # propagate -- ``subprocess.run`` used to do both.
        pidfile = tmp_path / "child.pid"
        cli = _script(tmp_path, f'echo $$ > "{pidfile}"\nexec sleep 30')

        class _InterruptedOnce(subprocess.Popen[str]):
            interrupted = False

            def communicate(
                self, input: str | None = None, timeout: float | None = None
            ) -> tuple[str, str]:
                if not _InterruptedOnce.interrupted:
                    _InterruptedOnce.interrupted = True
                    deadline = time.monotonic() + 5.0
                    while not pidfile.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    raise KeyboardInterrupt
                return super().communicate(input, timeout)

        monkeypatch.setattr(registry.subprocess, "Popen", _InterruptedOnce)
        with pytest.raises(KeyboardInterrupt):
            registry.probe("agy", executable=str(cli), timeout_seconds=10.0)
        child_pid = int(pidfile.read_text().strip())
        try:
            assert not _is_alive(child_pid), "the probed CLI outlived an interrupted probe"
        finally:
            if _is_alive(child_pid):
                os.kill(child_pid, 9)
