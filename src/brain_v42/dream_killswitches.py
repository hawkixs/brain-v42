"""Shared parsing and configuration for the local Dream killswitch drop-in."""

from __future__ import annotations

from collections.abc import Iterator
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
# `dict[str, bool]` and coerces through `value == "true"`: a project list
# would enter it as `False` and switch a phase off in the session briefing and in
# `/metrics` without touching the night. A second function, not one more key.
PROJECT_POOL_KEY = "BRAIN_DREAM_PROJECT_POOL"


#: The `*_DRY_RUN` keys, whose polarity is the OPPOSITE of the `*_ENABLED` ones
#: and whose coercion therefore cannot be shared with them.
_DRY_RUN_KEYS = frozenset(key for key in _KS_KEYS if key.endswith("_DRY_RUN"))


def _iter_settings(content: str) -> Iterator[tuple[str, str]]:
    """Yield ``(key, value)`` for every ``Environment=`` assignment this file owns."""
    for raw in content.splitlines():
        line = raw.strip()
        if not line.startswith("Environment="):
            continue
        for token in line.removeprefix("Environment=").split():
            key, sep, value = token.partition("=")
            if sep and key in _KS_KEYS:
                yield key, value.strip('"')


def parse_killswitches(content: str) -> dict[str, bool]:
    """Parse a systemd drop-in (``Environment=KEY=value``) into flags.

    THE TWO FAMILIES HAVE OPPOSITE POLARITY, and reading them with one rule is
    what made this display lie.

    ``*_ENABLED``: ``dream.sh`` tests ``!= "true"`` and SKIPS the phase, so any
    value that is not exactly ``true`` disables it. ``== "true"`` here says the
    same thing. These two sides have always agreed and still do.

    ``*_DRY_RUN``: since 2026-09-04 ``dream.sh`` arms ``--wet`` only on the
    literal ``false`` (`dream_wants_wet`), so anything else runs DRY. Reading
    them with ``== "true"`` reported ``dry=False`` on a typo — a WET phase in
    the briefing and in ``/metrics`` while the night ran it DRY. The rule is now
    ``!= "false"``, which is the same sentence the shell speaks.

    A value that is neither ``true`` nor ``false`` produces the SAFE boolean
    here and is reported separately by `non_canonical_killswitches`: a boolean
    cannot distinguish "dry because the operator said so" from "dry because
    nobody could read what the operator wrote", and only the second deserves a
    human's attention.

    THE COMPARISON IS CASE-SENSITIVE, and that is a correction rather than an
    oversight. It used to lower-case, so `True` read as enabled and `False` as
    wet — while `dream.sh` compares the raw string in both families and treats
    either as "not the canonical word". Being lenient here recreated the same
    divergence one line further along. Measured on the live drop-in of
    2026-09-04: every value is already lower-case, so this changes nothing that
    runs tonight.
    """
    flags: dict[str, bool] = {}
    for key, value in _iter_settings(content):
        if key in _DRY_RUN_KEYS:
            flags[_KS_KEYS[key]] = value != "false"
        else:
            flags[_KS_KEYS[key]] = value == "true"
    return flags


def non_canonical_killswitches(content: str) -> dict[str, str]:
    """Killswitch keys whose value is neither ``true`` nor ``false``, and what they hold.

    The third state, which no boolean can carry. `parse_killswitches` always
    answers on the safe side, so the night is never in danger — but "the phase
    is dry" and "the phase is dry because its switch is unreadable" are
    different facts, and a briefing that shows only the first hides a drop-in
    somebody has to fix (learning 083d74e5).

    The VALUE travels with the key because it is what distinguishes a typo from
    a decision (learning e2e5a550). A missing key is not reported: absent means
    "use the default", which is a choice rather than a mistake.

    A second function rather than one more key, for the same reason
    `parse_project_pool` is one: this mapping's values are booleans, and a
    string forced through it would become `False` and switch a phase off in the
    briefing without touching the night.
    """
    return {key: value for key, value in _iter_settings(content) if value not in {"true", "false"}}


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
