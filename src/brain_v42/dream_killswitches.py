"""Shared parsing and configuration for the local Dream killswitch drop-in."""

from __future__ import annotations

from pathlib import Path

KILLSWITCHES_PATH = (
    Path.home() / ".config" / "systemd" / "user" / "brain-v42-dream.service.d" / "killswitches.conf"
)

# Env systemd → clé courte du payload.
_KS_KEYS = {
    "BRAIN_DREAM_PROMOTE_ENABLED": "promote",
    "BRAIN_DREAM_REORG_ENABLED": "reorg",
    "BRAIN_DREAM_REORG_DRY_RUN": "reorg_dry",
    "BRAIN_DREAM_EXTRACT_ENABLED": "extract",
    "BRAIN_DREAM_EXTRACT_DRY_RUN": "extract_dry",
    "BRAIN_DREAM_ROADMAP_ENABLED": "roadmap",
    "BRAIN_DREAM_ROADMAP_DRY_RUN": "roadmap_dry",
    "BRAIN_DREAM_SWEEP_ENABLED": "sweep",
    "BRAIN_DREAM_SWEEP_DRY_RUN": "sweep_dry",
}


# A LIST-VALUED key, deliberately outside `_KS_KEYS`. That dictionary returns a
# `dict[str, bool]` and coerces through `value.lower() == "true"`: a project list
# would enter it as `False` and switch a phase off in the session briefing and in
# `/metrics` without touching the night. A second function, not one more key.
PROJECT_POOL_KEY = "BRAIN_DREAM_PROJECT_POOL"


def parse_killswitches(content: str) -> dict[str, bool]:
    """Parse a systemd drop-in (``Environment=KEY=value``) into flags."""
    flags: dict[str, bool] = {}
    for raw in content.splitlines():
        line = raw.strip()
        if not line.startswith("Environment="):
            continue
        for token in line.removeprefix("Environment=").split():
            key, sep, value = token.partition("=")
            if sep and key in _KS_KEYS:
                flags[_KS_KEYS[key]] = value.strip('"').lower() == "true"
    return flags


def parse_project_pool(content: str) -> list[str]:
    """Return the project pool the drop-in declares, in order, without duplicates.

    A missing key returns an EMPTY list, never a guessed default. This parser
    does not see ``ExecStart=``'s positional argument; inventing ``brain-v42``
    here would manufacture expectations for a project the night may not have
    served — that is, an alarm out of nowhere.

    It accepts BOTH transports, and that is not indulgence:

    - ``Environment=KEY=a,b,c`` — the chosen form, with no whitespace to quote;
    - ``Environment="KEY=a b"`` — the quoted form, which arrives whole.

    And it returns several keys for an UNQUOTED ``Environment=KEY=a b``, where
    systemd would set the variable to ``a`` and throw ``b`` away. The divergence
    is deliberate. ``dream.sh`` cannot see that trap: by the time it starts, ``b``
    has already disappeared from its environment. This file is the only place
    that reads the original text, hence the only one that can make something
    ring — a loud alarm about a missing ``b`` is better than silent agreement
    with a broken configuration.
    """
    pool: list[str] = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line.startswith("Environment="):
            continue
        assignment = line.removeprefix("Environment=").strip()
        if len(assignment) >= 2 and assignment[0] == assignment[-1] and assignment[0] in "\"'":
            assignment = assignment[1:-1]
        key, sep, value = assignment.partition("=")
        if not sep or key != PROJECT_POOL_KEY:
            continue
        for chunk in value.replace(",", " ").split():
            entry = chunk.strip().strip("\"'")
            if entry and entry not in pool:
                pool.append(entry)
    return pool
