"""Contract tests for the pure, deterministic claim extractor."""

from __future__ import annotations

import os
import subprocess
import sys
import time

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


def test_extract_claims_deterministic_output_order() -> None:
    """The output order of candidates is independent of PYTHONHASHSEED."""
    code = (
        "from brain_v42.facts.claim_extractor import extract_candidates\n"
        "result = extract_candidates(entity='decision', project_key='brain-v42', "
        "fields={'description': 'The live release is 1111111111111111111111111111111111111111. The production schema is 056.'})\n"
        "print([c.fact_name for c in result.candidates])\n"
    )
    env1 = dict(os.environ, PYTHONHASHSEED="1")
    env2 = dict(os.environ, PYTHONHASHSEED="2")

    out1 = subprocess.run(
        [sys.executable, "-c", code], env=env1, capture_output=True, text=True, check=True
    ).stdout
    out2 = subprocess.run(
        [sys.executable, "-c", code], env=env2, capture_output=True, text=True, check=True
    ).stdout
    assert out1 == out2
    assert out1.strip() == "['live_release_sha', 'alembic_head']"


def test_extract_claims_strips_fences_correctly() -> None:
    """A matching fence requires the same character and at least the same length."""
    text = "~~~\n```\nThe production schema is 056.\n```\n~~~\nThe production schema is 058."
    result = _extract("learning", {"insight": text})
    assert result.candidates == (
        ClaimCandidate(
            statement="The production schema is 058.",
            fact_name="alembic_head",
            expected={"path": "/revision", "op": "eq", "value": "058"},
        ),
    )
    assert result.reasons == frozenset()


def test_extract_claims_strips_real_single_quoted_span_with_punctuation() -> None:
    """Quotes with punctuation are still stripped."""
    quoted = _extract(
        "learning",
        {"insight": "The note says 'The production schema is 056.' today."},
    )
    assert quoted.candidates == ()
    assert quoted.reasons == frozenset()

    typo_quoted = _extract(
        "learning",
        {"insight": "The note says ‘The production schema is 056.’ today."},
    )
    assert typo_quoted.candidates == ()
    assert typo_quoted.reasons == frozenset()


@pytest.mark.parametrize(
    "insight",
    [
        "The production schema is going to be 056.",
        "The production schema is possibly 056.",
        "The production schema is probably 056.",
        "The production schema maybe 056.",
        "The production schema might be 056.",
        "The production schema should be 056.",
        "The production schema is never 056.",
        "The production schema is no longer 056.",
        "Le schéma de production va être 056.",
        "Le schéma de production sera 056.",
        "Le schéma de production devrait être 056.",
        "Le schéma de production est peut-être 056.",
        "Le schéma de production est probablement 056.",
        "Le schéma de production n'est jamais 056.",
    ],
)
def test_extract_claims_rejects_uncertainty_and_future(insight: str) -> None:
    """Future, uncertainty and negation markers reject an assertion."""
    result = _extract("learning", {"insight": insight})
    assert result.candidates == ()


def test_extract_claims_cue_word_boundaries() -> None:
    """Cues must match at word boundaries to avoid preproduction or non-production."""
    for insight in (
        "The preproduction schema is 056.",
        "The non-production schema is 056.",
        "The pre-production schema is 056.",
    ):
        result = _extract("learning", {"insight": insight})
        assert result.candidates == ()
        assert result.reasons == frozenset()


def test_extract_claims_rejects_revision_embedded_in_paths_or_dates() -> None:
    """Revision tokens embedded in paths or date-like sequences are rejected."""
    for insight in (
        "The production schema is /056.",
        "The production schema is 056/.",
        "The production schema is 2026-10-056.",
        "The production schema is 10-056.",
        "The production schema is 056.1.",
        "The production schema is 056_rev.",
    ):
        result = _extract("learning", {"insight": insight})
        assert result.candidates == ()


def test_extract_claims_performance_linear_negation() -> None:
    """A 60 KiB candidate sentence full of "ne" overflows before any regex runs."""
    text = "Le schéma de production est 058 " + "ne " * 20000
    start = time.perf_counter()
    result = _extract("learning", {"insight": text})
    duration = time.perf_counter() - start
    assert duration < 0.5
    assert result.candidates == ()
    assert result.reasons == frozenset({"sentence_overflow"})


