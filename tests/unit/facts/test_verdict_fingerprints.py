"""Tests for deterministic bounded verification fingerprints."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

import pytest

from brain_v42.facts.compare import Comparison
from brain_v42.facts.model import FactTarget, Measured, SourceIdentity, measurement_to_json
from brain_v42.facts.verdict_fingerprints import (
    MAX_ENVELOPE_BYTES,
    MAX_ENVELOPE_DEPTH,
    canonical_envelope,
    outcome_fingerprint,
    request_fingerprint,
)
from brain_v42.models.claim_verdict import ClaimVerificationError

_CLAIM_ID = UUID("00000000-0000-0000-0000-000000000003")


def _measurement(
    *,
    observed_at: datetime = datetime(2026, 9, 22, tzinfo=UTC),
    source: SourceIdentity | None = None,
    value: dict[str, object] | None = None,
) -> Measured:
    return Measured.from_value(
        fact="graph_projection_lag",
        definition_version=1,
        target=FactTarget.PRODUCTION,
        source=source or SourceIdentity("7612696091383607335", "brain", "127.0.0.1", 5432),
        value={"lag_seconds": 0} if value is None else value,
        observation_id=UUID("00000000-0000-0000-0000-000000000004"),
        measured_at=observed_at,
        duration_ms=1,
        ttl_seconds=60,
    )


def _request(**overrides: object) -> str:
    arguments: dict[str, object] = {
        "claim_id": _CLAIM_ID,
        "issuer_identity": "mcp:codex",
        "idempotency_key": "request-1",
        "expected_resolved": {"path": "/lag_seconds", "op": "lte", "value": 300},
        "definition_version": 1,
        "validity_seconds": 240,
    }
    arguments.update(overrides)
    return request_fingerprint(**arguments)  # type: ignore[arg-type]


def test_request_fingerprint_is_stable_across_mapping_order_and_measurement_time() -> None:
    """Retry identity comes from immutable request fields, never fresh evidence."""
    first = _request()
    reordered = _request(expected_resolved={"value": 300, "op": "lte", "path": "/lag_seconds"})

    assert first == reordered
    assert first != outcome_fingerprint(
        Comparison(verdict="holds", reason=None),
        _measurement(observed_at=datetime(2027, 1, 1, tzinfo=UTC)),
    )


def test_request_fingerprint_hashes_exact_immutable_fields() -> None:
    """A retry digest is the standard SHA256 of only the documented request payload."""
    expected_payload = {
        "claim_id": "00000000-0000-0000-0000-000000000003",
        "issuer_identity": "mcp:codex",
        "idempotency_key": "request-1",
        "expected_resolved": {"path": "/lag_seconds", "op": "lte", "value": 300},
        "definition_version": 1,
        "validity_seconds": 240,
    }
    expected = hashlib.sha256(
        json.dumps(
            expected_payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()

    assert _request() == expected


@pytest.mark.parametrize(
    "override",
    [
        {"issuer_identity": "mcp:other"},
        {"idempotency_key": "request-2"},
        {"expected_resolved": {"path": "/lag_seconds", "op": "lte", "value": 301}},
        {"claim_id": UUID("00000000-0000-0000-0000-000000000005")},
        {"definition_version": 2},
        {"validity_seconds": 241},
    ],
)
def test_request_fingerprint_changes_when_an_immutable_request_field_changes(
    override: dict[str, object],
) -> None:
    """Changing a persisted request field must reject a conflicting replay."""
    assert _request() != _request(**override)


def test_outcome_fingerprint_includes_the_full_existing_measurement_payload() -> None:
    """Changing verdict metadata or measured evidence changes the audit fingerprint."""
    measurement = _measurement()
    holds = outcome_fingerprint(Comparison(verdict="holds", reason=None), measurement)
    falsified = outcome_fingerprint(Comparison(verdict="falsified", reason=None), measurement)

    assert holds != falsified
    assert holds == outcome_fingerprint(Comparison(verdict="holds", reason=None), measurement)


def test_outcome_fingerprint_hashes_exact_verdict_and_measurement_fields() -> None:
    """The audit digest uses the existing wire measurement, including its value field."""
    measurement = _measurement()
    expected_payload = {
        "verdict": "holds",
        "reason": None,
        "measurement": {
            "status": "measured",
            "fact": "graph_projection_lag",
            "definition_version": 1,
            "target": "production",
            "observation_id": "00000000-0000-0000-0000-000000000004",
            "measured_at": "2026-09-22T00:00:00Z",
            "duration_ms": 1,
            "ttl_seconds": 60,
            "source_kind": "probe",
            "source": {
                "system_identifier": "7612696091383607335",
                "database": "brain",
                "server_addr": "127.0.0.1",
                "server_port": 5432,
            },
            "value": {"lag_seconds": 0},
            "digest": measurement.digest,
        },
    }
    expected = hashlib.sha256(
        json.dumps(
            expected_payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()

    assert outcome_fingerprint(Comparison(verdict="holds", reason=None), measurement) == expected


def test_outcome_fingerprint_changes_for_measurement_metadata_only() -> None:
    """Timestamps and source identity remain audit-significant even when the value is unchanged."""
    original = _measurement()
    later = _measurement(observed_at=datetime(2026, 9, 22, 0, 0, 1, tzinfo=UTC))
    other_source = _measurement(
        source=SourceIdentity("7612696091383607335", "brain", "127.0.0.1", 5433)
    )

    baseline = outcome_fingerprint(Comparison(verdict="holds", reason=None), original)
    assert baseline != outcome_fingerprint(Comparison(verdict="holds", reason=None), later)
    assert baseline != outcome_fingerprint(Comparison(verdict="holds", reason=None), other_source)


def test_canonical_envelope_accepts_the_exact_byte_and_depth_limits() -> None:
    """The verdict envelope is wider than a fact value without truncating it."""
    byte_value = {"payload": "x" * (MAX_ENVELOPE_BYTES - len('{"payload":""}'))}
    nested: object = 0
    for index in range(MAX_ENVELOPE_DEPTH):
        nested = {str(index): nested}

    assert len(canonical_envelope(byte_value).encode("utf-8")) == MAX_ENVELOPE_BYTES
    assert canonical_envelope(nested)  # type: ignore[arg-type]


def test_canonical_envelope_accepts_a_maximum_fact_value_without_truncation() -> None:
    """A real 4096-byte, depth-eight measurement fits inside the larger verdict envelope."""
    value: dict[str, object] = {"payload": "x" * 4012}
    for _ in range(7):
        value = {"child": value}
    measurement = _measurement(value=value)

    assert len(measurement.value_json.encode("utf-8")) == 4096
    assert canonical_envelope({"measurement": measurement_to_json(measurement)})


@pytest.mark.parametrize(
    "value",
    [
        {"payload": "x" * (MAX_ENVELOPE_BYTES - len('{"payload":""}') + 1)},
        {"bad": 1.5},
        {"bad": "nul\x00"},
        {"bad": "\ud800"},
    ],
)
def test_canonical_envelope_refuses_oversized_or_non_json_values(value: dict[str, object]) -> None:
    """Invalid evidence cannot be silently hashed into durable audit data."""
    with pytest.raises(ClaimVerificationError) as error:
        canonical_envelope(value)

    assert error.value.code == "invalid_argument"


def test_canonical_envelope_refuses_depth_beyond_its_closed_limit() -> None:
    """Unbounded nesting cannot cause unbounded verifier work."""
    nested: object = 0
    for index in range(MAX_ENVELOPE_DEPTH + 1):
        nested = {str(index): nested}

    with pytest.raises(ClaimVerificationError, match="invalid verification argument"):
        canonical_envelope(nested)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "override",
    [
        {"issuer_identity": ""},
        {"issuer_identity": "mcp:\x00codex"},
        {"idempotency_key": " "},
        {"idempotency_key": "k" * 201},
    ],
)
def test_request_fingerprint_refuses_unbounded_or_unsafe_caller_strings(
    override: dict[str, object],
) -> None:
    """Caller-controlled idempotency values stay inside the safe persistence contract."""
    with pytest.raises(ClaimVerificationError) as error:
        _request(**override)

    assert error.value.code == "invalid_argument"
