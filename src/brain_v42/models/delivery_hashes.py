"""Canonical digests for observable delivery workflow identities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from brain_v42.models.delivery import BindingEvidence, ContractRevision


DigestDomain = Literal["contract", "request", "result", "assessment", "attestation"]
_DIGEST_DOMAINS: frozenset[str] = frozenset(
    {"contract", "request", "result", "assessment", "attestation"}
)


#: The deepest nesting a canonical payload may carry. 64 is the bound the embedding
#: shim already applies to JSON; a ledger fact has no business nesting deeper.
MAX_CANONICAL_JSON_DEPTH = 64


def _reject_invalid_json_value(value: object) -> None:
    """Reject values outside the deliberately small canonical JSON domain.

    Iterative on purpose: a payload nested past the bound must surface as a
    ValueError the callers convert into a stable code, never as a RecursionError
    they cannot — which reached the caller as a retryable availability error.
    """
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if current is None or type(current) in {
            bool,
            int,
        }:  # bool is deliberately not an integer here.
            continue
        if isinstance(current, float):
            raise ValueError("canonical digest payload cannot contain floats")
        if isinstance(current, str):
            # JSON allows U+0000; PostgreSQL jsonb does not, and the INSERT would fail
            # AFTER every validator passed, as an availability error a caller retries.
            if "\x00" in current:
                raise ValueError("canonical digest payload cannot contain NUL characters")
            if any(0xD800 <= ord(character) <= 0xDFFF for character in current):
                raise ValueError("canonical digest payload cannot contain Unicode surrogates")
            continue
        if isinstance(current, Mapping | list | tuple):
            if depth >= MAX_CANONICAL_JSON_DEPTH:
                raise ValueError(
                    "canonical digest payload nests too deep "
                    f"(more than {MAX_CANONICAL_JSON_DEPTH} levels)"
                )
            if isinstance(current, Mapping):
                for key, nested_value in current.items():
                    if not isinstance(key, str):
                        raise ValueError("canonical digest payload requires string keys")
                    stack.append((key, depth + 1))
                    stack.append((nested_value, depth + 1))
            else:
                stack.extend((nested_value, depth + 1) for nested_value in current)
            continue
        raise ValueError(
            f"canonical digest payload has unsupported value type {type(current).__name__}"
        )


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
