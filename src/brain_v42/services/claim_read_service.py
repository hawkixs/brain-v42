"""Scope, validate and evaluate SELECT-only claim read responses."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.models.claim_read import ClaimState, evaluate_claim
from brain_v42.repositories import pg_claim_reads
from brain_v42.repositories.pg_claim_reads import ReadPage
from brain_v42.repositories.pg_claim_verdicts import VerdictRow

ClaimReadErrorCode = Literal["invalid_argument", "claim_not_found", "read_unavailable"]
_DETAILS: dict[ClaimReadErrorCode, str] = {
    "invalid_argument": "invalid claim read argument",
    "claim_not_found": "no claim with this id in the caller's scope",
    "read_unavailable": "claim state is unavailable",
}
_MAX_SEQ = 2**63 - 1
_ENTITY_TYPES = frozenset({"learning", "decision", "adr", "runbook", "snippet"})


class ClaimReadError(ValueError):
    """Expose only a stable code and fixed text across the read boundary."""

    def __init__(self, code: ClaimReadErrorCode) -> None:
        self.code = code
        self.detail = _DETAILS[code]
        super().__init__(f"{code}: {self.detail}")


@dataclass(frozen=True, slots=True)
class ClaimHistory:
    claim: ClaimState
    verdicts: tuple[VerdictRow, ...]
    next_after_seq: int | None


def _page_arguments(after_seq: object, limit: object) -> tuple[int, int]:
    if (
        type(after_seq) is not int
        or not 0 <= after_seq <= _MAX_SEQ
        or type(limit) is not int
        or not 1 <= limit <= 100
    ):
        raise ClaimReadError("invalid_argument")
    return after_seq, limit


def _project(value: object) -> None:
    if value is not None and (not isinstance(value, str) or not value or len(value) > 50):
        raise ClaimReadError("invalid_argument")


class ClaimReadService:
    """Keep one UTC clock reading per response and avoid exposing storage faults."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ClaimReadError("read_unavailable")
        return now.astimezone(UTC)

    async def batch_summaries(
        self,
        entries: Sequence[tuple[str, UUID]],
        *,
        trusted_project_key: str | None = None,
    ) -> dict[tuple[str, UUID], tuple[ClaimState, ...]]:
        """Return one batch keyed by entity type and authoritative source UUID."""
        _project(trusted_project_key)
        if len(entries) > 100 or any(
            not isinstance(entry, tuple)
            or len(entry) != 2
            or entry[0] not in _ENTITY_TYPES
            or not isinstance(entry[1], UUID)
            for entry in entries
        ):
            raise ClaimReadError("invalid_argument")
        if not entries:
            return {}
        try:
            now = self._now()
            async with self._session_factory() as session:
                claims = await pg_claim_reads.fetch_active_for_entries(
                    session, entries, trusted_project_key=trusted_project_key
                )
            grouped: dict[tuple[str, UUID], list[ClaimState]] = {key: [] for key in entries}
            for claim in claims:
                key = (claim.entity_type, claim.entry_id)
                if key in grouped:
                    grouped[key].append(evaluate_claim(claim, now))
            return {key: tuple(states) for key, states in grouped.items()}
        except ClaimReadError:
            raise
        except Exception:
            raise ClaimReadError("read_unavailable") from None

    async def list_claims(
        self,
        *,
        project_key: str | None = None,
        entry_id: UUID | None = None,
        include_retired: bool = False,
        after_seq: int = 0,
        limit: int = 50,
        trusted_project_key: str | None = None,
    ) -> ReadPage[ClaimState]:
        """Page occurrences while preserving the caller's project scope."""
        after_seq, limit = _page_arguments(after_seq, limit)
        _project(project_key)
        _project(trusted_project_key)
        if (entry_id is not None and not isinstance(entry_id, UUID)) or type(
            include_retired
        ) is not bool:
            raise ClaimReadError("invalid_argument")
        try:
            now = self._now()
            async with self._session_factory() as session:
                page = await pg_claim_reads.list_occurrences(
                    session,
                    project_key=project_key,
                    entry_id=entry_id,
                    include_retired=include_retired,
                    after_seq=after_seq,
                    limit=limit,
                    trusted_project_key=trusted_project_key,
                )
            return ReadPage(
                tuple(evaluate_claim(claim, now) for claim in page.items), page.next_after_seq
            )
        except ClaimReadError:
            raise
        except Exception:
            raise ClaimReadError("read_unavailable") from None

    async def history(
        self,
        claim_id: UUID,
        *,
        after_seq: int = 0,
        limit: int = 50,
        trusted_project_key: str | None = None,
    ) -> ClaimHistory:
        """Return the scoped occurrence and a complete verdict page, even after retirement."""
        after_seq, limit = _page_arguments(after_seq, limit)
        _project(trusted_project_key)
        if not isinstance(claim_id, UUID):
            raise ClaimReadError("invalid_argument")
        try:
            now = self._now()
            async with self._session_factory() as session:
                claim = await pg_claim_reads.fetch_claim(
                    session, claim_id, trusted_project_key=trusted_project_key
                )
                if claim is None:
                    raise ClaimReadError("claim_not_found")
                page = await pg_claim_reads.fetch_verdict_page(
                    session, claim_id, after_seq=after_seq, limit=limit
                )
            return ClaimHistory(evaluate_claim(claim, now), page.items, page.next_after_seq)
        except ClaimReadError:
            raise
        except Exception:
            raise ClaimReadError("read_unavailable") from None
