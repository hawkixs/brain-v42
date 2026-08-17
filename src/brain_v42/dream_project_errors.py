"""Stable Dream project authorization error shared below the service layer."""

from __future__ import annotations

from typing import Literal

from fastmcp.exceptions import AuthorizationError

type DreamProjectDenialReason = Literal[
    "invalid_project_claim",
    "policy_missing",
    "resolver_missing",
    "project_argument_mismatch",
    "project_group_forbidden",
    "dream_run_forbidden",
    "ownership_field_forbidden",
    "invalid_reference",
    "object_not_authorized",
    "resolver_failure",
]


class DreamProjectAuthorizationError(AuthorizationError):
    """Stable caller-visible authorization failure with an internal reason."""

    def __init__(self, reason: DreamProjectDenialReason) -> None:
        self.reason = reason
        super().__init__("Dream project authorization denied")
