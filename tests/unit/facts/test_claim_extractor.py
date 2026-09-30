"""Contract tests for the pure, deterministic claim extractor."""

from __future__ import annotations

import pytest

from brain_v42.facts.claim_extractor import ClaimCandidate, extract_candidates

_PROJECT = "brain-v42"
_SHA_40 = "abcdef0123456789abcdef0123456789abcdef01"


def _extract(entity: str, fields: dict[str, str | None]) -> object:
    return extract_candidates(entity=entity, project_key=_PROJECT, fields=fields)


def test_extract_claims_accepts_only_qualified_current_assertions() -> None:
    """The three allowlisted fixtures emit exact eq values, never a measured value."""
    head = _extract("learning", {"insight": "The production Alembic head is 056."})
    assert head.candidates == (
        ClaimCandidate(
            statement="The production Alembic head is 056.",
            fact_name="alembic_head",
            expected={"path": "/revision", "op": "eq", "value": "056"},
        ),
    )
    assert head.reasons == frozenset()

    shipped = _extract("learning", {"insight": "The shipped Alembic head is 057."})
    assert shipped.candidates == (
        ClaimCandidate(
            statement="The shipped Alembic head is 057.",
            fact_name="alembic_head_shipped",
            expected={"path": "/revision", "op": "eq", "value": "057"},
        ),
    )
    assert shipped.reasons == frozenset()

    release = _extract("learning", {"insight": f"The live release is {_SHA_40}."})
    assert release.candidates == (
        ClaimCandidate(
            statement=f"The live release is {_SHA_40}.",
            fact_name="live_release_sha",
            expected={"path": "/release_sha", "op": "eq", "value": _SHA_40},
        ),
    )
    assert release.reasons == frozenset()


def test_extract_claims_accepts_french_cue_variants() -> None:
    """French cue spellings extract the same eq value as their English counterpart."""
    assert _extract("learning", {"insight": "Le schéma de production est 056."}).candidates == (
        ClaimCandidate(
            statement="Le schéma de production est 056.",
            fact_name="alembic_head",
            expected={"path": "/revision", "op": "eq", "value": "056"},
        ),
    )
    assert _extract("learning", {"insight": "Le head livré est 057."}).candidates == (
        ClaimCandidate(
            statement="Le head livré est 057.",
            fact_name="alembic_head_shipped",
            expected={"path": "/revision", "op": "eq", "value": "057"},
        ),
    )
    assert _extract("learning", {"insight": f"La release live est {_SHA_40}."}).candidates == (
        ClaimCandidate(
            statement=f"La release live est {_SHA_40}.",
            fact_name="live_release_sha",
            expected={"path": "/release_sha", "op": "eq", "value": _SHA_40},
        ),
    )


def test_extract_claims_reads_only_assertion_bearing_fields() -> None:
    """Titles, code, examples and steps never contribute an assertion."""
    result = _extract(
        "snippet",
        {
            "title": "The production schema is 999.",
            "intention": "The production schema is 056.",
            "code": "The production schema is 998.",
            "usage_example": "The production schema is 997.",
            "gotchas": f"The live release is {_SHA_40}.",
        },
    )
    assert {candidate.fact_name for candidate in result.candidates} == {
        "alembic_head",
        "live_release_sha",
    }
    assert result.reasons == frozenset()


def test_extract_claims_caps_one_candidate_per_fact() -> None:
    """A fact qualifying in several fields yields one candidate from the first field."""
    result = _extract(
        "decision",
        {
            "description": "The production schema is 056.",
            "reasoning": "The production Alembic head is 056.",
        },
    )
    assert result.candidates == (
        ClaimCandidate(
            statement="The production schema is 056.",
            fact_name="alembic_head",
            expected={"path": "/revision", "op": "eq", "value": "056"},
        ),
    )
    assert result.reasons == frozenset()


@pytest.mark.parametrize(
    "insight",
    [
        "The production schema was 056 before the migration.",
        "Le schéma de production était 056.",
        "“The production schema is 056.”",
        "> The production schema is 056.",
        "`production schema is 056`",
        "```\nThe production schema is 056.\n```",
        "    The production schema is 056.",
        "The production schema will be 056.",
        "The production schema is not 056.",
        "Le schéma de production n'est pas 056.",
        "Version 056 is in this example.",
        "The production schema is v056.",
        "The production schema is 0567.",
        "Projection lag is 300 seconds",
        "VERIFY had 11 verdicts last night.",
    ],
)
def test_extract_claims_rejects_negative_corpus(insight: str) -> None:
    """History, quoted/code regions, future, negation and non-allowlisted facts yield none."""
    result = _extract("learning", {"insight": insight})
    assert result.candidates == ()
    assert result.reasons == frozenset()


