"""Safe errors shared by the claim-verification boundary."""

from __future__ import annotations

from types import MappingProxyType
from typing import Final, Literal

ClaimVerificationErrorCode = Literal[
    "invalid_argument",
    "unknown_actor",
    "claim_not_found",
    "claim_retired",
    "idempotency_conflict",
    "observation_already_verified",
    "invalid_emitted_at",
    "refresh_budget_exhausted",
]

#: One constant, caller-actionable text per code. Constant on purpose: nothing a
#: caller sent, and nothing a database or a probe said, ever reaches the message --
#: `business_errors` lets this family through the masked MCP boundary on that
#: condition alone.
_SAFE_DETAILS: Final = MappingProxyType(
    {
        "invalid_argument": "invalid verification argument",
        "unknown_actor": "the request carries no resolved actor, so it has no issuer",
        "claim_not_found": "no claim with this id in the caller's scope",
        "claim_retired": "the claim is retired and takes no new verdict",
        "idempotency_conflict": "this idempotency key was already used with other inputs",
        "observation_already_verified": (
            "the fresh observation is already bound to a verdict of this claim; retry later"
        ),
        "invalid_emitted_at": "the measurement instant is later than the server accepts",
        "refresh_budget_exhausted": (
            "the fact's forced-refresh budget is exhausted for now; retry later"
        ),
    }
)


class ClaimVerificationError(ValueError):
    """Expose a stable public code and its constant safe text, nothing internal."""

    code: ClaimVerificationErrorCode
    detail: str

    def __init__(self, code: ClaimVerificationErrorCode) -> None:
        if code not in _SAFE_DETAILS:
            raise ValueError("claim verification error code must be closed")
        self.code = code
        self.detail = _SAFE_DETAILS[code]
        super().__init__(f"{code}: {self.detail}")


#: The widest issuer identity or idempotency key the ledger stores (varchar(200)).
MAX_CALLER_STRING_LENGTH: Final = 200


def validate_caller_string(value: object) -> str:
    """Accept one bounded, printable caller string or refuse it as `invalid_argument`.

    Shared by the MCP tool, which refuses before any coordinator work, and by the
    coordinator, which refuses again for callers that are not the tool.
    """
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_CALLER_STRING_LENGTH:
        raise ClaimVerificationError("invalid_argument")
    if "\x00" in value or any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ClaimVerificationError("invalid_argument")
    return value
