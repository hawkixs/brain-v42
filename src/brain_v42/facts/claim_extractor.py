"""Deterministic, server-owned extraction of claims from knowledge prose.

The registry catalogue is closed and measured values are never read here: an
extracted claim records the value the prose *asserted*, so the nightly verifier
keeps the only measured verdict. Only a narrow, explicitly allowlisted set of
facts has a safe text grammar (a three-digit Alembic revision, or a full
40-character lowercase release SHA). The extractor stays pure — no database, no
registry, no write — so its behaviour is a pure function of the selected prose.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

EntityKind = Literal["learning", "decision", "snippet", "runbook", "adr"]

ExtractionReason = Literal[
    "no_project_key",
    "prose_overflow",
    "sentence_overflow",
    "ambiguous",
]

_PROSE_LIMIT_BYTES = 64 * 1024
_SENTENCE_LIMIT_CHARS = 500

_SELECTED_FIELDS: Mapping[EntityKind, tuple[str, ...]] = {
    "learning": ("insight",),
    "decision": ("description", "reasoning"),
    "snippet": ("intention", "gotchas"),
    "runbook": ("description", "trigger"),
    "adr": ("context", "decision", "consequences"),
}

_FACT_PATH: Mapping[str, str] = {
    "alembic_head": "/revision",
    "alembic_head_shipped": "/revision",
    "live_release_sha": "/release_sha",
}

_PRODUCTION_CUES: tuple[str, ...] = (
    "production alembic head",
    "production schema",
    "production database",
    "deployed schema",
    "deployed alembic head",
    "current alembic head",
    "current schema",
    "schéma de production",
    "schéma déployé",
    "base de production",
    "head de production",
)

_SHIPPED_CUES: tuple[str, ...] = (
    "shipped alembic head",
    "shipped schema",
    "shipped head",
    "packaged alembic head",
    "release alembic head",
    "head livré",
    "head embarqué",
    "head expédié",
    "schéma livré",
    "schéma embarqué",
    "head de la release",
    "head du release",
)

_LIVE_RELEASE_CUES: tuple[str, ...] = (
    "live release",
    "running release",
    "deployed release",
    "live release sha",
    "release live",
    "release en cours d'exécution",
    "release en cours",
    "release déployée",
    "version live",
)

_CUES: Mapping[str, tuple[str, ...]] = {
    "alembic_head": _PRODUCTION_CUES,
    "alembic_head_shipped": _SHIPPED_CUES,
    "live_release_sha": _LIVE_RELEASE_CUES,
}

_CUE_REGEXES: Mapping[str, re.Pattern[str]] = MappingProxyType(
    {
        fact: re.compile(r"(?<![\w-])(?:" + "|".join(re.escape(c) for c in cues) + r")(?![\w-])")
        for fact, cues in _CUES.items()
    }
)

# All regexes are single-character classes plus a fixed-length quantifier, with
# at most single-character lookarounds and no nested quantifiers: none can
# backtrack catastrophically on a bounded sentence.
_NEGATION = re.compile(
    r"\b(?:not|no\s+longer|isn['’]t|aren['’]t|doesn['’]t|don['’]t|hasn['’]t|"
    r"haven['’]t|shouldn['’]t|wouldn['’]t|can['’]t|cannot|won['’]t|never|"
    r"going\s+to\s+be|possibly|probably|maybe|might|should\s+be|"
    r"va\s+être|sera|devrait|peut[- ]être|probablement|jamais)\b"
    r"|n['’]est"
)
# A French "ne ... pas/plus/jamais" may span most of a sentence. Matching it as
# one regex with a free middle rescans the rest of the sentence for every "ne";
# two forward searches (the first opener, then any closer after it) stay linear.
_FRENCH_NEGATION_OPENER = re.compile(r"\b(?:ne\s|n['’])")
_FRENCH_NEGATION_CLOSER = re.compile(r"\b(?:pas|plus|jamais)\b")
_HISTORY = re.compile(
    r"\b(?:was|were|had|previously|formerly|before|used\s+to|will|planned|example|"
    r"était|anciennement|avant|sera)\b"
)
_PRESENT = re.compile(r"\b(?:is|runs|uses|at|est|tourne)\b|=")

# A path ("/056", "056/"), a date-like run ("2026-10-056"), a dotted version
# ("056.1") or an identifier ("056_rev") is not a revision; a sentence-ending
# dot still is.
_REVISION_TOKEN = re.compile(r"(?<![\w./-])([0-9]{3})(?![\w/-]|\.\w)")
_SHA_TOKEN = re.compile(r"(?<![0-9a-fA-F])([0-9a-f]{40})(?![0-9a-fA-F])")

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

_FENCE_START = re.compile(r"^\s*(`{3,}|~{3,})")
_INDENTED_LINE = re.compile(r"^[ \t]{4,}")
_BLOCKQUOTE_LINE = re.compile(r"^\s*>")
_INLINE_CODE = re.compile(r"`[^`]*`")
_STRAIGHT_DQUOTE = re.compile(r'"[^"]*"')
# A straight apostrophe sits between two letters (a contraction, a possessive or
# a French elision) and is never a quote. A quoted span opens only after a
# start, whitespace or punctuation, closes only before a whitespace,
# punctuation or end, and never spans a newline. It may span a sentence end so
# that a quoted sentence keeps its own final punctuation: stripping too much only
# ever removes prose, which is silence rather than a false assertion.
_STRAIGHT_SQUOTE = re.compile(r"(?<![\w])'[^'\n]*'(?![\w])")
_TYPO_DQUOTE = re.compile(r"“[^”]*”")
# The typographic opener is unambiguous, but its closer doubles as a French
# typographic apostrophe: only close before a non-word character.
_TYPO_SQUOTE = re.compile(r"‘[^’\n]*’(?![\w])")


@dataclass(frozen=True, slots=True)
class ClaimCandidate:
    """One candidate ready to become a ClaimInput with ``measure=False``."""

    statement: str
    fact_name: str
    expected: Mapping[str, object]

    def __post_init__(self) -> None:
        """Freeze the expectation so no caller can mutate a candidate after reading it."""
        object.__setattr__(self, "expected", MappingProxyType(dict(self.expected)))


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """Candidates plus the bounded reasons for anything the extractor skipped."""

    candidates: tuple[ClaimCandidate, ...]
    reasons: frozenset[ExtractionReason]


def _strip_non_assertion_regions(text: str) -> str:
    """Remove code, blockquote and quoted regions before any sentence matching.

    Examples and instructions describe possible state, not the current one, so
    they must never produce a claim. Fenced and indented code, Markdown
    blockquotes (including their lazy continuations, which run until a blank
    line), inline backticks and straight or typographic quotes are all removed
    line by line, then inline, in that order.
    """
    kept: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    in_blockquote = False
    for line in text.splitlines():
        if in_fence:
            m = _FENCE_START.match(line)
            if m and m.group(1)[0] == fence_char and len(m.group(1)) >= fence_len:
                in_fence = False
            continue
        m = _FENCE_START.match(line)
        if m:
            in_fence = True
            fence_char = m.group(1)[0]
            fence_len = len(m.group(1))
            in_blockquote = False
            continue
        if not line.strip():
            in_blockquote = False
            kept.append(line)
            continue
        if in_blockquote:
            continue
        if _INDENTED_LINE.match(line):
            continue
        if _BLOCKQUOTE_LINE.match(line):
            in_blockquote = True
            continue
        kept.append(line)
    joined = "\n".join(kept)
    for pattern in (
        _INLINE_CODE,
        _STRAIGHT_DQUOTE,
        _STRAIGHT_SQUOTE,
        _TYPO_DQUOTE,
        _TYPO_SQUOTE,
    ):
        joined = pattern.sub(" ", joined)
    return joined


def _has_french_negation(folded: str) -> bool:
    """Whether a French "ne"/"n'" is followed anywhere later by pas, plus or jamais."""
    opener = _FRENCH_NEGATION_OPENER.search(folded)
    return opener is not None and _FRENCH_NEGATION_CLOSER.search(folded, opener.end()) is not None


def _has_any_cue(folded: str) -> bool:
    """A plain substring test, cheap enough to run before the sentence bound."""
    return any(cue in folded for cues in _CUES.values() for cue in cues)


def _qualifying_facts(sentence: str, folded: str) -> tuple[str, ...]:
    """Return the allowlisted facts whose cue, tense and certainty all pass.

    A fact only qualifies when the sentence carries its cue, a present-tense
    marker, and no negation or history marker. Everything else is silence, not a
    candidate: favouring silence over a false assertion.
    """
    facts_found = [fact for fact, pattern in _CUE_REGEXES.items() if pattern.search(folded)]
    if not facts_found:
        return ()
    if _NEGATION.search(folded) or _HISTORY.search(folded) or _has_french_negation(folded):
        return ()
    if not _PRESENT.search(folded):
        return ()
    return tuple(facts_found)


def _token_values(fact: str, sentence: str) -> frozenset[str]:
    """Extract the bounded scalar token a fact's schema can persist as eq."""
    if fact in ("alembic_head", "alembic_head_shipped"):
        return frozenset(_REVISION_TOKEN.findall(sentence))
    return frozenset(_SHA_TOKEN.findall(sentence))


