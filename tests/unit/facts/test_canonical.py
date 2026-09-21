"""Tests for the versioned, bounded canonical measurement recipe."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import replace

import pytest

from brain_v42.facts.canonical import (
    CLAIM_KEY_PREFIX,
    FACT_DEFINITION_DIGEST_PREFIX,
    MAX_CANONICAL_BYTES,
    ValueTooLargeError,
    assert_expected_within_bounds,
    canonical_json,
    claim_key,
    definition_digest,
    measurement_digest,
)
from brain_v42.facts.model import FactTarget
from brain_v42.facts.probe import FactDescriptor


def _reference_descriptor() -> FactDescriptor:
    """Return one complete declaration whose digest inputs are easy to distinguish."""
    return FactDescriptor(
        name="project_count",
        definition_version=3,
        target=FactTarget.PRODUCTION,
        ttl_seconds=300,
        timeout_seconds=4,
        queue_timeout_seconds=1,
        deadline_seconds=5,
        briefing=True,
        policies={"late_after_seconds": 300},
        value_schema={"count": "int"},
    )


def _definition_payload(descriptor: FactDescriptor) -> dict[str, object]:
    """Spell the persisted definition payload independently from the digest function."""
    return {
        "fact_name": descriptor.name,
        "definition_version": descriptor.definition_version,
        "target": descriptor.target.value,
        "ttl_seconds": descriptor.ttl_seconds,
        "timeout_seconds": descriptor.timeout_seconds,
        "policies": dict(descriptor.policies),
        "value_schema": dict(descriptor.value_schema),
    }


def test_canonical_json_sorts_keys_and_uses_compact_separators() -> None:
    """A stable text prevents equivalent objects from receiving distinct digests."""
    assert canonical_json({"z": 1, "a": [True, None]}) == '{"a":[true,null],"z":1}'


def test_canonical_json_refuses_a_float() -> None:
    """A float would admit platform-dependent numeric representations."""
    with pytest.raises(ValueError, match="float"):
        canonical_json({"ratio": 1.5})


def test_canonical_json_refuses_nan_as_a_float() -> None:
    """NaN remains outside the recipe even when JSON libraries offer to encode it."""
    with pytest.raises(ValueError, match="float"):
        canonical_json({"ratio": float("nan")})


def test_canonical_json_keeps_booleans_distinct_from_integers() -> None:
    """Boolean status values must not collapse into their integer counterparts."""
    assert canonical_json({"false": False, "true": True}) == '{"false":false,"true":true}'


@pytest.mark.parametrize("value", [{"text": "before\x00after"}, {"\x00key": "value"}])
def test_canonical_json_refuses_nul_characters(value: dict[str, str]) -> None:
    """NUL would fail later in PostgreSQL instead of at the recipe boundary."""
    with pytest.raises(ValueError, match="NUL"):
        canonical_json(value)


@pytest.mark.parametrize("value", [{"text": "\ud800"}, {"\ud800": "value"}])
def test_canonical_json_refuses_lone_surrogates(value: dict[str, str]) -> None:
    """Lone surrogates are not valid UTF-8 measurement data."""
    with pytest.raises(ValueError, match="surrogate"):
        canonical_json(value)


def test_canonical_json_refuses_non_string_keys() -> None:
    """JSON object keys need the same unambiguous string domain at every depth."""
    with pytest.raises(ValueError, match="string keys"):
        canonical_json({1: "value"})  # type: ignore[dict-item]


def _nested_mapping(containers: int) -> dict[str, object]:
    value: dict[str, object] = {"leaf": 1}
    for _ in range(containers - 1):
        value = {"child": value}
    return value


def test_canonical_json_accepts_depth_eight_and_refuses_depth_nine() -> None:
    """The small depth limit makes malformed external payloads safely bounded."""
    assert canonical_json(_nested_mapping(8))
    with pytest.raises(ValueError, match="deep"):
        canonical_json(_nested_mapping(9))


def test_canonical_json_enforces_encoded_byte_limit() -> None:
    """The storage bound applies to UTF-8 bytes, not Python character count."""
    assert len(canonical_json({"value": "x" * 4084}).encode("utf-8")) == 4096
    with pytest.raises(ValueError, match="4096"):
        canonical_json({"value": "x" * 4085})


def test_canonical_json_preserves_non_ascii_characters() -> None:
    """UTF-8 text stays readable rather than being rewritten as ASCII escapes."""
    assert canonical_json({"word": "é"}) == '{"word":"é"}'


def test_measurement_digest_pins_its_domain_prefix() -> None:
    """The recipe version belongs to the hashed bytes, not only documentation."""
    expected = hashlib.sha256(b"brain-v42-fact-measurement:v1\n{}").hexdigest()
    assert measurement_digest("{}") == expected


def test_canonical_json_rejects_extreme_depth_without_recursion_error() -> None:
    """Untrusted nesting must fail predictably before JSON encoding can recurse."""
    nested: list[object] = []
    for _ in range(10_000):
        nested = [nested]

    with pytest.raises(ValueError, match="deep"):
        canonical_json({"nested": nested})


def test_definition_digest_is_stable_and_pinned() -> None:
    """Changing a definition recipe must make this persisted contract fail loudly."""
    assert (
        definition_digest(_reference_descriptor())
        == "6635ace873fd43a532250b3dfb7410bc68fdc43b76d8979b57248f8d679b02ef"
    )


def test_definition_digest_excludes_queue_timeout_seconds() -> None:
    """Queue waiting changes execution policy, not the persisted definition identity."""
    reference = _reference_descriptor()
    assert definition_digest(reference) == definition_digest(
        replace(reference, queue_timeout_seconds=2)
    )


def test_definition_digest_excludes_deadline_seconds() -> None:
    """The end-to-end deadline is not one of the seven stored definition fields."""
    reference = _reference_descriptor()
    assert definition_digest(reference) == definition_digest(replace(reference, deadline_seconds=6))


def test_definition_digest_excludes_briefing() -> None:
    """Presentation selection cannot alter the historical fact definition identity."""
    reference = _reference_descriptor()
    assert definition_digest(reference) == definition_digest(replace(reference, briefing=False))


@pytest.mark.parametrize(
    "change",
    [
        lambda descriptor: replace(descriptor, name="project_total"),
        lambda descriptor: replace(descriptor, definition_version=4),
        lambda descriptor: replace(descriptor, target=FactTarget.REPOSITORY),
        lambda descriptor: replace(descriptor, ttl_seconds=301),
        lambda descriptor: replace(descriptor, timeout_seconds=5),
        lambda descriptor: replace(descriptor, policies={"late_after_seconds": 301}),
        lambda descriptor: replace(descriptor, value_schema={"total": "int"}),
    ],
)
def test_definition_digest_includes_each_persisted_field(
    change: Callable[[FactDescriptor], FactDescriptor],
) -> None:
    """Omitting any persisted field would hide definition drift from existing rows."""
    reference = _reference_descriptor()
    assert definition_digest(reference) != definition_digest(change(reference))


def test_definition_digest_has_a_distinct_domain_prefix() -> None:
    """A definition and measurement cannot collide when their payload text matches."""
    descriptor = _reference_descriptor()
    assert definition_digest(descriptor) != measurement_digest(
        canonical_json(_definition_payload(descriptor))
    )


def test_definition_digest_accepts_enum_and_string_targets_identically() -> None:
    """Callers using the stored target spelling retain the enum digest identity."""
    descriptor = _reference_descriptor()
    string_target = replace(descriptor, target="production")  # type: ignore[arg-type]
    assert definition_digest(descriptor) == definition_digest(string_target)


def test_definition_digest_is_unchanged_by_later_source_mapping_mutation() -> None:
    """The digest captures descriptor values rather than a caller-owned mapping view."""
    policies = {"late_after_seconds": 300}
    descriptor = replace(_reference_descriptor(), policies=policies)
    first_digest = definition_digest(descriptor)

    policies["late_after_seconds"] = 301

    assert definition_digest(descriptor) == first_digest


def test_definition_digest_propagates_canonical_size_errors() -> None:
    """Definition payloads inherit the canonical byte cap without another error type."""
    descriptor = replace(
        _reference_descriptor(),
        value_schema={"value": "x" * MAX_CANONICAL_BYTES},  # type: ignore[dict-item]
    )

    with pytest.raises(ValueTooLargeError):
        definition_digest(descriptor)


def _claim_inputs() -> dict[str, object]:
    """Return a literal claim payload distinct enough to expose omitted key fields."""
    return {
        "statement": "The graph projection lag stays bounded.",
        "fact_name": "graph_projection_lag",
        "expected": {"path": "/lag_seconds", "op": "lte", "value": 300},
        "target": FactTarget.PRODUCTION,
        "definition_version": 3,
    }


def _claim_payload(inputs: dict[str, object]) -> dict[str, object]:
    """Spell the claim-key payload independently of the production helper."""
    target = inputs["target"]
    assert isinstance(target, FactTarget | str)
    expected = inputs["expected"]
    assert isinstance(expected, dict)
    return {
        "statement": inputs["statement"],
        "fact_name": inputs["fact_name"],
        "expected": dict(expected),
        "target": target.value if isinstance(target, FactTarget) else target,
        "definition_version": inputs["definition_version"],
    }


def test_claim_key_is_stable_and_pinned() -> None:
    """A claim content identity must retain one explicit, reviewable digest value."""
    inputs = _claim_inputs()

    assert (
        claim_key(
            statement=inputs["statement"],  # type: ignore[arg-type]
            fact_name=inputs["fact_name"],  # type: ignore[arg-type]
            expected=inputs["expected"],  # type: ignore[arg-type]
            target=inputs["target"],  # type: ignore[arg-type]
            definition_version=inputs["definition_version"],  # type: ignore[arg-type]
        )
        == "75631c937223c1f99b2374c78de6fa8fd5f92fb531a1a1e2803c50cfb6247594"
    )


@pytest.mark.parametrize(
    "changed",
    [
        lambda inputs: {**inputs, "statement": "A different statement."},
        lambda inputs: {**inputs, "fact_name": "another_fact"},
        lambda inputs: {**inputs, "expected": {"path": "/lag_seconds", "op": "lte", "value": 301}},
        lambda inputs: {**inputs, "target": FactTarget.REPOSITORY},
        lambda inputs: {**inputs, "definition_version": 4},
    ],
    ids=["statement", "fact_name", "expected", "target", "definition_version"],
)
def test_claim_key_includes_each_declared_field(
    changed: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Every persisted claim-key input changes the content identity when changed."""
    reference = _claim_inputs()
    changed_inputs = changed(reference)

    assert claim_key(**_claim_payload(reference)) != claim_key(**_claim_payload(changed_inputs))


