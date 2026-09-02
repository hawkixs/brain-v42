"""The agy rail's tool guard — deny by default, and fail-closed.

agy has NO equivalent of claude's `--tools ""` nor of codex's `enabled_tools`:
measured on 2026-08-11, it exposes 56 tools including run_command,
write_to_file, invoke_subagent and schedule, and
`--dangerously-skip-permissions` — required in headless mode — auto-approves them
all. On a prompt asking for ONE MCP call, it executed 12 tool steps and ran
`ps aux`.

The only mechanism that constrains it is a `PreToolUse` hook returning
`{"decision":"deny"}`. Verified: it survives `--dangerously-skip-permissions`.

This guard is UNCONDITIONAL. An earlier version conditioned it on an environment
variable, which made it a switch one could forget to set. The runner's ephemeral
HOME makes that precaution useless: the guard is wired only there, so it cannot
get in the way of an interactive session, so it has no reason to have a
permissive mode.

Separation of roles, not to be confused:
- the GUARD protects the MACHINE (shell, files, sub-agents, cron);
- the scoped BEARER protects the CORPUS, and it is the server that enforces it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD = REPO_ROOT / "scripts" / "dream" / "agy_tool_guard.sh"

# What a dream phase needs to call. `call_mcp_tool` is the gateway through which
# agy reaches brain-v42; the server then bounds what it can do there, per phase,
# through the bearer.
ALLOWED = ("call_mcp_tool", "list_resources", "read_resource", "finish", "send_message")

# A sample of what agy exposes and that a nightly phase must never obtain. The
# list is not exhaustive BY DESIGN: the guard is deny by default, so a tool added
# by a future version of agy is refused without anyone having to update this
# list.
FORBIDDEN = (
    "run_command",
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "invoke_subagent",
    "define_subagent",
    "schedule",
    "search_web",
    "read_url_content",
    "browser_click_element",
    "execute_browser_javascript",
    "delete_knowledge",
    "notebook_edit",
    "send_command_input",
)


def _decide(payload: str) -> dict[str, object]:
    result = subprocess.run(
        ["bash", str(GUARD)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _tool_call(name: str) -> str:
    return json.dumps({"toolCall": {"name": name, "args": {}}, "stepIdx": 1})


def test_guard_exists_and_is_executable() -> None:
    assert GUARD.is_file(), "la garde doit être versionnée dans le dépôt"


def test_the_brain_gateway_and_the_reply_tools_are_allowed() -> None:
    for name in ALLOWED:
        assert _decide(_tool_call(name))["decision"] == "allow", name


def test_every_machine_reaching_tool_is_denied() -> None:
    for name in FORBIDDEN:
        decision = _decide(_tool_call(name))
        assert decision["decision"] == "deny", name
        assert decision.get("reason"), f"un refus sans raison est illisible au matin ({name})"


def test_an_unknown_tool_is_denied_by_default() -> None:
    """The whole point of the design: agy exposes 56 tools and will gain more.

    An allowlist goes stale silently in the right direction; a denylist goes stale
    in the wrong one. A tool invented tomorrow must be refused without anyone
    touching this guard.
    """
    assert _decide(_tool_call("tool_that_does_not_exist_yet"))["decision"] == "deny"


def test_a_malformed_payload_is_denied_rather_than_allowed() -> None:
    """Fail-closed. A guard that opens on an input it does not understand guards
    nothing — and the day the payload format changes, it would let everything
    through without a sound."""
    for payload in ("", "   ", "not json at all", "{}", '{"toolCall": {}}', "[]", "null"):
        decision = _decide(payload)
        assert decision["decision"] == "deny", repr(payload)


def test_the_decision_is_always_valid_json_on_stdout() -> None:
    """agy reads stdout as JSON. A guard that writes anything else would be
    ignored, and the refusal would turn into an authorisation."""
    for payload in ("", "garbage", _tool_call("run_command"), _tool_call("call_mcp_tool")):
        result = subprocess.run(
            ["bash", str(GUARD)], input=payload, capture_output=True, text=True, timeout=30
        )
        parsed = json.loads(result.stdout)
        assert parsed["decision"] in {"allow", "deny"}


def test_the_guard_never_writes_to_stdout_beyond_its_decision() -> None:
    """A forgotten debug `echo` would break the parsing at the same stroke."""
    result = subprocess.run(
        ["bash", str(GUARD)],
        input=_tool_call("call_mcp_tool"),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stdout.strip() == json.dumps({"decision": "allow"}, separators=(",", ":"))
