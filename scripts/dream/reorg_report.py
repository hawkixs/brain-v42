"""The one reader of the REORG machine-readable trailer.

    === REORG REPORT ===
    {"dry_run": <bool>,
     "updated":  [full-UUIDs],          <- checkable against PostgreSQL
     "archived": [full-UUIDs],          <- checkable against PostgreSQL
     "declared": {                      <- the model talking about itself
       "candidates_examined": <int>,
       "archived": <int>,
       "refused": {"<reason>": <int>, ...},
       "deferred": <int>}}
    === END ===

TWO KINDS OF NUMBER, AND THEY MUST NOT MIX (learning c34fb865). The two id
lists name entities; a server can be asked whether those entities really
changed, and `reorg_validate` asks. Everything under `declared` is the phase's
own account of what it LOOKED at — candidates examined, refusals, deferrals —
and no call exists that could confirm it. A guard that summed the two would be
reading a signal produced by the process it guards. So the declared tally is
carried under a name that says whose word it is, cross-checked only against
itself, and never folded into a derived counter.

WHY THIS MODULE EXISTS AT ALL. The trailer had two readers —
`reorg_validate.parse_report` and a private regex inside `post_run_alert` — that
had to agree with nothing enforcing it. Measured on 2026-09-03: the alert's
pattern `\\{"dry_run":\\s*(?:true|false).*?\\}` is non-greedy, so a trailer
carrying any nested object truncates at the first inner brace and `json.loads`
raises inside an `except ValueError: continue` written so the morning report can
never fail. Adding `declared` without touching that regex would have zeroed the
morning line in silence: a phase that worked reading as a phase that did
nothing. The cost of unifying is real and named in the tests — accidental
redundancy is gone. The redundancy that matters is not here: it is the
validator's confrontation of this DECLARATION with the event stream's
OBSERVATION, and those two sources remain independent.

ABSENT IS NOT ZERO (learning 083d74e5). Three distinct states, all preserved:
no trailer at all (`found_marker=False`), a trailer from a prompt that does not
yet emit the tally (`declared=None`), and a tally that genuinely counted zero
(`declared=DeclaredTally(...)` with zeros). Collapsing any pair of them would
let a rail that ran without producing read as a quiet night.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

#: Both markers, tolerant of spacing. The payload is lazy but ANCHORED on the
#: closing marker, which is what makes it right twice over: it extends past the
#: inner braces of a nested object (a plain lazy `\{.*?\}` would stop at the
#: first one), and it stops at the END marker rather than swallowing a second
#: trailer (a greedy `\{.*\}` spans both and yields one unparseable blob).
#: A retried phase appending a second run to the same log is the case that makes
#: the difference; no log in this repository carries two trailers today, which
#: is exactly why the failure would have arrived unannounced.
_TRAILER_RE = re.compile(
    r"===\s*REORG\s+REPORT\s*===\s*(\{.*?\})\s*===\s*END\s*===",
    re.DOTALL,
)

#: The opening marker alone. A report killed mid-write, or one whose payload
#: lost a brace, carries this and no parseable trailer — and the difference
#: matters: "the phase never printed a block" and "the block is damaged" are
#: different failures, and only one of them is the phase's fault.
_MARKER_RE = re.compile(r"===\s*REORG\s+REPORT\s*===")


def marker_count(raw: str) -> int:
    """How many times the phase announced a trailer, parseable or not."""
    return len(_MARKER_RE.findall(raw))


#: The closed vocabulary of refusal reasons, one per rejection point written in
#: `phase_reorg.md` §Part 2. A reason outside this set is kept and reported as
#: drift rather than dropped: silently discarding it would hide the very
#: divergence between prompt and reader that this vocabulary exists to expose.
REFUSAL_REASONS: tuple[str, ...] = (
    "already_archived",
    "dream_managed",
    "access_above_threshold",
    "content_not_trivial",
)


@dataclass(frozen=True)
class DeclaredTally:
    """What the phase says it looked at. Hearsay, by construction."""

    candidates_examined: int = 0
    archived: int = 0
    refused: dict[str, int] = field(default_factory=dict)
    deferred: int = 0

    @property
    def refused_total(self) -> int:
        return sum(self.refused.values())

    def unknown_reasons(self) -> tuple[str, ...]:
        """Refusal keys the prompt does not define — prompt/reader drift."""
        return tuple(sorted(set(self.refused) - set(REFUSAL_REASONS)))

    def arithmetic_complaint(self) -> str | None:
        """Whether the tally adds up against itself.

        This is the ONLY check available on numbers no call can confirm, and it
        is weak on purpose: it catches a careless count, never a consistent
        fiction. Stating that weakness is the point — a reader who mistakes this
        for verification would trust the tally more than the evidence behind it,
        which is none.
        """
        accounted = self.archived + self.refused_total + self.deferred
        if accounted == self.candidates_examined:
            return None
        gap = self.candidates_examined - accounted
        return (
            f"declared tally does not add up: {self.candidates_examined} candidate(s) "
            f"examined, {self.archived} archived + {self.refused_total} refused + "
            f"{self.deferred} deferred = {accounted} accounted for, {gap} unaccounted"
        )


@dataclass(frozen=True)
class ReorgReport:
    """One project's trailer, with evidence and hearsay kept apart."""

    updated_ids: list[str] = field(default_factory=list)
    archived_ids: list[str] = field(default_factory=list)
    dry_run: bool = False
    found_marker: bool = False
    declared: DeclaredTally | None = None
    #: True when a `declared` key was present but unusable. Distinct from a
    #: missing key: one is an old prompt, the other is a broken new one.
    declared_malformed: bool = False

    def declared_list_mismatch(self) -> str | None:
        """The model's archive COUNT against the model's archive LIST.

        Two statements about one fact, and only one of them can be taken to
        PostgreSQL. When they disagree, the list is the one with evidence
        behind it.

        NOT ON A DRY RUN. `phase_reorg.md` forbids the `brain_update` call in
        DRY_RUN, so a candidate that cleared every guardrail yields no UUID —
        there was no call to return one — while the declared count must still
        record it, or the arithmetic gap would equal the number of would-be
        archives and fire a complaint on every nominal dry night. A guard that
        cries on the happy path is a guard that gets muted.

        The confrontation that matters is untouched: `symmetry_warnings` still
        compares DECLARED ids to OBSERVED ids, and a dry run declares none.
        """
        if self.declared is None or self.dry_run:
            return None
        if self.declared.archived == len(self.archived_ids):
            return None
        return (
            f"declared archived count is {self.declared.archived} but the report lists "
            f"{len(self.archived_ids)} archived id(s); only the list is checkable"
        )


