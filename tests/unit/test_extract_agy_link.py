"""Unit tests for `brain_v42.scripts.agy_completion` — the extract phase's third,
agy-backed (Gemini) link.

Never invokes the real `agy` binary: `asyncio.create_subprocess_exec` is
monkeypatched throughout. The one real invocation this feature earns lives
outside the test suite, run manually against the live binary.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from brain_v42.scripts.agy_completion import (
    _MAX_PROMPT_BYTES,
    DEFAULT_AGY_EXECUTABLE,
    AgyLinkError,
    agy_chat_completion,
    build_agy_completion_command,
    build_toolless_home,
    flatten_messages,
    resolve_agy_executable,
)


def test_resolve_agy_executable_defaults_to_agy() -> None:
    assert resolve_agy_executable({}) == DEFAULT_AGY_EXECUTABLE


def test_resolve_agy_executable_reads_the_dream_bin_env() -> None:
    assert resolve_agy_executable({"BRAIN_DREAM_AGY_BIN": "/opt/agy"}) == "/opt/agy"


class TestFlattenMessages:
    def test_sections_are_ordered_by_role_and_double_newline_joined(self) -> None:
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "usr"},
        ]
        assert flatten_messages(messages) == "### system\nsys\n\n### user\nusr"

    def test_the_corrective_assistant_turn_gets_its_own_section(self) -> None:
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "usr"},
            {"role": "assistant", "content": "bad json"},
            {"role": "user", "content": "fix it"},
        ]
        flattened = flatten_messages(messages)
        assert flattened.count("### assistant") == 1
        assert flattened.index("### assistant") < flattened.index("### user\nfix it")


class TestBuildAgyCompletionCommand:
    def test_the_prompt_travels_in_argv(self) -> None:
        command = build_agy_completion_command(
            model="gemini-3.8-flash-high",
            prompt="hello",
            agy_executable="agy",
            timeout_seconds=30.0,
        )
        assert "hello" in command
        assert "--print" in command

    def test_output_format_is_json_not_stream_json(self) -> None:
        command = build_agy_completion_command(
            model="gemini-3.8-flash-high",
            prompt="hello",
            agy_executable="agy",
            timeout_seconds=30.0,
        )
        assert command[command.index("--output-format") + 1] == "json"

    def test_never_grants_dangerously_skip_permissions(self) -> None:
        """extract's link is tool-less: no tool call is expected, so nothing
        should ever be pre-approved. `--print-timeout` bounds a stray attempt."""
        command = build_agy_completion_command(
            model="gemini-3.8-flash-high",
            prompt="hello",
            agy_executable="agy",
            timeout_seconds=30.0,
        )
        assert "--dangerously-skip-permissions" not in command

    def test_refuses_an_oversize_prompt_before_execve(self) -> None:
        oversized = "x" * (_MAX_PROMPT_BYTES + 1)
        with pytest.raises(ValueError):
            build_agy_completion_command(
                model="m", prompt=oversized, agy_executable="agy", timeout_seconds=30.0
            )


class TestToolLessHome:
    def test_the_mcp_config_declares_no_servers(self, tmp_path: Path) -> None:
        home = build_toolless_home(tmp_path, real_home=tmp_path / "real")
        config = json.loads((home / ".gemini" / "config" / "mcp_config.json").read_text())
        assert config == {"mcpServers": {}}

    def test_credentials_are_symlinked_never_copied(self, tmp_path: Path) -> None:
        real_home = tmp_path / "real"
        (real_home / ".gemini").mkdir(parents=True)
        (real_home / ".gemini" / "oauth_creds.json").write_text("{}")

        home = build_toolless_home(tmp_path, real_home=real_home)

        linked = home / ".gemini" / "oauth_creds.json"
        assert linked.is_symlink()

    def test_a_missing_credential_file_is_skipped_without_raising(self, tmp_path: Path) -> None:
        home = build_toolless_home(tmp_path, real_home=tmp_path / "real-without-gemini")
        assert not (home / ".gemini" / "oauth_creds.json").exists()


class _FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes, returncode: int) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> None:
        return None


def _envelope(**overrides: Any) -> bytes:
    payload: dict[str, Any] = {
        "conversation_id": "c1",
        "status": "SUCCESS",
        "response": '{"ok": true}',
        "duration_seconds": 1.92,
        "num_turns": 1,
        "usage": {
            "input_tokens": 22495,
            "output_tokens": 212,
            "thinking_tokens": 198,
            "cache_read_tokens": 0,
            "total_tokens": 22707,
        },
    }
    payload.update(overrides)
    return json.dumps(payload).encode("utf-8")


def _patch_subprocess(monkeypatch: pytest.MonkeyPatch, process: _FakeProcess) -> None:
    async def fake_exec(*_args: object, **_kwargs: object) -> _FakeProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


class TestAgyChatCompletion:
    @pytest.mark.asyncio
    async def test_a_successful_call_returns_the_response_and_mapped_usage(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        _patch_subprocess(monkeypatch, _FakeProcess(_envelope(), b"", 0))

        content, usage = await agy_chat_completion(
            model="gemini-3.8-flash-high",
            messages=[{"role": "user", "content": "hi"}],
            agy_executable="agy",
            timeout_seconds=5.0,
        )

        assert content == '{"ok": true}'
        assert usage == {"reasoning_tokens": 198}

    @pytest.mark.asyncio
    async def test_a_nonzero_exit_raises_agy_link_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        _patch_subprocess(monkeypatch, _FakeProcess(b"", b"boom", 1))

        with pytest.raises(AgyLinkError):
            await agy_chat_completion(
                model="m",
                messages=[{"role": "user", "content": "hi"}],
                agy_executable="agy",
                timeout_seconds=5.0,
            )

    @pytest.mark.asyncio
    async def test_a_non_success_status_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        _patch_subprocess(monkeypatch, _FakeProcess(_envelope(status="TIMEOUT"), b"", 0))

        with pytest.raises(AgyLinkError):
            await agy_chat_completion(
                model="m",
                messages=[{"role": "user", "content": "hi"}],
                agy_executable="agy",
                timeout_seconds=5.0,
            )

    @pytest.mark.asyncio
    async def test_non_json_stdout_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        _patch_subprocess(monkeypatch, _FakeProcess(b"not json", b"", 0))

        with pytest.raises(AgyLinkError):
            await agy_chat_completion(
                model="m",
                messages=[{"role": "user", "content": "hi"}],
                agy_executable="agy",
                timeout_seconds=5.0,
            )

    @pytest.mark.asyncio
    async def test_a_missing_response_field_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        _patch_subprocess(monkeypatch, _FakeProcess(_envelope(response=""), b"", 0))

        with pytest.raises(AgyLinkError):
            await agy_chat_completion(
                model="m",
                messages=[{"role": "user", "content": "hi"}],
                agy_executable="agy",
                timeout_seconds=5.0,
            )

    @pytest.mark.asyncio
    async def test_usage_without_thinking_tokens_maps_to_no_reasoning_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """049's contract: an absent count is `None`, never a fabricated zero.

        `thinking_tokens_from_usage` reads that as "not measured" only when the
        key itself is missing — so an unmeasured agy call must not fabricate the
        key either.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        payload = json.loads(_envelope())
        del payload["usage"]["thinking_tokens"]
        _patch_subprocess(monkeypatch, _FakeProcess(json.dumps(payload).encode("utf-8"), b"", 0))

        _content, usage = await agy_chat_completion(
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            agy_executable="agy",
            timeout_seconds=5.0,
        )
        assert usage == {}

    @pytest.mark.asyncio
    async def test_the_prompt_is_the_flattened_messages_not_raw_json(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        captured: dict[str, tuple[object, ...]] = {}

        async def fake_exec(*args: object, **kwargs: object) -> _FakeProcess:
            captured["args"] = args
            return _FakeProcess(_envelope(), b"", 0)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

        await agy_chat_completion(
            model="m",
            messages=[
                {"role": "system", "content": "extraction rules"},
                {"role": "user", "content": "ticket body"},
            ],
            agy_executable="agy",
            timeout_seconds=5.0,
        )

        prompt = captured["args"][captured["args"].index("--print") + 1]
        assert prompt == "### system\nextraction rules\n\n### user\nticket body"

    @pytest.mark.asyncio
    async def test_a_long_running_agy_call_is_killed_on_the_watchdog_deadline(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`--print-timeout` is agy's OWN deadline; this is the backstop for a
        binary that ignores it."""
        monkeypatch.setenv("HOME", str(tmp_path))

        class _HangingProcess(_FakeProcess):
            async def communicate(self) -> tuple[bytes, bytes]:
                await asyncio.sleep(10)
                return b"", b""

        hanging = _HangingProcess(b"", b"", 0)

        async def fake_exec(*_a: object, **_k: object) -> _HangingProcess:
            return hanging

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

        with pytest.raises(AgyLinkError):
            await agy_chat_completion(
                model="m",
                messages=[{"role": "user", "content": "hi"}],
                agy_executable="agy",
                timeout_seconds=0.01,
            )
        assert hanging.killed
