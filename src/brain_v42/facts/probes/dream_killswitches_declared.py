"""Read the Dream killswitch declarations through the verified host source.

The payload deliberately preserves raw strings.  The executable's polarity
rules belong to its readers; coercing here would hide a malformed declaration
from the operator who needs to repair it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import cast

from brain_v42.dream_killswitches import KILLSWITCH_SHORT_NAMES, iter_killswitch_settings
from brain_v42.facts.model import FactTarget
from brain_v42.facts.probe import SourceSession, ValueType
from brain_v42.facts.sources import HostSourceSession


class DreamKillswitchesDeclaredProbe:
    """Publish the host drop-in exactly as declared, with its modification time."""

    name: str = "dream_killswitches_declared"
    definition_version: int = 1
    target: FactTarget = FactTarget.HOST
    ttl: timedelta = timedelta(seconds=60)
    timeout: timedelta = timedelta(seconds=1)
    briefing: bool = True
    policies: Mapping[str, int] = {}
    value_schema: Mapping[str, ValueType] = {
        "promote": "string",
        "reorg": "string",
        "reorg_dry": "string",
        "extract": "string",
        "extract_dry": "string",
        "roadmap": "string",
        "roadmap_dry": "string",
        "sweep": "string",
        "sweep_dry": "string",
        "file_mtime_epoch": "int",
    }

    def __init__(self, relative: str = "brain-v42-dream.service.d/killswitches.conf") -> None:
        self.relative = relative

    async def measure(self, source: SourceSession) -> Mapping[str, object]:
        """Keep systemd's last assignment without converting the operator's words."""
        text, mtime_epoch = cast(HostSourceSession, source).read_text(self.relative)
        value: dict[str, object] = dict.fromkeys(KILLSWITCH_SHORT_NAMES.values(), "")
        for key, raw in iter_killswitch_settings(text):
            value[KILLSWITCH_SHORT_NAMES[key]] = raw
        value["file_mtime_epoch"] = mtime_epoch
        return value
