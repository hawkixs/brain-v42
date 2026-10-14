"""Resolve one evaluation without consulting historical evidence or sources."""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Literal

from brain_v42.knowledge.ranks import Centrality, Polarity, State, Temporal, ValidFromPrecision

RULES_VERSION = "v1"

Layer = Literal["structural", "check", "propagation"]
StructuralRule = Literal[
    "merged", "superseded_decision", "superseded_adr", "deprecated_adr", "decay_archived"
]


@dataclass(frozen=True)
class Observation:
    id: str
    assertion_key: str | None
    polarity: Polarity
    source_kind: str
    source_rank: int
    source_ref: str
    centrality: Centrality | None = None
    temporal: Temporal | None = None
    valid_from: date | None = None
    valid_from_precision: ValidFromPrecision = ValidFromPrecision.UNKNOWN
    rule: StructuralRule | None = None
    propagated_from: str | None = None


@dataclass(frozen=True)
class Decision:
    state: State
    reason: str
    observation_ids: tuple[str, ...]
    end_date: date | None = None
    end_date_precision: ValidFromPrecision | None = None
    layer: Layer = "check"


@dataclass(frozen=True)
class _EffectiveAssertion:
    key: str
    polarity: str
    observations: tuple[Observation, ...]

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(observation.id for observation in self.observations))


def _resolve_assertions(
    observations: Iterable[Observation], *, include_propagation: bool = False
) -> list[_EffectiveAssertion]:
    """Discard exempt evidence before ranking so it cannot introduce a tie."""
    grouped: dict[str, list[Observation]] = {}
    for observation in observations:
        if observation.assertion_key is None or observation.source_kind == "decay":
            continue
        if observation.source_kind == "propagation" and not include_propagation:
            continue
        group = grouped.setdefault(observation.assertion_key, [])
        if observation.polarity not in {Polarity.SUPPORTS, Polarity.CONTRADICTS}:
            continue
        if (
            observation.source_kind == "decision"
            and observation.temporal == Temporal.DURABLE
            and observation.polarity == Polarity.CONTRADICTS
        ):
            continue
        group.append(observation)

    resolved: list[_EffectiveAssertion] = []
    for key, group in sorted(grouped.items()):
        if not group:
            resolved.append(_EffectiveAssertion(key, Polarity.NONE.value, ()))
            continue
        rank = max(observation.source_rank for observation in group)
        top = tuple(
            sorted(
                (observation for observation in group if observation.source_rank == rank),
                key=lambda observation: (
                    observation.source_kind,
                    observation.source_ref,
                    observation.id,
                ),
            )
        )
        polarities = {observation.polarity for observation in top}
        polarity = "tied" if len(polarities) > 1 else top[0].polarity.value
        resolved.append(_EffectiveAssertion(key, polarity, top))
    return resolved


def decide(observations: Iterable[Observation], observed_day: date) -> Decision:
    """Use only this evaluation; the caller owns revision and completeness guards.

    A propagation evaluation contains only its own layer's observations. Its valid
    result lifts the marker; the caller restores the last applied non-propagation
    state, without re-deciding historical checks or starting another sweep.
    """
    evidence = sorted(
        observations,
        key=lambda observation: (
            -observation.source_rank,
            observation.source_kind,
            observation.source_ref,
            observation.rule or "",
            observation.propagated_from or "",
            observation.id,
        ),
    )
    propagation = [
        observation for observation in evidence if observation.source_kind == "propagation"
    ]
    if propagation and len(propagation) != len(evidence):
        raise ValueError("Propagation observations cannot be mixed with other source kinds")
    for observation in propagation:
        if observation.propagated_from is None:
            raise ValueError("A propagation observation must name its propagated_from parent")
    endings = sorted(
        (
            observation
            for observation in evidence
            if observation.polarity == Polarity.ENDS and observation.source_kind != "propagation"
        ),
        key=lambda observation: (-observation.source_rank, observation.id),
    )
    for observation in endings:
        if observation.rule is None:
            raise ValueError("An ending observation must name its structural rule")
        dated = observation.valid_from is not None
        return Decision(
            State.OBSOLETE,
            observation.rule,
            (observation.id,),
            observation.valid_from if dated else observed_day,
            observation.valid_from_precision if dated else ValidFromPrecision.FIRST_OBSERVED,
            "structural",
        )
    for observation in evidence:
        if observation.rule == "decay_archived":
            return Decision(
                State.UNVERIFIABLE, "decay_archived", (observation.id,), layer="structural"
            )
    resolved = _resolve_assertions(evidence)
    contradictions = [item for item in resolved if item.polarity == Polarity.CONTRADICTS]
    for item in contradictions:
        observation = item.observations[0]
        if (
            observation.centrality == Centrality.CORE
            and observation.temporal == Temporal.DURABLE
            and observation.source_rank >= 5
        ):
            return Decision(State.REFUTED, f"contradicted_by_{observation.source_ref}", item.ids)
    for item in contradictions:
        observation = item.observations[0]
        if (
            observation.centrality == Centrality.CORE
            and observation.temporal == Temporal.TRANSIENT
            and observation.source_rank >= 3
        ):
            exact = (
                observation.valid_from_precision == ValidFromPrecision.EXACT
                and observation.valid_from is not None
            )
            return Decision(
                State.OBSOLETE,
                f"pending_state_ended_{observation.source_kind}",
                item.ids,
                observation.valid_from if exact else observed_day,
                ValidFromPrecision.EXACT if exact else ValidFromPrecision.FIRST_OBSERVED,
            )
    for item in resolved:
        if item.polarity == Polarity.NONE:
            continue
        observation = item.observations[0]
        if item.polarity == "tied":
            return Decision(State.CONTESTED, "tied", item.ids)
        if item.polarity == Polarity.CONTRADICTS and not (
            observation.centrality == Centrality.INCIDENTAL
            and observation.temporal == Temporal.TRANSIENT
        ):
            return Decision(State.CONTESTED, f"contested_by_{observation.source_kind}", item.ids)
    for observation in propagation:
        if observation.polarity == Polarity.CONTRADICTS:
            return Decision(
                State.CONTESTED,
                f"propagated_from_{observation.propagated_from}",
                (observation.id,),
                layer="propagation",
            )
    checked = [
        observation
        for item in resolved
        if item.polarity == Polarity.SUPPORTS
        for observation in item.observations
        if observation.centrality == Centrality.CORE and observation.source_rank >= 4
    ]
    if checked:
        return Decision(State.VALID, "core_checked", tuple(sorted(item.id for item in checked)))
    layer: Layer = "propagation" if propagation else "check"
    return Decision(State.VALID, "declaration_only", (), layer=layer)


def fingerprint(decision: Decision, observations: Iterable[Observation]) -> str:
    """Ignore row ids and losing evidence so later runs can confirm the same claim.

    The v1 encoding is compact UTF-8 JSON: [state, reason, [[key, polarity], ...]].
    Assertion pairs are sorted by key, and a rank tie has polarity ``tied``.
    """
    pairs = [
        (item.key, item.polarity)
        for item in _resolve_assertions(observations, include_propagation=True)
    ]
    payload = json.dumps([decision.state.value, decision.reason, pairs], separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
