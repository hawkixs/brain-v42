"""Publish the measured identity of the immutable release running this process."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import cast

from brain_v42.facts.model import FactTarget
from brain_v42.facts.probe import SourceSession, ValueType
from brain_v42.facts.sources import ReleaseSourceSession


class LiveReleaseShaProbe:
    """Publish the measured release identity so a claim can name the release it proves.

    The fact is the identity itself, rather than a second path reader: the
    source owns that read so value and verified identity stay one observation.
    """

    name: str = "live_release_sha"
    definition_version: int = 1
    target: FactTarget = FactTarget.LIVE_RELEASE
    ttl: timedelta = timedelta(days=3650)
    timeout: timedelta = timedelta(seconds=1)
    briefing: bool = True
    policies: Mapping[str, int] = {}
    value_schema: Mapping[str, ValueType] = {
        "release_sha": "string",
        "package_version": "string",
    }

    async def measure(self, source: SourceSession) -> Mapping[str, object]:
        """Return the source-owned identity without reading the imported package path."""
        identity = await cast(ReleaseSourceSession, source).identity()
        return {
            "release_sha": identity.release_sha,
            "package_version": identity.package_version,
        }
