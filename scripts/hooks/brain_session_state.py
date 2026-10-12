#!/usr/bin/env python3
"""Per-session state feeding brain_admin_elevate.py, copied and registered by the operator.

Install it next to brain_admin_elevate.py and register this script, not the elevate hook,
for two Claude Code events. Claude Code runs the hooks of one event in parallel, so on
UserPromptSubmit this script calls the elevate hook itself: the prompt is judged against
the count of PRECEDING prompts, and only afterwards does it join the count. PostToolUse
(matcher on the Brain session lifecycle tools) records which Brain session this Claude
session holds. State lives in one private file per Claude session under
BRAIN_SESSION_STATE_DIR (default ~/.local/state/brain-v42/sessions): claude_session_id,
brain_session_id, prompt_count. Everything here fails closed and silently, because a
broken hook must never block the operator's prompt; neither prompts nor tool responses are
ever written anywhere, only validated UUIDs and a counter.
"""

from __future__ import annotations

import fcntl
import json
import os
import runpy
import stat
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

_PREFIX = "mcp__brain-v42__brain_session_"
_RECORDING = frozenset(_PREFIX + name for name in ("start", "resume", "relay"))
_CLEARING = frozenset(_PREFIX + name for name in ("end", "abandon"))
_MAX_RESPONSE = 1024 * 1024
_MAX_STDIN = 4 * 1024 * 1024
_MAX_STATE = 16 * 1024
_DEFAULT_DIR = Path("~/.local/state/brain-v42/sessions")
_ERRORS = (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, RuntimeError)


def _uuid(value: object, *, canonical_only: bool = False) -> str | None:
    """Accept only UUIDs; the Claude id also names a file, so it must be spelled canonically."""
    if not isinstance(value, str):
        return None
    try:
        parsed = str(UUID(value))
    except ValueError:
        return None
    return None if canonical_only and parsed != value else parsed


def _open_state_dir(environ: Mapping[str, str]) -> tuple[int, Path] | None:
    """Open the state directory, or refuse it: the elevation state is a privilege input.

    The checks run on the opened descriptor, so a swap between check and use cannot
    redirect later writes; O_NOFOLLOW rejects a symlinked directory outright.
    """
    raw = environ.get("BRAIN_SESSION_STATE_DIR")
    path = Path(raw) if raw else _DEFAULT_DIR.expanduser()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.mkdir(path, 0o700)
            except FileExistsError:
                pass
            descriptor = os.open(path, flags)
    except _ERRORS:
        return None
    metadata = os.fstat(descriptor)
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        os.close(descriptor)
        return None
    return descriptor, path


def _read_state(directory: int, name: str) -> dict[str, Any]:
    """A missing, foreign or corrupt file reads as empty: a lower count only refuses more."""
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    except OSError:
        return {}
    with os.fdopen(descriptor, "r", encoding="utf-8") as file:
        metadata = os.fstat(file.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            return {}
        try:
            state = json.loads(file.read(_MAX_STATE + 1))
        except ValueError:
            return {}
    return state if isinstance(state, dict) else {}


def _write_state(directory: int, name: str, state: Mapping[str, Any]) -> None:
    """Temp file in the same directory, fsync, rename: a reader never sees a torn state."""
    temporary = f".{uuid4().hex}.tmp"
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(json.dumps(state))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=directory)
        except OSError:
            pass
        raise


def _update(
    directory: int, claude_id: str, change: Callable[[dict[str, Any]], bool | None]
) -> None:
    """Read-modify-write under a directory lock; a change returning False writes nothing."""
    name = f"{claude_id}.json"
    fcntl.flock(directory, fcntl.LOCK_EX)
    state = _read_state(directory, name)
    if change(state) is False:
        return
    state["claude_session_id"] = claude_id
    _write_state(directory, name, state)


def _count_prompt(state: dict[str, Any]) -> None:
    count = state.get("prompt_count")
    state["prompt_count"] = count + 1 if type(count) is int and count >= 0 else 1


def _decode(value: object, depth: int = 0) -> dict[str, Any] | None:
    """Find the tool result in a dict, a JSON string or MCP content blocks; errors give None."""
    if depth > 3:
        return None
    if isinstance(value, str):
        try:
            return _decode(json.loads(value), depth + 1)
        except ValueError:
            return None
    if isinstance(value, list):
        for block in value:
            if isinstance(block, dict) and block.get("type") == "text":
                found = _decode(block.get("text"), depth + 1)
                if found is not None:
                    return found
        return None
    if not isinstance(value, dict) or value.get("isError"):
        return None
    if "session" in value:
        return value
    for key in ("structuredContent", "content"):
        found = _decode(value.get(key), depth + 1)
        if found is not None:
            return found
    return None


def _result_session_id(response: object) -> str | None:
    """`session.id` is the opened or resumed session, the ended one, and a relay's SUCCESSOR."""
    size = len(response) if isinstance(response, str) else len(json.dumps(response))
    if size > _MAX_RESPONSE:
        return None
    result = _decode(response)
    session = result.get("session") if result else None
    return _uuid(session.get("id")) if isinstance(session, dict) else None


def _on_tool(directory: int, claude_id: str, payload: Mapping[str, Any]) -> None:
    tool = payload.get("tool_name")
    if tool not in _RECORDING and tool not in _CLEARING:
        return
    brain_id = _result_session_id(payload.get("tool_response"))
    if brain_id is None:
        return

    def change(state: dict[str, Any]) -> bool | None:
        if tool in _RECORDING:
            state["brain_session_id"] = brain_id
            return None
        if _uuid(state.get("brain_session_id")) != brain_id:
            return False
        del state["brain_session_id"]
        return None

    _update(directory, claude_id, change)


def _on_prompt(
    directory: int,
    state_dir: Path,
    claude_id: str,
    payload: Mapping[str, Any],
    environ: Mapping[str, str],
    opener: Callable[..., Any] | None,
) -> str:
    elevate = runpy.run_path(str(Path(__file__).resolve().parent / "brain_admin_elevate.py"))
    scoped = {**environ, "BRAIN_ADMIN_STATE_FILE": str(state_dir / f"{claude_id}.json")}
    try:
        line = elevate["run_hook"](payload, environ=scoped, opener=opener)
    except _ERRORS:
        line = ""
    try:
        _update(directory, claude_id, _count_prompt)
    except _ERRORS:
        pass
    return line


def run(
    payload: Mapping[str, Any],
    *,
    environ: Mapping[str, str],
    opener: Callable[..., Any] | None = None,
) -> str:
    """Return the elevate hook's status line for a prompt; anything unexpected is a no-op."""
    claude_id = _uuid(payload.get("session_id"), canonical_only=True)
    event = payload.get("hook_event_name")
    if claude_id is None or event not in {"UserPromptSubmit", "PostToolUse"}:
        return ""
    if event == "UserPromptSubmit" and not isinstance(payload.get("prompt"), str):
        return ""
    opened = _open_state_dir(environ)
    if opened is None:
        return ""
    directory, state_dir = opened
    try:
        if event == "PostToolUse":
            _on_tool(directory, claude_id, payload)
            return ""
        return _on_prompt(directory, state_dir, claude_id, payload, environ, opener)
    except _ERRORS:
        return ""
    finally:
        os.close(directory)


def main() -> int:
    """Never prevent the operator's ordinary prompt or the tool result from flowing."""
    try:
        payload = json.loads(sys.stdin.read(_MAX_STDIN + 1))
        if isinstance(payload, dict):
            sys.stdout.write(run(payload, environ=os.environ))
    except _ERRORS:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
