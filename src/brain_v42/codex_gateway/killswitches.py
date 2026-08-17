"""Read-only adapter for the host-local Dream killswitch drop-in."""

from __future__ import annotations

from pathlib import Path

import structlog

from brain_v42.dream_killswitches import KILLSWITCHES_PATH, parse_killswitches

logger = structlog.get_logger(__name__)


class KillswitchReader:
    """Expose the local systemd drop-in as stable JSON booleans."""

    def __init__(self, path: Path = KILLSWITCHES_PATH) -> None:
        self._path = path

    async def read(self) -> dict[str, bool]:
        try:
            flags = parse_killswitches(self._path.read_text())
        except (OSError, UnicodeError):
            logger.warning("codex_gateway.killswitches_unreadable", path=str(self._path))
            flags = {}
        return {
            "promote_enabled": flags.get("promote", False),
            "promote_dry": False,
            "reorg_enabled": flags.get("reorg", False),
            "reorg_dry": flags.get("reorg_dry", False),
            "extract_enabled": flags.get("extract", False),
            "extract_dry": flags.get("extract_dry", True),
            "roadmap_enabled": flags.get("roadmap", False),
            "roadmap_dry": flags.get("roadmap_dry", True),
        }
