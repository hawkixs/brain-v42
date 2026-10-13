"""Programme progress, write side: the limits of contract programmes/v1, checked at write.

Contract `docs/contracts/programmes_v1.md` (tickets 8a063256, 24c1f172). red-cockpit
renders what brain publishes and red-monitor relays it verbatim, so a text that
breaks the one-line rule or a key outside its grammar must never be STORED: these
helpers refuse, with a stable code, and never cut or strip. The database repeats
the shape rules as CHECKs (migration 065); the character rules live here only.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Final, Literal

GateState = Literal["todo", "in_progress", "blocked", "done"]
ProgrammeState = Literal["active", "paused", "done"]
GATE_STATES: Final = ("todo", "in_progress", "blocked", "done")
PROGRAMME_STATES: Final = ("active", "paused", "done")

PROGRAMME_KEY_MAX: Final = 64
GATE_KEY_MAX: Final = 32
TITLE_MAX: Final = 120
LINE_MAX: Final = 200
UNIT_MAX: Final = 24
DEPENDS_ON_MAX: Final = 12
GATES_MAX: Final = 32
LIVE_PROGRAMMES_MAX: Final = 8
#: One coordinating project may hold at most half the live slots, and a bounded
#: number of programmes in any state: no project can crowd the others out or grow
#: the table without end (security pass 2026-10-13).
LIVE_PER_COORDINATOR_MAX: Final = 4
PROGRAMMES_PER_COORDINATOR_MAX: Final = 40
#: Order positions stop here, far below INTEGER: max(position) + 1 cannot overflow.
POSITION_MAX: Final = 9999
DONE_PROGRAMMES_PUBLISHED: Final = 4
DONE_PUBLISHED_DAYS: Final = 7
PROGRAMME_BYTES_MAX: Final = 16 * 1024
PROGRESS_TOTAL_MAX: Final = 100_000

_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
#: Contract §2, made general by a second security pass (2026-10-13). A declared text
#: is refused when it carries a character of these Unicode categories: controls (Cc,
#: line breaks included), format characters (Cf: zero-width, bidi marks and overrides,
#: soft hyphen, tag characters), surrogates (Cs), private use (Co), unassigned (Cn)
#: and the line and paragraph separators (Zl, Zp). Fillers that render blank but are
#: letters or marks are named one by one. The cockpit strips a subset of these; brain
#: refuses them all at write, so no invisible payload reaches a reader.
_FORBIDDEN_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
_BLANK_FILLERS: Final = frozenset({0x034F, 0x115F, 0x1160, 0x2800, 0x3164, 0xFFA0})


def _forbidden(character: str) -> bool:
    return (
        unicodedata.category(character) in _FORBIDDEN_CATEGORIES or ord(character) in _BLANK_FILLERS
    )


class ProgrammeError(Exception):
    """A programme refusal with a stable code and a caller-safe message.

    The message names the field and the rule, never a stored value of another
    project nor a database detail: it is surfaced to the caller as is.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _key(value: object, *, limit: int, field: str) -> str:
    if not isinstance(value, str) or len(value) > limit or not _KEY.fullmatch(value):
        raise ProgrammeError(
            "invalid_key",
            f"{field} must match [A-Za-z0-9][A-Za-z0-9._-]* and hold at most {limit} characters",
        )
    return value


def programme_key(value: object) -> str:
    return _key(value, limit=PROGRAMME_KEY_MAX, field="programme key")


def gate_key(value: object) -> str:
    return _key(value, limit=GATE_KEY_MAX, field="gate key")


def line(value: object, *, field: str, limit: int) -> str:
    """One declared line: 1 to `limit` characters, no outer blank, no forbidden character."""
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ProgrammeError("invalid_text", f"{field} must hold 1 to {limit} characters")
    if value.strip() != value:
        raise ProgrammeError("invalid_text", f"{field} must not start or end with a blank")
    if any(_forbidden(character) for character in value):
        raise ProgrammeError(
            "invalid_text",
            f"{field} must be one line of visible text: no control, format, separator, "
            "private-use or blank-filler character",
        )
    return value


def weight(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise ProgrammeError("invalid_weight", "weight must be an integer from 1 to 100")
    return value


def progress(done: object, total: object, unit: object) -> dict[str, int | str]:
    if (
        isinstance(done, bool)
        or isinstance(total, bool)
        or not isinstance(done, int)
        or not isinstance(total, int)
    ):
        raise ProgrammeError("invalid_progress", "progress done and total must be integers")
    if not 1 <= total <= PROGRESS_TOTAL_MAX or not 0 <= done <= total:
        raise ProgrammeError(
            "invalid_progress",
            f"progress needs 1 <= total <= {PROGRESS_TOTAL_MAX} and 0 <= done <= total",
        )
    return {"done": done, "total": total, "unit": line(unit, field="progress unit", limit=UNIT_MAX)}


def acyclic_or_refuse(graph: Mapping[str, Sequence[str]]) -> None:
    """Refuse a dependency cycle, naming one gate on it (contract §2)."""
    state: dict[str, int] = {}

    def visit(key: str) -> None:
        if state.get(key) == 1:
            raise ProgrammeError(
                "dependency_cycle", f"depends_on would close a cycle through {key}"
            )
        if state.get(key) == 2:
            return
        state[key] = 1
        for dependency in graph.get(key, ()):
            visit(dependency)
        state[key] = 2

    for key in graph:
        visit(key)
