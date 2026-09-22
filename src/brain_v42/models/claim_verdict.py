"""Safe errors shared by the claim-verification boundary."""

from __future__ import annotations

from typing import Final, Literal

ClaimVerificationErrorCode = Literal[
    "invalid_argument",
    "claim_not_found",
    "claim_retired",
    "idempotency_conflict",
    "observation_already_verified",
    "invalid_emitted_at",
]

_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "invalid_argument",
        "claim_not_found",
        "claim_retired",
        "idempotency_conflict",
        "observation_already_verified",
        "invalid_emitted_at",
    }
)
_SAFE_DETAIL: Final = "invalid verification argument"


class ClaimVerificationError(ValueError):
    """Expose a stable public code without retaining internal failure details."""

    code: ClaimVerificationErrorCode
    detail: str

    def __init__(self, code: ClaimVerificationErrorCode) -> None:
        if code not in _ERROR_CODES:
            raise ValueError("claim verification error code must be closed")
        self.code = code
        self.detail = _SAFE_DETAIL
        super().__init__(self.detail)