def test_claim_key_accepts_enum_and_string_target_identically() -> None:
    """Stored target spelling and the descriptor enum identify the same claim."""
    inputs = _claim_inputs()
    string_target = {**inputs, "target": FactTarget.PRODUCTION.value}

    assert claim_key(**_claim_payload(inputs)) == claim_key(**_claim_payload(string_target))


def test_claim_key_has_its_own_digest_domain() -> None:
    """Domain prefixes prevent another digest purpose from sharing a claim key."""
    payload = _claim_payload(_claim_inputs())
    text = canonical_json(payload)
    key = claim_key(**payload)
    definition_domain_digest = hashlib.sha256(
        FACT_DEFINITION_DIGEST_PREFIX + text.encode("utf-8")
    ).hexdigest()

    assert key == hashlib.sha256(CLAIM_KEY_PREFIX + text.encode("utf-8")).hexdigest()
    assert key != measurement_digest(text)
    assert key != definition_domain_digest


def _expected_with_depth(depth: int) -> dict[str, object]:
    """Build mappings whose root object counts as the first JSON depth level."""
    value: dict[str, object] = {"leaf": 1}
    for _ in range(depth - 1):
        value = {"child": value}
    return value


def test_expected_canonical_bounds_accept_exact_byte_limit() -> None:
    """The claim-specific byte boundary is measured on canonical UTF-8 text."""
    expected = {"value": "x" * 1012}
    assert len(canonical_json(expected).encode("utf-8")) == 1024

    assert_expected_within_bounds(expected)


def test_expected_canonical_bounds_refuse_one_byte_past_limit() -> None:
    """A 1025-byte declared expectation is rejected before a claim can be written."""
    expected = {"value": "x" * 1013}

    with pytest.raises(ValueError, match="expected byte-size rule.*1025"):
        assert_expected_within_bounds(expected)


def test_expected_canonical_bounds_accept_depth_four() -> None:
    """The root object plus three nested containers is within the claim depth rule."""
    assert_expected_within_bounds(_expected_with_depth(4))


def test_expected_canonical_bounds_refuse_depth_five() -> None:
    """One extra container must name the tighter claim depth rule, not the envelope."""
    with pytest.raises(ValueError, match="expected depth rule.*5"):
        assert_expected_within_bounds(_expected_with_depth(5))
