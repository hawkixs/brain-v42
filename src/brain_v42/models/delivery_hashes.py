"""Canonical digests for observable delivery workflow identities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from brain_v42.models.delivery import BindingEvidence, ContractRevision


DigestDomain = Literal["contract", "request", "result", "assessment"]
_DIGEST_DOMAINS: frozenset[str] = frozenset({"contract", "request", "result", "assessment"})


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


def contract_digest(contract: ContractRevision) -> str:
    """Digest one normalized stored contract through the sole content projector."""
    from brain_v42.models.delivery import ContractRevision, contract_content_payload

    if not isinstance(contract, ContractRevision):
        raise ValueError("contract digest requires a normalized ContractRevision")
    return canonical_digest(contract_content_payload(contract), domain="contract")


def delivery_digest(
    *,
    contract_digest: str,
    attempt: int,
    active_bindings: Iterable[BindingEvidence],
) -> str:
    """Return the current delivery identity from retained binding evidence."""
    bindings = []
    for item in sorted(active_bindings, key=lambda value: value.binding.deliverable_key):
        evidence = item.confirmation.evidence if item.confirmation is not None else None
        bindings.append(
            {
                "key": item.binding.deliverable_key,
                "binding_id": str(item.binding.id),
                "repository_id": item.binding.repository_id,
                "pr_number": item.binding.pr_number,
                "head_sha": evidence.head_sha if evidence is not None else item.binding.head_sha,
                "base_sha": evidence.base_sha if evidence is not None else item.binding.base_sha,
                "integration": {
                    "sha": evidence.integration_sha,
                    "revision": evidence.integration_revision,
                }
                if evidence is not None
                else None,
            }
        )
    return canonical_digest(
        {
            "contract_digest": contract_digest,
            "attempt": attempt,
            "bindings": bindings,
        },
        domain="result",
    )
