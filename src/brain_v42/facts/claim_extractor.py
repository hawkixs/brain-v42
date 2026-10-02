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
predicate ("stable; ticket 056") are refused by construction. The LEAD-IN and
the TRAILER are closed lists of measurement attributions ("d'après la mesure,
...", "..., vérifié"), not free clauses: a free clause around the assertion can
carry a condition ("if the migration ran"), a label ("TODO:"), a scope ("in
staging") or a hedge ("I think"), and enumerating those is the blacklist this
grammar replaced. The negation, history and uncertainty blacklists stay as an
extra filter on the whole sentence. Quotes and code are rejected, never
stripped: stripping can delete the very word ("not") that changes the meaning
of what is left.

Context is judged per paragraph, not per sentence. A paragraph yields claims
only when ALL of its sentences are plain assertions: a neighbouring sentence
can relabel ("Example."), retract ("Just kidding.") or date ("That was last
week.") an otherwise valid assertion, and enumerating such sentences is the
blacklist this grammar replaced. A closed list of context markers refuses a
whole field, because a label paragraph ("Example.", "Wrong:") governs the
paragraphs that follow it.
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
# Closed lists of measurement attributions. A free clause is never accepted
# before the cue or after the value (see the module docstring for why). Both
# apostrophes are accepted where one appears.
_LEAD_IN = re.compile(
    r"(?:d['\u2019]apr\u00e8s la mesure|selon la mesure|mesur\u00e9|it['\u2019]s measured|"
    r"measured|as measured|per the measurement)"
    r"(?:\s+today|\s+aujourd['\u2019]hui|\s+(?:on|le)\s+[0-9]{4}-[0-9]{2}-[0-9]{2}|"
    r"\s+le\s+[0-9]{2}/[0-9]{2}(?:/[0-9]{4})?)?"
    r"[,:]\s+"
)
_TRAILER = re.compile(
    r"[,;]\s+(?:c['\u2019]est v\u00e9rifi\u00e9|v\u00e9rifi\u00e9|mesur\u00e9|confirm\u00e9|"
    r"that['\u2019]s all|verified|measured|confirmed|as measured)[.!]?"
)
_COPULA = re.compile(r"\s+(?:is|est)(?![\w-])|\s*[=:]")
_ADVERB = re.compile(r"\s+(?:now|currently|actuellement|maintenant|d\u00e9sormais)(?![\w-])")

# A path ("/056", "056/"), a date-like run ("2026-10-056"), a dotted version
# ("056.1") or an identifier ("056_rev") is not a revision; a sentence-ending
# dot still is.
_REVISION_TOKEN = re.compile(r"(?<![\w./-])([0-9]{3})(?![\w/-]|\.\w)")
_SHA_TOKEN = re.compile(r"(?<![0-9a-fA-F])([0-9a-f]{40})(?![0-9a-fA-F])")

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

# A fence may open inside a list item, after indentation and a list marker.
_FENCE_START = re.compile(r"^\s*(?:(?:[-*+]|[0-9]{1,9}[.)])\s+)?(`{3,}|~{3,})")
# A closing fence is ONLY a run of the opener's character: "```python" inside an
# open fence is content, not a closer.
_FENCE_CLOSE = re.compile(r"^\s*(`{3,}|~{3,})\s*$")
_INDENTED_LINE = re.compile(r"^[ \t]{4,}")
_BLOCKQUOTE_LINE = re.compile(r"^\s*>")

# A label paragraph ("Example.", "Wrong:") governs the paragraphs after it, so
# one such word anywhere in a field refuses the whole field. Word-bounded and
# matched on the casefolded text.
_CONTEXT_MARKERS: tuple[str, ...] = (
    "example",
    "examples",
    "exemple",
    "exemples",
    "e.g.",
    "for instance",
    "par exemple",
    "hypothetical",
    "hypothetically",
    "hypoth\u00e8se",
    "hypoth\u00e9tique",
    "suppose",
    "supposons",
    "imagine",
    "imaginons",
    "sample",
    "illustration",
    "illustrative",
    "scenario",
    "sc\u00e9nario",
    "template",
    "fictional",
    "fictif",
    "demo",
    "wrong",
    "faux",
    "incorrect",
    "outdated",
    "obsol\u00e8te",
    "stale",
    "p\u00e9rim\u00e9",
    "deprecated",
    "myth",
    "todo",
    "draft",
    "brouillon",
)
_CONTEXT_MARKER = re.compile(
    r"(?<!\w)(?:"
    + "|".join(r"\s+".join(re.escape(w) for w in m.split(" ")) for m in _CONTEXT_MARKERS)
    + r")(?!\w)"
)

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


def _assertion_paragraphs(text: str) -> list[str]:
    """Return the paragraphs of a field that may carry an assertion.

    Examples and instructions describe possible state, not the current one, so
    fenced and indented code and Markdown blockquotes (including their lazy
    continuations, which run until a blank line) are removed line by line. A
    paragraph is a run of kept lines between blank lines; a removed region also
    ends one. A field is refused whole (no paragraph) when a line opens a quote
    or backtick span it does not close, since a span running across lines can
    hide a clean-looking sentence in its middle, or when it holds a context
    marker, since the label governs what follows it.
    """
    kept: list[str | None] = []
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
            kept.append(None)
            continue
        if not line.strip():
            in_blockquote = False
            kept.append(None)
            continue
        if in_blockquote:
            continue
        if _INDENTED_LINE.match(line):
            kept.append(None)
            continue
        if _BLOCKQUOTE_LINE.match(line):
            in_blockquote = True
            kept.append(None)
            continue
        kept.append(line)
    lines = [line for line in kept if line is not None]
    if any(_has_unbalanced_quote(line) for line in lines):
        return []
    if _CONTEXT_MARKER.search("\n".join(lines).casefold()):
        return []
    paragraphs: list[str] = []
    run: list[str] = []
    for entry in [*kept, None]:
        if entry is not None:
            run.append(entry)
        elif run:
            paragraphs.append("\n".join(run))
            run = []
    return paragraphs


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
    """Whether the text before the cue is empty, a determiner, or a closed lead-in.

    The determiner is the last word before the cue and must be one of a closed
    list; whatever precedes it must be empty or one of the measurement
    attributions of ``_LEAD_IN`` ("D'après la mesure, ", "It's measured: ").
    """
    remainder = prefix
    if prefix.endswith(_ELIDED_DETERMINERS) and (len(prefix) == 2 or prefix[-3].isspace()):
        remainder = prefix[:-2]
    else:
        stripped = prefix.rstrip()
        words = stripped.split()
        if len(stripped) < len(prefix) and words and words[-1] in _DETERMINERS:
            remainder = stripped[: len(stripped) - len(words[-1])]
    return not remainder or _LEAD_IN.fullmatch(remainder) is not None


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
    """Whether what follows the value is nothing, or a closed confirmation.

    Only a short attribution ("c'est vérifié", "that's all") may follow a comma
    or a semicolon: anything freer can hedge or condition the value.
    """
    return rest in ("", ".", "!") or _TRAILER.fullmatch(rest) is not None


def _token_values(fact: str, sentence: str) -> frozenset[str]:
    """Extract the bounded scalar token a fact's schema can persist as eq."""
    if fact in ("alembic_head", "alembic_head_shipped"):
        return frozenset(_REVISION_TOKEN.findall(sentence))
    return frozenset(_SHA_TOKEN.findall(sentence))


@dataclass(frozen=True, slots=True)
class _Reading:
    """What one sentence is: a plain assertion, or silence, ambiguity or overflow."""

    fact: str | None = None
    value: str = ""
    statement: str = ""
    ambiguous: tuple[str, ...] = ()
    overflow: bool = False


def _read_sentence(trimmed: str) -> _Reading:
    """Classify one trimmed sentence against the closed grammar."""
    folded = trimmed.casefold()
    if len(trimmed) > _SENTENCE_LIMIT_CHARS:
        # Only a sentence that could have been a candidate is an overflow;
        # the bound is checked before any regex runs on the sentence.
        return _Reading(overflow=_has_any_cue(folded))
    if _has_quote(trimmed):
        return _Reading()
    facts = _qualifying_facts(folded)
    if not facts:
        return _Reading()
    if len(facts) > 1:
        return _Reading(ambiguous=facts)
    fact = facts[0]
    anchor = _anchored_value(fact, folded)
    if anchor is None:
        return _Reading()
    # Tokens are read from the original sentence: casefolding would turn
    # an uppercase SHA into a valid one.
    values = _token_values(fact, trimmed)
    if len(values) > 1:
        return _Reading(ambiguous=(fact,))
    if values != {anchor[0]} or not _is_clean_trailer(folded[anchor[1] :]):
        return _Reading()
    return _Reading(fact=fact, value=anchor[0], statement=trimmed)


def extract_candidates(
    *,
    entity: EntityKind,
    project_key: str | None,
    fields: Mapping[str, str | None],
) -> ExtractionResult:
    """Extract at most one deterministic candidate per allowlisted fact.

    Selected prose fields are read in a fixed per-entity order, then paragraphs
    and sentences in order, so a retry with unchanged prose yields the same
    candidates. A paragraph counts only when every sentence in it is a plain
    assertion. The first qualifying sentence supplies the ``statement``; overflow, ambiguity
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
        for paragraph in _assertion_paragraphs(text):
            readings = [
                _read_sentence(trimmed)
                for sentence in _SENTENCE_SPLIT.split(paragraph)
                if (trimmed := sentence.strip())
            ]
            # Two passes: reasons and ambiguity are recorded for every sentence,
            # but claims are committed only when the whole paragraph matched.
            for reading in readings:
                fact_ambiguous.update(reading.ambiguous)
                if reading.overflow:
                    reasons.add("sentence_overflow")
            if not readings or any(reading.fact is None for reading in readings):
                continue
            for reading in readings:
                fact = reading.fact
                if fact is None:
                    continue
                if fact not in fact_values:
                    fact_values[fact] = set()
                    order.append(fact)
                fact_values[fact].add(reading.value)
                fact_statement.setdefault(fact, reading.statement)

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
