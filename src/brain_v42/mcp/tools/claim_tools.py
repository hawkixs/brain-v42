"""Claim tools: `brain_claim_verify` (write) and the two scoped SELECT-only reads.

`brain_claim_verify` -- spec 2026-09-19, section 6.3: there is no tool that accepts a
measurement. The only public inputs are a claim id and an idempotency key. The issuer
is the MCP actor the server resolved from the request (`mcp:<X-Brain-Agent>`), the kind
is `robot`, and a scoped Dream call contributes the trusted project of its scope -- no
argument can supply a measurement, an outcome, an instant, an issuer or a project.

No Dream phase reaches `brain_claim_verify` in lot B: it is in no phase allowlist and no
scoped project policy. Lot C adds the verify phase once a run id verified by the server
can name its issuer (`dream:verify:<run_id>`).

`brain_claim_list` / `brain_claim_history` -- spec 2026-09-19, section 6.6: bounded,
SELECT-only reads over the same immutable ledger. Registered only when `read_service`
is supplied (composition root, `mcp/server.py`), so the standalone
`brain_claim_verify` registration/formatter test seams stay unaffected. Like every
other read tool that carries project scope (`brain_list`, `crud_tools.py`), scope is
server-owned: a Dream call scoped to one project overrides `project_key` with its own
and refuses a caller-named project that disagrees. No Dream phase gains either tool.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from fastmcp import FastMCP

from brain_v42.mcp.dream_project_authorization import DreamProjectAuthorizationError
from brain_v42.mcp.facts_transport import _FactsRegistry
from brain_v42.mcp.tools.claim_rendering import format_claim_history, format_claim_list
from brain_v42.mcp.tools.tool_annotations import _READ_ANNOTATIONS, _WRITE_ANNOTATIONS
from brain_v42.models.claim_verdict import ClaimVerificationError, validate_caller_string
from brain_v42.models.project_key import canonicalize_project_key
from brain_v42.provenance import UNEXPANDED_ACTOR, UNKNOWN_ACTOR, get_current_actor
from brain_v42.services.claim_read_service import ClaimReadError
from brain_v42.services.dream_project_scope import get_dream_project_scope

if TYPE_CHECKING:
    from brain_v42.facts.verification import ClaimVerificationService
    from brain_v42.repositories.pg_claim_verdicts import VerdictRow
    from brain_v42.services.claim_read_service import ClaimReadService

__all__ = ["register_claim_tools"]

_UNRESOLVED_ACTORS = frozenset({UNKNOWN_ACTOR, UNEXPANDED_ACTOR, ""})
_MAX_SEQ = 2**63 - 1


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


def _canonical_read_uuid(value: object) -> UUID:
    """Same one-spelling rule as `_canonical_claim_id`, for the read-side error family."""
    if not isinstance(value, str):
        raise ClaimReadError("invalid_argument")
    try:
        parsed = UUID(value)
    except ValueError:
        raise ClaimReadError("invalid_argument") from None
    if str(parsed) != value:
        raise ClaimReadError("invalid_argument")
    return parsed


def _bounded_seq(value: int) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_SEQ:
        raise ClaimReadError("invalid_argument")
    return value


def _bounded_limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 100:
        raise ClaimReadError("invalid_argument")
    return value


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


def register_claim_tools(
    mcp: FastMCP,
    verifier: ClaimVerificationService,
    read_service: ClaimReadService | None = None,
) -> None:
    """Register the verification write and the two scoped, SELECT-only reads.

    `read_service` is optional so existing standalone tests that only exercise
    `brain_claim_verify` can omit it -- but the two read tools are always
    REGISTERED regardless. `mcp/server.py`'s composition root is walked
    statically by the MCP tool census (`tests/unit/test_documentation_contract.py`),
    which cannot trace a runtime-conditional tool registration ("dynamic
    control flow can bypass registration calls"): only the tools' BEHAVIOUR is
    gated on `read_service`, exactly like the optional `claim_read_svc` already
    gates `brain_get`/`brain_search` (crud_tools.py, brain_tools.py) without
    ever skipping their registration. Real composition (`mcp/server.py`)
    always supplies a `read_service`; without one, both read tools refuse
    every call with `read_unavailable`.
    """
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

    @claims.tool(version="1.0", annotations=_READ_ANNOTATIONS)
    async def brain_claim_list(
        project_key: str | None = None,
        entity_id: str | None = None,
        include_retired: bool = False,
        after_seq: int = 0,
        limit: int = 50,
    ) -> str:
        """List scoped claim occurrences, ordered by insertion sequence.

        SELECT-only: no probe runs, no verdict is written and no ranking
        changes. Scope is server-owned -- a Dream call scoped to one project
        overrides project_key with its own and refuses a caller-named project
        that disagrees, exactly like brain_list; an unscoped ordinary caller
        keeps the existing unrestricted read semantics.

        Args:
            project_key: filter by project (overridden under a Dream scope).
            entity_id: the entry's authoritative UUID, canonical lowercase form.
            include_retired: when True, also list retired occurrences.
            after_seq: cursor -- only occurrences with a greater seq (default 0).
            limit: page size, 1 to 100 (default 50).
        """
        if read_service is None:
            raise ClaimReadError("read_unavailable")
        entry_id = _canonical_read_uuid(entity_id) if entity_id is not None else None
        after_seq = _bounded_seq(after_seq)
        limit = _bounded_limit(limit)
        if type(include_retired) is not bool:
            raise ClaimReadError("invalid_argument")

        scope = get_dream_project_scope()
        trusted_project_key: str | None = None
        if scope is not None:
            requested = canonicalize_project_key(project_key, strict=False)
            if requested is not None and requested != scope.project_key:
                raise DreamProjectAuthorizationError("project_argument_mismatch")
            project_key = scope.project_key
            trusted_project_key = scope.project_key
        else:
            project_key = canonicalize_project_key(project_key, strict=False)

        page = await read_service.list_claims(
            project_key=project_key,
            entry_id=entry_id,
            include_retired=include_retired,
            after_seq=after_seq,
            limit=limit,
            trusted_project_key=trusted_project_key,
        )
        return format_claim_list(page.items, page.next_after_seq)

    @claims.tool(version="1.0", annotations=_READ_ANNOTATIONS)
    async def brain_claim_history(
        claim_id: str,
        after_seq: int = 0,
        limit: int = 50,
    ) -> str:
        """Return one claim's current state and its complete verdict history.

        Ordered by verdict seq, ascending; an empty history is a valid result
        for an existing claim. A missing occurrence and one outside the
        caller's trusted scope refuse identically -- this tool never confirms
        a claim's existence outside scope.

        Args:
            claim_id: the claim occurrence id, a UUID in canonical lowercase form.
            after_seq: cursor -- only verdicts with a greater seq (default 0).
            limit: page size, 1 to 100 (default 50).
        """
        if read_service is None:
            raise ClaimReadError("read_unavailable")
        checked_id = _canonical_read_uuid(claim_id)
        after_seq = _bounded_seq(after_seq)
        limit = _bounded_limit(limit)
        scope = get_dream_project_scope()

        history = await read_service.history(
            checked_id,
            after_seq=after_seq,
            limit=limit,
            trusted_project_key=scope.project_key if scope is not None else None,
        )
        return format_claim_history(history.claim, history.verdicts, history.next_after_seq)