def test_extract_claims_sentence_naming_two_different_facts_yields_none() -> None:
    """One sentence that asserts two different facts is ambiguous for both."""
    result = _extract(
        "learning",
        {"insight": f"The production schema is 056 and the live release is {_SHA_40}."},
    )
    assert result.candidates == ()
    assert result.reasons == frozenset({"ambiguous"})


def test_extract_claims_rejects_distant_french_negation() -> None:
    """A French "ne ... plus" refuses the sentence however far apart its two words are."""
    result = _extract(
        "learning",
        {
            "insight": "Le schéma de production ne tourne, d'après toutes les mesures "
            "relevées par l'équipe d'exploitation cette semaine, plus en 056."
        },
    )
    assert result.candidates == ()


def test_extract_claims_overflow_counts_only_candidate_sentences() -> None:
    """A long sentence without any target cue is not a candidate, so it is not an overflow."""
    result = _extract("learning", {"insight": "word " * 200 + "end."})
    assert result.candidates == ()
    assert result.reasons == frozenset()


def test_extract_claims_many_bounded_french_sentences_full_of_ne_stay_fast() -> None:
    """60 KiB of bounded French sentences with a cue and many "ne" stays linear."""
    sentence = "Le schéma de production est 058 " + "ne " * 150 + "fin. "
    text = sentence * (60 * 1024 // len(sentence.encode())) + "\n\nLe schéma de production est 058."
    start = time.perf_counter()
    result = _extract("learning", {"insight": text})
    duration = time.perf_counter() - start
    assert duration < 0.5
    assert [c.expected["value"] for c in result.candidates] == ["058"]


@pytest.mark.parametrize(
    "insight",
    [
        "The note says 'It's measured: the production schema is 056.' today.",
        "The note says 'Start.\nThe production schema is 056.\nEnd.' today.",
        "The note says ‘It’s measured: the production schema is 056.’ today.",
        "The note says “Start.\nThe production schema is 056.\nEnd.” today.",
        "The note says `Start.\nThe production schema is 056.\nEnd.` today.",
        "```\n```python\nThe production schema is 056.\n```",
        "The production schema is expected to be 056.",
        "The production schema is likely 056.",
        "Le schéma de production est censé être 056.",
        'The production schema is "not" 056.',
        "The production schema is “not” 056.",
        "The production schema is `not` 056.",
        "The production schema is stable; ticket 056 is open.",
        "The pre production schema is 056.",
        "The non production schema is 056.",
        "The production schema is 057; ticket 056 is open.",
    ],
)
def test_extract_claims_rejects_sentences_outside_the_closed_grammar(insight: str) -> None:
    """Quotes, code, hedges and stray words around the cue or value yield no claim."""
    assert _extract("learning", {"insight": insight}).candidates == ()


def test_extract_claims_unclosed_fence_variant_keeps_text_after_real_closer() -> None:
    """An info-string line never closes a fence, so only the later sentence counts."""
    text = "```\n```python\nThe production schema is 056.\n```\nThe production schema is 058."
    result = _extract("learning", {"insight": text})
    assert [c.expected["value"] for c in result.candidates] == ["058"]
    assert result.reasons == frozenset()


@pytest.mark.parametrize(
    "insight",
    [
        "The production schema is now 057.",
        "The production schema is currently 057.",
        "Le schéma de production est actuellement 057.",
        "Production schema: 057.",
        f"The live release sha is {_SHA_40}.",
    ],
)
def test_extract_claims_accepts_grammar_adverbs_and_colon_copula(insight: str) -> None:
    """An optional adverb or a colon copula still yields exactly one claim."""
    result = _extract("learning", {"insight": insight})
    assert [c.expected["value"] for c in result.candidates] in (["057"], [_SHA_40])
    assert result.reasons == frozenset()


@pytest.mark.parametrize(
    "insight",
    [
        # Lead-ins: conditions, labels, hedges and examples are not measurement attributions.
        "If the migration ran, the production schema is 058.",
        "When the release is live, the production schema is 058.",
        "Once deployed, the production schema is 058.",
        "After the migration, the production schema is 058.",
        "Si la migration passe, le schéma de production est 058.",
        "Une fois déployé, le schéma de production est 058.",
        "Après la migration, le schéma de production est 058.",
        "Expected: the production schema is 058.",
        "Target: the production schema is 058.",
        "TODO: the production schema is 058.",
        "Objectif : le schéma de production est 058.",
        "Attendu : le schéma de production est 058.",
        "In staging, the production schema is 058.",
        "Par exemple, le schéma de production est 058.",
        "Wrong: the production schema is 058.",
        "Faux : le schéma de production est 058.",
        "Suppose that, the production schema is 058.",
        # Trailers: only a short confirmation may follow the value.
        "The production schema is 058, once the release is live.",
        "The production schema is 058, if the migration ran.",
        "Le schéma de production est 058, si la migration passe.",
        "The production schema is 058, unless the rollback happened.",
        "The production schema is 058, I think.",
        "The production schema is 058, je crois.",
        "Le schéma de production est 058, à vérifier.",
        "The production schema is 058, to be confirmed.",
        "The production schema is 058, or higher.",
        "The production schema is 058, at least.",
        "The production schema is 058, right?",
        "The production schema is 058, according to the old runbook.",
        "La base de production est 058, sauf erreur.",
        "The production schema is 058, nope.",
    ],
)
def test_extract_claims_lead_in_and_trailer_are_closed_lists(insight: str) -> None:
    """A free clause around the assertion can carry a condition, a label or a hedge."""
    result = _extract("learning", {"insight": insight})
    assert result.candidates == ()
    assert result.reasons == frozenset()


@pytest.mark.parametrize(
    "insight",
    [
        "Measured on 2026-10-02, the production schema is 058.",
        "Le schéma de production est 058, vérifié.",
        "Mesuré le 02/10, le schéma de production est 058.",
        "It\u2019s measured today: the production schema is 058, that\u2019s all!",
    ],
)
def test_extract_claims_accepts_measurement_attributions(insight: str) -> None:
    """The closed lead-in and trailer lists still let a measured assertion through."""
    result = _extract("learning", {"insight": insight})
    assert [c.expected["value"] for c in result.candidates] == ["058"]
    assert result.reasons == frozenset()


@pytest.mark.parametrize(
    "insight",
    [
        "- ~~~text\n  comment.\n  The production schema is 056.\n  ~~~",
        "1) ~~~\n   x.\n   The production schema is 056.\n   ~~~",
        # A label paragraph governs what follows it: the whole field is refused.
        "Example.\nThe production schema is 056.",
        "Example. The production schema is 056.",
        "Exemple.\nLe schéma de production est 056.",
        "For instance. The production schema is 056.",
        "E.g. The production schema is 056.",
        "Par exemple. Le schéma de production est 056.",
        "Here is an example. The production schema is 056.",
        "Voici un exemple. Le schéma de production est 056.",
        "Hypothetically. The production schema is 056.",
        "Imagine this. The production schema is 056.",
        "Template. The production schema is 056.",
        "Example!\nThe production schema is 056.",
        "Example.\n\nThe production schema is 058.",
        "The production schema is 058.\n\nExample: none.",
        "Wrong. The production schema is 056.",
        "Faux. Le schéma de production est 056.",
        "Wrong:\n\nThe production schema is 056.",
        # A neighbouring sentence can retract or date an otherwise valid assertion.
        "The production schema is 056. Just kidding.",
        "The production schema is 056. That was last week.",
        "Le schéma de production est 056. Plus maintenant.",
        "Note this. The production schema is 056.",
    ],
)
def test_extract_claims_paragraph_must_be_pure_and_field_free_of_markers(insight: str) -> None:
    """Context a sentence cannot show on its own refuses the paragraph or the field."""
    result = _extract("learning", {"insight": insight})
    assert result.candidates == ()
    assert result.reasons == frozenset()


@pytest.mark.parametrize(
    ("insight", "value"),
    [
        (
            "- ~~~text\n  comment.\n  The production schema is 056.\n  ~~~\n\nThe production schema is 058.",
            "058",
        ),
        ("The production schema is 058.\n\nSome unrelated remark here.", "058"),
    ],
)
def test_extract_claims_other_paragraphs_are_independent(insight: str, value: str) -> None:
    """A list-item fence ends where its closer is, and a paragraph is judged alone."""
    result = _extract("learning", {"insight": insight})
    assert [c.expected["value"] for c in result.candidates] == [value]
    assert result.reasons == frozenset()
