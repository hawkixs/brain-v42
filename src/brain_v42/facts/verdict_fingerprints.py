"""Deterministic, bounded fingerprints for claim-verification persistence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Final
from uuid import UUID

from brain_v42.facts.compare import Comparison
from brain_v42.facts.model import Measurement, measurement_to_json
from brain_v42.models.claim_verdict import ClaimVerificationError

MAX_ENVELOPE_BYTES: Final = 16_384
MAX_ENVELOPE_DEPTH: Final = 12
_MAX_ISSUER_OR_KEY_LENGTH: Final = 200


def _invalid_argument() -> ClaimVerificationError:
    return ClaimVerificationError("invalid_argument")


def _validate_string(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_ISSUER_OR_KEY_LENGTH:
        raise _invalid_argument()
    if "\x00" in value or any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise _invalid_argument()
    return value


def _copy_json(value: object, *, depth: int = 1) -> object:
    if value is None or type(value) in {bool, int}:
        return value
    if isinstance(value, float) or not isinstance(value, (str, Mapping, list, tuple)):
        raise _invalid_argument()
    if isinstance(value, str):
        if "\x00" in value or any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise _invalid_argument()
        return value
    if depth > MAX_ENVELOPE_DEPTH:
        raise _invalid_argument()
    if isinstance(value, Mapping):
        copied: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise _invalid_argument()
            copied_key = _copy_json(key, depth=depth)
            assert isinstance(copied_key, str)
            copied[copied_key] = _copy_json(item, depth=depth + 1)
        return copied
    return [_copy_json(item, depth=depth + 1) for item in value]


def canonical_envelope(value: Mapping[str, object]) -> str:
    """Serialize a larger bounded envelope without changing fact-value canonicalization."""
    if not isinstance(value, Mapping):
        raise _invalid_argument()
    try:
        normalized = _copy_json(value)
        encoded = json.dumps(
            normalized,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _invalid_argument() from exc
    if len(encoded) > MAX_ENVELOPE_BYTES:
        raise _invalid_argument()
    return encoded.decode("utf-8")


def _fingerprint(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_envelope(payload).encode("utf-8")).hexdigest()


def request_fingerprint(
    *,
    claim_id: UUID,
    issuer_identity: str,
    idempotency_key: str,
    expected_resolved: Mapping[str, object],
    definition_version: int,
    validity_seconds: int,
) -> str:
    """Hash only immutable inputs available before a probe starts."""
    if (
        not isinstance(claim_id, UUID)
        or type(definition_version) is not int
        or definition_version < 1
        or type(validity_seconds) is not int
        or validity_seconds < 1
        or not isinstance(expected_resolved, Mapping)
    ):
        raise _invalid_argument()
    return _fingerprint(
        {
            "claim_id": str(claim_id),
            "issuer_identity": _validate_string(issuer_identity),
            "idempotency_key": _validate_string(idempotency_key),
            "expected_resolved": expected_resolved,
            "definition_version": definition_version,
            "validity_seconds": validity_seconds,
        }
    )


def outcome_fingerprint(comparison: Comparison, measurement: Measurement) -> str:
    """Hash the stored comparison and existing measurement wire payload for audit."""
    if not isinstance(comparison, Comparison):
        raise _invalid_argument()
    return _fingerprint(
        {
            "verdict": comparison.verdict,
            "reason": comparison.reason,
            "measurement": measurement_to_json(measurement),
        }
    )
