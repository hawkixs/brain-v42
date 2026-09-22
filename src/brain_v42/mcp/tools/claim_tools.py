"""`brain_claim_verify`: a caller names a claim; the server measures and records the verdict.

Spec 2026-09-19, section 6.3: there is no tool that accepts a measurement. The only
public inputs are a claim id and an idempotency key. The issuer is the MCP actor the
server resolved from the request (`mcp:<X-Brain-Agent>`), the kind is `robot`, and a
scoped Dream call contributes the trusted project of its scope -- no argument can
supply a measurement, an outcome, an instant, an issuer or a project.

No Dream phase reaches this tool in lot B: it is in no phase allowlist and no scoped
project policy. Lot C adds the verify phase once a run id verified by the server can
name its issuer (`dream:verify:<run_id>`).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from fastmcp import FastMCP

from brain_v42.mcp.facts_transport import _FactsRegistry
from brain_v42.mcp.tools.tool_annotations import _WRITE_ANNOTATIONS
from brain_v42.models.claim_verdict import ClaimVerificationError, validate_caller_string
from brain_v42.provenance import UNEXPANDED_ACTOR, UNKNOWN_ACTOR, get_current_actor
from brain_v42.services.dream_project_scope import get_dream_project_scope

if TYPE_CHECKING:
    from brain_v42.facts.verification import ClaimVerificationService
    from brain_v42.repositories.pg_claim_verdicts import VerdictRow

__all__ = ["register_claim_tools"]

_UNRESOLVED_ACTORS = frozenset({UNKNOWN_ACTOR, UNEXPANDED_ACTOR, ""})


def _canonical_claim_id(value: object) -> UUID:
    """One claim, one spelling: only the canonical lowercase hyphenated form names it."""
    if not isinstance(value, str):
        raise ClaimVerificationError("invalid_argument")
    try:
        parsed = UUID(value)
    except ValueError:
        raise ClaimVerificationError("invalid_argument") from None
    if str(parsed) != value:
        raise ClaimVerificationError("invalid_argument")
    return parsed


def _issuer() -> str:
    """`mcp:<actor>` -- and nothing for a request whose actor the server did not resolve."""
    actor = get_current_actor().strip()
    if actor in _UNRESOLVED_ACTORS:
        raise ClaimVerificationError("unknown_actor")
    return f"mcp:{actor}"


def _instant(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _verdict_json(verdict: VerdictRow) -> dict[str, object]:
    """The complete stored row, audit fields included, in plain JSON types."""
    measurement: Mapping[str, object] = verdict.measurement
    return {
        "id": str(verdict.id),
        "seq": verdict.seq,
        "claim_id": str(verdict.claim_id),
        "verdict": verdict.verdict,
        "reason": verdict.reason,
        "measurement": dict(measurement),
        "measurement_digest": verdict.measurement_digest,
        "observation_id": str(verdict.observation_id),
        "issuer_identity": verdict.issuer_identity,
        "issuer_kind": verdict.issuer_kind,
        "request_fingerprint": verdict.request_fingerprint,
        "outcome_fingerprint": verdict.outcome_fingerprint,
        "idempotency_key": verdict.idempotency_key,
        "emitted_at": _instant(verdict.emitted_at),
        "recorded_at": _instant(verdict.recorded_at),
    }


def register_claim_tools(mcp: FastMCP, verifier: ClaimVerificationService) -> None:
    """Register the one verification write, tagged with the facts it measures."""
    claims = _FactsRegistry(mcp)

    @claims.tool(version="1.0", annotations=_WRITE_ANNOTATIONS)
    async def brain_claim_verify(claim_id: str, idempotency_key: str) -> dict[str, object]:
        """Measure one claim's fact now, on the server, and append the verdict.

        You name the claim; the server measures through the fact registry and compares
        the value with the expectation stored on the claim. You never supply a
        measurement, an outcome or a timestamp. The verdict is `holds`, `falsified` or
        `unreadable` (with its reason) and is stored append-only.

        Retrying with the SAME idempotency_key returns the same verdict without
        measuring again; a new key produces a new verdict from a fresh observation.
        Verification changes no entry, archives nothing and does not alter ranking.

        Args:
            claim_id: the claim occurrence id, a UUID in canonical lowercase form.
            idempotency_key: your retry key for this request, 1 to 200 characters.
        """
        checked_id = _canonical_claim_id(claim_id)
        key = validate_caller_string(idempotency_key)
        issuer = _issuer()
        scope = get_dream_project_scope()
        verdict = await verifier.verify(
            checked_id,
            issuer,
            "robot",
            key,
            project_key=scope.project_key if scope is not None else None,
        )
        return _verdict_json(verdict)
