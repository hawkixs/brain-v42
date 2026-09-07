"""REORG, CLEAN and SCAN must tell the agent WHEN a search is appropriate.

Investigation W11 (2026-08-07 -> 2026-09-06) measured 224 of 1 027 empty
`brain_search` results (21.8%) coming from these three phases, issued as
one- or two-token queries -- "infra_status", "archived", "*" -- confirmed
again on 2026-09-05's own logs (`infra_status`, `status_infra`, `cpu_metrics`,
`test_`, `agent`, `dream`, all bare tokens, all returning "## 0 results").
None of `phase_reorg.md`, `phase_clean.md` or `phase_scan.md` ever asked for a
search: `brain_search` sat in each phase's "Allowed tools" line and nowhere
else, so the model improvised a query shape search was never built for --
semantic ranking against prose, not literal token matching. PR #112 made an
empty result explain itself (candidates considered, score threshold, tags
filter), which makes a bad query visible after the fact; it does not make the
query less likely before the call.

User decision 2026-09-07 (lot D18): keep `brain_search` in all three
allowlists, but add a short "When to search" paragraph to each prompt that
tells the agent what a good query looks like, that a bare tag/status word/
wildcard is not one, that `brain_list(tags=[...])` (already in every one of
these three allowlists) is the right tool for a literal tag filter -- and
`brain_list(..., include_archived=True)`, not a `status=` argument, for an
archived filter, since `brain_list`'s `status` filter only reaches decisions,
ADRs and plans and "archived" is not one of its values for any entity type --
that a zero result should not be retried with the same wording, and that a
search is optional here -- most runs will not need one.

Fix round (same lot, same date): the first draft of this paragraph pointed
the archived-filter case at `brain_list(status=...)`, which silently drops
the filter for `learning`, `snippet` and `runbook` (`status` is only wired
to `decision`/`adr`/`plan` in `crud_tools.py`) and used "archived" as its own
worked example of a `status` value that does not exist. The pinning
assertion for that pointer also asserted only the bare string `"brain_list"`,
which the section's own opening sentence already satisfies as a data-source
mention -- so the redirect itself was never under contract. Both are fixed
here: the prompt text and the assertion that pins it.

Two kinds of assertion, mirroring `test_dream_reorg_prompt_documents_both_
counters.py`:

- Prose-anchored: independently hardcoded phrases pinning the required
  content of the "## When to search" section (the three-word rule, the
  bare-token prohibition, the `brain_list(tags=...)` / `include_archived=True`
  pointer, the no-retry rule, the optionality statement). These do not
  derive from code, by construction -- they are a targeted prose contract.
- Code-derived, whole-file, negative: `_search_call_queries` parses every
  actual `brain_search(...)` CALL SITE the prompt carries (its own
  illustrative example included), in any of its three valid spellings
  (`query="..."`, `query='...'`, or a bare positional string), and asserts
  none of them is a query under three words. An example that violated the
  very rule it is meant to illustrate would be worse than no example -- and
  a bare-token call site added anywhere else in the file later, in any of
  those spellings, is caught the same way, not just in the new section.

`test_dream_prompts_match_phase_allowlists.py` and its siblings must stay
green: this file adds no new tool mention, only two more `brain_search(...)`
and `brain_list(...)` call sites, and both tools are already allowed in all
three of these phases -- pinned here too, as a guard on the guidance itself.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from brain_v42.mcp.dream_capabilities import DREAM_PHASE_TOOL_ALLOWLISTS

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = REPO_ROOT / "scripts" / "dream"
PHASES = ("reorg", "clean", "scan")
HEADING = "## When to search"

# `brain_search(...)` call site, capturing everything between the parens.
# Non-greedy up to the first `)` is deliberate: these calls are single-line
# and take one keyword argument, so this never needs to balance nested parens
# the way `test_dream_prompts_match_argument_policies.py`'s scanner does.
_SEARCH_CALL = re.compile(r"brain_search\(([^)]*)\)")
# The `query=` keyword is optional (a positional first argument is valid
# Python too), and the quoting can be single or double -- a bare-token call
# site written in any of these three spellings must still be caught by the
# whole-file negative scan below, not just the one spelling this regex used
# to require.
_QUERY_LITERAL = re.compile(r"""(?:query\s*=\s*)?["']([^"']*)["']""")


def _prompt_path(phase: str) -> Path:
    return PROMPT_DIR / f"phase_{phase}.md"


def _prompt_text(phase: str) -> str:
    return _prompt_path(phase).read_text(encoding="utf-8")


def _search_call_queries(text: str) -> list[str]:
    """Every literal query value from a `brain_search(...)` call site.

    Recognizes `query="..."`, `query='...'`, and a bare positional string
    (`brain_search("...")`) -- the three spellings a bare-token call site
    could plausibly use.
    """
    queries = []
    for call in _SEARCH_CALL.finditer(text):
        literal = _QUERY_LITERAL.search(call.group(1))
        if literal:
            queries.append(literal.group(1))
    return queries


def _when_to_search_section(text: str) -> str:
    """Slice from the heading to the next `## ` heading, or end of file."""
    start = text.index(HEADING)
    rest = text[start + len(HEADING) :]
    next_heading = re.search(r"\n## ", rest)
    end = next_heading.start() if next_heading else len(rest)
    return rest[:end]


def _unwrapped(text: str) -> str:
    """Collapse markdown's soft line-wrap whitespace to single spaces.

    A phrase-pinning assertion must not depend on WHERE a paragraph happens
    to wrap -- `"do not retry"` written across a line break is still the same
    sentence to a reader (and to the model executing this prompt), and a
    later re-wrap of the paragraph must not redden this test over formatting
    alone.
    """
    return re.sub(r"\s+", " ", text)


@pytest.mark.parametrize("phase", PHASES)
def test_brain_list_and_brain_search_are_actually_allowed_in_this_phase(phase: str) -> None:
    """Guard on the guidance itself: pointing at `brain_list` for tag/status
    filtering, or leaving `brain_search` in place, would be actively wrong
    advice if either tool were not reachable by this phase."""
    allowed = DREAM_PHASE_TOOL_ALLOWLISTS[phase]
    assert "brain_list" in allowed
    assert "brain_search" in allowed


@pytest.mark.parametrize(
    "call_source",
    [
        'brain_search(query="how does tag normalization handle plurals")',
        "brain_search(query='how does tag normalization handle plurals')",
        'brain_search("how does tag normalization handle plurals")',
    ],
    ids=["double-quoted keyword", "single-quoted keyword", "bare positional"],
)
def test_query_literal_extractor_recognizes_every_call_spelling(call_source: str) -> None:
    """Self-guard on the extractor: the whole-file negative scan
    (`test_no_search_call_site_anywhere_in_the_prompt_uses_a_bare_query`) is
    only as strong as its ability to find a call site. A single-quoted
    `query=` and a bare positional string are both valid Python and both
    unrecognized by a double-quoted-keyword-only regex -- exactly the two
    spellings a bare-token call site could use to slip past the scan."""
    assert _search_call_queries(call_source) == ["how does tag normalization handle plurals"]


@pytest.mark.parametrize("phase", PHASES)
def test_phase_prompt_has_a_when_to_search_section(phase: str) -> None:
    assert HEADING in _prompt_text(phase), (
        f"phase_{phase}.md has no '{HEADING}' section -- nothing tells the "
        "agent when a search is appropriate here, which is exactly what "
        "produced the bare one/two-token queries measured in W11."
    )


@pytest.mark.parametrize("phase", PHASES)
def test_when_to_search_states_the_three_word_natural_language_rule(phase: str) -> None:
    section = _unwrapped(_when_to_search_section(_prompt_text(phase)))
    assert "at least three words" in section, (
        f"phase_{phase}.md's '{HEADING}' section does not pin a minimum query "
        "length; a model left to guess a 'reasonable' shape is exactly how "
        "'infra_status' and 'archived' happened."
    )
    assert re.search(r"natural[- ]language question", section), (
        f"phase_{phase}.md's '{HEADING}' section does not say a query must be "
        "phrased as a natural-language question."
    )


@pytest.mark.parametrize("phase", PHASES)
def test_when_to_search_forbids_bare_tags_status_words_and_wildcards(phase: str) -> None:
    section = _unwrapped(_when_to_search_section(_prompt_text(phase)))
    assert "bare tag" in section, (
        f"phase_{phase}.md's '{HEADING}' section does not forbid a bare tag."
    )
    assert "wildcard" in section, (
        f"phase_{phase}.md's '{HEADING}' section does not forbid a wildcard "
        "query such as '*', which was measured live on 2026-09-05."
    )


@pytest.mark.parametrize("phase", PHASES)
def test_when_to_search_points_at_brain_list_for_literal_filters(phase: str) -> None:
    """Pin the actionable call shape, not just the bare tool name -- every
    section's opening sentence already names `brain_list` as a data source,
    so asserting the name alone is satisfied by that sentence and never
    exercises the redirect this paragraph exists to give."""
    section = _unwrapped(_when_to_search_section(_prompt_text(phase)))
    assert "brain_list(tags=" in section, (
        f"phase_{phase}.md's '{HEADING}' section does not redirect a "
        "tag filter to brain_list(tags=...), which this phase can already call."
    )
    assert "include_archived=True" in section, (
        f"phase_{phase}.md's '{HEADING}' section does not redirect an "
        "archived-entity filter to brain_list(..., include_archived=True). "
        "brain_list's `status` filter only reaches decisions, ADRs and "
        "plans -- pointing an archived-tag search at `status=` instead would "
        "silently list an unfiltered page for learning, snippet and runbook."
    )


@pytest.mark.parametrize("phase", PHASES)
def test_when_to_search_does_not_claim_brain_search_cannot_filter_by_tag(phase: str) -> None:
    """Negative assertion: `brain_search` DOES filter by tag overlap
    (`tags: list[str] | None` at brain_tools.py, forwarded by
    `brain_service.search()`, with `tags_filtered_out` rendered in the
    empty-result explanation) -- so the redirect to `brain_list` must not
    claim the opposite. An agent that believed the false claim would stop
    passing `tags=` to `brain_search` to narrow a real question, which is
    the opposite of what this section wants."""
    section = _unwrapped(_when_to_search_section(_prompt_text(phase)))
    assert "which `brain_search` does not" not in section, (
        f"phase_{phase}.md's '{HEADING}' section falsely claims brain_search "
        "cannot filter by tag literally -- it does, by overlap."
    )


@pytest.mark.parametrize("phase", PHASES)
def test_when_to_search_forbids_retrying_the_same_empty_query(phase: str) -> None:
    section = _unwrapped(_when_to_search_section(_prompt_text(phase)))
    assert "do not retry" in section, (
        f"phase_{phase}.md's '{HEADING}' section does not tell the agent to "
        "stop after reading an empty-result explanation, instead of retrying "
        "the same query verbatim."
    )


@pytest.mark.parametrize("phase", PHASES)
def test_when_to_search_states_search_is_optional_in_this_phase(phase: str) -> None:
    section = _unwrapped(_when_to_search_section(_prompt_text(phase)))
    assert "optional" in section, (
        f"phase_{phase}.md's '{HEADING}' section does not say a search is "
        "optional here -- leaving it implied is how a model ends up forcing "
        "one every run."
    )


@pytest.mark.parametrize("phase", PHASES)
def test_when_to_search_shows_an_example_call_obeying_its_own_rule(phase: str) -> None:
    """Positive witness, code-derived: the section's own example call site
    must itself carry a query of at least three words."""
    section = _when_to_search_section(_prompt_text(phase))
    queries = _search_call_queries(section)
    assert queries, (
        f"phase_{phase}.md's '{HEADING}' section shows no example "
        'brain_search(query="...") call site to anchor the rule it states.'
    )
    for query in queries:
        assert len(query.split()) >= 3, (
            f"phase_{phase}.md's example query {query!r} has fewer than three "
            "words -- it would contradict the very rule it illustrates."
        )


@pytest.mark.parametrize("phase", PHASES)
def test_no_search_call_site_anywhere_in_the_prompt_uses_a_bare_query(phase: str) -> None:
    """Negative assertion, whole-file, code-derived: no `brain_search(query=
    "...")` call site anywhere in the prompt -- not just inside the new
    section -- instructs a query under three words. This is the exact shape
    measured returning empty 21.8% of the time in W11 ("infra_status",
    "archived", "*", "test_", "agent", "dream")."""
    text = _prompt_text(phase)
    for query in _search_call_queries(text):
        assert len(query.split()) >= 3, (
            f"phase_{phase}.md instructs brain_search(query={query!r}), a bare "
            "token query of the exact shape measured empty in the W11 "
            "investigation."
        )
