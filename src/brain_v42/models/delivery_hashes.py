"""Canonical digests for observable delivery workflow identities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from brain_v42.models.delivery import ContractInput, ContractRevision


DigestDomain = Literal["contract", "request", "result", "assessment"]
_DIGEST_DOMAINS: frozenset[str] = frozenset({"contract", "request", "result", "assessment"})
_SERVER_CONTRACT_FIELDS: frozenset[str] = frozenset(
    {
        "ticket_id",
        "contract_revision",
        "content_digest",
        "author_project",
        "created_at",
        "amendment_reason",
    }
)


def _reject_invalid_json_value(value: object) -> None:
    """Reject values outside the deliberately small canonical JSON domain."""
    if value is None or type(value) in {bool, int}:  # bool is deliberately not an integer here.
        return
    if isinstance(value, float):
        raise ValueError("canonical digest payload cannot contain floats")
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError("canonical digest payload cannot contain Unicode surrogates")
        return
    if isinstance(value, Mapping):
        for key, nested_value in value.items():
            if not isinstance(key, str):
                raise ValueError("canonical digest payload requires string keys")
            _reject_invalid_json_value(key)
            _reject_invalid_json_value(nested_value)
        return
    if isinstance(value, (list, tuple)):
        for nested_value in value:
            _reject_invalid_json_value(nested_value)
        return
    raise ValueError(f"canonical digest payload has unsupported value type {type(value).__name__}")


def canonical_digest(payload: Mapping[str, Any], *, domain: DigestDomain) -> str:
    """Return the versioned SHA-256 digest for a validated canonical payload."""
    if domain not in _DIGEST_DOMAINS:
        raise ValueError(f"unsupported delivery digest domain: {domain}")
    _reject_invalid_json_value(payload)
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    prefix = f"brain-delivery-{domain}:v1\n".encode("ascii")
    return hashlib.sha256(prefix + encoded).hexdigest()


def contract_digest(contract: ContractInput | ContractRevision) -> str:
    """Digest contract content while excluding immutable server revision metadata."""
    payload = contract.model_dump(mode="json", by_alias=True)
    for field_name in _SERVER_CONTRACT_FIELDS:
        payload.pop(field_name, None)
    return canonical_digest(payload, domain="contract")
