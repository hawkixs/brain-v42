"""Opaque list continuation bound to its project and assessment filters."""

from __future__ import annotations

import base64
import binascii
import json
from uuid import UUID

from brain_v42.models.delivery import DeliveryError, DeliveryListFilters
from brain_v42.models.delivery_hashes import canonical_digest


def list_scope(actor_project: str, filters: DeliveryListFilters) -> str:
    return canonical_digest(
        {
            "operation": "delivery_list_cursor",
            "project": actor_project,
            "filters": filters.model_dump(),
        },
        domain="request",
    )


def encode_list_cursor(after: UUID, scope: str) -> str:
    payload = {"v": 1, "scope": scope, "after": str(after)}
    return (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )


def decode_list_cursor(cursor: str | None, scope: str) -> UUID | None:
    if cursor is None:
        return None
    try:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 1000:
            raise ValueError
        value = json.loads(
            base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        )
        if (
            not isinstance(value, dict)
            or set(value) != {"v", "scope", "after"}
            or type(value["v"]) is not int
            or value["v"] != 1
            or value["scope"] != scope
            or not isinstance(value["after"], str)
        ):
            raise ValueError
        return UUID(value["after"])
    except (ValueError, TypeError, KeyError, binascii.Error, UnicodeError):
        raise DeliveryError(
            "invalid_cursor", "delivery cursor is invalid for this project and filter set"
        ) from None
