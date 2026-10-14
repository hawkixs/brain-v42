"""Compare assertions against supplied snapshots, never a model verdict."""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import PurePosixPath

from brain_v42.knowledge.decide import Observation
from brain_v42.knowledge.facts_registry import FactDefinition
from brain_v42.knowledge.ranks import RANKS, Centrality, Polarity, Temporal, ValidFromPrecision


@dataclass(frozen=True)
class Sources:
    tree: frozenset[str]
    identifiers: frozenset[str]
    prs: Mapping[int, tuple[str, date | datetime | None]]
    tickets: Mapping[str, tuple[str, date | datetime | None]]
    facts: Mapping[str, str | None]
    registry: Mapping[str, FactDefinition]
    origin_main_sha: str


def _normalized(text: str) -> str:
    return " ".join(text.casefold().split())


def _digest(parts: list[object]) -> str:
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


def _path_present(ref: str, tree: frozenset[str]) -> bool:
    """Match whole suffix components, including directories implied by files."""
    suffix = str(PurePosixPath(ref)).removeprefix("./").strip("/")
    for path in tree:
        candidates = (PurePosixPath(path), *PurePosixPath(path).parents)
        if any(
            str(candidate) == suffix or str(candidate).endswith("/" + suffix)
            for candidate in candidates
        ):
            return True
    return False


def _path_like(ref: str) -> bool:
    return "/" in ref or PurePosixPath(ref).suffix in {
        ".py",
        ".pyi",
        ".yml",
        ".yaml",
        ".json",
        ".toml",
        ".md",
        ".sh",
        ".js",
        ".ts",
        ".tsx",
        ".txt",
        ".cfg",
        ".ini",
    }


def check(
    assertion: Mapping[str, object], sources: Sources, entry_text: str, *, entry_id: str
) -> Observation | None:
    """Drop unmappable claims rather than turning missing evidence into absence.

    The assertion key hashes compact UTF-8 JSON [entry id, check, normalized ref,
    normalized quote]. Observation ids additionally identify the source/value;
    the future evaluation writer owns evaluation-scoped database row identities.
    """
    quote = assertion.get("quote")
    if (
        not isinstance(quote, str)
        or not _normalized(quote)
        or _normalized(quote) not in _normalized(entry_text)
    ):
        return None
    kind = assertion.get("check")
    if kind in {"code_path", "code_symbol", "pr"} and assertion.get("repo") != "brain-v42":
        return None
    try:
        centrality = (
            Centrality(str(assertion["centrality"]))
            if assertion.get("centrality") is not None
            else None
        )
        temporal = (
            Temporal(str(assertion["temporal"])) if assertion.get("temporal") is not None else None
        )
    except ValueError:
        return None
    ref = str(assertion.get("ref", "")).strip()
    at: date | datetime | None = None
    current: str
    source_kind: str
    source_ref: str
    equal: bool

    if kind == "fact":
        key = assertion.get("fact_key")
        if not isinstance(key, str) or key not in sources.registry:
            return None
        definition = sources.registry[key]
        claimed = assertion.get("claimed_value")
        measured = sources.facts.get(key)
        if not isinstance(claimed, str) or measured is None:
            return None
        if definition.domain is not None and (
            claimed not in definition.domain or measured not in definition.domain
        ):
            return None
        ref = key
        current, equal = measured, claimed == measured
        source_kind = definition.source_kind
        source_ref = f"{key}:{definition.source}"
        if source_kind == "config":
            source_ref = f"origin/main@{sources.origin_main_sha}:{source_ref}"
    elif kind in {"code_path", "code_symbol"}:
        claimed = assertion.get("asserts")
        if not ref or ref in {".", "/"} or claimed not in {"exists", "absent"}:
            return None
        path = kind == "code_path" or _path_like(ref)
        symbol = ref.rsplit(".", 1)[-1]
        if not path and not symbol:
            return None  # "Server." names no identifier: unchecked, never an absence
        present = _path_present(ref, sources.tree) if path else symbol in sources.identifiers
        current = "exists" if present else "absent"
        equal = claimed == current
        source_kind = "code"
        source_ref = (
            f"origin/main@{sources.origin_main_sha}:{'path' if path else 'identifier'}:{ref}"
        )
    elif kind == "pr":
        if not re.fullmatch(r"#?[1-9]\d*", ref):
            return None
        number = int(ref.lstrip("#"))
        record = sources.prs.get(number)
        claimed = assertion.get("claimed_status")
        if record is None or claimed not in {"open", "merged", "closed"}:
            return None
        current, ended_at = record
        if current not in {"open", "merged", "closed"}:
            return None
        at = ended_at if current != "open" else None
        equal = claimed == current
        ref = str(number)
        source_kind, source_ref = "pr", f"brain-v42:pr:{number}"
    elif kind == "ticket":
        ref = ref.lower()
        if not re.fullmatch(r"[0-9a-f]{8}", ref):
            return None
        record = sources.tickets.get(ref)
        claimed = assertion.get("claimed_status")
        states = {"open", "resolved", "closed", "wontfix"}
        if record is None or claimed not in states:
            return None
        current, at = record
        if current not in states:
            return None
        equal = (claimed == "open") == (current == "open")
        source_kind, source_ref = "ticket", f"brain-v42:ticket:{ref}"
    else:
        return None

    day = at.date() if isinstance(at, datetime) else at
    polarity = Polarity.SUPPORTS if equal else Polarity.CONTRADICTS
    key = _digest([str(entry_id), kind, _normalized(ref), _normalized(quote)])
    return Observation(
        id=_digest(
            [key, source_kind, source_ref, current, polarity, day.isoformat() if day else None]
        ),
        assertion_key=key,
        polarity=polarity,
        source_kind=source_kind,
        source_rank=RANKS[source_kind],
        source_ref=source_ref,
        centrality=centrality,
        temporal=temporal,
        observed_value=current,
        valid_from=day,
        valid_from_precision=ValidFromPrecision.EXACT if day else ValidFromPrecision.UNKNOWN,
    )
