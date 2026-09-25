"""A rail's event log, read for a measurement (spec 0.5.0 §3.11).

The rails' own readers skip a line they cannot parse: right for a guard that
must keep going. A count cannot do that. A log with a line it cannot read is a
count it cannot vouch for, so the count is "not measured" -- ``None`` -- never a
partial number passed off as complete. A provider killed mid-write can leave a
half-written last line: that run's tools then read as not measured.
"""

from __future__ import annotations

import json
from pathlib import Path


def read_events(path: Path | None) -> list[dict[str, object]] | None:
    """Every event of the JSON-lines log at ``path``, or ``None`` when it cannot be read whole.

    Blank lines are skipped. ``None`` when ``path`` is ``None``, missing,
    unreadable or not UTF-8, or when any other line is not a JSON object.
    """
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    events: list[dict[str, object]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            return None
        if not isinstance(event, dict):
            return None
        events.append(event)
    return events


__all__ = ["read_events"]
