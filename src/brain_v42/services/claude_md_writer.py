"""Marker-based CLAUDE.md section updater.

Writes a small dynamic section between markers in CLAUDE.md files.
Only the content between markers is modified. Everything else is preserved.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

MARKER_START = "<!-- brain-v42:start -->"
MARKER_END = "<!-- brain-v42:end -->"

_PATTERN = re.compile(
    rf"{re.escape(MARKER_START)}.*?{re.escape(MARKER_END)}",
    re.DOTALL,
)


def update_claude_md(path: str | Path, focus: str) -> bool:
    """Update the brain-v42 section in a CLAUDE.md file.

    Args:
        path: Path to CLAUDE.md (absolute, or with ~ for home).
        focus: New focus text to write.

    Returns:
        True if the file was updated, False if markers not found or file missing.
    """
    p = Path(path).expanduser()
    if not p.exists():
        return False

    content = p.read_text(encoding="utf-8")
    if MARKER_START not in content:
        return False

    new_block = (
        f"{MARKER_START}\n**Focus:** {focus}\n**Updated:** {date.today().isoformat()}\n{MARKER_END}"
    )
    updated = _PATTERN.sub(new_block, content)
    p.write_text(updated, encoding="utf-8")
    return True
