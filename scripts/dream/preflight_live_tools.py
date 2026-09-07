"""Fail-closed preflight: no phase may name a tool the LIVE server lacks.

Incident (2026-09-07 06:00): PR #102 was merged at 01:57 and rewrote
``scripts/dream/phase_promote.md`` to call ``brain_promote_adr``.
``scripts/`` is read from the repository at run time, but ``src/`` only
becomes live at the next ``brain-mcp-http`` restart, which happened at
10:15 — the running server had no such tool. ``auto-discord`` and
``watchk-claude`` refused to promote (2 promotions lost); ``brain-v42`` and
``refondrre`` disobeyed the prompt and used the old tools instead.

``tests/unit/test_dream_prompts_only_name_real_tools.py`` proves a prompt
names only tools the REPOSITORY declares — a purely static, syntax-level
check against the tool modules on disk. It cannot see whether the RUNNING
process has actually loaded that declaration: the server imports its tool
constants once, at start-up, and is restarted by hand. This module closes
that gap by asking the live server itself, over the same MCP transport the
phases use, right before the night's pool loop.

Usage::

    python -m scripts.dream.preflight_live_tools
    -> exit 0 and a one-line OK summary when every phase prompt names only
       tools the live server registers.
    -> exit 2 and one line per missing (phase, tool) pair, or a named
       reason the live server could not be reached, otherwise. Fail-closed:
       an unknown server state must never let the night start.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from scripts.dream._agent_capability import (
    DEFAULT_MCP_URL,
    MCP_TOKEN_ENV,
    MCP_URL_ENV,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = REPO_ROOT / "scripts" / "dream"

#: Mirrors tests/unit/test_dream_prompts_only_name_real_tools.py::_NOT_A_TOOL —
#: the only `brain_*` token a prompt legitimately names without a tool behind
#: it. Kept in sync by test_phase_tool_mentions_agrees_with_the_repository_guard.
_NOT_A_TOOL = frozenset({"brain_v42"})
_TOOL_MENTION = re.compile(r"\b(brain_[a-z][a-z0-9_]*)")

# A plain admin bearer sees the COMPACT, BM25-filtered catalogue by default in
# production (BRAIN_MCP_PROFILE=compact) — only the session-lifecycle tools
# and the two find/call gateways. Asking tools/list without this header would
# make every phase look broken. See
# brain_v42.mcp.tool_catalog._RequestAwareBM25SearchTransform.transform_tools.
NATIVE_PROFILE_HEADERS = {"x-brain-tool-profile": "native"}

DEFAULT_TIMEOUT_SECONDS = 10.0
FAIL_EXIT_CODE = 2


class LiveServerUnreachable(RuntimeError):
    """The live ``tools/list`` call could not be completed."""


def phase_tool_mentions(prompt_dir: Path = PROMPT_DIR) -> dict[str, frozenset[str]]:
    """Map each phase name (``phase_scan.md`` -> ``scan``) to the tools it names."""
    mentions: dict[str, frozenset[str]] = {}
    for path in sorted(prompt_dir.glob("phase_*.md")):
        phase = path.stem.removeprefix("phase_")
        text = path.read_text(encoding="utf-8")
        mentions[phase] = frozenset(_TOOL_MENTION.findall(text)) - _NOT_A_TOOL
    return mentions


async def fetch_live_tool_names(
    url: str,
    token: str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> frozenset[str]:
    """Return every tool name the live server registers, or raise fail-closed.

    Bounded by ``timeout_seconds`` end to end (connect + handshake + list) so a
    hung server cannot hang the night's start instead of refusing it.
    """
    transport = StreamableHttpTransport(url, auth=token, headers=NATIVE_PROFILE_HEADERS)
    try:
        async with Client(transport) as client:
            listed = await asyncio.wait_for(client.list_tools(), timeout=timeout_seconds)
    except Exception as exc:  # noqa: BLE001 — fail-closed: any error refuses the night
        raise LiveServerUnreachable(f"{type(exc).__name__}: {exc}") from exc
    return frozenset(tool.name for tool in listed)


def missing_tool_pairs(
    mentions: Mapping[str, frozenset[str]],
    live_tools: frozenset[str],
) -> list[tuple[str, str]]:
    """Every (phase, tool) pair the live server does not register, sorted."""
    return sorted(
        (phase, tool)
        for phase, tools in mentions.items()
        for tool in tools
        if tool not in live_tools
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fail-closed check: every Dream phase prompt names only "
        "tools the LIVE MCP server registers."
    )
    parser.add_argument(
        "--url",
        default=os.environ.get(MCP_URL_ENV, DEFAULT_MCP_URL),
        help=f"Live MCP endpoint (default: ${MCP_URL_ENV} or {DEFAULT_MCP_URL})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Bounded seconds for the whole tools/list round trip",
    )
    parser.add_argument(
        "--prompt-dir",
        type=Path,
        default=PROMPT_DIR,
        help="Directory holding phase_*.md prompts (default: scripts/dream)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    token = os.environ.get(MCP_TOKEN_ENV)
    if not token:
        print(
            f"FAIL preflight_live_tools — {MCP_TOKEN_ENV} is not set",
            file=sys.stderr,
        )
        return FAIL_EXIT_CODE

    mentions = phase_tool_mentions(args.prompt_dir)

    try:
        live_tools = asyncio.run(
            fetch_live_tool_names(args.url, token, timeout_seconds=args.timeout)
        )
    except LiveServerUnreachable as exc:
        print(
            f"FAIL preflight_live_tools — could not reach the live MCP server at {args.url}: {exc}",
            file=sys.stderr,
        )
        return FAIL_EXIT_CODE

    missing = missing_tool_pairs(mentions, live_tools)
    if missing:
        print(
            f"FAIL preflight_live_tools — {len(missing)} phase/tool pair(s) the "
            "live server does not register (repo says it exists, running "
            "process disagrees — restart brain-mcp-http, or fix the prompt):",
            file=sys.stderr,
        )
        for phase, tool in missing:
            print(f"  {phase}: {tool}", file=sys.stderr)
        return FAIL_EXIT_CODE

    print(
        f"OK preflight_live_tools — {len(mentions)} phase prompt(s) checked "
        "against the live tool catalogue, nothing missing"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