def extract_candidates(
    *,
    entity: EntityKind,
    project_key: str | None,
    fields: Mapping[str, str | None],
) -> ExtractionResult:
    """Extract at most one deterministic candidate per allowlisted fact.

    Selected prose fields are read in a fixed per-entity order, then sentences
    in order, so a retry with unchanged prose yields the same candidates. The
    first qualifying sentence supplies the ``statement``; overflow, ambiguity
    and missing scope are reported as reasons rather than truncating or
    guessing a value.
    """
    if not project_key or not project_key.strip():
        return ExtractionResult((), frozenset({"no_project_key"}))

    prose: list[str] = []
    total_bytes = 0
    for name in _SELECTED_FIELDS[entity]:
        value = fields.get(name)
        if value:
            total_bytes += len(value.encode("utf-8"))
            prose.append(value)
    if total_bytes > _PROSE_LIMIT_BYTES:
        return ExtractionResult((), frozenset({"prose_overflow"}))

    reasons: set[ExtractionReason] = set()
    fact_values: dict[str, set[str]] = {}
    fact_statement: dict[str, str] = {}
    fact_ambiguous: set[str] = set()
    order: list[str] = []

    for text in prose:
        cleaned = _strip_non_assertion_regions(text)
        for sentence in _SENTENCE_SPLIT.split(cleaned):
            trimmed = sentence.strip()
            if not trimmed:
                continue
            folded = trimmed.casefold()
            if len(trimmed) > _SENTENCE_LIMIT_CHARS:
                # Only a sentence that could have been a candidate is an overflow;
                # the bound is checked before any regex runs on the sentence.
                if _has_any_cue(folded):
                    reasons.add("sentence_overflow")
                continue
            facts = _qualifying_facts(trimmed, folded)
            if not facts:
                continue
            if len(facts) > 1:
                fact_ambiguous.update(facts)
            for fact in facts:
                if fact in fact_ambiguous:
                    continue
                values = _token_values(fact, trimmed)
                if not values:
                    continue
                if len(values) > 1:
                    fact_ambiguous.add(fact)
                    continue
                if fact not in fact_values:
                    fact_values[fact] = set()
                    order.append(fact)
                fact_values[fact].update(values)
                if fact not in fact_statement:
                    fact_statement[fact] = trimmed

    candidates: list[ClaimCandidate] = []
    for fact in order:
        if fact in fact_ambiguous or len(fact_values[fact]) != 1:
            reasons.add("ambiguous")
            continue
        value = next(iter(fact_values[fact]))
        candidates.append(
            ClaimCandidate(
                statement=fact_statement[fact],
                fact_name=fact,
                expected={"path": _FACT_PATH[fact], "op": "eq", "value": value},
            )
        )

    if fact_ambiguous:
        reasons.add("ambiguous")

    return ExtractionResult(tuple(candidates), frozenset(reasons))
