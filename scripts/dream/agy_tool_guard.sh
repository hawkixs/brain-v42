#!/usr/bin/env bash
#
# Tool guard of the agy rail — a `PreToolUse` hook, deny by default.
#
# WHY IT EXISTS. agy has no equivalent of claude's `--tools ""` nor of codex's
# `enabled_tools`. Measured on 2026-08-11 against v1.1.11: it exposes 56 tools,
# among them run_command, write_to_file, invoke_subagent, schedule and browser
# control. `--dangerously-skip-permissions`, REQUIRED in headless mode or the
# session waits for an approval that will never come, auto-approves every one
# of them. On a prompt asking for ONE MCP call, agy ran twelve tool steps and
# launched `ps aux`. Without this guard, an unattended dream phase gets a shell
# under an account that belongs to `sudo` and `docker`.
#
# Three other mechanisms were tried and MEASURED inoperative: `disabledTools`
# in settings.json (no effect), `--mode plan` documented as "read-only" (runs
# the shell anyway), and a CLI restriction flag (does not exist). The
# `PreToolUse` hook returning `{"decision":"deny"}` is the only one that holds
# — and it survives `--dangerously-skip-permissions`, which is the point that
# could have invalidated everything.
#
# WHAT IT DOES NOT DO. It protects the MACHINE, not the corpus. What a phase
# can do to the brain is bounded SERVER-side by the (project, phase) bearer —
# verified: a direct tools/call outside the perimeter answers
# "Dream capability authorization denied". Hence authorising `call_mcp_tool`
# wholesale here: this guard has no business replaying a check the server does
# better, and duplicating it would create two lists to keep agreeing by hand.
#
# UNCONDITIONAL, with no switch. An earlier version made it conditional on
# BRAIN_DREAM_PHASE; the runner's ephemeral HOME makes that pointless, since it
# is wired nowhere else. A switch would only be one more thing to forget to
# set — and forgetting it would be silent.
#
# Contract (agy's docs/hooks.md): JSON payload on stdin, JSON decision on
# stdout. Anything else written to stdout would break the parsing and
# transformerait un refus en autorisation.

set -uo pipefail

payload=$(cat 2>/dev/null || true)

# An allowlist, not a denylist. agy exposes 56 tools today and will gain more
# with every release: an allowlist goes stale in the right direction (a new tool
# is refused), a denylist in the wrong one (a new tool gets through).
_allow() { printf '{"decision":"allow"}'; }
_deny()  { printf '{"decision":"deny","reason":"%s"}' "$1"; }

tool_name=$(
  printf '%s' "$payload" | python3 -c '
import json, sys
try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if not isinstance(payload, dict):
    sys.exit(0)
call = payload.get("toolCall")
if not isinstance(call, dict):
    sys.exit(0)
name = call.get("name")
if isinstance(name, str):
    print(name)
' 2>/dev/null
)

# Fail-closed. An empty, unreadable or unexpectedly shaped payload proves
# nothing — and the day agy changes this format, a guard that opens on the
# unknown would let everything through without a sound.
if [[ -z "$tool_name" ]]; then
  _deny "payload de hook illisible — refus par defaut"
  exit 0
fi

case "$tool_name" in
  # The gateway to brain-v42, and the strict minimum for an agent turn to be
  # able to hand back its report.
  call_mcp_tool|list_resources|read_resource|finish|send_message)
    _allow
    ;;
  *)
    _deny "outil hors perimetre d'une phase de dream: $tool_name"
    ;;
esac
