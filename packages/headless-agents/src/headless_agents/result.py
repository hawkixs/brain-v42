"""``RunResult`` -- the common output shape across the providers.

``tokens`` is ``None``, not a zeroed :class:`TokenUsage`, when the rail's own
telemetry does not measure a count: absent is not zero, and a consumer that
persists these figures must be able to tell the two apart. A provider that
cannot observe cached-input tokens must report ``cached=None`` there, never
``0``.

``model`` is the label the caller asked for; ``model_reported`` is what the
CLI's envelope says actually answered, when it says anything (only ``claude``
names its model today). The two differ whenever a CLI silently substitutes,
which is exactly the case a consumer wants to display rather than normalise.

``text`` is the run's final answer, read by the provider from the file its
rail writes the answer to, and ``None`` when the run produced none -- a failed
run, or an answer file left empty. It is VERBATIM: an answer that is itself
JSON comes back as the agent wrote it, never re-read as an envelope.

:meth:`RunResult.to_dict` is the serialised form, schema
:data:`RESULT_SCHEMA_VERSION`: the contract of ``result.json`` and of
``ha run --json``. ``null`` in it keeps the meaning "not measured", never zero.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

RESULT_SCHEMA_VERSION = 1


@dataclass(frozen=True, kw_only=True)
class TokenUsage:
    """Token counts a provider measured for one run. ``None`` = not measured."""

    input: int | None = None
    output: int | None = None
    fresh: int | None = None
    cached: int | None = None
    thinking: int | None = None


def _path(value: Path | None) -> str | None:
    return None if value is None else str(value)


@dataclass(frozen=True, kw_only=True)
class RunResult:
    """The outcome of one :meth:`AgentProvider.run` call."""

    exit_code: int
    provider: str
    model: str
    report_path: Path | None
    events_log: Path | None
    tokens: TokenUsage | None
    duration_seconds: float
    tool_call_completed: bool
    model_reported: str | None = None
    cost_usd: float | None = None
    text: str | None = None
    run_id: str | None = None
    stderr_log: Path | None = None
    raw_log: Path | None = None
    context: tuple[dict[str, object], ...] | None = None
    workspace: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        """The JSON-safe schema-1 form of this result.

        ``context``, ``workspace`` and ``branch`` belong to schema 1 from the
        start, so a consumer can pin the key set. ``context`` and ``workspace``
        stay ``None`` until the caller fills them; ``branch`` stays ``None``
        until lot 4 (the ``ha run --write`` carrier branch).
        """
        return {
            "schema": RESULT_SCHEMA_VERSION,
            "run_id": self.run_id,
            "provider": self.provider,
            "model": self.model,
            "model_reported": self.model_reported,
            "exit_code": self.exit_code,
            "text": self.text,
            "tokens": None if self.tokens is None else asdict(self.tokens),
            "cost_usd": self.cost_usd,
            "duration_seconds": self.duration_seconds,
            "tool_call_completed": self.tool_call_completed,
            "context": None if self.context is None else [dict(entry) for entry in self.context],
            "workspace": None if self.workspace is None else dict(self.workspace),
            "branch": None,
            "logs": {
                "report": _path(self.report_path),
                "events": _path(self.events_log),
                "stderr": _path(self.stderr_log),
                "raw": _path(self.raw_log),
            },
        }
