"""Tests for immutable measured and unreadable fact results."""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from brain_v42.facts.model import (
    FactTarget,
    InvalidFactNameError,
    Measured,
    SourceIdentity,
    Unreadable,
    measurement_to_json,
    validate_fact_name,
    with_source_kind,
)

NOW = datetime(2026, 9, 20, 12, 34, 56, tzinfo=UTC)


def _source() -> SourceIdentity:
    return SourceIdentity(
        system_identifier="7612696091383607335",
        database="brain",
        server_addr="172.18.0.2",
        server_port=5432,
    )


def _measured(**overrides: object) -> Measured:
    values: dict[str, object] = {
        "fact": "graph_projection_lag",
        "definition_version": 1,
        "target": FactTarget.PRODUCTION,
        "source": _source(),
        "value": {"lag_seconds": 12, "queues": ["graph"]},
        "observation_id": uuid4(),
        "measured_at": NOW,
        "duration_ms": 4,
        "ttl_seconds": 30,
    }
    values.update(overrides)
    return Measured.from_value(**values)  # type: ignore[arg-type]


def test_validate_fact_name_enforces_the_published_grammar() -> None:
    """A fact key is stable catalogue vocabulary rather than free-form user text."""
    assert validate_fact_name("graph_projection_lag") == "graph_projection_lag"
    for invalid in ("Graph", "1x", "a" * 65, ""):
        with pytest.raises(InvalidFactNameError):
            validate_fact_name(invalid)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("system_identifier", "-1"),
        ("system_identifier", "1" * 21),
        ("database", "Brain"),
        ("server_addr", "not-an-ip"),
        ("server_port", 0),
        ("server_port", 65536),
    ],
)
def test_source_identity_refuses_invalid_fields(field: str, value: object) -> None:
    """An identity must be unambiguous before the registry can compare targets."""
    values: dict[str, object] = _source().as_dict()
    values[field] = value
    with pytest.raises(ValueError):
        SourceIdentity(**values)  # type: ignore[arg-type]


def test_source_identity_strips_postgres_network_suffix() -> None:
    """PostgreSQL text network output compares equal to the declared IP literal."""
    identity = SourceIdentity(
        system_identifier="7612696091383607335",
        database="brain",
        server_addr="172.18.0.2/32",
        server_port=5432,
    )
    assert identity.server_addr == "172.18.0.2"


def test_source_identity_mapping_requires_exactly_its_four_keys() -> None:
    """Composition fails closed if an identity declaration is incomplete or ambiguous."""
    mapping = _source().as_dict()
    with pytest.raises(ValueError, match="missing"):
        SourceIdentity.from_mapping(
            {key: value for key, value in mapping.items() if key != "database"}
        )
    with pytest.raises(ValueError, match="extra"):
        SourceIdentity.from_mapping({**mapping, "revision": "052"})


def test_measured_from_value_stores_canonical_json_and_its_digest() -> None:
    """Callers cannot accidentally retain an unordered object beside its digest."""
    measured = _measured(value={"z": 1, "a": True})
    assert measured.value_json == '{"a":true,"z":1}'
    assert measured.digest == "38f6fbfe500b3f47bb8876e871422399399067186be5a97d2bce97f7300d7599"


@pytest.mark.parametrize("value_json", ['{"b":1,"a":2}', '{"a": 2}'])
def test_measured_refuses_noncanonical_json_text(value_json: str) -> None:
    """Stored measurement text must be the sole recipe output, not merely valid JSON."""
    with pytest.raises(ValueError, match="canonical"):
        Measured(
            fact="graph_projection_lag",
            definition_version=1,
            target=FactTarget.PRODUCTION,
            source=_source(),
            value_json=value_json,
            digest="0" * 64,
            observation_id=uuid4(),
            measured_at=NOW,
            duration_ms=1,
            ttl_seconds=30,
            source_kind="probe",
        )


def test_measured_refuses_a_wrong_digest() -> None:
    """The digest must attest exactly the stored canonical text."""
    measured = _measured()
    with pytest.raises(ValueError, match="digest"):
        Measured(
            fact=measured.fact,
            definition_version=measured.definition_version,
            target=measured.target,
            source=measured.source,
            value_json=measured.value_json,
            digest="0" * 64,
            observation_id=measured.observation_id,
            measured_at=measured.measured_at,
            duration_ms=measured.duration_ms,
            ttl_seconds=measured.ttl_seconds,
            source_kind=measured.source_kind,
        )


def test_measured_refuses_a_naive_instant() -> None:
    """An observation instant without a timezone cannot support an honest age."""
    with pytest.raises(ValueError, match="UTC"):
        _measured(measured_at=datetime(2026, 9, 20, 12, 34, 56))


def test_measured_refuses_negative_duration() -> None:
    """Durations are elapsed milliseconds and cannot be negative."""
    with pytest.raises(ValueError, match="duration"):
        _measured(duration_ms=-1)


def test_measured_value_is_fresh_after_top_level_and_nested_mutation() -> None:
    """A frozen measurement never exposes a mutable object it owns."""
    measured = _measured()
    first = measured.value
    first["new"] = "local"
    first["queues"].append("mutated")

    assert measured.value == {"lag_seconds": 12, "queues": ["graph"]}


def test_unreadable_refuses_an_unknown_error_code() -> None:
    """Callers rely on a stable, closed reason vocabulary."""
    with pytest.raises(ValueError, match="error_code"):
        Unreadable(
            fact="graph_projection_lag",
            definition_version=1,
            target=FactTarget.PRODUCTION,
            error_code="network_glitch",
            where=None,
            observation_id=uuid4(),
            measured_at=NOW,
            duration_ms=1,
            ttl_seconds=30,
            source_kind="probe",
        )


def test_unreadable_refuses_an_overlong_where() -> None:
    """Diagnostic labels remain small and safe to publish in an API result."""
    with pytest.raises(ValueError, match="where"):
        Unreadable(
            fact="graph_projection_lag",
            definition_version=1,
            target=FactTarget.PRODUCTION,
            error_code="probe_error",
            where="x" * 121,
            observation_id=uuid4(),
            measured_at=NOW,
            duration_ms=1,
            ttl_seconds=30,
            source_kind="probe",
        )


def test_measurement_to_json_emits_measured_and_unreadable_shapes() -> None:
    """Tool callers select the result shape by status instead of nullable fields."""
    measured_json = measurement_to_json(_measured())
    unreadable_json = measurement_to_json(
        Unreadable(
            fact="graph_projection_lag",
            definition_version=1,
            target=FactTarget.PRODUCTION,
            error_code="timeout",
            where=None,
            observation_id=uuid4(),
            measured_at=NOW,
            duration_ms=1,
            ttl_seconds=30,
            source_kind="probe",
        )
    )

    assert measured_json["status"] == "measured"
    assert measured_json["measured_at"] == "2026-09-20T12:34:56Z"
    assert measured_json["value"] == {"lag_seconds": 12, "queues": ["graph"]}
    assert measured_json["source"] == _source().as_dict()
    assert unreadable_json["status"] == "unreadable"
    assert unreadable_json["error_code"] == "timeout"
    assert unreadable_json["measured_at"] == "2026-09-20T12:34:56Z"


def test_with_source_kind_changes_only_the_cache_provenance() -> None:
    """Caching preserves the observed content and only marks how it was served."""
    measured = _measured()
    cached = with_source_kind(measured, "cache")
    assert cached.source_kind == "cache"
    assert cached != measured
    assert all(
        getattr(cached, field.name) == getattr(measured, field.name)
        for field in fields(measured)
        if field.name != "source_kind"
    )
