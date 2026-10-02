"""Deterministic, server-owned extraction of claims from knowledge prose.

The registry catalogue is closed and measured values are never read here: an
extracted claim records the value the prose *asserted*, so the nightly verifier
keeps the only measured verdict. Only a narrow, explicitly allowlisted set of
facts has a safe text grammar (a three-digit Alembic revision, or a full
40-character lowercase release SHA). The extractor stays pure — no database, no
registry, no write — so its behaviour is a pure function of the selected prose.

Matching is a CLOSED, ANCHORED grammar rather than a blacklist. Earlier rules
accepted "a cue anywhere, a present-tense marker anywhere and a value anywhere"
and then tried to subtract bad words; every review round found one more word
that slipped through. A sentence now yields a claim only when, once casefolded
and trimmed, it has this exact shape::

    [LEAD-IN] [DETERMINER] CUE COPULA [ADVERB] VALUE [TRAILER] [END]

Nothing else may sit between those parts, so hedges ("likely", "expected to
be"), qualifiers ("pre production") and a value that is not the cue's own
predicate ("stable; ticket 056") are refused by construction. The negation,
history and uncertainty blacklists stay as an extra filter on the whole
sentence. Quotes and code are rejected, never stripped: stripping can delete the
very word ("not") that changes the meaning of what is left.
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

# Longest cue first: "live release sha" must win over its own prefix "live
# release", otherwise the copula would be looked for right after "release".
_CUE_REGEXES: Mapping[str, re.Pattern[str]] = MappingProxyType(
    {
        fact: re.compile(
            r"(?<![\w-])(?:"
            + "|".join(re.escape(c) for c in sorted(cues, key=len, reverse=True))
            + r")(?![\w-])"
        )
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

# Grammar parts. The determiner list is exact: any other word between the
# lead-in and the cue ("pre", "non", "old") refuses the sentence.
_DETERMINERS = frozenset({"the", "our", "le", "la"})
_ELIDED_DETERMINERS = ("l'", "l\u2019")
_LEAD_IN_MAX_CHARS = 120
_COPULA = re.compile(r"\s+(?:is|est)(?![\w-])|\s*[=:]")
_ADVERB = re.compile(r"\s+(?:now|currently|actuellement|maintenant|d\u00e9sormais)(?![\w-])")

# A path ("/056", "056/"), a date-like run ("2026-10-056"), a dotted version
# ("056.1") or an identifier ("056_rev") is not a revision; a sentence-ending
# dot still is.
_REVISION_TOKEN = re.compile(r"(?<![\w./-])([0-9]{3})(?![\w/-]|\.\w)")
_SHA_TOKEN = re.compile(r"(?<![0-9a-fA-F])([0-9a-f]{40})(?![0-9a-fA-F])")

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

_FENCE_START = re.compile(r"^\s*(`{3,}|~{3,})")
# A closing fence is ONLY a run of the opener's character: "```python" inside an
# open fence is content, not a closer.
_FENCE_CLOSE = re.compile(r"^\s*(`{3,}|~{3,})\s*$")
_INDENTED_LINE = re.compile(r"^[ \t]{4,}")
_BLOCKQUOTE_LINE = re.compile(r"^\s*>")

# A straight apostrophe or a typographic right quote between two letters is a
# contraction, a possessive or a French elision, never a quote. Anything else
# (a quote mark, a backtick, an apostrophe next to a non-letter) marks quoted or
# code material: the sentence is refused rather than stripped. A letter is a
# word character that is neither a digit nor an underscore.
_STRAY_APOSTROPHE = re.compile(r"(?<![^\W\d_])['\u2019]|['\u2019](?![^\W\d_])")
_STRAIGHT_STRAY_APOSTROPHE = re.compile(r"(?<![^\W\d_])'|'(?![^\W\d_])")
_TYPOGRAPHIC_STRAY_CLOSER = re.compile(r"(?<![^\W\d_])\u2019|\u2019(?![^\W\d_])")
_QUOTE_MARK = re.compile(r"[`\"\u201c\u201d\u00ab\u00bb\u2018]")
_QUOTE_PAIRS = (("\u201c", "\u201d"), ("\u00ab", "\u00bb"))


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


def _has_unbalanced_quote(line: str) -> bool:
    """Whether a line opens a quote or code span that it does not close itself."""
    if line.count("`") % 2 or line.count('"') % 2:
        return True
    if len(_STRAIGHT_STRAY_APOSTROPHE.findall(line)) % 2:
        return True
    for opener, closer in _QUOTE_PAIRS:
        if opener in line and line.rfind(closer) < line.rfind(opener):
            return True
    last_open = line.rfind("\u2018")
    if last_open != -1:
        return not any(m.start() > last_open for m in _TYPOGRAPHIC_STRAY_CLOSER.finditer(line))
    return False


def _assertion_text(text: str) -> str:
    """Keep only the lines of a field that may carry an assertion.

    Examples and instructions describe possible state, not the current one, so
    fenced and indented code and Markdown blockquotes (including their lazy
    continuations, which run until a blank line) are removed line by line. A
    field with a line that opens a quote or backtick span it does not close is
    refused whole: a span running across lines can hide a clean-looking sentence
    in its middle, and no line-level rule can tell which lines are inside it.
    """
    kept: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    in_blockquote = False
    for line in text.splitlines():
        if in_fence:
            m = _FENCE_CLOSE.match(line)
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
    if any(_has_unbalanced_quote(line) for line in kept):
        return ""
    return "\n".join(kept)


def _has_quote(sentence: str) -> bool:
    """Whether a sentence holds a quote mark, a backtick or a non-elision apostrophe."""
    return bool(_QUOTE_MARK.search(sentence) or _STRAY_APOSTROPHE.search(sentence))


def _has_french_negation(folded: str) -> bool:
    """Whether a French "ne"/"n'" is followed anywhere later by pas, plus or jamais."""
    opener = _FRENCH_NEGATION_OPENER.search(folded)
    return opener is not None and _FRENCH_NEGATION_CLOSER.search(folded, opener.end()) is not None


def _has_any_cue(folded: str) -> bool:
    """A plain substring test, cheap enough to run before the sentence bound."""
    return any(cue in folded for cues in _CUES.values() for cue in cues)


def _qualifying_facts(folded: str) -> tuple[str, ...]:
    """Return the allowlisted facts whose cue appears, unless the sentence is hedged.

    A negation, history or uncertainty marker anywhere refuses the whole
    sentence. Everything else is silence, not a candidate: favouring silence
    over a false assertion. Whether the cue is actually followed by a value is
    the grammar's job, not this filter's.
    """
    facts_found = tuple(fact for fact, pattern in _CUE_REGEXES.items() if pattern.search(folded))
    if not facts_found:
        return ()
    if _NEGATION.search(folded) or _HISTORY.search(folded) or _has_french_negation(folded):
        return ()
    return facts_found


def _is_valid_lead(prefix: str) -> bool:
    """Whether the text before the cue is empty, a determiner, or a lead-in clause.

    The determiner is the last word before the cue and must be one of a closed
    list; whatever precedes it must be empty or a short clause ending in a comma
    or a colon followed by whitespace ("D'après la mesure, ", "It's measured: ").
    """
    remainder = prefix
    if prefix.endswith(_ELIDED_DETERMINERS) and (len(prefix) == 2 or prefix[-3].isspace()):
        remainder = prefix[:-2]
    else:
        stripped = prefix.rstrip()
        words = stripped.split()
        if len(stripped) < len(prefix) and words and words[-1] in _DETERMINERS:
            remainder = stripped[: len(stripped) - len(words[-1])]
    if not remainder:
        return True
    clause = remainder.rstrip()
    return len(clause) < len(remainder) and clause[-1] in ",:" and len(clause) <= _LEAD_IN_MAX_CHARS


def _anchored_value(fact: str, folded: str) -> tuple[str, int] | None:
    """Match ``[LEAD-IN] [DETERMINER] CUE COPULA [ADVERB] VALUE`` and return the value.

    The value is returned with the offset where it ends, so the caller can check
    the trailer. ``None`` means the sentence is not a plain assertion of this
    fact: any other word between the cue and the value refuses it.
    """
    cue = _CUE_REGEXES[fact].search(folded)
    if cue is None or not _is_valid_lead(folded[: cue.start()]):
        return None
    copula = _COPULA.match(folded, cue.end())
    if copula is None:
        return None
    pos = copula.end()
    adverb = _ADVERB.match(folded, pos)
    if adverb is not None:
        pos = adverb.end()
    pos += len(folded) - pos - len(folded[pos:].lstrip())
    pattern = _SHA_TOKEN if fact == "live_release_sha" else _REVISION_TOKEN
    token = pattern.match(folded, pos)
    if token is None:
        return None
    return token.group(1), token.end()


def _is_clean_trailer(rest: str) -> bool:
    """Whether what follows the value is nothing, or a comma/semicolon aside.

    The aside must not carry a revision, a SHA or a cue: it may comment on the
    value, never assert a second one.
    """
    if rest in ("", ".", "!"):
        return True
    if rest[0] not in ",;":
        return False
    clause = rest[1:]
    if clause[-1:] in (".", "!"):
        clause = clause[:-1]
    if not clause.strip():
        return False
    return not (_has_any_cue(clause) or _REVISION_TOKEN.search(clause) or _SHA_TOKEN.search(clause))


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
        for sentence in _SENTENCE_SPLIT.split(_assertion_text(text)):
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
            if _has_quote(trimmed):
                continue
            facts = _qualifying_facts(folded)
            if not facts:
                continue
            if len(facts) > 1:
                fact_ambiguous.update(facts)
                continue
            fact = facts[0]
            if fact in fact_ambiguous:
                continue
            anchor = _anchored_value(fact, folded)
            if anchor is None:
                continue
            # Tokens are read from the original sentence: casefolding would turn
            # an uppercase SHA into a valid one.
            values = _token_values(fact, trimmed)
            if len(values) > 1:
                fact_ambiguous.add(fact)
                continue
            if values != {anchor[0]} or not _is_clean_trailer(folded[anchor[1] :]):
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
