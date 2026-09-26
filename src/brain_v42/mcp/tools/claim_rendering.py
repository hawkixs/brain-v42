"""Compact claim suffixes for brain_get, brain_search and the session briefing.

Spec 2026-09-19, section 6.6: a suffix built from the three reads (history, latest
attempt, current validity) is appended to a knowledge entry, French because it is
rendered to the operator -- every OTHER string in this module (and the rest of the
codebase) stays English. No active claim renders nothing at all, so the pre-claims
output stays byte-identical; a lookup failure renders a visible marker instead of
silently looking identical to "no active claim".

This module does one batch fetch per caller-supplied entry set (no per-entity
lookup) and never mutates ranking, filtering or the underlying knowledge result --
it only turns already-evaluated ``ClaimState`` rows into text.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING
from uuid import UUID

from brain_v42.models.claim_read import ClaimState
from brain_v42.services.claim_read_service import ClaimReadError

if TYPE_CHECKING:
    from brain_v42.repositories.pg_claim_verdicts import VerdictRow
    from brain_v42.services.claim_read_service import ClaimReadService

#: Visible marker for a suffix lookup that failed -- never confused with silence,
#: which means "no active claim" instead (contract: "do not silently equate
#: unavailable with absent").
CLAIM_SUFFIX_UNAVAILABLE = "[claims : indisponible]"

_REASON_LABELS: dict[str, str] = {
    "path_absent": "chemin absent",
    "type_mismatch": "type incompatible",
    "target_mismatch": "cible inattendue",
    "definition_changed": "définition modifiée",
    "fact_mismatch": "fait inattendu",
}

_MISSING = object()


def _reason_label(reason: str | None) -> str:
    if reason is None:
        return "raison inconnue"
    if reason in _REASON_LABELS:
        return _REASON_LABELS[reason]
    if reason.startswith("probe:"):
        return f"sonde {reason.removeprefix('probe:')}"
    return reason


def _decode_segment(segment: str) -> str | None:
    """RFC 6901 unescape (``~1`` -> ``/``, ``~0`` -> ``~``); ``None`` when malformed."""
    decoded: list[str] = []
    index = 0
    while index < len(segment):
        character = segment[index]
        if character != "~":
            decoded.append(character)
            index += 1
            continue
        if index + 1 == len(segment) or segment[index + 1] not in {"0", "1"}:
            return None
        decoded.append("~" if segment[index + 1] == "0" else "/")
        index += 2
    return "".join(decoded)


def _pointer_value(value: object, path: object) -> object:
    """A bounded, read-only JSON-pointer walk; ``_MISSING`` for any dead end.

    Rendering-only duplicate of the same walk `facts.compare` performs at write
    time -- this module never imports `facts` (spec: only services/repositories/
    models are barred from it, but keeping this leaf self-contained avoids a
    dependency on another module's private helper for a few lines of logic).
    """
    if not isinstance(path, str) or not path.startswith("/"):
        return _MISSING
    current: object = value
    for encoded in path[1:].split("/"):
        segment = _decode_segment(encoded)
        if segment is None:
            return _MISSING
        if isinstance(current, Mapping):
            if segment not in current:
                return _MISSING
            current = current[segment]
        else:
            return _MISSING
    return current


def _falsified_detail(state: ClaimState) -> str | None:
    """Bounded fact/expected/observed detail; ``None`` when it cannot be read cleanly."""
    conclusive = state.conclusive
    if conclusive is None:
        return None
    expected = state.claim.expected
    path = expected.get("path") if isinstance(expected, Mapping) else None
    resolved = state.claim.expected_resolved
    attendu = resolved.get("value", _MISSING) if isinstance(resolved, Mapping) else _MISSING
    measurement = conclusive.measurement
    value_json = measurement.get("value_json") if isinstance(measurement, Mapping) else None
    observed = _pointer_value(value_json, path)
    if attendu is _MISSING or observed is _MISSING:
        return None
    return f"{state.claim.fact_name} mesuré {observed}, attendu {attendu}"


def _newer_unreadable_note(state: ClaimState) -> str | None:
    """A newer failed attempt after conclusive evidence -- kept visible, never dropped."""
    latest = state.latest
    conclusive = state.conclusive
    if latest is None or latest.verdict != "unreadable":
        return None
    if conclusive is not None and latest.seq == conclusive.seq:
        return None
    return f"dernier essai illisible ({_reason_label(latest.reason)})"


_BUCKET_ORDER = ("holds", "falsified", "stale", "unverified", "unreadable")


def _bucket_text(status: str, states: Sequence[ClaimState]) -> str:
    """One status bucket's text; a bounded detail is added only when it is singular."""
    count = len(states)
    if status == "holds":
        label = "tient" if count == 1 else "tiennent"
    elif status == "falsified":
        label = "FALSIFIÉ" if count == 1 else "FALSIFIÉS"
    elif status == "stale":
        label = "périmé" if count == 1 else "périmés"
    elif status == "unreadable":
        label = "illisible" if count == 1 else "illisibles"
    else:  # unverified
        label = "non vérifiée" if count == 1 else "non vérifiées"
    text = f"{count} {label}"
    if count != 1:
        return text

    state = states[0]
    if status == "falsified" and state.conclusive is not None:
        date = state.conclusive.emitted_at.date().isoformat()
        detail = _falsified_detail(state)
        text += f" le {date}" + (f" ({detail})" if detail else "")
    elif status == "stale" and state.previous_conclusive_kind is not None and state.conclusive:
        date = state.conclusive.emitted_at.date().isoformat()
        verb = "tenait" if state.previous_conclusive_kind == "holds" else "était FALSIFIÉ"
        text += f" ({verb} le {date})"
    elif status == "unreadable" and state.latest is not None:
        text += f" ({_reason_label(state.latest.reason)})"

    if status != "unreadable":
        note = _newer_unreadable_note(state)
        if note:
            text += f" ; {note}"
    return text


def render_claim_suffix(states: Sequence[ClaimState]) -> str | None:
    """Build the bracketed suffix for one entry's active claims, or ``None``.

    ``None`` means "nothing to show" (no active claim) -- the caller appends
    nothing, which is what keeps the pre-claims rendering byte-identical.
    """
    if not states:
        return None
    buckets: dict[str, list[ClaimState]] = {key: [] for key in _BUCKET_ORDER}
    for state in states:
        buckets[state.status].append(state)
    parts = [_bucket_text(status, items) for status, items in buckets.items() if items]
    return f"[claims : {' · '.join(parts)}]"


def _single_state_status_text(state: ClaimState) -> str:
    """One claim's own status line, reusing the suffix's singular bucket vocabulary."""
    return _bucket_text(state.status, [state])


def format_claim_list(items: Sequence[ClaimState], next_after_seq: int | None) -> str:
    """Render one page of scoped occurrences -- ``brain_claim_list``.

    Every filter and scope has already been applied by the caller; this only
    turns the page into text, one line per occurrence, retirement and paging
    named explicitly rather than left for the reader to infer.
    """
    n = len(items)
    header = f"## {n} claim{'s' if n != 1 else ''}"
    if not items:
        return header
    lines = [header]
    for state in items:
        claim = state.claim
        retired = " [retired]" if claim.retired_at is not None else ""
        lines.append(
            f"- {claim.id} ({claim.entity_type}/{claim.entry_id}) "
            f'"{claim.statement}"{retired} — {_single_state_status_text(state)}'
        )
    if next_after_seq is not None:
        lines.append(f"\n… (more results — after_seq={next_after_seq})")
    return "\n".join(lines)


def format_claim_history(
    state: ClaimState, verdicts: Sequence[VerdictRow], next_after_seq: int | None
) -> str:
    """Render one claim's current state and its complete verdict page in order.

    An empty history is a valid result for an existing claim (contract) and
    renders explicitly as such, never as an omission.
    """
    claim = state.claim
    retired = f" — retired {claim.retired_at.isoformat()}" if claim.retired_at else ""
    lines = [
        f"## Claim {claim.id}",
        f'"{claim.statement}"{retired}',
        f"State: {_single_state_status_text(state)}",
        f"### History ({len(verdicts)})",
    ]
    if not verdicts:
        lines.append("(empty)")
    for verdict in verdicts:
        detail = f" ({verdict.reason})" if verdict.reason else ""
        lines.append(
            f"- seq {verdict.seq}: {verdict.verdict}{detail} — {verdict.emitted_at.isoformat()}"
        )
    if next_after_seq is not None:
        lines.append(f"\n… (more results — after_seq={next_after_seq})")
    return "\n".join(lines)


async def claim_suffix_map(
    service: ClaimReadService,
    entries: Sequence[tuple[str, UUID]],
    *,
    trusted_project_key: str | None = None,
) -> dict[tuple[str, UUID], str]:
    """One batch fetch per result set; a failure marks every entry, never silently.

    Returns a mapping that carries a rendered value only for an entry that has
    something to show: a real suffix, or ``CLAIM_SUFFIX_UNAVAILABLE`` for every
    requested entry when the single batch fetch itself failed. A key absent
    from the result means "no active claim" -- render nothing for it.
    """
    if not entries:
        return {}
    try:
        summaries = await service.batch_summaries(entries, trusted_project_key=trusted_project_key)
    except ClaimReadError:
        return dict.fromkeys(entries, CLAIM_SUFFIX_UNAVAILABLE)
    result: dict[tuple[str, UUID], str] = {}
    for key, states in summaries.items():
        suffix = render_claim_suffix(states)
        if suffix:
            result[key] = suffix
    return result