def _dedup(values: object) -> list[str]:
    """Preserve order, drop repeats, tolerate a non-list."""
    if not isinstance(values, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value)
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _coerce_count(value: object) -> int | None:
    """An int, or None. A bool is not a count, and a float is not either."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _parse_declared(payload: dict[str, Any]) -> tuple[DeclaredTally | None, bool]:
    """Return (tally, malformed). A missing key is neither a tally nor an error."""
    if "declared" not in payload:
        return None, False
    raw = payload["declared"]
    if not isinstance(raw, dict):
        return None, True

    examined = _coerce_count(raw.get("candidates_examined", 0))
    archived = _coerce_count(raw.get("archived", 0))
    deferred = _coerce_count(raw.get("deferred", 0))
    if examined is None or archived is None or deferred is None:
        return None, True

    refused_raw = raw.get("refused", {})
    if not isinstance(refused_raw, dict):
        return None, True
    refused: dict[str, int] = {}
    for reason, count in refused_raw.items():
        parsed = _coerce_count(count)
        if parsed is None:
            return None, True
        refused[str(reason)] = parsed

    return (
        DeclaredTally(
            candidates_examined=examined,
            archived=archived,
            refused=refused,
            deferred=deferred,
        ),
        False,
    )


def iter_trailers(raw: str) -> list[ReorgReport]:
    """Every trailer in one report text, in order.

    A phase retried inside the night appends a second run to the same log. The
    morning line counts projects by counting trailers, so silently keeping only
    the first would undercount a night that needed a retry — and undercounting
    is the shape this whole ticket exists to remove.
    """
    reports: list[ReorgReport] = []
    for match in _TRAILER_RE.finditer(raw):
        reports.append(_from_payload_text(match.group(1)))
    return reports


def _from_payload_text(text: str) -> ReorgReport:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed REORG REPORT JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("REORG REPORT payload is not an object")

    declared, malformed = _parse_declared(payload)
    return ReorgReport(
        updated_ids=_dedup(payload.get("updated", [])),
        archived_ids=_dedup(payload.get("archived", [])),
        dry_run=bool(payload.get("dry_run", False)),
        found_marker=True,
        declared=declared,
        declared_malformed=malformed,
    )


def parse_trailer(raw: str) -> ReorgReport:
    """Read one project's REORG trailer out of its report text.

    Raises `ValueError` when the markers ARE present and the JSON between them
    is malformed — a broken trailer from a phase that claimed to write one is a
    fact worth failing on. An ABSENT trailer is not an error here: the caller
    decides, because the validator fails closed on a wet run while the morning
    report must never fail at all.
    """
    match = _TRAILER_RE.search(raw)
    if match is None:
        return ReorgReport()
    return _from_payload_text(match.group(1))
