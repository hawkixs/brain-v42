"""Acceptance boundaries for issuer-declared delivery attestations.

Ticket 04bc1f4a. Brain is the LEDGER: it validates the FORM of an attestation —
the kind vocabulary shape, a JSON-object payload, the surrogate-free strings and
the canonical digest recipe — and it NEVER judges the kind. These tests pin the
boundaries a stored row must not cross and the published digest vector a
consumer recomputes.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError


def _attestation(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "id": str(uuid4()),
        "ticket_id": str(uuid4()),
        "contract_revision": 1,
        "kind": "gate_passed",
        "payload": {"gate": "unit"},
        "digest": "a" * 64,
        "issuer_project": "brain-v42",
        "issuer_identity": "delivery-observer",
        "idempotency_key": "gate-key-1",
        "emitted_at": "2026-09-16T10:00:00+00:00",
        "recorded_at": "2026-09-16T10:00:01+00:00",
    }
    values.update(overrides)
    return values


def test_attestation_digest_preserves_the_published_vector() -> None:
    """The server computes this recipe; a consumer must be able to recompute it."""
    from brain_v42.models.delivery_hashes import canonical_digest

    assert canonical_digest({"gate": "unit"}, domain="attestation") == (
        "7fdcbddf961e6f3efc75d3edfbc198efd9d830200da818c75f6500c35d7e95d7"
    )


def test_the_attestation_domain_is_declared() -> None:
    from brain_v42.models.delivery_hashes import _DIGEST_DOMAINS

    assert "attestation" in _DIGEST_DOMAINS


def test_a_stored_attestation_validates_and_keeps_its_nullable_revision() -> None:
    from brain_v42.models.delivery import DeliveryAttestation

    attestation = DeliveryAttestation.model_validate(_attestation(contract_revision=None))

    assert attestation.kind == "gate_passed"
    assert attestation.contract_revision is None
    assert attestation.payload == {"gate": "unit"}


@pytest.mark.parametrize(
    "kind",
    ["Gate_passed", "gate-passed", "1gate", "", "a" * 65, "gate passed"],
)
def test_kind_must_match_the_lowercase_shape(kind: str) -> None:
    from brain_v42.models.delivery import DeliveryAttestation

    with pytest.raises(ValidationError, match="kind"):
        DeliveryAttestation.model_validate(_attestation(kind=kind))


@pytest.mark.parametrize("kind", ["released", "deployed", "an_unknown_but_valid_kind"])
def test_well_known_and_unknown_but_valid_kinds_are_both_accepted(kind: str) -> None:
    """The kind vocabulary is documentation, never an enforced allowlist."""
    from brain_v42.models.delivery import DeliveryAttestation

    assert DeliveryAttestation.model_validate(_attestation(kind=kind)).kind == kind


def test_payload_must_be_a_json_object() -> None:
    from brain_v42.models.delivery import DeliveryAttestation

    with pytest.raises(ValidationError, match="payload"):
        DeliveryAttestation.model_validate(_attestation(payload=["not", "an", "object"]))


def test_payload_rejects_floats() -> None:
    from brain_v42.models.delivery import DeliveryAttestation

    with pytest.raises(ValidationError, match="float"):
        DeliveryAttestation.model_validate(_attestation(payload={"ratio": 1.5}))


def test_payload_is_bounded_to_sixty_four_kibibytes() -> None:
    from brain_v42.models.delivery import DeliveryAttestation

    oversized = {"blob": "x" * (65536 + 1)}
    with pytest.raises(ValidationError, match="65536"):
        DeliveryAttestation.model_validate(_attestation(payload=oversized))

    accepted = {"blob": "x" * 65000}
    assert DeliveryAttestation.model_validate(_attestation(payload=accepted)).payload == accepted


def test_payload_rejects_unicode_surrogates() -> None:
    from brain_v42.models.delivery import DeliveryAttestation

    with pytest.raises(ValidationError, match="surrogate"):
        DeliveryAttestation.model_validate(_attestation(payload={"label": "\ud800"}))


@pytest.mark.parametrize("field", ["issuer_project", "issuer_identity", "idempotency_key"])
def test_string_fields_reject_unicode_surrogates(field: str) -> None:
    from brain_v42.models.delivery import DeliveryAttestation

    with pytest.raises(ValidationError, match="surrogate|unicode"):
        DeliveryAttestation.model_validate(_attestation(**{field: "bad\ud800label"}))


@pytest.mark.parametrize("digest", ["a" * 63, "A" * 64, "z" * 64, "a" * 65])
def test_digest_must_be_sixty_four_lowercase_hex(digest: str) -> None:
    from brain_v42.models.delivery import DeliveryAttestation

    with pytest.raises(ValidationError, match="digest"):
        DeliveryAttestation.model_validate(_attestation(digest=digest))


def test_delivery_view_defaults_attestations_to_not_loaded() -> None:
    from brain_v42.models.delivery import DeliveryView

    assert "attestations" in DeliveryView.model_fields
    assert DeliveryView.model_fields["attestations"].default is None


def test_attestation_page_has_the_history_page_shape() -> None:
    from brain_v42.models.delivery import (
        DeliveryAttestation,
        DeliveryAttestationPage,
    )

    item = DeliveryAttestation.model_validate(_attestation())
    page = DeliveryAttestationPage(
        items=(item,),
        next_cursor="opaque-cursor",
        omitted_count=3,
    )

    assert page.items == (item,)
    assert page.next_cursor == "opaque-cursor"
    assert page.omitted_count == 3
    assert DeliveryAttestationPage().omitted_count == 0
