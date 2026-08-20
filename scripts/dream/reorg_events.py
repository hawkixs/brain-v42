"""Read the brain-v42 tool calls a REORG phase ACTUALLY emitted.

The phase report is a DECLARATION. The event stream is an OBSERVATION. Only
confronting the two can surface the ``bccc9115`` ghost — an id listed under
``updated`` for which no ``brain_update`` was ever emitted — and its symmetric
case, a ``brain_update`` emitted for an id the report never mentions. The second
direction is invisible today, and it is the worse of the two: a mutation nobody
holds a record of.

TWO DIALECTS, and the requirement is hard. The live rail is codex
(``BRAIN_DREAM_AGENT_PROVIDER=codex`` by default); agy is the fallback. A
single-dialect parser would report "0 writes observed" on every fallback night —
a false negative shaped exactly like good news, which nobody would question.
"Nothing recognised" must therefore stay a DIFFERENT fact from "nothing called".

Both shapes are MEASURED on real streams in this repo, not assumed. The agy one
in particular cannot be guessed: tool arguments live under
``tool_info.parameters.Arguments`` while the tool name lives under ``ToolName``,
``tool_name`` being invariably ``call_mcp_tool``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field

#: The MCP server whose calls are ours. Another server's ``brain_update`` proves
#: nothing about this corpus.
_SERVER = "brain-v42"

#: REORG mutates through this single tool — Part 1 writes ``tags``, Part 2 writes
#: ``freshness_status`` (phase_reorg.md §Part 1 c, §Part 2 d).
_MUTATING_TOOL = "brain_update"


@dataclass
class EventScan:
    """What the stream showed, kept separate from what it failed to show."""

    updated_ids: set[str] = field(default_factory=set)
    codex_events: int = 0
    agy_events: int = 0

    @property
    def recognised(self) -> bool:
        """True when at least one brain-v42 tool call was parsed, in any dialect.

        Guards the single most dangerous reading of this module: an empty
        ``updated_ids`` means "no mutation observed" only if the stream was
        readable at all. A new agent format, an empty file, or a stream from some
        other command all yield the same empty set, and calling that a clean
        night would be a lie the checker told itself.
        """
        return bool(self.codex_events or self.agy_events)


def _lines(content: str) -> Iterator[dict]:
    """Yield JSON objects, skipping blank, malformed, and non-object lines.

    A stream truncated mid-line by a phase timeout is the NORMAL case here, not
    the exception: refusing to parse the rest would throw away the very calls a
    dying phase managed to make.
    """
    for raw in content.splitlines():
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def _entity_id(arguments: object) -> str | None:
    if not isinstance(arguments, dict):
        return None
    value = arguments.get("entity_id")
    return value if isinstance(value, str) and value else None


def scan_events(content: str) -> EventScan:
    """Return the brain-v42 mutations observed in a phase event stream."""
    scan = EventScan()

    for event in _lines(content):
        # ── codex: {"type": "item.completed", "item": {...mcp_tool_call...}} ──
        if event.get("type") == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict):
                continue
            if item.get("type") != "mcp_tool_call" or item.get("server") != _SERVER:
                continue
            scan.codex_events += 1
            if item.get("tool") == _MUTATING_TOOL:
                entity_id = _entity_id(item.get("arguments"))
                if entity_id is not None:
                    scan.updated_ids.add(entity_id)
            continue

        # ── agy: {"event": "step_update", "step_update": {...call_mcp_tool...}} ──
        if event.get("event") == "step_update":
            step = event.get("step_update")
            if not isinstance(step, dict) or step.get("tool_name") != "call_mcp_tool":
                continue
            info = step.get("tool_info")
            parameters = info.get("parameters") if isinstance(info, dict) else None
            if not isinstance(parameters, dict) or parameters.get("ServerName") != _SERVER:
                continue
            scan.agy_events += 1
            if parameters.get("ToolName") == _MUTATING_TOOL:
                entity_id = _entity_id(parameters.get("Arguments"))
                if entity_id is not None:
                    scan.updated_ids.add(entity_id)

    return scan