def test_extract_claims_keeps_apostrophes_out_of_quote_stripping() -> None:
    """Contractions, possessives and French elisions are never quote delimiters."""
    french = _extract(
        "learning",
        {"insight": "D'après la mesure, le schéma de production est 058, c'est vérifié."},
    )
    assert french.candidates == (
        ClaimCandidate(
            statement="D'après la mesure, le schéma de production est 058, c'est vérifié.",
            fact_name="alembic_head",
            expected={"path": "/revision", "op": "eq", "value": "058"},
        ),
    )
    assert french.reasons == frozenset()

    english = _extract(
        "learning",
        {"insight": "It's measured: the production schema is 058, that's all."},
    )
    assert english.candidates == (
        ClaimCandidate(
            statement="It's measured: the production schema is 058, that's all.",
            fact_name="alembic_head",
            expected={"path": "/revision", "op": "eq", "value": "058"},
        ),
    )
    assert english.reasons == frozenset()


def test_extract_claims_strips_real_single_quoted_span() -> None:
    """A single-quoted span opens and closes around prose, not around apostrophes."""
    quoted = _extract(
        "learning",
        {"insight": "The note says 'the production schema is 056' today."},
    )
    assert quoted.candidates == ()
    assert quoted.reasons == frozenset()


def test_extract_claims_strips_lazy_blockquote_continuation() -> None:
    """A blockquote continues on non-blank lines without a ``>`` prefix until blank."""
    text = (
        "> The production schema is 056\n"
        "continued here and the production schema is 056.\n"
        "\n"
        "The production schema is 058."
    )
    result = _extract("learning", {"insight": text})
    assert result.candidates == (
        ClaimCandidate(
            statement="The production schema is 058.",
            fact_name="alembic_head",
            expected={"path": "/revision", "op": "eq", "value": "058"},
        ),
    )
    assert result.reasons == frozenset()


@pytest.mark.parametrize(
    "insight",
    [
        "The production schema is no longer 056.",
        "The production schema is 056, it hasn't changed.",
        "The production schema is 056, it shouldn't change.",
        "The production schema is 056, it wouldn't change.",
        "I don't think the production schema is 056.",
        "The production schema is 056, that isn\u2019t right.",
        "Le schéma de production ne tourne pas 058.",
        "Le schéma de production est 058, il n'utilise pas l'ancienne.",
        "Le schéma de production est 058, il n'utilise plus l'ancienne.",
    ],
)
def test_extract_claims_rejects_extended_negations(insight: str) -> None:
    """Extended English and French negations refuse a current assertion."""
    result = _extract("learning", {"insight": insight})
    assert result.candidates == ()
    assert result.reasons == frozenset()


def test_extract_claims_rejects_sha_prefix_and_ambiguous_fact() -> None:
    """Prefixes, overlong/uppercase tokens and disagreeing values yield none."""
    for insight in (
        "The live release is deadbeef.",
        f"The live release is {_SHA_40}a.",
        f"The live release is {_SHA_40.upper()}.",
        f"The live release is A{_SHA_40[1:]}.",
        f"The live release was {_SHA_40}.",
    ):
        result = _extract("learning", {"insight": insight})
        assert result.candidates == ()
        assert result.reasons == frozenset()

    two_targets = _extract(
        "learning",
        {"insight": "The production schema is 056 and the shipped head is 057."},
    )
    assert two_targets.candidates == ()
    assert two_targets.reasons == frozenset({"ambiguous"})

    disagreeing = _extract(
        "decision",
        {
            "description": "The production schema is 056.",
            "reasoning": "The production schema is 057.",
        },
    )
    assert disagreeing.candidates == ()
    assert disagreeing.reasons == frozenset({"ambiguous"})


def test_extract_claims_skips_overflow_without_truncating() -> None:
    """Overflowing prose and candidate sentences skip with a reason, never truncate."""
    overlong_prose = _extract("learning", {"insight": "x" * (64 * 1024 + 1)})
    assert overlong_prose.candidates == ()
    assert overlong_prose.reasons == frozenset({"prose_overflow"})

    overlong_sentence = _extract(
        "learning", {"insight": "The production schema is 056 " + "x" * 500}
    )
    assert overlong_sentence.candidates == ()
    assert overlong_sentence.reasons == frozenset({"sentence_overflow"})


def test_extract_claims_requires_project_key() -> None:
    """Claim anchors are project scoped, so an unscoped write extracts nothing."""
    result = extract_candidates(
        entity="learning",
        project_key=None,
        fields={"insight": "The production Alembic head is 056."},
    )
    assert result.candidates == ()
    assert result.reasons == frozenset({"no_project_key"})


def test_extracted_candidate_matches_claim_input_contract() -> None:
    """A candidate is exactly a valid ClaimInput with measure=False."""
    from brain_v42.models.claim_input import ClaimInput

    result = _extract("learning", {"insight": "The production Alembic head is 056."})
    candidate = result.candidates[0]
    claim = ClaimInput(
        statement=candidate.statement,
        fact_name=candidate.fact_name,
        expected=dict(candidate.expected),
        measure=False,
    )
    assert claim.expected == {"path": "/revision", "op": "eq", "value": "056"}
    assert claim.measure is False
